#!/usr/bin/env bash
# exit_contract_probe.sh — the exit contract measured at the CONTAINER boundary
# (DoD-P2-07 re-closure condition ③; M2, scripts/measure).
#
# WHY (G-62, measured p5c13): the runner decided 0/1/2/3 correctly, but
# `SimulationApp.close()` does NOT return — it ends the process with status 0 —
# so every code decided AFTER a successful boot was erased before `docker wait`
# could see it (live: die codes 16/16 = 0, verdict=fail and timeout included).
# Only the pre-boot 2/3 survived, which is exactly why the contract LOOKED whole.
# The p5c14 repair decides the code, flushes, and delivers it with `os._exit`
# (cv_infra/runner/main.py::hard_exit), and never calls the vendor close() on the
# terminal path. That repair is only PROVEN here — a CPU test cannot see a
# container's status, and a wrapper-level 0/1/2/3 pass does not cover the
# post-boot path (G-62 ①).
#
# What this script does NOT do: it never fabricates operator consent. The arm
# that boots Isaac goes through the harness EULA gate (`CV_EULA_CONSENT=yes`,
# NEG-2 / LOCKED §8) exactly like warm_cache.sh / isaac_baseline_run.sh; the
# no-consent arm is run WITHOUT it on purpose (that is its measurement).
#
# ---------------------------------------------------------------------------
# MODES
#   arms    (default)  run the self-contained arms and compare to the contract:
#                        usage2    -> 2   malformed JOB_SPEC (pre-boot)
#                        preboot3  -> 3   no operator consent (pre-boot guard)
#                        postboot3 -> 3   boot OK, then a runner exception in
#                                         load_scene (THE arm p5c13 measured as 0)
#                        closeprobe-> ?   diagnostic, never a pass/fail: does
#                                         close() raise a catchable SystemExit
#                                         (42) / return (43) / kill us (0)?
#   events  [SINCE]    harvest the die codes of the supervisor's cvj-*-runner
#                      containers from `docker events` — the ONLY way to read the
#                      code of a container the supervisor already removed. This is
#                      how the remaining two values are collected: run a real
#                      passing job (-> 0) and a real failing job (-> 1, e.g. a
#                      scenario declaring scenario.debug_obstacle so the collision
#                      oracle fails) through the control plane, then harvest.
#
# USAGE
#   bash exit_contract_probe.sh arms --image cv-infra-runner:p5c15 \
#        [--run-name p5c14/exit] [--arm-timeout-s 1800]
#   CV_EULA_CONSENT=yes bash exit_contract_probe.sh arms --image <ref>   # incl. boot arms
#   bash exit_contract_probe.sh events '30m'
#
# Evidence (G-24): every arm writes container log + observed code under
#   ${CV_MEASURE_OUT_ROOT:-$HOME/cv-infra-p2-out}/<run-name>/ , plus SUMMARY.tsv.
# Quote the paths in the report; a code without its log is an unverified claim.
#
# Exit: 0 = every contract arm matched · 2 = usage · 3 = a mismatch, or an arm
# could not be run (fail-closed — the same class as the consent/skew gates).
set -euo pipefail

export CV_STEP=exit-probe
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=scripts/measure/common.sh
source "$SCRIPT_DIR/common.sh"

readonly EXIT_USAGE=2
readonly EXIT_MISMATCH=3

MODE=arms
if [[ $# -gt 0 && "$1" != -* ]]; then
  MODE="$1"; shift
fi

CV_EXIT_IMAGE="${CV_EXIT_IMAGE:-}"
RUN_NAME="${CV_EXIT_RUN_NAME:-p5c14/exit}"
ARM_TIMEOUT_S="${CV_EXIT_ARM_TIMEOUT_S:-1800}"
EVENTS_SINCE=""

while [[ $# -gt 0 ]]; do
  case "$1" in
    --image)         CV_EXIT_IMAGE="${2:?--image needs a runner image ref}"; shift 2 ;;
    --run-name)      RUN_NAME="${2:?--run-name needs a value}"; shift 2 ;;
    --arm-timeout-s) ARM_TIMEOUT_S="${2:?--arm-timeout-s needs seconds}"; shift 2 ;;
    -h|--help)       sed -n '2,60p' "${BASH_SOURCE[0]}" | sed 's/^# \{0,1\}//'; exit 0 ;;
    *)               [[ -z "$EVENTS_SINCE" ]] && { EVENTS_SINCE="$1"; shift; continue; }
                     err "unknown argument: $1"; exit "$EXIT_USAGE" ;;
  esac
