#!/usr/bin/env bash
# odom_relay_fidelity.sh — how much of the sim's odom stream actually reached the SUT
# (M2 relay fidelity, G-63; scripts/measure).
#
# WHY: p5c13 measured the runner's /chassis/odom -> /odom relay passing 24.9±0.1 % of
# its input in 21/21 jobs — silently, because a relay that "works" is never asked HOW
# MUCH it passed (G-63 ①). nav2's controller_server consumes the relayed side. p5c14
# widened the step-and-spin callback budget; this script is how the effect is re-measured
# instead of assumed. It is a MEASUREMENT, not a gate: it prints counts and (when asked)
# a ratio, and hardcodes no expected value (CLAUDE §2-4).
#
# ⚠ Scope discipline: this is a FIDELITY measurement, not a determinism one. QA rejected
# the promotion of this defect to "determinism cause" in p5c13 (amcl does not consume the
# topic; the relay was a constant across all measurements). Do not report a ratio change
# as a determinism result.
#
# TWO independent readings — quote BOTH:
#   (a) in-process   : the runner's own line, `[cv-runner] odom relay fidelity: relayed=…
#                      clock_msgs=… ratio_vs_clock=…` (adapter/ros2.py, printed at teardown
#                      on every path). Free, but it is the relay counting itself.
#   (b) recorded bag : per-topic message counts from the job's MCAP — the authoritative
#                      cross-check (this is what p5c13's 151/605 … numbers were).
# The bag reading needs the runner image because `ros2` lives in its apt layer, sourced
# from /opt/ros/<distro>/setup.bash (same glue the recorder itself uses).
#
# USAGE
#   bash odom_relay_fidelity.sh --bag <bag-dir> --image <runner-image> \
#        [--log <runner-container-log>] [--source /chassis/odom --relayed /odom] \
#        [--ros-distro jazzy] [--run-name p5c14/odom]
#   (topic names are ARGUMENTS: they come from the scenario's adapter_config, never from
#    this script — R7. Without them every topic count is still printed.)
#
# Exit: 0 = counts collected · 2 = usage · 3 = the bag could not be read.
set -euo pipefail

export CV_STEP=odom-fidelity
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=scripts/measure/common.sh
source "$SCRIPT_DIR/common.sh"

readonly EXIT_USAGE=2
readonly EXIT_UNREADABLE=3

BAG_DIR=""; IMAGE=""; RUNNER_LOG=""; SOURCE_TOPIC=""; RELAYED_TOPIC=""
ROS_DISTRO_ARG="${CV_ODOM_ROS_DISTRO:-jazzy}"
RUN_NAME="${CV_ODOM_RUN_NAME:-p5c14/odom}"

while [[ $# -gt 0 ]]; do
  case "$1" in
    --bag)        BAG_DIR="${2:?--bag needs a rosbag2 dir}"; shift 2 ;;
    --image)      IMAGE="${2:?--image needs a runner image ref}"; shift 2 ;;
    --log)        RUNNER_LOG="${2:?--log needs a file}"; shift 2 ;;
    --source)     SOURCE_TOPIC="${2:?--source needs a topic}"; shift 2 ;;
    --relayed)    RELAYED_TOPIC="${2:?--relayed needs a topic}"; shift 2 ;;
    --ros-distro) ROS_DISTRO_ARG="${2:?--ros-distro needs a distro}"; shift 2 ;;
    --run-name)   RUN_NAME="${2:?--run-name needs a value}"; shift 2 ;;
    -h|--help)    sed -n '2,32p' "${BASH_SOURCE[0]}" | sed 's/^# \{0,1\}//'; exit 0 ;;
    *)            err "unknown argument: $1"; exit "$EXIT_USAGE" ;;
  esac
done

[[ -n "$BAG_DIR" && -n "$IMAGE" ]] || {
  err "--bag and --image are both required (see --help)"; exit "$EXIT_USAGE"
}
[[ -d "$BAG_DIR" ]] || { err "no such bag dir: $BAG_DIR"; exit "$EXIT_USAGE"; }
require_cmd docker

OUT_DIR="${CV_MEASURE_OUT_ROOT:-$HOME/cv-infra-p2-out}/$RUN_NAME"
mkdir -p "$OUT_DIR"
INFO="$OUT_DIR/$(basename "$BAG_DIR").bag-info.txt"

# (a) the runner's own count, if the container log was preserved.
if [[ -n "$RUNNER_LOG" ]]; then
  if grep -h 'odom relay' "$RUNNER_LOG" | tee "$OUT_DIR/$(basename "$RUNNER_LOG").relay-lines.txt"; then
    :
  else
    err "no 'odom relay' line in $RUNNER_LOG (pre-p5c14 runner image, or no relay was wired)"
  fi
fi

# (b) the authoritative per-topic counts from the recorded bag.
log "reading $BAG_DIR with ros2 bag info (image=$IMAGE, distro=$ROS_DISTRO_ARG)"
if ! docker run --rm --network none --entrypoint bash \
      -v "$BAG_DIR":/cv-bag:ro "$IMAGE" \
      -c "source /opt/ros/${ROS_DISTRO_ARG}/setup.bash && ros2 bag info /cv-bag" \
      > "$INFO" 2>&1; then
  err "ros2 bag info failed — see $INFO"
  exit "$EXIT_UNREADABLE"
fi

log "per-topic message counts (raw info preserved at $INFO):"
awk '/Topic: /{ t=""; c=""; for (i=1;i<=NF;i++){ if($i=="Topic:") t=$(i+1); if($i=="Count:") c=$(i+1) }
                if (t != "") printf "  %-40s %s\n", t, c }' "$INFO"

topic_count() {  # $1 = topic name -> its Count, or empty
  awk -v want="$1" '/Topic: /{ t=""; c=""; for (i=1;i<=NF;i++){ if($i=="Topic:") t=$(i+1); if($i=="Count:") c=$(i+1) }
                               if (t == want) print c }' "$INFO"
}

if [[ -n "$SOURCE_TOPIC" && -n "$RELAYED_TOPIC" ]]; then
  src="$(topic_count "$SOURCE_TOPIC")"; dst="$(topic_count "$RELAYED_TOPIC")"
  if [[ -n "$src" && -n "$dst" && "$src" -gt 0 ]]; then
    awk -v s="$src" -v d="$dst" -v st="$SOURCE_TOPIC" -v dt="$RELAYED_TOPIC" \
      'BEGIN { printf "[cv-infra][odom-fidelity] relay fidelity: %s=%d -> %s=%d  ratio=%.3f\n", st, s, dt, d, d/s }' \
      | tee -a "$OUT_DIR/RATIOS.txt"
  else
    err "could not read both counts ($SOURCE_TOPIC=$src, $RELAYED_TOPIC=$dst) — check the bag topics above"
    exit "$EXIT_UNREADABLE"
  fi
fi

log "evidence: $INFO  (quote this path with any number taken from it — G-24)"
