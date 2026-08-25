#!/usr/bin/env bash
# p6c1 spike — shared knobs + helpers for both arms. THROWAWAY (experiments/**).
#
# Everything a host can differ on is an env knob with a default; nothing about
# etri6000 is baked into the arm scripts themselves.

set -euo pipefail

SPIKE_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SPIKE_DIR/../.." && pwd)"

# --- pins the operator supplies (defaults = what this measurement host holds) ---
: "${CV_RUNNER_IMAGE:=cv-infra-runner:prod-d6da2b5}"
: "${CV_WARM_CACHE_BASE:=$HOME/cv-infra-prod/cache-warm}"   # READ-ONLY copy source
: "${CV_EVIDENCE_DIR:=$HOME/cv-infra-p6c1-evidence}"
: "${CV_CLI:=$HOME/cv-infra-host-venv/bin/cv-infra}"
: "${ACCEPT_EULA:=}"        # operator consent, per run (NEG-2 — never defaulted here)
: "${PRIVACY_CONSENT:=}"

# The six cache binds, VERBATIM from cv_infra/orchestrator/supervisor.py
# (CACHE_BASE_MOUNTS + CACHE_SCRATCH_MOUNTS) — same order, same targets.
CACHE_BASE_TIERS=(
  "cache/kit:/isaac-sim/kit/cache"
  "cache/home:/isaac-sim/.cache"
  "cache/computecache:/isaac-sim/.nv/ComputeCache"
)
CACHE_SCRATCH_TIERS=(
  "logs:/isaac-sim/.nvidia-omniverse/logs"
  "data:/isaac-sim/.local/share/ov/data"
  "documents:/isaac-sim/Documents"
)

log() { printf '[p6c1] %s\n' "$*" >&2; }
die() { printf '[p6c1] ERROR: %s\n' "$*" >&2; exit 1; }

now_s() { date +%s.%N; }
elapsed() { python3 -c "import sys; print('%.3f' % (float(sys.argv[2]) - float(sys.argv[1])))" "$1" "$2"; }

require_consent() {
  [[ -n "$ACCEPT_EULA" && -n "$PRIVACY_CONSENT" ]] \
    || die "ACCEPT_EULA / PRIVACY_CONSENT must be supplied by the OPERATOR for this run (NEG-2). \
The host consent RECORD is a precondition, not a substitute — verify it with \
scripts/consent/check_consent.sh."
  bash "$REPO_ROOT/scripts/consent/check_consent.sh" --quiet \
    || die "no valid consent record on this host — refusing to boot Isaac"
}

require_exclusive_gpu() {
  local jobs apps
  jobs="$(docker ps --filter label=cv-infra.job_id --format '{{.Names}}' || true)"
  [[ -z "$jobs" ]] || die "cv-infra job container(s) are running — WAIT, do not kill: $jobs"
  apps="$(nvidia-smi --query-compute-apps=pid,process_name --format=csv,noheader || true)"
  [[ -z "$apps" ]] || die "another GPU compute process is present — WAIT: $apps"
  log "exclusive GPU window confirmed (0 cv-infra job containers, 0 compute apps)"
}

# seed_cache <destination-root>
# Reproduces supervisor._seed_cache_tiers by hand: `cp -a` the three warm tiers out of
# the shared base into a FRESH per-process tree and create the three runtime dirs.
# Runs inside a --user 0 throwaway container (the warm_cache.sh idiom) for two reasons:
# ownership must survive the copy (uid 1234, else the cache silently turns OFF — G-15/
# G-34), and the host operator account is not root. The base is bound :ro, so the
# shared warm tree is structurally a copy SOURCE and never a write target.
seed_cache() {
  local dest="$1" spec inner=""
  mkdir -p "$dest"
  for spec in "${CACHE_BASE_TIERS[@]}"; do
    inner+="mkdir -p \"/dst/$(dirname "${spec%%:*}")\"; cp -a \"/base/${spec%%:*}\" \"/dst/${spec%%:*}\"; "
  done
  for spec in "${CACHE_SCRATCH_TIERS[@]}"; do
    inner+="mkdir -p \"/dst/${spec%%:*}\"; "
  done
  inner+="chown -R 1234:1234 /dst; "
  docker run --rm --user 0 --network none \
    -v "$CV_WARM_CACHE_BASE":/base:ro -v "$dest":/dst \
    --entrypoint bash "$CV_RUNNER_IMAGE" -c "set -eu; $inner" >/dev/null
  # Loud guard, same one the supervisor applies (a cache the runner cannot write is a
  # cache that is OFF, at ~47 s/job).
  local uid
  uid="$(stat -c %u "$dest/cache/kit")"
  [[ "$uid" == "1234" ]] || die "seeded cache tier $dest/cache/kit is uid $uid, expected 1234"
}

# cache_bind_args <cache-root>  -> the six `-v` arguments, in supervisor order
cache_bind_args() {
  local root="$1" spec out=()
  for spec in "${CACHE_BASE_TIERS[@]}" "${CACHE_SCRATCH_TIERS[@]}"; do
    out+=(-v "$root/${spec%%:*}:${spec#*:}:rw")
  done
  printf '%s\n' "${out[@]}"
}

# discard_tree <path>  — delete a tree this spike created (uid-1234 content needs root)
discard_tree() {
  local path="$1"
  [[ -d "$path" ]] || return 0
  docker run --rm --user 0 --network none -v "$path":/victim \
    --entrypoint bash "$CV_RUNNER_IMAGE" -c 'find /victim -mindepth 1 -delete' >/dev/null
  rmdir "$path" 2>/dev/null || true
}
