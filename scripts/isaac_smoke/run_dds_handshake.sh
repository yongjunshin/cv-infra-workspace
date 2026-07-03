#!/usr/bin/env bash
# run_dds_handshake.sh — DoD-P1-05 host-side wrapper (M2, walking skeleton #1.5).
# Serial contract: refuses to run before run_smoke.sh has passed (DoD-P1-04 first).
#
# Proves container-boundary DDS on the dedicated bridge network (R8):
#   forward : isaac bridge publishes /clock -> ros:jazzy container echoes >0 msgs
#   reverse : ros:jazzy publishes /cmd_vel (linear.x=0.42) -> received in-process in Isaac
#   QoS     : ros2 topic info -v shows matching endpoint count >= 1
#   SHM off : UDPv4-only Fast DDS profile on both sides; no fastrtps segs in /dev/shm
# Multicast discovery is MEASURED first; on failure an initialPeers unicast fallback
# profile is generated and the result recorded (R8 measure-and-adjust).
#
# Usage (on the workstation):  CV_EULA_CONSENT=yes bash run_dds_handshake.sh
set -euo pipefail

export CV_STEP=dds-handshake
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=../workstation_setup/common.sh
source "$SCRIPT_DIR/../workstation_setup/common.sh"
require_cmd docker

# EULA runtime consent gate — identical contract to run_smoke.sh (NEG-2).
if [[ "${CV_EULA_CONSENT:-}" != "yes" ]]; then
  err "NVIDIA Isaac Sim EULA consent required (runtime operator input; never committed)."
  die "Re-run with:  CV_EULA_CONSENT=yes $0"
fi
_consent_upper="${CV_EULA_CONSENT^^}"
CV_EULA_DOCKER_ARGS=(-e "ACCEPT_EULA=${_consent_upper:0:1}")

# Serial contract: DoD-P1-04 must be green first.
CV_SMOKE_HOME="${CV_SMOKE_HOME:-$HOME/cv-infra-p1-smoke}"
OUT_ROOT="$CV_SMOKE_HOME/out"
[[ -s "$OUT_ROOT/last_smoke_pass" ]] || \
  die "DoD-P1-04 smoke has not passed yet (missing $OUT_ROOT/last_smoke_pass). Run run_smoke.sh first."

IMG="$CV_ISAAC_IMAGE"
[[ -n "$CV_ISAAC_DIGEST" ]] && IMG="${CV_ISAAC_IMAGE%:*}@${CV_ISAAC_DIGEST}"

# ros:jazzy peer — 2-stage digest pin (pull by exact tag once, then lock @sha256).
JIMG="$CV_ROS_JAZZY_IMAGE"
if [[ -n "$CV_ROS_JAZZY_DIGEST" ]]; then
  JIMG="${CV_ROS_JAZZY_IMAGE%:*}@${CV_ROS_JAZZY_DIGEST}"
else
  warn "CV_ROS_JAZZY_DIGEST not locked yet — pulling by exact tag '$CV_ROS_JAZZY_IMAGE'."
  warn "Lock the digest in common.sh after this first pull (printed below)."
fi
if ! "${CV_SUDO[@]}" docker image inspect "$JIMG" >/dev/null 2>&1; then
  log "pulling $JIMG"
  "${CV_SUDO[@]}" docker pull "$JIMG"
fi
log "ros:jazzy RepoDigest: $("${CV_SUDO[@]}" docker image inspect --format '{{index .RepoDigests 0}}' "$JIMG")"

RUN_ID="handshake-$(date +%Y%m%d-%H%M%S)"
RUN_DIR="$OUT_ROOT/$RUN_ID"
mkdir -p "$RUN_DIR/container" "$RUN_DIR/host"
"${CV_SUDO[@]}" docker run --rm --user 0 --entrypoint bash -v "$RUN_DIR/container":/cv-fix "$IMG" \
  -c "chown -R 1234:1234 /cv-fix"

