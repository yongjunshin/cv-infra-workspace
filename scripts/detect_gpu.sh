#!/usr/bin/env bash
# detect_gpu.sh — adaptive GPU profile selection (M5 §3.4; REQ-DEPLOY-003, NFR-DEPLOY-001).
#
# WHAT IT DOES: asks the LIVE GPU what it is, picks the matching profiles/*.yaml, and
# prints a docker/.env fragment. Selection is decided by the RUNTIME query result —
# never by hostname, never by a model literal in this script. Support for another GPU
# is a new file in profiles/, not an edit here.
#
#   ./scripts/detect_gpu.sh >> docker/.env      # step ②' of the C-2 deploy flow
#
# The deploy flow this is part of (docs/deploy/gpu-profiles.md, docker/compose.yaml):
#   ① scripts/workstation_setup/provision.sh    host prereqs (driver + toolkit)
#   ②' scripts/detect_gpu.sh >> docker/.env     ← THIS SCRIPT (adaptive parameters)
#   ② scripts/consent/accept_eula.sh            NVIDIA EULA (explicit, once)
#   ③ CV_SOURCE_REVISION="$(git rev-parse HEAD)" \
#       docker compose -f docker/compose.yaml up -d --build
#
# Reuse (do-not-reinvent): the identification is stock `nvidia-smi --query-gpu=name`
# — the same NVIDIA tool the provisioning preflight already uses. This is a ONE-SHOT
# query at deploy time, NOT a poller: cv_infra/orchestrator/monitor.py holds the
# project's sole periodic NVML poller and this script must never become a second one.
#
# NO MODEL LITERAL LIVES HERE. The M5 D9 allowlist would permit a mapping table in
# this file, but the patterns live in the profiles instead (each profile declares its
# own `match_name_pattern`), so this script stays model-agnostic and
# tests/negative/test_deployment_identity_hardcoding.py scans it like any other code.
#
# Inputs — all env/arg, nothing baked in:
#   --profile ID     / CV_GPU_PROFILE    skip detection, force a profile (C-2 "auto
#                                        detect + MANUAL selection"; use when
#                                        nvidia-smi is unavailable or a host must be
#                                        pinned to a profile deliberately)
#   --profile-dir D  / CV_PROFILE_DIR    profile directory (default: <repo>/profiles)
#   --nvidia-smi CMD / CV_NVIDIA_SMI     the query command (default: nvidia-smi).
#                                        Also the CPU test seam.
#
# Exit codes: 0 = a profile was selected (fragment on stdout) · 2 = usage error ·
#             3 = NO profile could be selected (unknown/heterogeneous GPU, missing
#                 nvidia-smi, ambiguous or malformed profiles). FAIL-CLOSED on
#                 purpose: emitting a guessed budget for an unknown card is exactly
#                 the silent misconfiguration this gate exists to prevent — same
#                 infra/config exit class as the consent gate and check_plane_skew.sh.
set -euo pipefail

readonly CV_STEP=detect-gpu
readonly EXIT_USAGE=2
readonly EXIT_UNSUPPORTED=3

log() { printf '[cv-infra][%s] %s\n' "$CV_STEP" "$*" >&2; }
err() { printf '[cv-infra][%s][ERROR] %s\n' "$CV_STEP" "$*" >&2; }
die() { err "$*"; exit "$EXIT_UNSUPPORTED"; }

usage() {
  sed -n '2,45p' "${BASH_SOURCE[0]}" | sed 's/^# \{0,1\}//'
}

_here="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
readonly CV_REPO_ROOT="${_here%/scripts}"

CV_PROFILE_DIR="${CV_PROFILE_DIR:-$CV_REPO_ROOT/profiles}"
CV_NVIDIA_SMI="${CV_NVIDIA_SMI:-nvidia-smi}"
CV_GPU_PROFILE="${CV_GPU_PROFILE:-}"

while [[ $# -gt 0 ]]; do
  case "$1" in
    --profile)     CV_GPU_PROFILE="${2:?--profile needs a profile id}"; shift 2 ;;
    --profile-dir) CV_PROFILE_DIR="${2:?--profile-dir needs a path}"; shift 2 ;;
    --nvidia-smi)  CV_NVIDIA_SMI="${2:?--nvidia-smi needs a command}"; shift 2 ;;
    -h|--help)     usage; exit 0 ;;
    *) err "unknown argument: $1"; usage >&2; exit "$EXIT_USAGE" ;;
  esac
done

[[ -d "$CV_PROFILE_DIR" ]] || die "profile directory not found: '$CV_PROFILE_DIR' (set --profile-dir / CV_PROFILE_DIR)."

