#!/usr/bin/env bash
# p6c2 — one Arm B VARIANT run (ablation / cleanup / long run). THROWAWAY.
#
# Same spawn recipe as arm_b.sh (per-job bridge network, one ROS_DOMAIN_ID on both
# containers, runner first, SUT as an unmodified blackbox, the six cache binds), with
# three differences the p6c2 experiment needs:
#   1. the iteration list is BUILT from a cycling sample set (CV_SPIKE_SAMPLES) to a
#      length of CV_SPIKE_N, so every variant runs the same workload;
#   2. the component toggles ride in as CV_SPIKE_ABLATE / CV_SPIKE_CLEANUP;
#   3. the VRAM sampler is started and stopped BY THIS SCRIPT, so each variant owns
#      its own CSV whose window is exactly the variant's container lifetime.
#
#   usage: ACCEPT_EULA=... PRIVACY_CONSENT=... arm_b2.sh <variant-name>
#   env:   CV_SPIKE_SAMPLES=2,3,5,6,7  CV_SPIKE_N=24
#          CV_SPIKE_ABLATE=...  CV_SPIKE_CLEANUP=...  CV_SPIKE_WALL_CAP_S=...
set -euo pipefail

SPIKE_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=common.sh
source "$SPIKE_DIR/common.sh"

VARIANT="${1:?usage: arm_b2.sh <variant-name>}"
: "${CV_SPIKE_SAMPLES:=2,3,5,6,7}"
: "${CV_SPIKE_N:=24}"
: "${CV_SPIKE_ABLATE:=}"
: "${CV_SPIKE_CLEANUP:=}"
: "${CV_SPIKE_DOMAIN_ID:=99}"
: "${CV_SPIKE_NET:=p6spike-net}"
: "${CV_SPIKE_WALL_CAP_S:=5400}"
RUNNER_CT="p6spike-runner"
SUT_CT="p6spike-sut"

EVID="${CV_EVIDENCE_DIR}/p6c2/${VARIANT}"
mkdir -p "$EVID/logs" "$EVID/out" "$EVID/crosscheck"
chmod 777 "$EVID/out"
CACHE_ROOT="$EVID/cache"
SPECS="$EVID/specs.json"

require_consent
require_exclusive_gpu

# --- the iteration list: cycle the sample set to length N -------------------- #
IFS=',' read -r -a SAMPLES <<< "$CV_SPIKE_SAMPLES"
YAMLS=()
for ((i = 0; i < CV_SPIKE_N; i++)); do
  s="${SAMPLES[$((i % ${#SAMPLES[@]}))]}"
  YAMLS+=("$SPIKE_DIR/scenarios/sample_$(printf '%02d' "$s").yaml")
done
log "variant=$VARIANT n=$CV_SPIKE_N samples=$CV_SPIKE_SAMPLES ablate=[${CV_SPIKE_ABLATE}] cleanup=[${CV_SPIKE_CLEANUP}]"

"${CV_PY:-$HOME/cv-infra-host-venv/bin/python}" "$SPIKE_DIR/make_specs.py" \
  "${YAMLS[@]}" --out "$SPECS" --job-id-prefix "p6c2-${VARIANT}"
SUT_IMAGE="$("${CV_PY:-$HOME/cv-infra-host-venv/bin/python}" -c \
  "import json,sys;print(json.load(open(sys.argv[1]))[0]['sut_image_ref'])" "$SPECS")"
docker image inspect "$SUT_IMAGE" >/dev/null \
  || die "SUT image is not present locally and this experiment does not pull: $SUT_IMAGE"
docker image inspect "$CV_RUNNER_IMAGE" >/dev/null \
  || die "runner image is not present locally and this experiment does not pull: $CV_RUNNER_IMAGE"

SAMPLER_PID=""
cleanup() {
  local rc=$?
  log "teardown ($VARIANT)"
  docker logs "$RUNNER_CT" > "$EVID/logs/runner.log" 2>&1 || true
  docker logs "$SUT_CT" > "$EVID/logs/sut.log" 2>&1 || true
  docker rm -f "$SUT_CT" >/dev/null 2>&1 || true
  docker rm -f "$RUNNER_CT" >/dev/null 2>&1 || true
  docker network rm "$CV_SPIKE_NET" >/dev/null 2>&1 || true
  [[ -n "$SAMPLER_PID" ]] && kill "$SAMPLER_PID" 2>/dev/null || true
  exit $rc
}
trap cleanup EXIT

