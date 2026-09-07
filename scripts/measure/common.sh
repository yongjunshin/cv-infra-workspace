#!/usr/bin/env bash
# shellcheck shell=bash
# shellcheck disable=SC2034  # pins/args are consumed by the scripts that source this lib
# common.sh — shared helpers + pins for the cache-tree scripts (scripts/measure).
#
# Sourced by: warm_cache.sh. Not meant to be executed on its own.
#
# Reproducibility (CLAUDE.md §2-7): rather than REDEFINE what the provisioning SoT
# already owns, this lib SOURCES scripts/workstation_setup/common.sh and reuses its
# log/err/die/require_cmd helpers and its image/cache pins, adding only what the cache
# scripts need on top. It deliberately does NOT use CV_SUDO from there: this harness runs
# sudo-free (G-15 file-perm work goes through a docker root helper, --user 0).

# Idempotent source guard (readonly pins must not be re-declared on re-source).
[[ -z "${_CV_MEASURE_COMMON_LOADED:-}" ]] || return 0
_CV_MEASURE_COMMON_LOADED=1

_CV_MEASURE_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=../workstation_setup/common.sh
source "$_CV_MEASURE_DIR/../workstation_setup/common.sh"

# ---------------------------------------------------------------------------
# PINS (only what workstation_setup/common.sh does NOT define)
# ---------------------------------------------------------------------------

# The image used as a ROOT HELPER (mkdir/chown/du inside the cache tree) — it must be an
# image that exists on the host, and the stock Isaac release is the one every runner
# already pulls. Env-overridable, never floating: the digest pin comes from the sourced
# workstation_setup SoT.
readonly CV_MEASURE_IMAGE="${CV_MEASURE_IMAGE:-$CV_ISAAC_IMAGE@$CV_ISAAC_DIGEST}"

# ---------------------------------------------------------------------------
# EULA RUNTIME CONSENT GATE (NEG-2; LOCKED §8)
# ---------------------------------------------------------------------------
# Every script that BOOTS Isaac calls this FIRST. Refuses without per-run operator
# input (exit 3); synthesizes the acceptance env from that input at run time. No
# committed file carries the acceptance literal (run_smoke.sh / headless_smoke.py idiom).
# Sets CV_EULA_DOCKER_ARGS for the caller's `docker run`.
measure_eula_gate() {
  local _boots="${1:-boot}"

  if ! bash "$_CV_MEASURE_DIR/../consent/check_consent.sh" --quiet; then
    err "This host has no valid NVIDIA Isaac Sim consent record — refusing."
    err "That record is what makes running this image lawful here, and only an operator"
    err "can create it:  bash scripts/consent/accept_eula.sh"
    exit 3
  fi

  [[ "$_boots" == "boot" ]] || return 0

  if [[ "${CV_EULA_CONSENT:-}" != "yes" ]]; then
    err "NVIDIA Isaac Sim EULA consent is REQUIRED before Isaac Sim may boot (NEG-2)."
    err "License: https://www.nvidia.com/en-us/agreements/enterprise-software/isaac-sim-additional-software-and-materials-license/"
    err "This gate never auto-accepts; consent is a per-run operator input."
    err "Re-run with:  CV_EULA_CONSENT=yes <script> ..."
    exit 3
  fi
  # Runtime-only synthesis ("yes" -> first char "Y"); the literal is never committed.
  local _c="${CV_EULA_CONSENT^^}"
  CV_EULA_DOCKER_ARGS=(-e "ACCEPT_EULA=${_c:0:1}" -e "PRIVACY_CONSENT=${_c:0:1}")
}

# ---------------------------------------------------------------------------
# CACHE-TREE PROVISIONING (G-15 — docker root helper, no host sudo)
# ---------------------------------------------------------------------------

# The host-side subpaths of the 6-way cache tree. The in-container targets they are
# bound to live in ONE place — cv_infra/execution.py (CACHE_MOUNTS) — so tree creation
# here and the run-time mount list there cannot drift apart.
CV_MEASURE_CACHE_SUBPATHS=(cache/kit cache/home cache/computecache logs data documents)

# Create the 6-way cache subtree under $1 and chown it to uid 1234 (isaac-sim), via the
# image itself as a root helper (--user 0). Idempotent. G-15: docker would otherwise
# create missing mount PARENTS as root, and the uid-1234 app cannot mkdir siblings. The
# execution seam deliberately does NOT do this (it refuses loudly on a missing root) —
# host provisioning is this script's job.
measure_provision_tree() {
  local root="$1" img="${2:-$CV_MEASURE_IMAGE}"
  log "provisioning cache tree + chown 1234:1234 under $root (G-15 root helper)"
  docker run --rm --user 0 --network none --entrypoint bash \
    -v "$root":/cv-fix "$img" -c '
      set -e
      mkdir -p /cv-fix/cache/kit /cv-fix/cache/home /cv-fix/cache/computecache \
               /cv-fix/logs /cv-fix/data /cv-fix/documents
      chown -R 1234:1234 /cv-fix'
}

# `du -sb` via the root helper — the cache dirs are uid-1234 0700, so a host-side `du`
# under-counts (permission denied). Prints the byte count on stdout.
measure_du_bytes() {
  local path="$1" img="${2:-$CV_MEASURE_IMAGE}"
  docker run --rm --user 0 --network none --entrypoint bash \
    -v "$path":/cv-fix "$img" -c 'du -sb /cv-fix | cut -f1' 2>/dev/null || echo "NA"
}
