#!/usr/bin/env bash
# p6c1 Arm B (C-2, 1 job x n repeat) — ONE runner container, 8 missions in series.
#
# The orchestrator is NOT involved (it only knows how to spawn one runner per job), so
# this script reproduces M3's spawn recipe BY HAND, verbatim where it matters:
#   * a per-job docker BRIDGE network (never host networking)
#   * one ROS_DOMAIN_ID stamped on BOTH containers
#   * runner FIRST (the sim is the /clock source, G-19), SUT joined once it is running
#   * SUT started as an UNMODIFIED blackbox: no command/entrypoint override, no operator
#     env leak, no GPU device request
#   * the six cache binds + shm-size 1g + --gpus all on the runner
# The one deliberate difference: the runner's entrypoint is the spike loop module,
# injected by bind-mount + PYTHONPATH so no image is rebuilt or retagged.
#
#   usage: ACCEPT_EULA=... PRIVACY_CONSENT=... arm_b.sh [evidence_dir]
set -euo pipefail

SPIKE_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=common.sh
source "$SPIKE_DIR/common.sh"

EVID="${1:-$CV_EVIDENCE_DIR}/arm_b"
: "${CV_SPIKE_DOMAIN_ID:=99}"          # inside the 0..101 LOCKED §7.5 space, spike-only
: "${CV_SPIKE_NET:=p6spike-net}"
RUNNER_CT="p6spike-runner"
SUT_CT="p6spike-sut"

mkdir -p "$EVID/logs" "$EVID/out"
chmod 777 "$EVID/out"
CACHE_ROOT="$EVID/cache"
SPECS="$EVID/specs.json"

require_consent
require_exclusive_gpu

# --- the SAME JOB_SPEC dicts Arm A's CLI builds (production admit + wire builder) ---
"${CV_PY:-$HOME/cv-infra-host-venv/bin/python}" "$SPIKE_DIR/make_specs.py" \
  "$SPIKE_DIR"/scenarios/sample_*.yaml --out "$SPECS" --job-id-prefix p6b
SUT_IMAGE="$("${CV_PY:-$HOME/cv-infra-host-venv/bin/python}" -c \
  "import json,sys;print(json.load(open(sys.argv[1]))[0]['sut_image_ref'])" "$SPECS")"
log "SUT image (from the specs): $SUT_IMAGE"
docker image inspect "$SUT_IMAGE" >/dev/null \
  || die "SUT image is not present locally and this spike does not pull: $SUT_IMAGE"
docker image inspect "$CV_RUNNER_IMAGE" >/dev/null \
  || die "runner image is not present locally and this spike does not pull: $CV_RUNNER_IMAGE"

cleanup() {
  local rc=$?
  log "teardown"
  docker logs "$RUNNER_CT" > "$EVID/logs/runner.log" 2>&1 || true
  docker logs "$SUT_CT" > "$EVID/logs/sut.log" 2>&1 || true
  docker rm -f "$SUT_CT" >/dev/null 2>&1 || true
  docker rm -f "$RUNNER_CT" >/dev/null 2>&1 || true
  docker network rm "$CV_SPIKE_NET" >/dev/null 2>&1 || true
  exit $rc
}
trap cleanup EXIT

t_seed0="$(now_s)"
seed_cache "$CACHE_ROOT"
t_seed1="$(now_s)"
log "cache seeded in $(elapsed "$t_seed0" "$t_seed1")s -> $CACHE_ROOT"

mapfile -t CACHE_ARGS < <(cache_bind_args "$CACHE_ROOT")

docker network create --driver bridge "$CV_SPIKE_NET" >/dev/null
log "network $CV_SPIKE_NET created; ROS_DOMAIN_ID=$CV_SPIKE_DOMAIN_ID"

T_START="$(now_s)"
docker run -d --name "$RUNNER_CT" \
  --network "$CV_SPIKE_NET" \
  --gpus all --shm-size 1g \
  -e ACCEPT_EULA="$ACCEPT_EULA" -e PRIVACY_CONSENT="$PRIVACY_CONSENT" \
  -e ROS_DOMAIN_ID="$CV_SPIKE_DOMAIN_ID" \
  -e ROS_DISTRO=jazzy -e RMW_IMPLEMENTATION=rmw_fastrtps_cpp \
  -e PYTHONPATH=/exp \
  -e CV_SPIKE_SPECS=/cv/specs.json \
  -e RESULT_OUT=/cv/out \
  -v "$SPECS":/cv/specs.json:ro \
  -v "$EVID/out":/cv/out:rw \
  -v "$SPIKE_DIR":/exp/p6spike:ro \
  "${CACHE_ARGS[@]}" \
  --entrypoint ./python.sh \
  "$CV_RUNNER_IMAGE" -m p6spike.loop_runner >/dev/null
log "runner container started"

# Runner readiness gate = the supervisor's default probe (container reports running).
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
log "SUT container started (blackbox: no command/entrypoint override, no GPU)"

set +e
RUNNER_EXIT="$(docker wait "$RUNNER_CT")"
set -e
T_END="$(now_s)"
log "runner exited: code=$RUNNER_EXIT wall=$(elapsed "$T_START" "$T_END")s"

{
  echo "# Arm B (C-2) — one runner process, 8 missions in series"
  echo "runner_image=$CV_RUNNER_IMAGE"
  echo "sut_image=$SUT_IMAGE"
  echo "ros_domain_id=$CV_SPIKE_DOMAIN_ID"
  echo "network=$CV_SPIKE_NET"
  echo "cache_seed_s=$(elapsed "$t_seed0" "$t_seed1")"
  echo "container_wall_s=$(elapsed "$T_START" "$T_END")"
  echo "t0_epoch=$T_START"
  echo "t1_epoch=$T_END"
  echo "runner_exit_code=$RUNNER_EXIT"
  echo "finished=$(date -Is)"
} > "$EVID/arm_b_summary.txt"
cat "$EVID/arm_b_summary.txt"
