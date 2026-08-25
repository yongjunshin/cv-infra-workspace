#!/usr/bin/env bash
# p6c1 spike — VRAM sampler. THROWAWAY (experiments/**).
#
# RECIPE = profiles/rtx_4080.yaml's adoption block + docs/deploy/gpu-profiles.md §4,
# unchanged: method (B) per-PID `nvidia-smi --query-compute-apps` at 0.5 s, cross-checked
# against the full (G+C) process table, with the GPU-wide `memory.used` sampled on the
# SAME tick so method (A) (GPU-wide peak minus idle) is derivable from the same file.
# The adoption rule (larger of A and B) is applied in the REPORT, not here — this script
# only preserves raw samples (G-18).
#
#   usage: vram_sampler.sh <out.csv> <cross-check-dir> [interval_s]
#   stop:  kill the PID it prints (SIGTERM); the CSV is complete at every line.
#
# CSV columns: ts_epoch,kind,pid,process_name,used_mib
#   kind=gpu -> GPU-wide memory.used (pid/process_name = __gpu__)
#   kind=app -> one row per compute app in the table at that tick
set -euo pipefail

CSV="${1:?usage: vram_sampler.sh <out.csv> <cross-check-dir> [interval_s]}"
XDIR="${2:?}"
INTERVAL="${3:-0.5}"
: "${CV_VRAM_CROSSCHECK_EVERY:=120}"   # ticks between full-table / pmon captures

mkdir -p "$(dirname "$CSV")" "$XDIR"
printf 'ts_epoch,kind,pid,process_name,used_mib\n' > "$CSV"

tick=0
while :; do
  ts="$(date +%s.%N)"
  gpu="$(nvidia-smi --query-gpu=memory.used --format=csv,noheader,nounits 2>/dev/null || echo NA)"
  printf '%s,gpu,,__gpu__,%s\n' "$ts" "$gpu" >> "$CSV"
  while IFS= read -r line; do
    [[ -z "$line" ]] && continue
    pid="$(cut -d, -f1 <<<"$line" | tr -d ' ')"
    name="$(cut -d, -f2 <<<"$line" | tr -d ' ')"
    mem="$(cut -d, -f3 <<<"$line" | tr -d ' ')"
    printf '%s,app,%s,%s,%s\n' "$ts" "$pid" "$name" "$mem" >> "$CSV"
  done < <(nvidia-smi --query-compute-apps=pid,process_name,used_gpu_memory \
             --format=csv,noheader,nounits 2>/dev/null || true)

  tick=$((tick + 1))
  if (( tick % CV_VRAM_CROSSCHECK_EVERY == 0 )); then
    stamp="$(date +%Y%m%dT%H%M%SZ -u)"
    {
      echo "# full nvidia-smi process table (G+C) at $stamp"
      nvidia-smi
    } > "$XDIR/nvidia_smi_full_$stamp.txt" 2>&1 || true
    nvidia-smi pmon -s m -c 3 -d 1 > "$XDIR/pmon_$stamp.txt" 2>&1 || true
  fi
  sleep "$INTERVAL"
done
