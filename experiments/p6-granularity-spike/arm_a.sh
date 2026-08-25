#!/usr/bin/env bash
# p6c1 Arm A (C-1, n job) — the 8 concrete scenarios as 8 SEPARATE jobs, SERIAL.
#
# ZERO new platform code by construction: every job goes through the existing
# `cv-infra run` entrypoint (admit -> JOB_SPEC -> supervisor -> runner+SUT containers ->
# result.json -> exit code). This script only drives it and times it.
#
#   usage: ACCEPT_EULA=... PRIVACY_CONSENT=... arm_a.sh [evidence_dir]
set -euo pipefail

SPIKE_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=common.sh
source "$SPIKE_DIR/common.sh"

EVID="${1:-$CV_EVIDENCE_DIR}/arm_a"
mkdir -p "$EVID/logs" "$EVID/jobs" "$EVID/cache"
CSV="$EVID/arm_a_jobs.csv"

require_consent
require_exclusive_gpu

log "Arm A: runner_image=$CV_RUNNER_IMAGE cli=$CV_CLI evidence=$EVID"
{
  echo "# Arm A (C-1) — one job per sample, serial (k=1)"
  echo "# runner_image=$CV_RUNNER_IMAGE"
  echo "# warm cache base (read-only copy source)=$CV_WARM_CACHE_BASE"
  echo "# started=$(date -Is)"
  echo "index,job_id,scenario,cache_seed_s,cli_wall_s,t0_epoch,t1_epoch,exit_code,result_json"
} > "$CSV"

for path in "$SPIKE_DIR"/scenarios/sample_*.yaml; do
  name="$(basename "$path" .yaml)"          # sample_0N
  index="${name##*_}"
  job_id="p6a-$index"
  cache_root="$EVID/cache/$job_id"

  log "--- Arm A job $index ($job_id) ---"
  t_seed0="$(now_s)"
  seed_cache "$cache_root"
  t_seed1="$(now_s)"

  t0="$(now_s)"
  set +e
  env ACCEPT_EULA="$ACCEPT_EULA" PRIVACY_CONSENT="$PRIVACY_CONSENT" \
      CV_ISAAC_CACHE_ROOT="$cache_root" \
      "$CV_CLI" run "$path" \
        --runner-image "$CV_RUNNER_IMAGE" \
        --out-dir "$EVID/jobs" \
        --job-id "$job_id" \
      > "$EVID/logs/${job_id}.log" 2>&1
  rc=$?
  set -e
  t1="$(now_s)"

  result="$EVID/jobs/$(ls "$EVID/jobs" | grep -m1 "$job_id" || true)/result/result.json"
  [[ -f "$result" ]] || result="MISSING"
  printf '%s,%s,%s,%s,%s,%s,%s,%s,%s\n' \
    "$index" "$job_id" "$name" \
    "$(elapsed "$t_seed0" "$t_seed1")" "$(elapsed "$t0" "$t1")" \
    "$t0" "$t1" "$rc" "$result" >> "$CSV"
  log "job $index done: exit=$rc wall=$(elapsed "$t0" "$t1")s result=$result"

  discard_tree "$cache_root"   # stateless, like the supervisor's per-job scratch
done

echo "# finished=$(date -Is)" >> "$CSV"
log "Arm A complete — $CSV"