# --- profile file access (the shell half of the flat `key: value` grammar) --------
# Strips up to the FIRST colon only, then trims blanks and one layer of quotes, so a
# value may itself contain '/', '=', '~' and '-'. Missing key or empty value -> "".
profile_get() {
  local file="$1" key="$2" value
  value="$(sed -n "s/^${key}:[[:space:]]*//p" "$file" | head -n1)"
  value="${value%"${value##*[![:space:]]}"}"
  value="${value%\"}"; value="${value#\"}"
  printf '%s' "$value"
}

profile_path() { printf '%s/%s.yaml' "$CV_PROFILE_DIR" "$1"; }

# --- 1. identify the GPU (or take the operator's manual selection) ----------------
detect_gpu_name() {
  local names line uniq_names=()
  command -v "$CV_NVIDIA_SMI" >/dev/null 2>&1 \
    || die "'$CV_NVIDIA_SMI' not found — this deployment targets a GPU host (NFR-DEPLOY-005). Install the driver (scripts/workstation_setup/provision.sh), or select a profile manually with --profile."
  names="$("$CV_NVIDIA_SMI" --query-gpu=name --format=csv,noheader 2>/dev/null)" \
    || die "'$CV_NVIDIA_SMI --query-gpu=name' failed — is the driver loaded? (nvidia-smi alone should print a device table.)"

  while IFS= read -r line; do
    line="${line%$'\r'}"
    line="${line#"${line%%[![:space:]]*}"}"
    line="${line%"${line##*[![:space:]]}"}"
    [[ -z "$line" ]] && continue
    local seen=0 known
    for known in ${uniq_names[@]+"${uniq_names[@]}"}; do
      [[ "$known" == "$line" ]] && seen=1 && break
    done
    (( seen )) || uniq_names+=("$line")
  done <<<"$names"

  (( ${#uniq_names[@]} > 0 )) || die "'$CV_NVIDIA_SMI --query-gpu=name' returned no device."
  # Heterogeneous multi-GPU: one per-instance VRAM budget cannot describe two cards.
  # Refuse instead of silently profiling the first one (single-deployment assumption,
  # deployment group §Assumptions).
  (( ${#uniq_names[@]} == 1 )) \
    || die "host reports MORE THAN ONE GPU model (${uniq_names[*]}) — one resource budget cannot describe both. Select deliberately with --profile <id> after deciding which card runs the jobs."
  printf '%s' "${uniq_names[0]}"
}

select_profile_by_name() {  # <detected name> -> profile id (or die)
  local name="$1" file id pattern matches=()
  shopt -s nullglob
  for file in "$CV_PROFILE_DIR"/*.yaml; do
    id="$(profile_get "$file" id)"
    pattern="$(profile_get "$file" match_name_pattern)"
    [[ -n "$id" && -n "$pattern" ]] \
      || die "malformed profile '$file': both 'id' and 'match_name_pattern' are required."
    if (shopt -s nocasematch; [[ "$name" =~ $pattern ]]); then
      matches+=("$id")
    fi
  done
  shopt -u nullglob

  case ${#matches[@]} in
    1) printf '%s' "${matches[0]}" ;;
    0) die "no profile matches the detected GPU '$name'. This deployment supports the GPUs described in $CV_PROFILE_DIR (REQ-DEPLOY-003). To add this one: copy an existing profile, set 'id' + 'match_name_pattern', leave 'vram_per_instance_mb' EMPTY until you MEASURE it (see profiles/a100.yaml), then re-run." ;;
    *) die "ambiguous: GPU '$name' matches ${#matches[@]} profiles (${matches[*]}). Tighten their match_name_pattern, or pick one with --profile." ;;
  esac
}

if [[ -n "$CV_GPU_PROFILE" ]]; then
  profile_id="$CV_GPU_PROFILE"
  detected="(manual selection — detection skipped)"
  [[ -f "$(profile_path "$profile_id")" ]] \
    || die "no such profile: '$profile_id' (looked for $(profile_path "$profile_id"))."
  log "profile FORCED by operator: $profile_id"
else
  detected="$(detect_gpu_name)"
  profile_id="$(select_profile_by_name "$detected")"
  log "detected GPU '$detected' -> profile '$profile_id'"
fi

profile_file="$(profile_path "$profile_id")"
vram_mb="$(profile_get "$profile_file" vram_per_instance_mb)"
vram_source="$(profile_get "$profile_file" vram_per_instance_source)"

# --- 2. emit the docker/.env fragment ---------------------------------------------
printf '# --- cv-infra adaptive GPU profile (scripts/detect_gpu.sh, REQ-DEPLOY-003) ---\n'
printf '# detected GPU : %s\n' "$detected"
printf '# profile      : %s (%s)\n' "$profile_id" "${profile_file#"$CV_REPO_ROOT/"}"
printf '# generated    : %s\n' "$(date -u +%Y-%m-%dT%H:%M:%SZ)"

if [[ -n "$vram_mb" ]]; then
  [[ -n "$vram_source" ]] \
    || die "profile '$profile_id' states vram_per_instance_mb=$vram_mb with NO vram_per_instance_source — an unanchored number is a G-24 violation and must not reach a deployment."
  printf '# measured     : %s\n' "$vram_source"
  printf 'CV_VRAM_PER_INSTANCE_MB=%s\n' "$vram_mb"
else
  log "profile '$profile_id' has NO measured vram_per_instance_mb (TBD / 미실측) — the NVML VRAM guard stays OFF and k is capped by CV_MAX_CONCURRENT alone. Measure it before raising the cap (recipe in $profile_file)."
  printf '# vram_per_instance_mb : TBD (unmeasured / 미실측) for this profile.\n'
  printf '#   The NVML 2nd guard stays OFF -> k = min(CV_MAX_CONCURRENT, render_cap).\n'
  printf '#   Measure per-PID VRAM during one job (DoD-P2-09 recipe), write it into\n'
  printf '#   %s WITH its evidence path, then re-run this script.\n' "${profile_file#"$CV_REPO_ROOT/"}"
  printf '#CV_VRAM_PER_INSTANCE_MB=\n'
fi
