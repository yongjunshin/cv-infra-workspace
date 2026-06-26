#!/usr/bin/env bash
# pull_isaac.sh — DoD-P1-03: pull the pinned Isaac Sim base image (anonymous NGC pull,
# with a clear `docker login` fallback) and prepare the host-side cache scaffold.
# Idempotent: skips the pull if the image is already present locally.
set -euo pipefail

export CV_STEP=isaac
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source-path=SCRIPTDIR
# shellcheck source=common.sh
source "$SCRIPT_DIR/common.sh"

require_cmd docker

prepare_cache() {
  log "preparing Isaac host-side cache scaffold under $CV_ISAAC_CACHE_ROOT (no sudo; under \$HOME)"
  local d
  for d in cache/kit cache/ov cache/pip cache/glcache cache/computecache logs data documents; do
    mkdir -p "$CV_ISAAC_CACHE_ROOT/$d"
  done
}

main() {
  local img="$CV_ISAAC_IMAGE"
  if [[ -n "$CV_ISAAC_DIGEST" ]]; then
    img="${CV_ISAAC_IMAGE%:*}@${CV_ISAAC_DIGEST}"
  else
    warn "Isaac image digest not locked yet — using the LOCKED tag pin '$CV_ISAAC_IMAGE' (CLAUDE.md §5)."
    warn "Lock CV_ISAAC_DIGEST after first pull for digest-level hardening (see README → Locking digests)."
  fi

  prepare_cache

  # `sudo -n docker`: the docker group is not yet effective in this SSH session.
  if "${CV_SUDO[@]}" docker image inspect "$img" >/dev/null 2>&1; then
    log "Isaac image already present locally ($img) — skipping pull (idempotent)"
    return 0
  fi

  log "DoD-P1-03 -> docker pull $img (anonymous NGC pull)"
  if "${CV_SUDO[@]}" docker pull "$img"; then
    log "Isaac image pulled OK"
  else
    err "Anonymous NGC pull failed for $img (NGC may require auth — rate-limit / org terms; R13)."
    err "FALLBACK — run in YOUR own terminal (interactive password prompt; G-06):"
    err "    sudo docker login nvcr.io        # username: \$oauthtoken    password: <NGC API key>"
    err "Then re-run:  bash scripts/workstation_setup/pull_isaac.sh"
    die "Isaac pull requires authentication — see the fallback above (no silent retry)."
  fi
}

main "$@"