if ! "${CV_SUDO[@]}" docker network inspect "$CV_SMOKE_NET" >/dev/null 2>&1; then
  "${CV_SUDO[@]}" docker network create --driver bridge "$CV_SMOKE_NET" >/dev/null
fi

ISAAC_C="cv-hs-isaac"; JAZZY_C="cv-hs-jazzy"
cleanup() { "${CV_SUDO[@]}" docker rm -f "$ISAAC_C" "$JAZZY_C" >/dev/null 2>&1 || true; }
trap cleanup EXIT
cleanup

EXPECT_X="0.42"
PROFILE_IN_CTR="/cv/smoke/fastdds_udp_profile.xml"
ROS_ENV=(-e RMW_IMPLEMENTATION=rmw_fastrtps_cpp -e ROS_DOMAIN_ID="$CV_SMOKE_DOMAIN_ID"
         -e FASTRTPS_DEFAULT_PROFILES_FILE="$PROFILE_IN_CTR")

# Whole-~/.cache mount — same measured root-owned-parent trap as run_smoke.sh.
CACHE_MOUNTS=(
  -v "$CV_ISAAC_CACHE_ROOT/cache/kit:/isaac-sim/kit/cache:rw"
  -v "$CV_ISAAC_CACHE_ROOT/cache/home:/isaac-sim/.cache:rw"
  -v "$CV_ISAAC_CACHE_ROOT/cache/computecache:/isaac-sim/.nv/ComputeCache:rw"
  -v "$CV_ISAAC_CACHE_ROOT/logs:/isaac-sim/.nvidia-omniverse/logs:rw"
  -v "$CV_ISAAC_CACHE_ROOT/data:/isaac-sim/.local/share/ov/data:rw"
  -v "$CV_ISAAC_CACHE_ROOT/documents:/isaac-sim/Documents:rw"
)

# start_isaac [extra docker args...] — internal Jazzy env is set BEFORE python.sh by
# sourcing the OFFICIAL /isaac-sim/setup_ros_env.sh (LOCKED: env before interpreter
# boot; do NOT pass -e ROS_DISTRO — the helper only appends the internal jazzy
# LD_LIBRARY_PATH when ROS_DISTRO is unset). CV_ROS_ENV_MODE=auto skips the helper
# to answer the [VERIFY] "does enable_extension handle the path itself?".
start_isaac() {
  local boot_cmd
  if [[ "${CV_ROS_ENV_MODE:-preset}" == "auto" ]]; then
    boot_cmd="exec /isaac-sim/python.sh /cv/smoke/headless_smoke.py --mode handshake --out /cv/out --wait-s $CV_HANDSHAKE_WAIT_S --expect-linear-x $EXPECT_X"
  else
    boot_cmd="source /isaac-sim/setup_ros_env.sh && exec /isaac-sim/python.sh /cv/smoke/headless_smoke.py --mode handshake --out /cv/out --wait-s $CV_HANDSHAKE_WAIT_S --expect-linear-x $EXPECT_X"
  fi
  "${CV_SUDO[@]}" docker run -d --name "$ISAAC_C" \
    --network "$CV_SMOKE_NET" --gpus all -e NVIDIA_DRIVER_CAPABILITIES=all \
    "${CV_EULA_DOCKER_ARGS[@]}" --shm-size "$CV_SMOKE_SHM_SIZE" \
    "${ROS_ENV[@]}" "$@" \
    "${CACHE_MOUNTS[@]}" \
    -v "$SCRIPT_DIR:/cv/smoke:ro" -v "$RUN_DIR/container:/cv/out:rw" \
    --entrypoint bash "$IMG" -c "$boot_cmd" >/dev/null
}

# wait_marker <container> <marker> <timeout_s>
wait_marker() {
  local c="$1" marker="$2" deadline=$((SECONDS + $3))
  while ((SECONDS < deadline)); do
    if "${CV_SUDO[@]}" docker logs "$c" 2>&1 | grep -q "$marker"; then return 0; fi
    local st
    st="$("${CV_SUDO[@]}" docker inspect -f '{{.State.Running}}' "$c" 2>/dev/null || echo false)"
    [[ "$st" == "true" ]] || { err "$c exited while waiting for $marker"; return 1; }
    sleep 5
  done
  err "timeout waiting for $marker in $c"; return 1
}

