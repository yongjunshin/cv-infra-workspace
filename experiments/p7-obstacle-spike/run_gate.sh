#!/usr/bin/env bash
# p7c1 W0 spike — run ONE gate batch inside the runner image. THROWAWAY.
#
#   usage: ACCEPT_EULA=... PRIVACY_CONSENT=... run_gate.sh <run-name> <gate> [gate ...]
#
#   env:  CV_SPIKE_CACHE=warm|cold        (default warm; cold = brand-new EMPTY tiers)
#         CV_SPIKE_ASSETS_SRC=<host path> (assets.json produced by the enumerate gate)
#         CV_SPIKE_SAMPLER=1|0            (default 1 — own 0.5 s per-PID VRAM CSV)
#         CV_SPIKE_*                      (any spike knob; forwarded verbatim)
#
# Each run gets its own evidence directory, its own cache root and its own sampler
# window, so a run's numbers are never read out of another run's window (G-18).
set -euo pipefail

SPIKE_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=common.sh
source "$SPIKE_DIR/common.sh"

RUN="${1:?usage: run_gate.sh <run-name> <gate> [gate ...]}"
shift
GATES=("$@")
[[ ${#GATES[@]} -ge 1 ]] || die "at least one gate is required"

: "${CV_SPIKE_CACHE:=warm}"
: "${CV_SPIKE_SAMPLER:=1}"
: "${CV_SPIKE_ASSETS_SRC:=}"
: "${CV_SPIKE_URLS_SRC:=}"
: "${CV_SPIKE_DOMAIN_ID:=77}"
: "${CV_SPIKE_WALL_CAP_S:=5400}"

EVID="$CV_EVIDENCE_DIR/$RUN"
mkdir -p "$EVID/out" "$EVID/crosscheck"
chmod 777 "$EVID/out"
CACHE_ROOT="$EVID/cache"
CT="p7spike-$RUN"

require_consent
require_exclusive_gpu
docker image inspect "$CV_RUNNER_IMAGE" >/dev/null \
  || die "runner image absent locally and this spike does not pull: $CV_RUNNER_IMAGE"

SAMPLER_PID=""
cleanup() {
  local rc=$?
  docker logs "$CT" > "$EVID/container.log" 2>&1 || true
  docker rm -f "$CT" >/dev/null 2>&1 || true
  [[ -n "$SAMPLER_PID" ]] && kill "$SAMPLER_PID" 2>/dev/null || true
  exit $rc
}
trap cleanup EXIT

t_c0="$(now_s)"
if [[ -n "${CV_SPIKE_CACHE_ROOT:-}" ]]; then
  # REUSE an existing root untouched — this is how the cold/warm PAIR is measured:
  # the cold run fetches into a brand-new empty root, and the warm run points here so
  # "warm" means "these very assets are already on disk", not "some other scene is".
  CACHE_ROOT="$CV_SPIKE_CACHE_ROOT"
  [[ -d "$CACHE_ROOT" ]] || die "CV_SPIKE_CACHE_ROOT does not exist: $CACHE_ROOT"
  CV_SPIKE_CACHE="reuse:$CACHE_ROOT"
elif [[ "$CV_SPIKE_CACHE" == "warm" ]]; then
  seed_cache "$CACHE_ROOT"
else
  empty_cache "$CACHE_ROOT"
fi
t_c1="$(now_s)"
log "cache root ($CV_SPIKE_CACHE) prepared in $(elapsed "$t_c0" "$t_c1")s"
mapfile -t CACHE_ARGS < <(cache_bind_args "$CACHE_ROOT")

ASSET_ARGS=()
if [[ -n "$CV_SPIKE_ASSETS_SRC" ]]; then
  [[ -f "$CV_SPIKE_ASSETS_SRC" ]] || die "CV_SPIKE_ASSETS_SRC not a file: $CV_SPIKE_ASSETS_SRC"
  ASSET_ARGS=(-v "$CV_SPIKE_ASSETS_SRC":/cv/assets.json:ro)
fi
if [[ -n "$CV_SPIKE_URLS_SRC" ]]; then
  [[ -f "$CV_SPIKE_URLS_SRC" ]] || die "CV_SPIKE_URLS_SRC not a file: $CV_SPIKE_URLS_SRC"
  ASSET_ARGS+=(-v "$CV_SPIKE_URLS_SRC":/cv/urls.json:ro)
fi

# Forward every CV_SPIKE_* knob the operator set (the script's own defaults live in
# spike.py's argparse, so an unset knob is not passed as an empty string).
ENV_ARGS=()
for name in $(compgen -e | grep '^CV_SPIKE_' || true); do
  ENV_ARGS+=(-e "$name=${!name}")
done

LD_PREPEND="/isaac-sim/exts/isaacsim.ros2.bridge/jazzy/lib"
BASE_LD="$(image_ld_library_path)"
[[ -n "$BASE_LD" ]] && LD_PREPEND="$LD_PREPEND:$BASE_LD"

if [[ "$CV_SPIKE_SAMPLER" == "1" ]]; then
  bash "$SPIKE_DIR/vram_sampler.sh" "$EVID/vram_0.5s.csv" "$EVID/crosscheck" 0.5 &
  SAMPLER_PID=$!
  log "vram sampler pid=$SAMPLER_PID -> $EVID/vram_0.5s.csv"
fi

T0="$(now_s)"
docker run -d --name "$CT" \
  --gpus all --shm-size 1g \
  -e ACCEPT_EULA="$ACCEPT_EULA" -e PRIVACY_CONSENT="$PRIVACY_CONSENT" \
  -e ROS_DOMAIN_ID="$CV_SPIKE_DOMAIN_ID" \
  -e ROS_DISTRO=jazzy -e RMW_IMPLEMENTATION=rmw_fastrtps_cpp \
  -e LD_LIBRARY_PATH="$LD_PREPEND" \
  -e CV_SPIKE_OUT=/cv/out \
  "${ENV_ARGS[@]}" \
  -v "$SPIKE_DIR":/exp:ro \
  -v "$EVID/out":/cv/out:rw \
  "${ASSET_ARGS[@]}" \
  "${CACHE_ARGS[@]}" \
  --entrypoint /isaac-sim/python.sh \
  "$CV_RUNNER_IMAGE" /exp/spike.py "${GATES[@]}" >/dev/null
log "container $CT started (gates: ${GATES[*]}, cache=$CV_SPIKE_CACHE)"

deadline=$(( $(date +%s) + CV_SPIKE_WALL_CAP_S ))
CAP_FIRED=no
while :; do
  status="$(docker inspect -f '{{.State.Status}}' "$CT" 2>/dev/null || echo gone)"
  [[ "$status" == "running" ]] || break
  if (( $(date +%s) > deadline )); then
    CAP_FIRED=yes
    log "WALL CAP ${CV_SPIKE_WALL_CAP_S}s exceeded — stopping the spike container (graceful)"
    docker stop -t 30 "$CT" >/dev/null || true
    break
  fi
  sleep 2
done
set +e
EXIT_CODE="$(docker wait "$CT")"
set -e
T1="$(now_s)"

{
  echo "# p7c1 W0 spike run"
  echo "run=$RUN"
  echo "gates=${GATES[*]}"
  echo "runner_image=$CV_RUNNER_IMAGE"
  echo "cache_mode=$CV_SPIKE_CACHE"
  echo "cache_prep_s=$(elapsed "$t_c0" "$t_c1")"
  echo "container_wall_s=$(elapsed "$T0" "$T1")"
  echo "t0_epoch=$T0"
  echo "t1_epoch=$T1"
  echo "exit_code=$EXIT_CODE"
  echo "wall_cap_fired=$CAP_FIRED"
  echo "ld_library_path=$LD_PREPEND"
  echo "finished=$(date -Is)"
} > "$EVID/summary.txt"
cat "$EVID/summary.txt"