t_seed0="$(now_s)"
seed_cache "$CACHE_ROOT"
t_seed1="$(now_s)"
log "cache seeded in $(elapsed "$t_seed0" "$t_seed1")s"

mapfile -t CACHE_ARGS < <(cache_bind_args "$CACHE_ROOT")

bash "$SPIKE_DIR/vram_sampler.sh" "$EVID/vram_0.5s.csv" "$EVID/crosscheck" 0.5 &
SAMPLER_PID=$!
log "vram sampler pid=$SAMPLER_PID"

docker network create --driver bridge "$CV_SPIKE_NET" >/dev/null

T_START="$(now_s)"
docker run -d --name "$RUNNER_CT" \
  --network "$CV_SPIKE_NET" \
  --gpus all --shm-size 1g \
  -e ACCEPT_EULA="$ACCEPT_EULA" -e PRIVACY_CONSENT="$PRIVACY_CONSENT" \
  -e ROS_DOMAIN_ID="$CV_SPIKE_DOMAIN_ID" \
  -e ROS_DISTRO=jazzy -e RMW_IMPLEMENTATION=rmw_fastrtps_cpp \
  -e PYTHONPATH=/exp \
  -e CV_SPIKE_SPECS=/cv/specs.json \
  -e CV_SPIKE_ABLATE="$CV_SPIKE_ABLATE" \
  -e CV_SPIKE_CLEANUP="$CV_SPIKE_CLEANUP" \
  -e RESULT_OUT=/cv/out \
  -v "$SPECS":/cv/specs.json:ro \
  -v "$EVID/out":/cv/out:rw \
  -v "$SPIKE_DIR":/exp/p6spike:ro \
  "${CACHE_ARGS[@]}" \
  --entrypoint ./python.sh \
  "$CV_RUNNER_IMAGE" -m p6spike.loop_runner >/dev/null
log "runner container started"

for _ in $(seq 1 120); do
  [[ "$(docker inspect -f '{{.State.Status}}' "$RUNNER_CT")" == "running" ]] && break
  sleep 1
done
[[ "$(docker inspect -f '{{.State.Status}}' "$RUNNER_CT")" == "running" ]] \
  || die "runner container never reached 'running'"

docker run -d --name "$SUT_CT" \
  --network "$CV_SPIKE_NET" \
  -e ROS_DOMAIN_ID="$CV_SPIKE_DOMAIN_ID" \
  "$SUT_IMAGE" >/dev/null
log "SUT container started (blackbox)"

WATCHDOG_FIRED=no
deadline=$(( $(date +%s) + CV_SPIKE_WALL_CAP_S ))
while :; do
  status="$(docker inspect -f '{{.State.Status}}' "$RUNNER_CT" 2>/dev/null || echo gone)"
  [[ "$status" == "running" ]] || break
  if (( $(date +%s) > deadline )); then
    WATCHDOG_FIRED=yes
    log "WALL-CLOCK CAP ${CV_SPIKE_WALL_CAP_S}s EXCEEDED — stopping the runner (graceful)"
    docker stop -t 30 "$RUNNER_CT" >/dev/null || true
    break
  fi
  sleep 2
done
set +e
RUNNER_EXIT="$(docker wait "$RUNNER_CT")"
set -e
T_END="$(now_s)"
log "runner exited: code=$RUNNER_EXIT wall=$(elapsed "$T_START" "$T_END")s"

{
  echo "# p6c2 Arm B variant"
  echo "variant=$VARIANT"
  echo "n=$CV_SPIKE_N"
  echo "samples=$CV_SPIKE_SAMPLES"
  echo "ablate=$CV_SPIKE_ABLATE"
  echo "cleanup=$CV_SPIKE_CLEANUP"
  echo "runner_image=$CV_RUNNER_IMAGE"
  echo "sut_image=$SUT_IMAGE"
  echo "ros_domain_id=$CV_SPIKE_DOMAIN_ID"
  echo "cache_seed_s=$(elapsed "$t_seed0" "$t_seed1")"
  echo "container_wall_s=$(elapsed "$T_START" "$T_END")"
  echo "t0_epoch=$T_START"
  echo "t1_epoch=$T_END"
  echo "runner_exit_code=$RUNNER_EXIT"
  echo "wall_watchdog_fired=$WATCHDOG_FIRED"
  echo "finished=$(date -Is)"
} > "$EVID/summary.txt"
cat "$EVID/summary.txt"
