#!/usr/bin/env bash
# accept_eula.sh — NVIDIA Isaac Sim consent gate (M5 §3.7; REQ-DEPLOY-008/009/010/011,
# NFR-DEPLOY-004, LOCKED §8). Step ② of the C-2 deployment flow:
#
#   ① provision.sh   ② THIS SCRIPT   ③ docker compose up   ④ cv-infra selftest
#
# Four steps, in this order (M5 §3.7):
#   1. present the license + privacy notice the operator is being asked to accept
#   2. take an EXPLICIT operator decision — never a default, never inferred
#   3. record it: eula_accepted + eula_operator_identity + eula_consented_at
#   4. only THEN write the derived acceptance values into the runtime env file
#      (docker/.env, git-ignored) that the control plane forwards to runners
#
# NEG-2 / LOCKED §8: no acceptance value is committed anywhere in this repository. The
# value written in step 4 is DERIVED at run time from what the operator typed (same idiom
# as scripts/isaac_smoke/run_smoke.sh) — this file contains no acceptance literal, and CI
# may not synthesize one to skip the step.
#
# Exit codes: 0 = consent recorded · 2 = usage error · 3 = consent NOT given / cannot be
# obtained (platform not ready — the same code the runner boot guard uses).
#
# Usage:
#   bash scripts/consent/accept_eula.sh                     # interactive (prompts)
#   CV_EULA_CONSENT=<the word the prompt asks for> \
#   CV_CONSENT_IDENTITY="jane@etri" \
#     bash scripts/consent/accept_eula.sh                   # non-interactive (e.g. SSH)
#
# Env overrides: CV_CONSENT_RECORD (record path) · CV_ENV_FILE (runtime env file) ·
# CV_CONSENT_IDENTITY (operator identity) — defaults in workstation_setup/common.sh.
set -euo pipefail

export CV_STEP=consent
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=../workstation_setup/common.sh
source "$SCRIPT_DIR/../workstation_setup/common.sh"

REPO_ROOT="$(cd "$SCRIPT_DIR/../.." && pwd)"
ENV_FILE="${CV_ENV_FILE:-$REPO_ROOT/docker/.env}"
IDENTITY="${CV_CONSENT_IDENTITY:-}"

while (($#)); do
  case "$1" in
    --operator) shift || true; IDENTITY="${1:-}" ;;
    -h|--help) sed -n '2,30p' "${BASH_SOURCE[0]}"; exit 0 ;;
    *) err "unknown argument: $1"; err "usage: $0 [--operator NAME]"; exit 2 ;;
  esac
  shift
done

require_cmd python3

# ---------------------------------------------------------------------------
# 1. Present what is being accepted (REQ-DEPLOY-008)
# ---------------------------------------------------------------------------
cat <<PRESENT

  cv-infra runs NVIDIA Isaac Sim ($CV_ISAAC_IMAGE) on this host.
  Before it may start, you must accept, as the operator of this host:

    (a) the NVIDIA Isaac Sim license agreement
        $CV_EULA_URL
    (b) the NVIDIA privacy policy / Omniverse data-collection notice
        $CV_PRIVACY_URL

  What happens if you accept:
    * your decision is recorded (identity + timestamp) at
        $CV_CONSENT_RECORD
    * the acceptance is written to the runtime env file
        $ENV_FILE
      which is git-ignored — it stays on this host and is never committed.
  What does NOT happen: nothing here accepts on your behalf, and no CI job can
  skip this step. Without a recorded consent, Isaac Sim refuses to boot.

PRESENT

# ---------------------------------------------------------------------------
# 2. Explicit operator decision (REQ-DEPLOY-009, NFR-DEPLOY-004)
# ---------------------------------------------------------------------------
CONSENT_CHANNEL="interactive"
CONSENT_INPUT="${CV_EULA_CONSENT:-}"

if [[ -n "$CONSENT_INPUT" ]]; then
  # Non-interactive path: an operator supplying the consent input on the command line
  # IS an explicit decision (decision 2026-07-03-p1-eula-runtime-consent), but it must
  # name who is deciding — an anonymous automated "yes" is exactly what NEG-2 forbids.
  CONSENT_CHANNEL="non-interactive-env"
  if [[ -z "$IDENTITY" ]]; then
    err "non-interactive consent needs an operator identity to record (REQ-DEPLOY-010)."
    err "Re-run with:  CV_CONSENT_IDENTITY='<who you are>' CV_EULA_CONSENT=... $0"
    exit 3
  fi
  log "consent input supplied non-interactively by '${IDENTITY}' (decision is checked below)"
elif [[ -t 0 ]]; then
  read -r -p "  Do you accept (a) and (b) above? Type 'yes' to accept: " CONSENT_INPUT
  if [[ -z "$IDENTITY" ]]; then
    default_identity="$(id -un)@$(hostname)"
    read -r -p "  Operator identity to record [${default_identity}]: " IDENTITY
    IDENTITY="${IDENTITY:-$default_identity}"
  fi
else
  err "no terminal to ask for consent on, and no consent input was supplied."
  err "This script never assumes acceptance (LOCKED §8). For a non-interactive host:"
  err "  CV_CONSENT_IDENTITY='<who you are>' CV_EULA_CONSENT=<the affirmative word> $0"
  exit 3
fi

