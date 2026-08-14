#!/usr/bin/env bash
# probe_readiness.sh — does the built-in stub SUT satisfy the runner's readiness
# contract? (M7 §3.5 option B; feeds `DoD-P5-07`, `REQ-SELFTEST-001/003`.)
#
# WHAT IT PROVES — the four things `cv_infra/runner/adapter/ros2.py` demands of a
# SUT, measured ACROSS A CONTAINER BOUNDARY on a dedicated bridge network, i.e. the
# same topology M3 spawns for a real job (LOCKED §5: never host networking):
#
#   A  <is_active>/std_srvs.Trigger answers success=True        (readiness step 2)
#   B  <node>/get_parameters says use_sim_time=true             (readiness step 3)
#   C  A still holds once a /clock publisher exists on the net  (live topology)
#   D  NavigateToPose accepts the goal and terminates SUCCEEDED (drive_mission)
#   E  the stub container is STILL RUNNING at the end — a SUT that exits burns the
#      supervisor's restart budget and the job dies as an infra failure
#      (`cv_infra/orchestrator/supervisor.py`), so "it answered once" is not enough
#
# WHAT IT DOES NOT PROVE (say it out loud — GPU honesty): readiness step 1, the
# /clock FLOW, is the RUNNER's (Isaac's) job, not the stub's; and this probe boots
# no Isaac, so it is NOT a self-test round-trip. `cv-infra selftest` green on a GPU
# host is a separate, later claim.
#
# WHY IT CAN RUN WITHOUT A GPU: the same reason DoD-P1-05 could measure
# container-boundary DDS with a ros:jazzy peer (`scripts/isaac_smoke/run_dds_handshake.sh`)
# — the SUT surface is pure ROS 2, so the peer side can be any ROS 2 container. Here
# the peer IS the stub image itself (it already carries the nav2_msgs typesupport the
# probe needs), so the probe adds no third artifact.
#
# USAGE
#   CV_SELFTEST_STUB_IMAGE=cv-infra-selftest-stub:<tag> bash scripts/selftest_stub/probe_readiness.sh
#     exit 0 = every phase passed        exit 1 = a phase failed (evidence path printed)
#     exit 2 = usage/preflight error
# On a host where docker needs sudo, run the whole script under sudo (this probe
# deliberately does not carry the workstation scripts' `sudo -n` docker prefix: it
# must also run on a plain dev box and on a clean redeploy target).
set -euo pipefail

readonly EXIT_FAIL=1
readonly EXIT_USAGE=2

log()  { printf '[cv-infra][stub-probe] %s\n' "$*"; }
err()  { printf '[cv-infra][stub-probe][ERROR] %s\n' "$*" >&2; }
die()  { err "$*"; exit "${2:-$EXIT_USAGE}"; }

# --- configuration ----------------------------------------------------------
# The image ref is REQUIRED and never guessed (image-as-artifact, FU-10 — the same
# policy `CV_SELFTEST_SUT_IMAGE` follows in cv_infra/orchestrator/selftest.py).
IMAGE="${CV_SELFTEST_STUB_IMAGE:-}"
[[ -n "$IMAGE" ]] || die "set CV_SELFTEST_STUB_IMAGE=<stub image ref> (build: docker/selftest_stub/README.md)"

# Wiring the probe expects to find. Defaults mirror the M1 adapter schema defaults
# (cv_infra/contract/adapter_schema.py), which is what the built-in stub request
# resolves to — so a green probe means "matches the self-test scenario", not
# "matches whatever I typed here".
IS_ACTIVE="${CV_STUB_IS_ACTIVE_SERVICE:-/lifecycle_manager_navigation/is_active}"
GOAL_ACTION="${CV_STUB_GOAL_ACTION:-/navigate_to_pose}"
# Same derivation the adapter does (`get_parameters_service_for`): the Trigger path
# minus its last segment. Re-deriving it here (instead of hardcoding) is what makes
# the probe fail when the stub's node identity drifts away from its service name.
NODE_PATH="${IS_ACTIVE%/*}"
GET_PARAMS="$NODE_PATH/get_parameters"

