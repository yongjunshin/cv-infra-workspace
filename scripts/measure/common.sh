#!/usr/bin/env bash
# shellcheck shell=bash
# shellcheck disable=SC2034  # pins/args are consumed by the scripts that source this lib
# common.sh — shared helpers + pins for the M5 measurement harness (scripts/measure).
#
# Sourced by: warm_cache.sh, isaac_baseline_run.sh, observe_cv_infra_run.sh.
# Not meant to be executed on its own.
#
# Reproducibility (CLAUDE.md §2-7): to avoid REDEFINING what the provisioning SoT
# already owns, this lib SOURCES scripts/workstation_setup/common.sh and reuses its
# log/err/die/require_cmd helpers + image/cache pins. It then adds ONLY the values the
# measurement harness needs on top. It deliberately does NOT use CV_SUDO from there —
# T0 (fu16-probe) measured that plain `docker` is sufficient (etri is in the docker
# group); this whole harness runs sudo-free (G-15 file-perm work goes through a docker
# root helper, --user 0, not host sudo).
#
# NFR discipline (CLAUDE.md §2-4): this harness MEASURES and RECORDS. No measured NFR
# value (VRAM MiB, throughput, closure size, cold/warm penalty) is hardcoded here — the
# only literals below are JUDGEMENT/window knobs, flagged as "not a measurement target".

# Idempotent source guard (readonly pins must not be re-declared on re-source).
[[ -z "${_CV_MEASURE_COMMON_LOADED:-}" ]] || return 0
_CV_MEASURE_COMMON_LOADED=1

_CV_MEASURE_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=../workstation_setup/common.sh
source "$_CV_MEASURE_DIR/../workstation_setup/common.sh"

# ---------------------------------------------------------------------------
# MEASURE-SPECIFIC PINS (only what workstation_setup/common.sh does NOT define)
# ---------------------------------------------------------------------------

# The harness boots the RUNNER image (cv_infra wheel + Isaac), NOT the bare isaac-sim
# base (CV_ISAAC_IMAGE from the sourced SoT). Wave 2 overrides with the freshly-built
# runner image (image 5호) via CV_MEASURE_IMAGE. Default = the last known-good runner
# tag verified end-to-end in T0 (fu16-probe); env-overridable, never floating.
readonly CV_MEASURE_IMAGE="${CV_MEASURE_IMAGE:-cv-infra-runner:p2c5}"

# Scene ref used to WARM the asset closure. Joined to get_assets_root_path() at run
# time by warm_scene.py — a measurement INPUT, never a product hardcode (R7). It MUST
# match the scene of the scenario under P2-09 test so the correct closure is warmed.
# Default = the P2 canonical carter warehouse nav scene (T0 asset root).
readonly CV_MEASURE_SCENE_REL="${CV_MEASURE_SCENE_REL:-/Isaac/Samples/ROS2/Scenario/carter_warehouse_navigation.usd}"

# Dedicated non-host bridge network for the single-instance baseline / warm boot (R8;
# host networking is forbidden project-wide). Per-job observe watches the supervisor's
# own cvj-* network instead (it does not create one).
readonly CV_MEASURE_NET="${CV_MEASURE_NET:-cv-measure-net}"

# /dev/shm for Kit (docker default 64m is too small for Kit; separate from the DDS SHM
# transport, which stays disabled via the UDPv4 profile).
readonly CV_MEASURE_SHM_SIZE="${CV_MEASURE_SHM_SIZE:-1g}"

# Wall guard for a single warm/baseline boot (cold shader compile + closure download).
readonly CV_MEASURE_TIMEOUT_S="${CV_MEASURE_TIMEOUT_S:-2400}"

# How long observe_cv_infra_run.sh waits for the operator-launched cvj-*-runner to appear.
readonly CV_OBSERVE_WAIT_S="${CV_OBSERVE_WAIT_S:-900}"

# Steady-state DETECTION knobs (G-18). These are the JUDGEMENT WINDOW that decides WHEN
# the (measured) steady VRAM is sampled — NOT a measurement target (CLAUDE §2-4).
#   CV_STEADY_K   = # of consecutive samples that must each move < CV_STEADY_EPS.
#   CV_STEADY_EPS = per-step relative-change tolerance for "settled".
readonly CV_STEADY_K="${CV_STEADY_K:-4}"
readonly CV_STEADY_EPS="${CV_STEADY_EPS:-0.02}"
readonly CV_SAMPLE_INTERVAL_S="${CV_SAMPLE_INTERVAL_S:-2}"

