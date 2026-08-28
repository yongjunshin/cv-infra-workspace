#!/usr/bin/env bash
# p7c1 W0 spike — shared knobs + helpers. THROWAWAY (experiments/**).
#
# ADAPTED FROM experiments/p6-granularity-spike/common.sh (exp/p6-granularity-spike):
# same consent gate, same exclusive-window check, same six cache binds copied
# VERBATIM from cv_infra/orchestrator/supervisor.py, same "never prune, never pull"
# stance. Differences: p7 evidence root, an EMPTY cache root for the cold-cost arm,
# and the LD_LIBRARY_PATH prepend is computed FROM THE IMAGE (so the bundled jazzy
# lib is visible to the loader at process start and the runner's re-exec never fires).

set -euo pipefail

SPIKE_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SPIKE_DIR/../.." && pwd)"

: "${CV_RUNNER_IMAGE:=cv-infra-runner:prod-e571d6e}"
: "${CV_WARM_CACHE_BASE:=$HOME/cv-infra-prod/cache-warm}"   # READ-ONLY copy source
: "${CV_EVIDENCE_DIR:=$HOME/cv-infra-p7c1-evidence/w0}"
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

log() { printf '[p7c1] %s\n' "$*" >&2; }
die() { printf '[p7c1] ERROR: %s\n' "$*" >&2; exit 1; }

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

# G-101: the window is proven by CONTINUOUS watch (the sampler labels every tick),
# but a run still refuses to START on a dirty GPU. Never kills anything.
require_exclusive_gpu() {
  local jobs apps
  jobs="$(docker ps --filter label=cv-infra.job_id --format '{{.Names}}' || true)"
  [[ -z "$jobs" ]] || die "cv-infra job container(s) are running — WAIT, do not kill: $jobs"
  apps="$(nvidia-smi --query-compute-apps=pid,process_name --format=csv,noheader || true)"
  [[ -z "$apps" ]] || die "another GPU compute process is present — WAIT: $apps"
  log "exclusive GPU window confirmed (0 cv-infra job containers, 0 compute apps)"
}

# seed_cache <destination-root> — p6c1's helper verbatim in intent: `cp -a` the three
# warm tiers out of the shared base into a FRESH per-process tree (base bound :ro) and
# create the three runtime dirs, all owned by uid 1234 or the cache is silently OFF.
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
  local uid
  uid="$(stat -c %u "$dest/cache/kit")"
  [[ "$uid" == "1234" ]] || die "seeded cache tier $dest/cache/kit is uid $uid, expected 1234"
}

# empty_cache <destination-root> — the COLD arm: same six tiers, all EMPTY. The
# production cache tree is never a write target here (§task: cold measurement uses a
# brand-new temp root only).
empty_cache() {
  local dest="$1" spec inner=""
  mkdir -p "$dest"
  for spec in "${CACHE_BASE_TIERS[@]}" "${CACHE_SCRATCH_TIERS[@]}"; do
    inner+="mkdir -p \"/dst/${spec%%:*}\"; "
  done
  inner+="chown -R 1234:1234 /dst; "
  docker run --rm --user 0 --network none -v "$dest":/dst \
    --entrypoint bash "$CV_RUNNER_IMAGE" -c "set -eu; $inner" >/dev/null
}

# cache_bind_args <cache-root>  -> the six `-v` arguments, in supervisor order
cache_bind_args() {
  local root="$1" spec out=()
  for spec in "${CACHE_BASE_TIERS[@]}" "${CACHE_SCRATCH_TIERS[@]}"; do
    out+=(-v "$root/${spec%%:*}:${spec#*:}:rw")
  done
  printf '%s\n' "${out[@]}"
}

# image_ld_library_path — the image's OWN LD_LIBRARY_PATH, read from the image config.
# The spike prepends the bundled jazzy lib to THIS value at `docker run` time: the
# glibc loader snapshots LD_LIBRARY_PATH at process start (measured p2c5 probe-01), so
# setting it here removes the need for the runner's in-process re-exec.
image_ld_library_path() {
  docker image inspect -f '{{range .Config.Env}}{{println .}}{{end}}' "$CV_RUNNER_IMAGE" \
    | sed -n 's/^LD_LIBRARY_PATH=//p' | head -1
}

# discard_tree <path> — delete a tree this spike created (uid-1234 content needs root).
# Full path named explicitly; no prune, ever.
discard_tree() {
  local path="$1"
  [[ -d "$path" ]] || return 0
  docker run --rm --user 0 --network none -v "$path":/victim \
    --entrypoint bash "$CV_RUNNER_IMAGE" -c 'find /victim -mindepth 1 -delete' >/dev/null
  rmdir "$path" 2>/dev/null || true
}
