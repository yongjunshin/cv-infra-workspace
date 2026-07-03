#!/usr/bin/env bash
# run_smoke.sh — DoD-P1-04 host-side wrapper (M2, walking skeleton #1).
#
# Boots headless_smoke.py --mode smoke inside the pinned isaac-sim:5.1.0 BASE image
# (no custom image build) on a dedicated non-host bridge network, livestream off
# (we bypass the runheadless.sh streaming entrypoint with --entrypoint python.sh),
# then asserts:
#   * container exit 0                                        (REQ-EXEC-001)
#   * nvidia-smi shows the container's process occupying GPU  (PID evidence)
#   * >=1 off-screen render-product frame, NON-black          (D-A / R19)
#   * PhysX stepped (falling cube)                            (R5 diagnostics logged)
#
# EULA (decision 2026-07-03-p1-eula-runtime-consent, NEG-2): this wrapper REFUSES to
# start without explicit operator consent (CV_EULA_CONSENT=yes). The acceptance env is
# synthesized from that input at run time — no committed file carries the literal.
#
# Usage (on the workstation):  CV_EULA_CONSENT=yes bash run_smoke.sh
set -euo pipefail

export CV_STEP=isaac-smoke
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=../workstation_setup/common.sh
source "$SCRIPT_DIR/../workstation_setup/common.sh"

require_cmd docker
require_cmd nvidia-smi

# ---------------------------------------------------------------------------
# EULA runtime consent gate (NEG-2; LOCKED #8) — refuse without operator input.
# ---------------------------------------------------------------------------
if [[ "${CV_EULA_CONSENT:-}" != "yes" ]]; then
  err "NVIDIA Isaac Sim EULA consent is REQUIRED before Isaac Sim may boot."
  err "License: https://www.nvidia.com/en-us/agreements/enterprise-software/isaac-sim-additional-software-and-materials-license/"
  err "This gate never auto-accepts (NEG-2); consent is a per-run operator input."
  die  "Re-run with:  CV_EULA_CONSENT=yes $0"
fi
# Synthesize the runtime-only acceptance env from the operator's consent ("yes" -> "Y").
_consent_upper="${CV_EULA_CONSENT^^}"
CV_EULA_DOCKER_ARGS=(-e "ACCEPT_EULA=${_consent_upper:0:1}")
log "EULA consent provided at runtime by operator (CV_EULA_CONSENT=yes) — injecting acceptance env for this run only"

# ---------------------------------------------------------------------------
# Image / paths / output dirs
# ---------------------------------------------------------------------------
IMG="$CV_ISAAC_IMAGE"
[[ -n "$CV_ISAAC_DIGEST" ]] && IMG="${CV_ISAAC_IMAGE%:*}@${CV_ISAAC_DIGEST}"

CV_SMOKE_HOME="${CV_SMOKE_HOME:-$HOME/cv-infra-p1-smoke}"
OUT_ROOT="$CV_SMOKE_HOME/out"
RUN_ID="smoke-$(date +%Y%m%d-%H%M%S)"
RUN_DIR="$OUT_ROOT/$RUN_ID"
mkdir -p "$RUN_DIR/container" "$RUN_DIR/host"

# The 5.1.0 image runs as uid 1234 (user isaac-sim, HOME=/isaac-sim — MEASURED
# 2026-07-03 on etri6000; NOT root//root/.cache as the pre-measurement R2 note
# guessed). Mounted dirs must be writable by uid 1234; chown via the already-pinned
# image itself (no extra image, sudo docker is the whitelisted path).
chown_for_container() {
  "${CV_SUDO[@]}" docker run --rm --user 0 --entrypoint bash \
    -v "$1":/cv-fix "$IMG" -c "chown -R 1234:1234 /cv-fix"
}
log "preparing cache scaffold + output dir ownership for container uid 1234"
# MEASURED trap (2026-07-03, smoke attempt 1): docker creates missing mount-point
# PARENTS as root, so per-subdir mounts under ~/.cache leave /isaac-sim/.cache
# root-owned and the uid-1234 app cannot mkdir siblings (warp cache PermissionError
# -> replicator boot crash). Fix: mount the WHOLE ~/.cache from one host dir.
# mkdir runs inside the root helper container too (scaffold is 1234-owned after the
# first run, so a plain host mkdir would be denied).
"${CV_SUDO[@]}" docker run --rm --user 0 --entrypoint bash \
  -v "$CV_ISAAC_CACHE_ROOT":/cv-fix "$IMG" \
  -c "mkdir -p /cv-fix/cache/home/ov /cv-fix/cache/home/pip /cv-fix/cache/home/warp /cv-fix/cache/home/nvidia/GLCache && chown -R 1234:1234 /cv-fix"