done

require_cmd docker
OUT_DIR="${CV_MEASURE_OUT_ROOT:-$HOME/cv-infra-p2-out}/$RUN_NAME"
mkdir -p "$OUT_DIR"

# The JOB_SPEC both boot arms use. `scenario.scene` is a deliberately UNKNOWN name:
# resolve_scene() raises inside load_scene(), i.e. AFTER SimulationApp came up — the
# cheapest honest post-boot runner exception, and it needs no SUT, no network and no
# scene download. (A probe INPUT, not a product hardcode: nothing here is a real
# asset path.) Validity is guarded on CPU by tests/test_runner_exit_contract.py, so a
# typo cannot silently turn the postboot3 arm into a usage-2 arm.
# --- BEGIN PROBE_JOB_SPEC ---
read -r -d '' PROBE_JOB_SPEC <<'JSON' || true
{
  "job_id": "exit-contract-probe",
  "scenario": {
    "scene": "cv_exit_probe_no_such_scene",
    "robot": "nova_carter",
    "goal": {"x": 0.0, "y": 0.0, "yaw": 0.0},
    "seed": 42,
    "timeout_s": 30
  },
  "sut_image_ref": "none:none",
  "interface": {"type": "ros2", "adapter_config": {}},
  "acceptance_criteria": [
    {"oracle": "reached_goal", "params": {"position_tolerance_m": 0.75}},
    {"oracle": "no_collision", "params": {"chassis_path": "/World/Nova_Carter_ROS/chassis_link"}}
  ]
}
JSON
# --- END PROBE_JOB_SPEC ---

SUMMARY="$OUT_DIR/SUMMARY.tsv"

record() {  # arm  expected  observed  verdict
  printf '%s\t%s\t%s\t%s\n' "$1" "$2" "$3" "$4" >> "$SUMMARY"
  log "arm=$1 expected=$2 observed=$3 -> $4"
}

# Run one arm through the image's REAL ENTRYPOINT (`./python.sh -m
# cv_infra.runner.main`) and return its `docker wait` status. Detached + wait so a
# hung arm is bounded and the log is captured either way.
run_arm() {  # $1 = arm name, rest = docker run args (after --name)
  local arm="$1"; shift
  local cname="cv-exit-probe-${arm}-$$" code
  docker rm -f "$cname" >/dev/null 2>&1 || true
  if ! docker run -d --name "$cname" "$@" >/dev/null; then
    err "arm $arm: docker run failed to start"
    printf 'START_FAILED\n' > "$OUT_DIR/$arm.code"
    echo "START_FAILED"
    return 0
  fi
  if ! code="$(timeout "$ARM_TIMEOUT_S" docker wait "$cname" 2>/dev/null)"; then
    err "arm $arm: exceeded ${ARM_TIMEOUT_S}s — killing (recorded as TIMEOUT)"
    docker kill "$cname" >/dev/null 2>&1 || true
    code="TIMEOUT"
  fi
  docker logs "$cname" > "$OUT_DIR/$arm.log" 2>&1 || true
  printf '%s\n' "$code" > "$OUT_DIR/$arm.code"
  docker rm -f "$cname" >/dev/null 2>&1 || true
  echo "$code"
}

