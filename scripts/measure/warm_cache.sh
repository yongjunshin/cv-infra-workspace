#!/usr/bin/env bash
# warm_cache.sh — Omniverse cache-tree lifecycle for the runner host (scripts/measure).
#
# Owns the disk-cache tree the case containers mount (CV_ISAAC_CACHE_ROOT). Two
# idempotent modes:
#
#   provision   create the 6-way cache subtree + chown 1234:1234 (G-15). EMPTY tree.
#               -> the "cold-fresh" condition (assets + shaders + compute all cold).
#               (default)
#   strip-gpu   from a warmed tree, delete only the GPU-DERIVED caches (Kit shader +
#               ComputeCache + GLCache), KEEP the portable asset cache (.cache/ov).
#               -> the "cold-assets-warm-shaders" condition.
#
# WARMING the tree is no longer a mode here: it is just a run. `cv-infra verify` boots
# the consumer's own sim script against these mounts, so the first run fills the closure
# for the scene that consumer actually opens — which the removed `warm` mode could only
# guess at (it booted a fixed scene through a script that no longer exists).
#
# The cache root is a HOST ABSOLUTE path (sibling-container safety). It is PER IMAGE:
# the execution seam mounts `$CV_ISAAC_CACHE_ROOT/<digest12>` (first 12 hex of the sim
# image's sha256), because Kit shader / CUDA compute / asset caches belong to one Isaac
# build and sharing one tree across images is corruption, not a warm cache. So the root
# passed here is that per-image subtree:
#
#   bash warm_cache.sh /var/cache/cv-infra/f3563cb2ba0c provision
#
# The platform deliberately does NOT create or chown it (it refuses loudly on a missing
# subtree, printing this very command) — that is THIS script's job.
#
# sudo (G-15): none — file perms go through a docker root helper (--user 0), not host sudo.
#
# Usage: bash warm_cache.sh <cache-root-abs>/<digest12> [provision|strip-gpu]
set -euo pipefail

export CV_STEP=measure-warm
# Capture the operator's EXPLICIT cache root BEFORE sourcing common.sh — the sourced
# workstation_setup SoT gives CV_ISAAC_CACHE_ROOT a P1-smoke DEFAULT, which must NOT
# silently stand in for the intended measurement tree. D-1: root = arg OR explicit env.
_OPERATOR_CACHE_ROOT="${CV_ISAAC_CACHE_ROOT:-}"
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=scripts/measure/common.sh
source "$SCRIPT_DIR/common.sh"

# Consent gate FIRST — before touching args or the filesystem. Nothing here BOOTS Isaac,
# but every mode runs the NVIDIA image as a root helper, so the host still needs a valid
# consent record (exit 3 without one, before the root is ever used).
measure_eula_gate no-boot

CACHE_ROOT="${1:-$_OPERATOR_CACHE_ROOT}"
MODE="${2:-provision}"
[[ -n "$CACHE_ROOT" ]] \
  || die "cache root required: pass <cache-root-abs> or set CV_ISAAC_CACHE_ROOT. usage: $0 <cache-root-abs> [provision|strip-gpu]"
case "$CACHE_ROOT" in
  /*) : ;;
  *) die "cache root must be a HOST ABSOLUTE path (sibling-container safety, D-O): $CACHE_ROOT" ;;
esac
case "$MODE" in
  provision | strip-gpu) : ;;
  *) die "unknown mode '$MODE' (want: provision | strip-gpu)" ;;
esac

require_cmd docker
IMG="$CV_MEASURE_IMAGE"

strip_gpu_cache() {
  # Remove GPU-DERIVED caches only (they regenerate per GPU): Kit RTX/shader cache
  # (cache/kit), ComputeCache (cache/computecache), and the GL shader cache nested under
  # the asset mount (.cache/nvidia). KEEP the portable asset closure (.cache/ov). Root
  # helper: the dirs are uid-1234 0700, so a host `rm` would be denied (G-15). Idempotent.
  # NOTE (Wave 2): confirm via `measure_du_bytes` before/after that only GPU-derived
  # bytes drop and the next run reloads the same prim count with LOW received bytes.
  log "stripping GPU-derived caches (Kit shader + ComputeCache + GLCache); keeping asset cache"
  docker run --rm --user 0 --network none --entrypoint bash \
    -v "$CACHE_ROOT":/cv-fix "$IMG" -c '
      set -e
      rm -rf /cv-fix/cache/kit/* /cv-fix/cache/computecache/* /cv-fix/cache/home/nvidia
      mkdir -p /cv-fix/cache/kit /cv-fix/cache/computecache
      chown -R 1234:1234 /cv-fix/cache/kit /cv-fix/cache/computecache'
}

# ---------------------------------------------------------------------------
before_bytes="$(measure_du_bytes "$CACHE_ROOT" "$IMG" 2>/dev/null || echo NA)"
log "mode=$MODE cache_root=$CACHE_ROOT image=$IMG cache_bytes_before=$before_bytes"

case "$MODE" in
  provision)
    measure_provision_tree "$CACHE_ROOT" "$IMG"
    ;;
  strip-gpu)
    measure_provision_tree "$CACHE_ROOT" "$IMG"   # ensure tree shape (idempotent)
    strip_gpu_cache
    ;;
esac

after_bytes="$(measure_du_bytes "$CACHE_ROOT" "$IMG" 2>/dev/null || echo NA)"
log "DONE mode=$MODE cache_bytes_after=$after_bytes (before=$before_bytes) root=$CACHE_ROOT"