chown_for_container "$RUN_DIR/container"

# Cache mounts follow the MEASURED 5.1.0 layout (uid 1234, HOME=/isaac-sim; R2).
CACHE_MOUNTS=(
  -v "$CV_ISAAC_CACHE_ROOT/cache/kit:/isaac-sim/kit/cache:rw"
  -v "$CV_ISAAC_CACHE_ROOT/cache/home:/isaac-sim/.cache:rw"
  -v "$CV_ISAAC_CACHE_ROOT/cache/computecache:/isaac-sim/.nv/ComputeCache:rw"
  -v "$CV_ISAAC_CACHE_ROOT/logs:/isaac-sim/.nvidia-omniverse/logs:rw"
  -v "$CV_ISAAC_CACHE_ROOT/data:/isaac-sim/.local/share/ov/data:rw"
  -v "$CV_ISAAC_CACHE_ROOT/documents:/isaac-sim/Documents:rw"
)

# Dedicated bridge network (idempotent). Non-host networking from the very first
# smoke (R8): host networking is forbidden project-wide.
if ! "${CV_SUDO[@]}" docker network inspect "$CV_SMOKE_NET" >/dev/null 2>&1; then
  log "creating dedicated bridge network $CV_SMOKE_NET"
  "${CV_SUDO[@]}" docker network create --driver bridge "$CV_SMOKE_NET" >/dev/null
fi

# ---------------------------------------------------------------------------
# Launch (detached so we can poll nvidia-smi for PID evidence during the run)
# ---------------------------------------------------------------------------
CNAME="cv-smoke-isaac"
"${CV_SUDO[@]}" docker rm -f "$CNAME" >/dev/null 2>&1 || true

log "DoD-P1-04 -> booting headless smoke in $IMG (network=$CV_SMOKE_NET, shm-size=$CV_SMOKE_SHM_SIZE, timeout=${CV_SMOKE_TIMEOUT_S}s)"
START_TS=$SECONDS
"${CV_SUDO[@]}" docker run -d --name "$CNAME" \
  --network "$CV_SMOKE_NET" \
  --gpus all \
  -e NVIDIA_DRIVER_CAPABILITIES=all \
  "${CV_EULA_DOCKER_ARGS[@]}" \
  --shm-size "$CV_SMOKE_SHM_SIZE" \
  "${CACHE_MOUNTS[@]}" \
  -v "$SCRIPT_DIR:/cv/smoke:ro" \
  -v "$RUN_DIR/container:/cv/out:rw" \
  --entrypoint /isaac-sim/python.sh \
  "$IMG" /cv/smoke/headless_smoke.py --mode smoke --out /cv/out >/dev/null

# R19 evidence: the effective NVIDIA_DRIVER_CAPABILITIES env of the running container.
"${CV_SUDO[@]}" docker inspect -f '{{range .Config.Env}}{{println .}}{{end}}' "$CNAME" \
  | grep -E '^NVIDIA_' > "$RUN_DIR/host/container_nvidia_env.txt" || true