probe_arms() {
  [[ -n "$CV_EXIT_IMAGE" ]] || {
    err "--image / CV_EXIT_IMAGE is REQUIRED — this probe measures ONE image's boundary."
    err "    Pass the rebuilt runner image the live leg will spawn, e.g. cv-infra-runner:p5c15."
    exit "$EXIT_USAGE"
  }
  local result_dir="$OUT_DIR/result-out" failures=0 code
  mkdir -p "$result_dir"
  measure_chown_dir "$result_dir" "$CV_EXIT_IMAGE"   # uid-1234 app writes result.json (G-15)

  : > "$SUMMARY"
  printf '# arm\texpected\tobserved\tverdict\n' >> "$SUMMARY"
  log "image=$CV_EXIT_IMAGE  evidence=$OUT_DIR"

  # --- arm 1: malformed JOB_SPEC -> 2 (pre-boot; survived p5c13, must not regress).
  code="$(run_arm usage2 --network none \
    -e JOB_SPEC='{not json' -e RESULT_OUT=/cv-out -v "$result_dir":/cv-out "$CV_EXIT_IMAGE")"
  [[ "$code" == "2" ]] || failures=$((failures + 1))
  record usage2 2 "$code" "$([[ "$code" == "2" ]] && echo OK || echo MISMATCH)"

  # --- arm 2: valid spec, NO operator consent -> 3 (pre-boot EULA guard, NEG-2).
  code="$(run_arm preboot3 --network none \
    -e JOB_SPEC="$PROBE_JOB_SPEC" -e RESULT_OUT=/cv-out -v "$result_dir":/cv-out "$CV_EXIT_IMAGE")"
  [[ "$code" == "3" ]] || failures=$((failures + 1))
  record preboot3 3 "$code" "$([[ "$code" == "3" ]] && echo OK || echo MISMATCH)"

  # --- arm 3: boot OK, then a runner exception -> 3. THE regression arm (p5c13: 0).
  measure_eula_gate   # per-run operator consent; refuses (exit 3) without it
  # Cache mounts are OPTIONAL here (unlike the VRAM/wall harness, this probe measures
  # a status, not a duration): mounted when CV_ISAAC_CACHE_ROOT is set, so a warm tree
  # shortens the boot; absent, the arm just boots cold. D-1 table, verbatim.
  local cache_mounts=()
  if [[ -n "${CV_ISAAC_CACHE_ROOT:-}" ]]; then
    measure_provision_tree "$CV_ISAAC_CACHE_ROOT" "$CV_EXIT_IMAGE"
    cache_mounts=(
      -v "$CV_ISAAC_CACHE_ROOT/cache/kit:/isaac-sim/kit/cache:rw"
      -v "$CV_ISAAC_CACHE_ROOT/cache/home:/isaac-sim/.cache:rw"
      -v "$CV_ISAAC_CACHE_ROOT/cache/computecache:/isaac-sim/.nv/ComputeCache:rw"
      -v "$CV_ISAAC_CACHE_ROOT/logs:/isaac-sim/.nvidia-omniverse/logs:rw"
      -v "$CV_ISAAC_CACHE_ROOT/data:/isaac-sim/.local/share/ov/data:rw"
      -v "$CV_ISAAC_CACHE_ROOT/documents:/isaac-sim/Documents:rw"
    )
  fi
  code="$(run_arm postboot3 --gpus all -e NVIDIA_DRIVER_CAPABILITIES=all \
    --network none --shm-size "$CV_MEASURE_SHM_SIZE" \
    "${CV_EULA_DOCKER_ARGS[@]}" "${cache_mounts[@]+"${cache_mounts[@]}"}" \
    -e JOB_SPEC="$PROBE_JOB_SPEC" -e RESULT_OUT=/cv-out -v "$result_dir":/cv-out "$CV_EXIT_IMAGE")"
  [[ "$code" == "3" ]] || failures=$((failures + 1))
  record postboot3 3 "$code" "$([[ "$code" == "3" ]] && echo OK || echo MISMATCH)"
  # Two cross-checks on the SAME arm (a code alone does not prove the repair):
  #   (a) the runner's LAST line survived os._exit -> the log is not truncated;
  #   (b) the degraded result.json was still written (verdict=error, REQ-EXEC-013).
  if grep -q 'cache_summary' "$OUT_DIR/postboot3.log" 2>/dev/null; then
    log "postboot3: final diagnostic line present (hard_exit did not truncate the log)"
  else
    err "postboot3: no cache_summary line — stdout may be truncated by the hard exit"
    failures=$((failures + 1))
  fi
  if grep -q '"verdict": "error"' "$result_dir/result.json" 2>/dev/null; then
    log "postboot3: result.json verdict=error (code 3 and the artifact agree)"
  else
    err "postboot3: result.json missing or not verdict=error under $result_dir"
    failures=$((failures + 1))
  fi

  # --- arm 4 (diagnostic, never a pass/fail): is close() catchable? If it raises a
  # SystemExit we can restore graceful Kit shutdown IN FRONT of hard_exit at zero cost
  # to the contract; if it kills us, the p5c14 choice (skip close) is the only one.
  cat > "$OUT_DIR/close_probe.py" <<'PY'
"""Does SimulationApp.close() raise (catchable) or terminate the process?"""
import os

from isaacsim import SimulationApp

app = SimulationApp({"headless": True})
try:
    app.close()
except BaseException as exc:  # noqa: BLE001 - the question IS whether anything is catchable
    print(f"[probe] close() RAISED {type(exc).__name__} code={getattr(exc, 'code', None)}", flush=True)
    os._exit(42)
print("[probe] close() RETURNED", flush=True)
os._exit(43)
PY
  code="$(run_arm closeprobe --gpus all -e NVIDIA_DRIVER_CAPABILITIES=all \
    --network none --shm-size "$CV_MEASURE_SHM_SIZE" \
    "${CV_EULA_DOCKER_ARGS[@]}" "${cache_mounts[@]+"${cache_mounts[@]}"}" \
    --entrypoint /isaac-sim/python.sh \
    -v "$OUT_DIR/close_probe.py":/cv-probe/close_probe.py:ro "$CV_EXIT_IMAGE" /cv-probe/close_probe.py)"
  case "$code" in
    42) record closeprobe "?" "$code" "INFO: close() raised a CATCHABLE exception" ;;
    43) record closeprobe "?" "$code" "INFO: close() RETURNED (vendor behaviour changed)" ;;
    0)  record closeprobe "?" "$code" "INFO: close() killed the process with 0 (G-62 confirmed)" ;;
    *)  record closeprobe "?" "$code" "INFO: unexpected — read closeprobe.log" ;;
  esac

  log "SUMMARY -> $SUMMARY"
  cat "$SUMMARY"
  if ((failures > 0)); then
    err "$failures contract arm(s) did not match — the exit contract is NOT re-closed."
    exit "$EXIT_MISMATCH"
  fi
  log "every self-contained arm matched. Codes 0 and 1 come from real jobs: run one"
  log "passing and one failing job through the control plane, then: $0 events '30m'"
}