# ---------------------------------------------------------------------------
# EULA RUNTIME CONSENT GATE (NEG-2; LOCKED §8)
# ---------------------------------------------------------------------------
# Every script that BOOTS Isaac calls this FIRST. Refuses without per-run operator
# input (exit 3); synthesizes the acceptance env from that input at run time. No
# committed file carries the acceptance literal (run_smoke.sh / headless_smoke.py idiom).
# Sets CV_EULA_DOCKER_ARGS for the caller's `docker run`.
measure_eula_gate() {
  if [[ "${CV_EULA_CONSENT:-}" != "yes" ]]; then
    err "NVIDIA Isaac Sim EULA consent is REQUIRED before Isaac Sim may boot (NEG-2)."
    err "License: https://www.nvidia.com/en-us/agreements/enterprise-software/isaac-sim-additional-software-and-materials-license/"
    err "This gate never auto-accepts; consent is a per-run operator input."
    err "Re-run with:  CV_EULA_CONSENT=yes <script> ..."
    exit 3
  fi
  # Runtime-only synthesis ("yes" -> first char "Y"); the literal is never committed.
  local _c="${CV_EULA_CONSENT^^}"
  CV_EULA_DOCKER_ARGS=(-e "ACCEPT_EULA=${_c:0:1}" -e "PRIVACY_CONSENT=${_c:0:1}")
}

# ---------------------------------------------------------------------------
# CACHE-TREE PROVISIONING (G-15 — docker root helper, no host sudo)
# ---------------------------------------------------------------------------

# Canonical D-1 mount subpaths (host side). The in-container targets live with the
# `docker run` volume flags in each caller (verbatim D-1 table). Kept here so tree
# creation and the run-time mount list cannot drift apart.
CV_MEASURE_CACHE_SUBPATHS=(cache/kit cache/home cache/computecache logs data documents)

# Create the 6-way cache subtree under $1 and chown it to uid 1234 (isaac-sim), via the
# runner image itself as a root helper (--user 0). Idempotent. G-15: docker would
# otherwise create missing mount PARENTS as root, and the uid-1234 app cannot mkdir
# siblings. The supervisor deliberately does NOT do this (D-1: it raises a loud
# ValueError on a missing root) — provisioning is M5's job, right here.
measure_provision_tree() {
  local root="$1" img="${2:-$CV_MEASURE_IMAGE}"
  log "provisioning cache tree + chown 1234:1234 under $root (G-15 root helper)"
  docker run --rm --user 0 --network none --entrypoint bash \
    -v "$root":/cv-fix "$img" -c '
      set -e
      mkdir -p /cv-fix/cache/kit /cv-fix/cache/home /cv-fix/cache/computecache \
               /cv-fix/logs /cv-fix/data /cv-fix/documents
      chown -R 1234:1234 /cv-fix'
}

# chown a single host dir to uid 1234 via the root helper (for container-out dirs the
# uid-1234 app writes into). Idempotent.
measure_chown_dir() {
  local dir="$1" img="${2:-$CV_MEASURE_IMAGE}"
  docker run --rm --user 0 --network none --entrypoint bash \
    -v "$dir":/cv-fix "$img" -c 'mkdir -p /cv-fix && chown -R 1234:1234 /cv-fix'
}

# `du -sb` via the root helper — the cache dirs are uid-1234 0700, so a host-side `du`
# under-counts (permission denied). Prints the byte count on stdout.
measure_du_bytes() {
  local path="$1" img="${2:-$CV_MEASURE_IMAGE}"
  docker run --rm --user 0 --network none --entrypoint bash \
    -v "$path":/cv-fix "$img" -c 'du -sb /cv-fix | cut -f1' 2>/dev/null || echo "NA"
}

# ---------------------------------------------------------------------------
# SAMPLING HELPERS (raw-preserving; summaries are DERIVED, never overwrite raw — G-18)
# ---------------------------------------------------------------------------

# Print the container's PID that nvidia-smi reports as a GPU compute app (intersect
# `docker top` PIDs with the compute-apps table). Empty + return 1 if none yet.
measure_container_gpu_pid() {
  local cname="$1" smi_out ctr_pids p
  smi_out="$(nvidia-smi --query-compute-apps=pid,process_name,used_gpu_memory --format=csv,noheader 2>/dev/null || true)"
  ctr_pids="$(docker top "$cname" -eo pid 2>/dev/null | tail -n +2 || true)"
  for p in $ctr_pids; do
    if grep -q "^${p}," <<<"$smi_out"; then
      printf '%s\n' "$p"
      return 0
    fi
  done
  return 1
}