# ---------------------------------------------------------------------------
# Poll loop: GPU PID evidence + /dev/shm usage, until the container exits.
# ---------------------------------------------------------------------------
GPU_EVIDENCE="$RUN_DIR/host/nvidia_smi_evidence.txt"
SHM_EVIDENCE="$RUN_DIR/host/shm_usage.txt"
PID_MATCHED=""
DEADLINE=$((SECONDS + CV_SMOKE_TIMEOUT_S))
while :; do
  running="$("${CV_SUDO[@]}" docker inspect -f '{{.State.Running}}' "$CNAME" 2>/dev/null || echo false)"
  [[ "$running" == "true" ]] || break
  if ((SECONDS >= DEADLINE)); then
    "${CV_SUDO[@]}" docker logs "$CNAME" > "$RUN_DIR/host/container.log" 2>&1 || true
    "${CV_SUDO[@]}" docker rm -f "$CNAME" >/dev/null 2>&1 || true
    die "smoke timed out after ${CV_SMOKE_TIMEOUT_S}s (wall guard) — log: $RUN_DIR/host/container.log"
  fi
  if [[ -z "$PID_MATCHED" ]]; then
    # Intersect the container's PIDs with nvidia-smi's compute-app PIDs.
    ctr_pids="$("${CV_SUDO[@]}" docker top "$CNAME" -eo pid 2>/dev/null | tail -n +2 || true)"
    smi_out="$(nvidia-smi --query-compute-apps=pid,process_name,used_gpu_memory --format=csv,noheader 2>/dev/null || true)"
    for p in $ctr_pids; do
      if grep -q "^${p}," <<<"$smi_out"; then
        PID_MATCHED="$p"
        {
          echo "# matched container PID on GPU: $PID_MATCHED  ($(date -Is))"
          echo "# docker top $CNAME:"; "${CV_SUDO[@]}" docker top "$CNAME" || true
          echo "# nvidia-smi --query-compute-apps:"; echo "$smi_out"
          echo "# full nvidia-smi:"; nvidia-smi
        } > "$GPU_EVIDENCE"
        log "GPU PID evidence captured (container pid $PID_MATCHED on GPU) -> $GPU_EVIDENCE"
        # Kit /dev/shm usage snapshot while alive ([VERIFY] shm-size record).
        "${CV_SUDO[@]}" docker exec "$CNAME" df -h /dev/shm > "$SHM_EVIDENCE" 2>/dev/null || true
      fi
    done
  fi
  sleep 5
done

RC="$("${CV_SUDO[@]}" docker inspect -f '{{.State.ExitCode}}' "$CNAME")"
WALL_S=$((SECONDS - START_TS))
"${CV_SUDO[@]}" docker logs "$CNAME" > "$RUN_DIR/host/container.log" 2>&1 || true
"${CV_SUDO[@]}" docker rm "$CNAME" >/dev/null 2>&1 || true

# ---------------------------------------------------------------------------
# Gate assertions (DoD-P1-04)
# ---------------------------------------------------------------------------
LOG_FILE="$RUN_DIR/host/container.log"
fail=0
[[ "$RC" == "0" ]] || { err "container exit code = $RC (want 0)"; fail=1; }
grep -q "CV_SMOKE_RENDER_OK" "$LOG_FILE" || { err "missing CV_SMOKE_RENDER_OK (non-black frame assertion)"; fail=1; }
grep -q "CV_SMOKE_PHYSX_OK"  "$LOG_FILE" || { err "missing CV_SMOKE_PHYSX_OK (physics step evidence)"; fail=1; }
compgen -G "$RUN_DIR/container/frame_0001.*" >/dev/null || { err "no frame file produced in $RUN_DIR/container"; fail=1; }
[[ -n "$PID_MATCHED" && -s "$GPU_EVIDENCE" ]] || { err "no nvidia-smi PID evidence captured"; fail=1; }

# R5 diagnostics surface (informational, not gating): PhysX fallback / perf lines.
grep -E "CV_SMOKE_PERF|SimulationApp headless boot took" "$LOG_FILE" || true
grep -iE "fall.?back|tiled.?camera" "$LOG_FILE" > "$RUN_DIR/host/physx_warnings.txt" || true

if ((fail)); then
  die "DoD-P1-04 smoke FAILED — see $RUN_DIR (log: $LOG_FILE)"
fi

date -Is > "$OUT_ROOT/last_smoke_pass"
echo "$RUN_DIR" >> "$OUT_ROOT/last_smoke_pass"
log "DoD-P1-04 smoke PASS — exit 0, GPU pid $PID_MATCHED, non-black frame, wall ${WALL_S}s"
log "evidence: $RUN_DIR (container.log, nvidia_smi_evidence.txt, frame_0001.*, shm_usage.txt)"