if [[ "${CONSENT_INPUT,,}" != "yes" ]]; then
  err "consent NOT given — nothing was recorded and Isaac Sim stays blocked."
  err "Re-run this script when you are ready to accept."
  exit 3
fi
IDENTITY="$(printf '%s' "$IDENTITY" | tr -d '[:cntrl:]')"
[[ -n "${IDENTITY// /}" ]] || { err "operator identity must not be empty (REQ-DEPLOY-010)"; exit 3; }

# ---------------------------------------------------------------------------
# 3. Record the consent — identity + timestamp (REQ-DEPLOY-010)
# ---------------------------------------------------------------------------
CONSENTED_AT="$(date -Is)"      # ISO-8601 with UTC offset (the gate parses it back)
mkdir -p "$(dirname "$CV_CONSENT_RECORD")"
chmod 700 "$(dirname "$CV_CONSENT_RECORD")" 2>/dev/null || true

# python3 writes the JSON so an identity containing quotes/backslashes cannot corrupt
# the record (and the gate reads it back with the same stdlib parser).
CV_RECORD_PATH="$CV_CONSENT_RECORD" \
CV_RECORD_SCHEMA="$CV_CONSENT_RECORD_SCHEMA" \
CV_RECORD_ACCEPTED="$CONSENT_INPUT" \
CV_RECORD_IDENTITY="$IDENTITY" \
CV_RECORD_AT="$CONSENTED_AT" \
CV_RECORD_CHANNEL="$CONSENT_CHANNEL" \
CV_RECORD_EULA_URL="$CV_EULA_URL" \
CV_RECORD_PRIVACY_URL="$CV_PRIVACY_URL" \
CV_RECORD_SUBJECT="$CV_ISAAC_IMAGE" \
CV_RECORD_HOST="$(hostname)" \
python3 - <<'PY'
import json
import os

record = {
    "schema": os.environ["CV_RECORD_SCHEMA"],
    # The operator's own words — the acceptance value handed to the runtime is DERIVED
    # from this at write time, so no acceptance literal exists in any committed file.
    "eula_accepted": os.environ["CV_RECORD_ACCEPTED"],
    "eula_operator_identity": os.environ["CV_RECORD_IDENTITY"],
    "eula_consented_at": os.environ["CV_RECORD_AT"],
    "consent_channel": os.environ["CV_RECORD_CHANNEL"],
    "eula_document_url": os.environ["CV_RECORD_EULA_URL"],
    "privacy_notice_url": os.environ["CV_RECORD_PRIVACY_URL"],
    "subject_image": os.environ["CV_RECORD_SUBJECT"],
    "recorded_by": "scripts/consent/accept_eula.sh",
    "recorded_on_host": os.environ["CV_RECORD_HOST"],
}
path = os.environ["CV_RECORD_PATH"]
with open(path, "w", encoding="utf-8") as handle:
    json.dump(record, handle, indent=2, sort_keys=True)
    handle.write("\n")
os.chmod(path, 0o600)
PY
log "consent recorded -> $CV_CONSENT_RECORD"

# ---------------------------------------------------------------------------
# 4. ONLY NOW: derive the runtime acceptance env (M5 §3.7 step 4)
# ---------------------------------------------------------------------------
# Derived from what the operator typed ("yes" -> "Y"), exactly like run_smoke.sh does for
# a single run. The runtime env — not this record — is what the runner's boot guard
# honors (single source of truth, M5 §3.7 D-O/F7); the record is why it may exist.
_consent_upper="${CONSENT_INPUT^^}"
ACCEPT_VALUE="${_consent_upper:0:1}"

env_set() {   # key value file  — idempotent single-line upsert, mode 0600
  local key="$1" value="$2" file="$3" tmp
  tmp="$(mktemp)"
  if [[ -f "$file" ]]; then
    { grep -v -E "^[[:space:]]*(export[[:space:]]+)?${key}=" "$file" || true; } > "$tmp"
  fi
  printf '%s=%s\n' "$key" "$value" >> "$tmp"
  install -m 600 "$tmp" "$file"
  rm -f "$tmp"
}

mkdir -p "$(dirname "$ENV_FILE")"
for key in ACCEPT_EULA PRIVACY_CONSENT; do
  env_set "$key" "$ACCEPT_VALUE" "$ENV_FILE"
done
log "runtime acceptance written -> $ENV_FILE (git-ignored; never commit it)"

# Loud guard against the one way this could still leak: an env file that is NOT ignored.
if command -v git >/dev/null 2>&1 \
   && git -C "$(dirname "$ENV_FILE")" rev-parse --is-inside-work-tree >/dev/null 2>&1 \
   && ! git -C "$(dirname "$ENV_FILE")" check-ignore -q "$ENV_FILE"; then
  warn "$ENV_FILE is inside a git work tree but NOT git-ignored — it now holds an"
  warn "acceptance value. Add it to .gitignore before committing anything (NEG-2)."
fi

# ---------------------------------------------------------------------------
# Verify what we wrote with the SAME gate the deployment uses (no second opinion).
# ---------------------------------------------------------------------------
bash "$SCRIPT_DIR/check_consent.sh"

cat <<NEXT

  Consent is on file. Next:
    docker compose -f docker/compose.yaml up -d --build
  The gate can be re-checked at any time (exit 0 = recorded, exit 3 = not):
    bash scripts/consent/check_consent.sh

NEXT