# jexec <cmd...> — run inside the jazzy peer with ROS sourced.
jexec() { "${CV_SUDO[@]}" docker exec "$JAZZY_C" bash -c "source /opt/ros/jazzy/setup.bash && $*"; }

# ---------------------------------------------------------------------------
# Boot both sides (multicast discovery attempt first)
# ---------------------------------------------------------------------------
log "starting Isaac handshake container (mode=${CV_ROS_ENV_MODE:-preset}, domain=$CV_SMOKE_DOMAIN_ID)"
start_isaac
wait_marker "$ISAAC_C" "CV_HANDSHAKE_CLOCK_TICKING" "$CV_HANDSHAKE_BOOT_TIMEOUT_S" || {
  "${CV_SUDO[@]}" docker logs "$ISAAC_C" > "$RUN_DIR/host/isaac.log" 2>&1 || true
  die "isaac bridge never started ticking — log: $RUN_DIR/host/isaac.log"
}

"${CV_SUDO[@]}" docker run -d --name "$JAZZY_C" --network "$CV_SMOKE_NET" \
  "${ROS_ENV[@]}" -v "$SCRIPT_DIR:/cv/smoke:ro" -v "$RUN_DIR/host:/cv/hostout" \
  "$JIMG" sleep infinity >/dev/null

# Forward path + multicast discovery measurement: /clock must arrive at the peer.
DISCOVERY_MODE="multicast"
if jexec "timeout $CV_HANDSHAKE_ECHO_TIMEOUT_S ros2 topic echo /clock --once" > "$RUN_DIR/host/clock_echo.txt" 2>&1; then
  log "R8 measurement: multicast discovery WORKS on bridge net '$CV_SMOKE_NET' (/clock echoed)"
