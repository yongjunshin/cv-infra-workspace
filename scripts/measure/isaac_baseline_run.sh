#!/usr/bin/env bash
# isaac_baseline_run.sh — FU-12: single-instance baseline measurement (M5, scripts/measure).
#
# The repo-committed successor of the workstation-only ~/cv-infra-p2-out/p2_baseline_run.sh.
# Boots the runner image with the P1 asset scripts/isaac_smoke/headless_smoke.py --mode
# smoke (NO new boot script — reuse rule) against the shared cache tree, and measures the
# single-instance baseline: cold/warm boot wall + STEADY-STATE per-PID VRAM.
#
# FU-12 FIX (QA cycle-2 Issue-1 — "pmon/full-table steady-state re-capture not preserved;
# boot-init value only"). The old glue snapped pmon + the full process table ONCE, at the
# instant the PID first appeared on the GPU (= boot transient, ~731 MiB), and only
# NARRATED a steady-state re-capture. This version (G-18):
#   * samples per-PID VRAM CONTINUOUSLY to a preserved CSV;
#   * detects steady-state explicitly (K consecutive samples move < eps — window knobs,
#     NOT a measurement target: CV_STEADY_K/CV_STEADY_EPS in common.sh);
#   * captures the pmon burst + full G+C table INSIDE the steady window and PRESERVES
#     the raw files;
#   * DERIVES peak/steady/median from the raw CSV, never deleting the raw.
#
# EULA (NEG-2): per-run operator consent required (exit 3); no ACCEPT_EULA literal baked.
# sudo (G-15): none — file perms via docker root helper (--user 0).
#
# Usage: CV_EULA_CONSENT=yes bash isaac_baseline_run.sh <run-name> [steps] [cache-root-abs]
set -euo pipefail

export CV_STEP=measure-baseline
# Capture the operator's EXPLICIT cache root BEFORE sourcing (see warm_cache.sh note):
# the sourced SoT default must not silently substitute for the measurement tree.
_OPERATOR_CACHE_ROOT="${CV_ISAAC_CACHE_ROOT:-}"
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=scripts/measure/common.sh
source "$SCRIPT_DIR/common.sh"

measure_eula_gate   # exit 3 without consent, before any work

