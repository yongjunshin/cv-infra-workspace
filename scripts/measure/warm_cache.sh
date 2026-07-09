#!/usr/bin/env bash
# warm_cache.sh — M5 asset/shader cache warming + provisioning (scripts/measure).
#
# Owns the Omniverse disk-cache tree lifecycle for the P2-09 cold/warm measurement and
# for the production runner mounts (D-1 opt B). Three idempotent modes:
#
#   provision   create the 6-way cache subtree + chown 1234:1234 (G-15). EMPTY tree.
#               -> prepares the "cold-fresh" condition (assets+shaders+compute all cold).
#   warm        provision, then boot the scene ONCE (warm_scene.py) to fill the FULL
#               dependency closure (assets on disk) + the GPU-derived shader/compute
#               caches. -> prepares the "warm-all" condition. (default)
#   strip-gpu   from a warmed tree, delete only the GPU-DERIVED caches (Kit shader +
#               ComputeCache + GLCache), KEEP the portable asset cache (.cache/ov).
#               -> prepares the "cold-assets-warm-shaders" condition.
#
# The cache root is a HOST ABSOLUTE path (sibling-container safety, D-O) and is what the
# supervisor reads as CV_ISAAC_CACHE_ROOT (D-1). The supervisor deliberately does NOT
# create or chown it (it raises a loud ValueError on a missing root) — that is THIS
# script's job. Mounts follow the D-1 canonical 6-way table verbatim.
#
# EULA (NEG-2; LOCKED §8): booting to warm requires per-run operator consent
# (CV_EULA_CONSENT=yes) — refused otherwise (exit 3). No ACCEPT_EULA literal is baked.
# sudo (G-15): none — file perms go through a docker root helper (--user 0), not host sudo.
#
# Usage: CV_EULA_CONSENT=yes bash warm_cache.sh <cache-root-abs> [provision|warm|strip-gpu]
set -euo pipefail

export CV_STEP=measure-warm
# Capture the operator's EXPLICIT cache root BEFORE sourcing common.sh — the sourced
# workstation_setup SoT gives CV_ISAAC_CACHE_ROOT a P1-smoke DEFAULT, which must NOT
# silently stand in for the intended measurement tree. D-1: root = arg OR explicit env.
_OPERATOR_CACHE_ROOT="${CV_ISAAC_CACHE_ROOT:-}"
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=scripts/measure/common.sh
source "$SCRIPT_DIR/common.sh"

# EULA gate FIRST — before touching args or the filesystem. Booting Isaac to warm needs
# per-run operator consent (NEG-2). Verification: `unset CV_EULA_CONSENT;
# warm_cache.sh /tmp/nonexistent` -> exit 3 (this line, before the root is ever used).
measure_eula_gate

CACHE_ROOT="${1:-$_OPERATOR_CACHE_ROOT}"
MODE="${2:-warm}"
[[ -n "$CACHE_ROOT" ]] \
  || die "cache root required: pass <cache-root-abs> or set CV_ISAAC_CACHE_ROOT (D-1). usage: CV_EULA_CONSENT=yes $0 <cache-root-abs> [provision|warm|strip-gpu]"
case "$CACHE_ROOT" in
  /*) : ;;
  *) die "cache root must be a HOST ABSOLUTE path (sibling-container safety, D-O): $CACHE_ROOT" ;;
esac
case "$MODE" in
  provision | warm | strip-gpu) : ;;
  *) die "unknown mode '$MODE' (want: provision | warm | strip-gpu)" ;;
esac

require_cmd docker
IMG="$CV_MEASURE_IMAGE"

# Cache mounts = D-1 canonical 6-way (verbatim). The host subpaths mirror
# CV_MEASURE_CACHE_SUBPATHS in common.sh so tree creation cannot drift from the mounts.
cache_mounts() {
  printf '%s\n' \
    -v "$CACHE_ROOT/cache/kit:/isaac-sim/kit/cache:rw" \
    -v "$CACHE_ROOT/cache/home:/isaac-sim/.cache:rw" \
    -v "$CACHE_ROOT/cache/computecache:/isaac-sim/.nv/ComputeCache:rw" \
    -v "$CACHE_ROOT/logs:/isaac-sim/.nvidia-omniverse/logs:rw" \
    -v "$CACHE_ROOT/data:/isaac-sim/.local/share/ov/data:rw" \
    -v "$CACHE_ROOT/documents:/isaac-sim/Documents:rw"
}

warm_scene() {
  # Boot the runner image with warm_scene.py (entrypoint override, G-14) on a dedicated
  # non-host bridge (R8). One scene load fills the closure; --rm cleans up.
  docker network inspect "$CV_MEASURE_NET" >/dev/null 2>&1 \
    || docker network create --driver bridge "$CV_MEASURE_NET" >/dev/null
  local cname="cv-measure-warm-$$"
  local evid="${CV_MEASURE_OUT:-$CACHE_ROOT/.warm-evidence}"
  mkdir -p "$evid" 2>/dev/null || true
  measure_chown_dir "$evid" "$IMG"   # warm_scene writes as uid 1234

  local mounts=()
  mapfile -t mounts < <(cache_mounts)

  log "warming closure: booting $IMG with warm_scene.py (net=$CV_MEASURE_NET, evidence=$evid)"
  docker rm -f "$cname" >/dev/null 2>&1 || true
  docker run --rm --name "$cname" \
    --network "$CV_MEASURE_NET" \
    --gpus all \
    -e NVIDIA_DRIVER_CAPABILITIES=all \
    "${CV_EULA_DOCKER_ARGS[@]}" \
    --shm-size "$CV_MEASURE_SHM_SIZE" \
    "${mounts[@]}" \
    -v "$SCRIPT_DIR:/cv/measure:ro" \
    -v "$evid:/cv/measure-out:rw" \
    --entrypoint /isaac-sim/python.sh \
    "$IMG" /cv/measure/warm_scene.py \
    --scene-rel "$CV_MEASURE_SCENE_REL" --out /cv/measure-out \
    || die "warm_scene failed (see $evid; container=$cname)"
}

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
  warm)
    measure_provision_tree "$CACHE_ROOT" "$IMG"
    warm_scene
    ;;
  strip-gpu)
    measure_provision_tree "$CACHE_ROOT" "$IMG"   # ensure tree shape (idempotent)
    strip_gpu_cache
    ;;
esac

after_bytes="$(measure_du_bytes "$CACHE_ROOT" "$IMG" 2>/dev/null || echo NA)"
log "DONE mode=$MODE cache_bytes_after=$after_bytes (before=$before_bytes) root=$CACHE_ROOT"