else
  warn "R8 measurement: multicast discovery FAILED — applying initialPeers unicast fallback"
  DISCOVERY_MODE="initialPeers-unicast"
  ISAAC_IP="$("${CV_SUDO[@]}" docker inspect -f "{{(index .NetworkSettings.Networks \"$CV_SMOKE_NET\").IPAddress}}" "$ISAAC_C")"
  JAZZY_IP="$("${CV_SUDO[@]}" docker inspect -f "{{(index .NetworkSettings.Networks \"$CV_SMOKE_NET\").IPAddress}}" "$JAZZY_C")"
  gen_peers_profile() { # $1=peer ip, $2=out file (host side)
    sed "s|</rtps>|    <builtin><initialPeersList><locator><udpv4><address>$1</address></udpv4></locator></initialPeersList></builtin>\n        </rtps>|" \
      "$SCRIPT_DIR/fastdds_udp_profile.xml" > "$2"
  }
  gen_peers_profile "$JAZZY_IP" "$RUN_DIR/host/fastdds_peers_isaac.xml"
  gen_peers_profile "$ISAAC_IP" "$RUN_DIR/host/fastdds_peers_jazzy.xml"
  "${CV_SUDO[@]}" docker rm -f "$ISAAC_C" >/dev/null
  start_isaac -v "$RUN_DIR/host/fastdds_peers_isaac.xml:$PROFILE_IN_CTR.peers:ro" \
    -e FASTRTPS_DEFAULT_PROFILES_FILE="$PROFILE_IN_CTR.peers"
  wait_marker "$ISAAC_C" "CV_HANDSHAKE_CLOCK_TICKING" "$CV_HANDSHAKE_BOOT_TIMEOUT_S" || die "isaac re-boot (fallback) failed"
  jexec_profile="FASTRTPS_DEFAULT_PROFILES_FILE=/cv/hostout/fastdds_peers_jazzy.xml"
  jexec() { "${CV_SUDO[@]}" docker exec -e "$jexec_profile" "$JAZZY_C" bash -c "source /opt/ros/jazzy/setup.bash && $*"; }
  jexec "timeout $CV_HANDSHAKE_ECHO_TIMEOUT_S ros2 topic echo /clock --once" > "$RUN_DIR/host/clock_echo.txt" 2>&1 \
    || die "/clock still not received after initialPeers fallback — see $RUN_DIR/host/clock_echo.txt"
fi

# ---------------------------------------------------------------------------
# Evidence: endpoints, rate, SHM-off; then reverse /cmd_vel
# ---------------------------------------------------------------------------
jexec "ros2 topic info -v /clock"   > "$RUN_DIR/host/clock_info.txt"   2>&1 || true
jexec "ros2 topic info -v /cmd_vel" > "$RUN_DIR/host/cmd_vel_info.txt" 2>&1 || true
jexec "timeout 12 ros2 topic hz /clock" > "$RUN_DIR/host/clock_hz.txt" 2>&1 || true
jexec "ls -la /dev/shm" > "$RUN_DIR/host/jazzy_dev_shm.txt" 2>&1 || true
"${CV_SUDO[@]}" docker exec "$ISAAC_C" ls -la /dev/shm > "$RUN_DIR/host/isaac_dev_shm.txt" 2>&1 || true

log "publishing reverse /cmd_vel (linear.x=$EXPECT_X) from ros:jazzy"
jexec "timeout 90 ros2 topic pub --rate 20 /cmd_vel geometry_msgs/msg/Twist '{linear: {x: $EXPECT_X}}'" \
  > "$RUN_DIR/host/cmd_vel_pub.txt" 2>&1 &
PUB_PID=$!

# The isaac side exits 0 as soon as it observes the expected Twist in-process.
RC=1
DEADLINE=$((SECONDS + CV_HANDSHAKE_WAIT_S + 60))
while ((SECONDS < DEADLINE)); do
  st="$("${CV_SUDO[@]}" docker inspect -f '{{.State.Running}}' "$ISAAC_C" 2>/dev/null || echo false)"
  [[ "$st" == "true" ]] || { RC="$("${CV_SUDO[@]}" docker inspect -f '{{.State.ExitCode}}' "$ISAAC_C")"; break; }
  sleep 5
done
kill "$PUB_PID" >/dev/null 2>&1 || true
"${CV_SUDO[@]}" docker logs "$ISAAC_C" > "$RUN_DIR/host/isaac.log" 2>&1 || true

# ---------------------------------------------------------------------------
# Gate assertions (DoD-P1-05)
# ---------------------------------------------------------------------------
fail=0
[[ "$RC" == "0" ]] || { err "isaac handshake container exit=$RC (want 0)"; fail=1; }
grep -q "clock:" "$RUN_DIR/host/clock_echo.txt" || grep -q "sec" "$RUN_DIR/host/clock_echo.txt" \
  || { err "/clock echo produced no message"; fail=1; }
grep -q "CV_HANDSHAKE_CMDVEL_RX" "$RUN_DIR/host/isaac.log" \
  || { err "reverse /cmd_vel never observed in-process"; fail=1; }
grep -Eq "Subscription count: [1-9]" "$RUN_DIR/host/cmd_vel_info.txt" \
  || { err "no matched /cmd_vel subscription endpoint (QoS/discovery)"; fail=1; }
grep -Eq "Publisher count: [1-9]" "$RUN_DIR/host/clock_info.txt" \
  || { err "no matched /clock publisher endpoint"; fail=1; }
if grep -qiE "fastrtps|fast_datasharing" "$RUN_DIR/host/jazzy_dev_shm.txt" "$RUN_DIR/host/isaac_dev_shm.txt"; then
  err "found Fast DDS SHM segments in /dev/shm — UDPv4-only profile not effective"; fail=1
fi

((fail)) && die "DoD-P1-05 handshake FAILED — evidence: $RUN_DIR/host"
log "DoD-P1-05 handshake PASS — discovery=$DISCOVERY_MODE, /clock>0, /cmd_vel reverse OK, endpoints matched, SHM segs 0"
log "evidence: $RUN_DIR/host"

