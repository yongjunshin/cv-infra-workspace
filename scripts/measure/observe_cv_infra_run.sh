#!/usr/bin/env bash
# observe_cv_infra_run.sh — DoD-P2-09 per-job observer (M5, scripts/measure).
#
# Repo-committed successor of the workstation-only observe_run.sh. ATTACHES to a
# `cv-infra run` job that the operator launches SEPARATELY (this script does not launch
# it, mount caches, or boot Isaac — the supervisor does that, mounting the D-1 caches
# from CV_ISAAC_CACHE_ROOT). It watches the supervisor's own cvj-*-runner / cvj-*-sut
# containers (supervisor.py: name = f"{cvj-<slug>-<hash>}-runner") and collects, per job:
#   * per-PID VRAM samples -> preserved CSV, steady-state-gated pmon + full table (G-18);
#   * container received bytes (docker stats NetIO) -> the ASSET-DOWNLOAD signal;
#   * runner/sut wall from docker inspect StartedAt/FinishedAt;
#   * runner/sut logs + post-teardown isolation check + result.json count.
#
# NO EULA gate here (NEG-2): this observer boots no Isaac. The `cv-infra run` it watches
# carries the operator's own per-run consent. (warm_cache.sh / isaac_baseline_run.sh —
# which DO boot Isaac — carry the gate.) No ACCEPT_EULA literal is written anywhere.
# sudo (G-15): none — plain docker.
#
# Usage:  (terminal A) CV_ISAAC_CACHE_ROOT=/abs/warm-tree CV_EULA_CONSENT=yes cv-infra run ...
#         (terminal B) bash observe_cv_infra_run.sh <run-name> [cold-fresh|warm-all|cold-assets-warm-shaders]
set -euo pipefail

export CV_STEP=measure-observe
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=scripts/measure/common.sh
source "$SCRIPT_DIR/common.sh"

RUN_NAME="${1:?run name required (usage: bash $0 <run-name> [condition-label])}"
CONDITION="${2:-unlabeled}"   # cold-fresh | warm-all | cold-assets-warm-shaders (for the attribution table)

require_cmd docker
require_cmd nvidia-smi

OUT_ROOT="${CV_MEASURE_OUT_ROOT:-$HOME/cv-infra-p2-out}"
RUN_DIR="$OUT_ROOT/$RUN_NAME"
mkdir -p "$RUN_DIR"

# Wait for the operator-launched runner container to appear.
log "waiting up to ${CV_OBSERVE_WAIT_S}s for a cvj-*-runner container (condition=$CONDITION)"
RUNNER=""
deadline=$((SECONDS + CV_OBSERVE_WAIT_S))
while ((SECONDS < deadline)); do
  RUNNER="$(docker ps --format '{{.Names}}' | grep -E '^cvj-.*-runner$' | head -1 || true)"
  [[ -n "$RUNNER" ]] && break
  sleep 1
done
[[ -n "$RUNNER" ]] || die "no cvj-*-runner container appeared within ${CV_OBSERVE_WAIT_S}s — is 'cv-infra run' running?"
log "observing runner container: $RUNNER"

# Robust wall signal: the supervisor's finally-teardown REMOVES the cvj-* containers
# when the job ends, so a post-hoc `docker inspect` for FinishedAt often returns NA.
# So (a) record the observer's own wall-clock window (runner-first-seen -> all-gone),
# and (b) snapshot the runner's StartedAt now, while it is still alive.
OBS_START_SECONDS=$SECONDS
RUNNER_STARTED_AT="$(docker inspect -f '{{.State.StartedAt}}' "$RUNNER" 2>/dev/null || echo NA)"

{
  echo "# $RUN_NAME mid-run snapshot ($(date -Is)) condition=$CONDITION"
  docker ps --format '{{.Names}}\t{{.Image}}\t{{.Status}}\t{{.Networks}}'
  docker network ls --format '{{.Name}}\t{{.Driver}}' | grep -E 'cvj-' || true
} > "$RUN_DIR/obs.txt" 2>&1

VRAM_CSV="$RUN_DIR/vram_compute_apps_samples.csv"
NETIO_CSV="$RUN_DIR/docker_stats_netio.csv"
echo "ts,pid,process_name,used_gpu_memory" > "$VRAM_CSV"
echo "ts,netio,memusage" > "$NETIO_CSV"