# Bounded waits — guards against a hang, NOT measured thresholds (nothing here is a
# pass/fail number that belongs in an NFR; CLAUDE §2-4).
DISCOVERY_TIMEOUT_S="${CV_STUB_PROBE_DISCOVERY_TIMEOUT_S:-90}"
CALL_TIMEOUT_S="${CV_STUB_PROBE_CALL_TIMEOUT_S:-60}"

DOMAIN_ID="${CV_STUB_PROBE_DOMAIN_ID:-91}"
RUN_ID="stubprobe-$(date +%Y%m%d-%H%M%S)-$$"
NET="${CV_STUB_PROBE_NET:-cv-stub-probe-$$}"
STUB_C="cv-stub-probe-sut-$$"
PEER_C="cv-stub-probe-peer-$$"
OUT_DIR="${CV_STUB_PROBE_OUT:-$HOME/cv-infra-selftest-stub-probe}/$RUN_ID"

command -v docker >/dev/null 2>&1 || die "docker not found on PATH"
docker image inspect "$IMAGE" >/dev/null 2>&1 \
  || die "image '$IMAGE' not present — build it first (docker/selftest_stub/README.md)"

mkdir -p "$OUT_DIR"

cleanup() {
  docker rm -f "$STUB_C" "$PEER_C" >/dev/null 2>&1 || true
  docker network rm "$NET" >/dev/null 2>&1 || true
}
trap cleanup EXIT

fail=0
# check <phase> <description> <evidence-file> <grep-args...>
check() {
  local phase="$1" what="$2" file="$3"; shift 3
  if grep -qE "$@" "$file"; then
    log "PASS $phase — $what"
  else
    err "FAIL $phase — $what (looked for: $* in $(basename "$file"))"
    fail=1
  fi
}

# peer_run <outfile> <cmd...> — run a ROS 2 command in the peer container.
# /ros_entrypoint.sh is the base image's own env glue (sources setup.bash, execs).
peer_run() {
  local out="$1"; shift
  timeout "$CALL_TIMEOUT_S" docker exec "$PEER_C" /ros_entrypoint.sh "$@" >"$out" 2>&1 || true
}

# --- bring up the two containers -------------------------------------------
log "image      : $IMAGE"
log "revision   : $(docker image inspect --format '{{index .Config.Labels "org.opencontainers.image.revision"}}' "$IMAGE")"
log "network    : $NET (bridge)   domain: $DOMAIN_ID"
log "evidence   : $OUT_DIR"
docker network create --driver bridge "$NET" >/dev/null

# PRODUCTION SHAPE, on purpose: the supervisor starts the SUT with no command
# override and exactly one env var (ROS_DOMAIN_ID) — nothing else, no GPU, no
# docker.sock. Anything this probe had to add would be a wiring the real job never
# gets, and the green would be a lie.
docker run -d --name "$STUB_C" --network "$NET" -e ROS_DOMAIN_ID="$DOMAIN_ID" "$IMAGE" >/dev/null
docker run -d --name "$PEER_C" --network "$NET" -e ROS_DOMAIN_ID="$DOMAIN_ID" \
  --entrypoint /ros_entrypoint.sh "$IMAGE" sleep infinity >/dev/null

# --- discovery --------------------------------------------------------------
log "waiting for '$NODE_PATH' to appear on the peer's ROS graph (<= ${DISCOVERY_TIMEOUT_S}s)"
deadline=$((SECONDS + DISCOVERY_TIMEOUT_S))
discovered=0
while ((SECONDS < deadline)); do
  peer_run "$OUT_DIR/node_list.txt" ros2 node list
  if grep -qx -- "$NODE_PATH" "$OUT_DIR/node_list.txt"; then discovered=1; break; fi
  running="$(docker inspect -f '{{.State.Running}}' "$STUB_C" 2>/dev/null || echo false)"
  [[ "$running" == "true" ]] || { docker logs "$STUB_C" >"$OUT_DIR/stub.log" 2>&1 || true
                                  die "stub container exited during discovery — see $OUT_DIR/stub.log" "$EXIT_FAIL"; }
  sleep 3