# Harvest runner-container die codes from the docker event stream. Works for
# containers the supervisor already removed (p5c13 used exactly this), which is why
# `docker inspect` is NOT used here.
probe_events() {
  local since="${EVENTS_SINCE:-30m}" out="$OUT_DIR/events-die-codes.txt"
  log "harvesting cvj-*-runner die codes since $since"
  # Default (unformatted) output on purpose: it carries the timestamp, the container
  # name and `exitCode=` already, and it cannot break on a Go-template field rename
  # across docker versions (measured: `{{.time}}` is rejected by docker 29.x).
  docker events --since "$since" --until "0s" \
    --filter type=container --filter event=die \
    | grep -E 'cvj-.*-runner' | tee "$out" || true
  if [[ ! -s "$out" ]]; then
    err "no cvj-*-runner die events in the window — widen --since, or the jobs are still running."
    exit "$EXIT_MISMATCH"
  fi
  log "die codes -> $out (pair each with its job's result.json verdict:"
  log " verdict=pass must be 0, verdict=fail/timeout must be 1 — G-62's 'the 1 that means fail')"
}

case "$MODE" in
  arms)   probe_arms ;;
  events) probe_events ;;
  *)      err "unknown mode '$MODE' (arms | events)"; exit "$EXIT_USAGE" ;;
esac