# Append one timestamped per-PID VRAM sample block (all compute-apps) to CSV $1.
measure_sample_vram() {
  local csv="$1" ts line
  ts="$(date +%s)"
  while IFS= read -r line; do
    [[ -n "$line" ]] && printf '%s,%s\n' "$ts" "$line" >> "$csv"
  done < <(nvidia-smi --query-compute-apps=pid,process_name,used_gpu_memory --format=csv,noheader 2>/dev/null || true)
}

# Append one timestamped docker-stats NetIO/MemUsage sample for $2 to CSV $1.
measure_sample_netio() {
  local csv="$1" cname="$2" line
  line="$(docker stats --no-stream --format '{{.NetIO}}|{{.MemUsage}}' "$cname" 2>/dev/null || true)"
  [[ -n "$line" ]] && printf '%s,%s\n' "$(date +%s)" "$line" >> "$csv"
}

# Capture the pmon burst + the full nvidia-smi process table (G+C) into dir $1. Call
# this INSIDE the steady-state window (G-18): these are the cross-check evidence files,
# preserved raw, that the boot-init 1-shot snapshot bug (QA cycle-2 Issue-1) skipped.
measure_capture_steady_evidence() {
  local dir="$1" n="${2:-5}"
  {
    echo "# steady-state cross-check captured $(date -Is)"
    echo "# full nvidia-smi process table (G+C):"
  } > "$dir/nvidia_smi_full_steady.txt"
  nvidia-smi >> "$dir/nvidia_smi_full_steady.txt" 2>&1 || true
  nvidia-smi pmon -s m -c "$n" -d 1 > "$dir/pmon_steady.txt" 2>&1 || true
}

# Steady-state test on the PRESERVED VRAM CSV: the last K samples of PID $2 each moved
# < eps (relative). Returns 0 (steady) / 1 (not yet). Derives a judgement, writes
# nothing. K/eps default to the window knobs above (NOT measurement targets).
measure_vram_steady() {
  local csv="$1" pid="$2" k="${3:-$CV_STEADY_K}" eps="${4:-$CV_STEADY_EPS}"
  awk -F',[[:space:]]*' -v pid="$pid" -v k="$k" -v eps="$eps" '
    $2 == pid { v=$4; gsub(/[^0-9.]/, "", v); vals[n++] = v + 0 }
    END {
      if (n < k + 1) exit 1
      for (i = n - k; i < n; i++) {
        prev = vals[i - 1]; cur = vals[i]
        if (prev <= 0) exit 1
        d = (cur - prev) / prev; if (d < 0) d = -d
        if (d >= eps) exit 1
      }
      exit 0
    }' "$csv"
}

# Derive peak / last-steady / median VRAM (MiB) for PID $2 from the PRESERVED CSV $1.
# Prints "peak=<> steady=<> median=<> samples=<>" — a derivation, never edits raw.
measure_vram_summary() {
  local csv="$1" pid="$2"
  awk -F',[[:space:]]*' -v pid="$pid" '
    $2 == pid { v=$4; gsub(/[^0-9.]/, "", v); vals[n++] = v + 0; if (v + 0 > peak) peak = v + 0 }
    END {
      if (n == 0) { print "peak=NA steady=NA median=NA samples=0"; exit }
      # median
      for (i = 0; i < n; i++) s[i] = vals[i]
      for (i = 0; i < n; i++) for (j = i + 1; j < n; j++) if (s[j] < s[i]) { t = s[i]; s[i] = s[j]; s[j] = t }
      med = (n % 2) ? s[int(n / 2)] : (s[n / 2 - 1] + s[n / 2]) / 2
      printf "peak=%g steady=%g median=%g samples=%d\n", peak, vals[n - 1], med, n
    }' "$csv"
}

# Wall seconds of a container from docker inspect StartedAt/FinishedAt. Prints a float.
measure_container_wall_s() {
  local cname="$1" started finished
  started="$(docker inspect -f '{{.State.StartedAt}}' "$cname" 2>/dev/null || echo '')"
  finished="$(docker inspect -f '{{.State.FinishedAt}}' "$cname" 2>/dev/null || echo '')"
  [[ -n "$started" && -n "$finished" ]] || { echo "NA"; return; }
  local t0 t1
  t0="$(date -d "$started" +%s.%N 2>/dev/null || echo '')"
  t1="$(date -d "$finished" +%s.%N 2>/dev/null || echo '')"
  [[ -n "$t0" && -n "$t1" ]] || { echo "NA"; return; }
  awk -v a="$t0" -v b="$t1" 'BEGIN { printf "%.1f", b - a }'
}