done
((discovered)) || { docker logs "$STUB_C" >"$OUT_DIR/stub.log" 2>&1 || true
                    die "'$NODE_PATH' never appeared across the container boundary — see $OUT_DIR" "$EXIT_FAIL"; }
peer_run "$OUT_DIR/service_list.txt" ros2 service list

# --- A: readiness gate (no /clock on the network yet) -----------------------
peer_run "$OUT_DIR/A_is_active.txt" ros2 service call "$IS_ACTIVE" std_srvs/srv/Trigger "{}"
check A "$IS_ACTIVE answers Trigger success=True" "$OUT_DIR/A_is_active.txt" 'success=True'

# --- B: use_sim_time verification (the exact service the adapter derives) ----
peer_run "$OUT_DIR/B_use_sim_time.txt" ros2 service call "$GET_PARAMS" \
  rcl_interfaces/srv/GetParameters "{names: ['use_sim_time']}"
# type=1 is PARAMETER_BOOL — the adapter REJECTS a non-bool answer as unknown, so the
# type is as load-bearing as the value.
check B "$GET_PARAMS reports use_sim_time=true as a BOOL" \
  "$OUT_DIR/B_use_sim_time.txt" 'type=1, bool_value=True'

# --- C: same gate with a /clock publisher present (live topology) -----------
# Isaac is the /clock source in a real job. This one is constant-valued (the stub
# reads no clock value; what is being checked is that a use_sim_time node with a
# clock on the wire keeps serving).
docker exec -d "$PEER_C" /ros_entrypoint.sh \
  ros2 topic pub --rate 10 /clock rosgraph_msgs/msg/Clock "{clock: {sec: 1, nanosec: 0}}" \
  >/dev/null 2>&1 || true
sleep 5
peer_run "$OUT_DIR/C_clock_info.txt" ros2 topic info -v /clock
peer_run "$OUT_DIR/C_is_active.txt" ros2 service call "$IS_ACTIVE" std_srvs/srv/Trigger "{}"
check C "$IS_ACTIVE still answers with /clock flowing on the job network" \
  "$OUT_DIR/C_is_active.txt" 'success=True'

# --- D: the mission surface -------------------------------------------------
peer_run "$OUT_DIR/D_goal.txt" ros2 action send_goal "$GOAL_ACTION" \
  nav2_msgs/action/NavigateToPose \
  "{pose: {header: {frame_id: 'map'}, pose: {position: {x: -6.0, y: -1.0, z: 0.0}}}}"
check D "$GOAL_ACTION accepts the goal" "$OUT_DIR/D_goal.txt" 'Goal accepted with ID'
check D "$GOAL_ACTION reaches terminal SUCCEEDED" "$OUT_DIR/D_goal.txt" 'status: SUCCEEDED'

# --- E: the SUT survived the job -------------------------------------------
docker logs "$STUB_C" >"$OUT_DIR/stub.log" 2>&1 || true
docker inspect -f 'Running={{.State.Running}} Restarting={{.State.Restarting}} ExitCode={{.State.ExitCode}}' \
  "$STUB_C" >"$OUT_DIR/E_state.txt" 2>&1 || true
check E "stub container is still running after the mission" "$OUT_DIR/E_state.txt" 'Running=true'

if ((fail)); then
  err "STUB READINESS PROBE FAILED — evidence: $OUT_DIR"
  exit "$EXIT_FAIL"
fi
log "STUB READINESS PROBE PASS — A/B/C/D/E green across the container boundary"
log "evidence: $OUT_DIR"