PID_MATCHED=""
STEADY_DONE=""
SUT=""
# Loop while ANY cvj-* container is alive (the whole job), sampling + capturing logs.
while docker ps --format '{{.Names}}' | grep -qE '^cvj-'; do
  [[ -z "$SUT" ]] && SUT="$(docker ps --format '{{.Names}}' | grep -E '^cvj-.*-sut$' | head -1 || true)"

  measure_sample_vram "$VRAM_CSV"
  measure_sample_netio "$NETIO_CSV" "$RUNNER"

  if [[ -z "$PID_MATCHED" ]]; then
    PID_MATCHED="$(measure_container_gpu_pid "$RUNNER" || true)"
    [[ -n "$PID_MATCHED" ]] && log "runner GPU compute PID matched: $PID_MATCHED"
  fi

  # G-18: capture pmon + full table only INSIDE the steady window (not boot transient).
  if [[ -n "$PID_MATCHED" && -z "$STEADY_DONE" ]] \
     && measure_vram_steady "$VRAM_CSV" "$PID_MATCHED"; then
    log "steady-state reached for pid $PID_MATCHED -> capturing preserved pmon + full table"
    measure_capture_steady_evidence "$RUN_DIR" 5
    STEADY_DONE=1
  fi

  # Keep the latest runner/sut logs (docker inspect StartedAt/FinishedAt is read post-teardown).
  docker logs "$RUNNER" > "$RUN_DIR/runner.log" 2>&1 || true
  [[ -n "$SUT" ]] && docker logs "$SUT" > "$RUN_DIR/sut.log" 2>&1 || true

  sleep "$CV_SAMPLE_INTERVAL_S"
done

# Observer wall-clock window = the robust runner-present duration (the attribution uses
# DELTAS across the three conditions, all measured identically, so this proxy is sound).
OBS_WALL_S=$((SECONDS - OBS_START_SECONDS))
# docker inspect walls (may be NA post-teardown — that is why OBS_WALL_S is the primary).
RUNNER_WALL="$(measure_container_wall_s "$RUNNER")"
SUT_WALL="NA"; [[ -n "$SUT" ]] && SUT_WALL="$(measure_container_wall_s "$SUT")"
NETIO_LAST="$(tail -n1 "$NETIO_CSV" 2>/dev/null || echo NA)"
if [[ -z "$STEADY_DONE" ]]; then
  warn "steady window not reached before teardown — VRAM summary is from whatever samples exist"
fi

VRAM_SUMMARY="$(measure_vram_summary "$VRAM_CSV" "$PID_MATCHED")"
{
  echo "==== PER-JOB OBSERVE SUMMARY $RUN_NAME ($(date -Is)) ===="
  echo "condition=$CONDITION runner=$RUNNER sut=${SUT:-none}"
  echo "observer_wall_s=$OBS_WALL_S (primary) runner_started_at=$RUNNER_STARTED_AT"
  echo "runner_wall_s=$RUNNER_WALL sut_wall_s=$SUT_WALL gpu_pid=$PID_MATCHED steady_reached=${STEADY_DONE:-0}"
  echo "per-PID VRAM (derived from raw CSV): $VRAM_SUMMARY"
  echo "container received bytes (docker stats NetIO, last sample): $NETIO_LAST"
  echo "  -> RX = the asset-download signal for cold/warm attribution (see README)."
  echo "post-teardown isolation:"
  docker ps -a --format '{{.Names}}' | grep -E '^cvj-' || echo "  cvj containers: 0"
  docker network ls --format '{{.Name}}' | grep -E '^cvj-' || echo "  cvj networks: 0"
  echo "result.json count under $RUN_DIR: $(find "$RUN_DIR" -name result.json 2>/dev/null | wc -l)"
  echo "raw evidence: vram_compute_apps_samples.csv, docker_stats_netio.csv, pmon_steady.txt, nvidia_smi_full_steady.txt, runner.log, sut.log"
} | tee "$RUN_DIR/SUMMARY.txt"

log "per-job observe DONE — $RUN_NAME (condition=$CONDITION); evidence: $RUN_DIR"