RUN_NAME="${1:?run name required (usage: CV_EULA_CONSENT=yes $0 <run-name> [steps] [cache-root])}"
STEPS="${2:-60}"
CACHE_ROOT="${3:-$_OPERATOR_CACHE_ROOT}"
[[ -n "$CACHE_ROOT" ]] || die "cache root required (arg 3 or explicit CV_ISAAC_CACHE_ROOT env)"
case "$CACHE_ROOT" in /*) : ;; *) die "cache root must be a HOST ABSOLUTE path: $CACHE_ROOT" ;; esac

require_cmd docker
require_cmd nvidia-smi

IMG="$CV_MEASURE_IMAGE"
SMOKE_DIR="$SCRIPT_DIR/../isaac_smoke"
OUT_ROOT="${CV_MEASURE_OUT_ROOT:-$HOME/cv-infra-p2-out}"
RUN_DIR="$OUT_ROOT/$RUN_NAME"
CNAME="cv-measure-$RUN_NAME"
mkdir -p "$RUN_DIR/host"

# The uid-1234 app writes frames into container-out; chown it via the root helper (G-15).
measure_chown_dir "$RUN_DIR/container" "$IMG"
# Cache tree present + chowned (idempotent; warm_cache.sh normally did this already).
measure_provision_tree "$CACHE_ROOT" "$IMG"

# Dedicated non-host bridge (R8), idempotent.
docker network inspect "$CV_MEASURE_NET" >/dev/null 2>&1 \
  || docker network create --driver bridge "$CV_MEASURE_NET" >/dev/null

CACHE_MOUNTS=(
  -v "$CACHE_ROOT/cache/kit:/isaac-sim/kit/cache:rw"
  -v "$CACHE_ROOT/cache/home:/isaac-sim/.cache:rw"
  -v "$CACHE_ROOT/cache/computecache:/isaac-sim/.nv/ComputeCache:rw"
  -v "$CACHE_ROOT/logs:/isaac-sim/.nvidia-omniverse/logs:rw"
  -v "$CACHE_ROOT/data:/isaac-sim/.local/share/ov/data:rw"
  -v "$CACHE_ROOT/documents:/isaac-sim/Documents:rw"
)

nvidia-smi --query-gpu=memory.used,memory.total --format=csv,noheader > "$RUN_DIR/host/gpu_mem_idle_before.txt"
cache_bytes_before="$(measure_du_bytes "$CACHE_ROOT" "$IMG")"

log "baseline boot: $IMG headless_smoke --mode smoke (steps=$STEPS, net=$CV_MEASURE_NET, cache_before=$cache_bytes_before)"
docker rm -f "$CNAME" >/dev/null 2>&1 || true
docker run -d --name "$CNAME" \
  --network "$CV_MEASURE_NET" \
  --gpus all \
  -e NVIDIA_DRIVER_CAPABILITIES=all \
  "${CV_EULA_DOCKER_ARGS[@]}" \
  --shm-size "$CV_MEASURE_SHM_SIZE" \
  "${CACHE_MOUNTS[@]}" \
  -v "$SMOKE_DIR:/cv/smoke:ro" \
  -v "$RUN_DIR/container:/cv/out:rw" \
  --entrypoint /isaac-sim/python.sh \
  "$IMG" /cv/smoke/headless_smoke.py --mode smoke --out /cv/out --steps "$STEPS" >/dev/null

# ---------------------------------------------------------------------------
# Poll loop — continuous per-PID VRAM sampling; steady-state-gated evidence (G-18).
# ---------------------------------------------------------------------------
VRAM_CSV="$RUN_DIR/host/vram_compute_apps_samples.csv"
NETIO_CSV="$RUN_DIR/host/docker_stats_netio.csv"
echo "ts,pid,process_name,used_gpu_memory" > "$VRAM_CSV"
echo "ts,netio,memusage" > "$NETIO_CSV"

PID_MATCHED=""
STEADY_DONE=""
DEADLINE=$((SECONDS + CV_MEASURE_TIMEOUT_S))
while :; do
  running="$(docker inspect -f '{{.State.Running}}' "$CNAME" 2>/dev/null || echo false)"
  [[ "$running" == "true" ]] || break
  if ((SECONDS >= DEADLINE)); then
    docker logs "$CNAME" > "$RUN_DIR/host/container.log" 2>&1 || true
    docker rm -f "$CNAME" >/dev/null 2>&1 || true
    die "baseline timed out after ${CV_MEASURE_TIMEOUT_S}s (wall guard) — log: $RUN_DIR/host/container.log"
  fi

  measure_sample_vram "$VRAM_CSV"
  measure_sample_netio "$NETIO_CSV" "$CNAME"

  if [[ -z "$PID_MATCHED" ]]; then
    PID_MATCHED="$(measure_container_gpu_pid "$CNAME" || true)"
    [[ -n "$PID_MATCHED" ]] && log "GPU compute PID matched: $PID_MATCHED"
  fi

  # FU-12 fix: only capture the pmon burst + full table ONCE the per-PID VRAM has
  # SETTLED (steady window), not at first detection (which was the boot transient).
  if [[ -n "$PID_MATCHED" && -z "$STEADY_DONE" ]] \
     && measure_vram_steady "$VRAM_CSV" "$PID_MATCHED"; then
    log "steady-state reached for pid $PID_MATCHED -> capturing preserved pmon + full table"
    measure_capture_steady_evidence "$RUN_DIR/host" 5
    STEADY_DONE=1
  fi

  sleep "$CV_SAMPLE_INTERVAL_S"
done

RC="$(docker inspect -f '{{.State.ExitCode}}' "$CNAME")"
WALL_S="$(measure_container_wall_s "$CNAME")"
docker logs "$CNAME" > "$RUN_DIR/host/container.log" 2>&1 || true
docker rm "$CNAME" >/dev/null 2>&1 || true
cache_bytes_after="$(measure_du_bytes "$CACHE_ROOT" "$IMG")"

# Fallback: if the run was too short/fast to ever register a steady window, still
# preserve a final table so the evidence set is complete (marked as such).
if [[ -z "$STEADY_DONE" ]]; then
  warn "steady window not reached before exit — preserving a final (possibly transient) table"
  measure_capture_steady_evidence "$RUN_DIR/host" 3
fi

# ---------------------------------------------------------------------------
# Derived summary (from PRESERVED raw — never edits it) + gate assertions.
# ---------------------------------------------------------------------------
LOG_FILE="$RUN_DIR/host/container.log"
VRAM_SUMMARY="$(measure_vram_summary "$VRAM_CSV" "$PID_MATCHED")"
{
  echo "==== BASELINE SUMMARY $RUN_NAME ($(date -Is)) ===="
  echo "image=$IMG steps=$STEPS exit_code=$RC total_wall_s=$WALL_S gpu_pid=$PID_MATCHED steady_reached=${STEADY_DONE:-0}"
  echo "per-PID VRAM (derived from raw CSV): $VRAM_SUMMARY"
  echo "cache_bytes: before=$cache_bytes_before after=$cache_bytes_after"
  echo "boot/perf markers:"
  grep -E "SimulationApp headless boot took|CV_SMOKE_PERF|CV_SMOKE_RENDER_OK|CV_SMOKE_PHYSX_OK|CV_SMOKE_PASS" "$LOG_FILE" || true
  echo "raw evidence: vram_compute_apps_samples.csv, docker_stats_netio.csv, pmon_steady.txt, nvidia_smi_full_steady.txt, container.log"
} | tee "$RUN_DIR/host/SUMMARY.txt"

fail=0
[[ "$RC" == "0" ]] || { err "container exit code = $RC (want 0)"; fail=1; }
grep -q "CV_SMOKE_PASS" "$LOG_FILE" || { err "missing CV_SMOKE_PASS marker"; fail=1; }
[[ -n "$PID_MATCHED" ]] || { err "no GPU compute PID was matched (no VRAM evidence)"; fail=1; }
[[ -s "$RUN_DIR/host/pmon_steady.txt" ]] || { err "no preserved pmon evidence"; fail=1; }
((fail == 0)) || die "FU-12 baseline FAILED — see $RUN_DIR (log: $LOG_FILE)"

log "FU-12 baseline PASS — exit 0, gpu pid $PID_MATCHED, wall ${WALL_S}s; evidence: $RUN_DIR/host"
