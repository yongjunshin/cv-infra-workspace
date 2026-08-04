#!/usr/bin/env bash
# check_consent.sh — the EULA consent GATE (M5 §3.7; REQ-DEPLOY-010/011, NFR-DEPLOY-004).
#
# Answers exactly one question, read-only: has an operator recorded consent on THIS host?
#
#   exit 0 — a valid record exists (operator identity + timestamp are printed)
#   exit 3 — record absent or invalid  -> the platform is not ready (exit-code contract:
#            3 = platform/infra, the same code the runner's own boot guard uses)
#   exit 2 — usage error
#
# It never accepts, never writes, never repairs. Consent is created only by an operator
# running accept_eula.sh (LOCKED §8: no auto-acceptance, and CI may not bypass this).
#
# Two gates, one boundary (M5 §3.7 D-O/F7) — do NOT collapse them into one:
#   * THIS record = "did an operator consent, who, when" — host-side audit + gate.
#   * The runtime ENV that the runner receives = the boot gate the runner actually
#     honors (ACCEPT_EULA absent -> cv_infra/runner/sim_runtime.py refuses to boot Isaac,
#     exit 3). The record is the REASON that env is allowed to exist, not a second
#     source of truth for the boot decision.
#
# Usage:
#   bash scripts/consent/check_consent.sh [--quiet]
#   CV_CONSENT_RECORD=/path/to/record.json bash scripts/consent/check_consent.sh
set -euo pipefail

export CV_STEP=consent-gate
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=../workstation_setup/common.sh
source "$SCRIPT_DIR/../workstation_setup/common.sh"

QUIET=0
while (($#)); do
  case "$1" in
    --quiet) QUIET=1 ;;
    -h|--help) sed -n '2,25p' "${BASH_SOURCE[0]}"; exit 0 ;;
    *) err "unknown argument: $1"; err "usage: $0 [--quiet]"; exit 2 ;;
  esac
  shift
done

require_cmd python3

if [[ ! -f "$CV_CONSENT_RECORD" ]]; then
  err "no NVIDIA Isaac Sim consent record on this host: $CV_CONSENT_RECORD"
  err "This deployment never accepts the license on your behalf (NEG-2 / LOCKED §8)."
  err "Fix:  bash $SCRIPT_DIR/accept_eula.sh"
  exit 3
fi

# Validation lives in ONE place (python3, stdlib only) so the writer and the gate can
# never disagree about what "valid" means — accept_eula.sh re-runs this very script to
# verify what it just wrote.
# (the prefix uses CV_RECORD_* names on purpose: CV_CONSENT_RECORD/_SCHEMA are `readonly`
# in this shell, and a command-prefix assignment to a readonly name fails the command)
if ! summary="$(
  CV_RECORD_PATH="$CV_CONSENT_RECORD" \
  CV_RECORD_SCHEMA="$CV_CONSENT_RECORD_SCHEMA" \
  python3 - <<'PY'
import json
import os
import sys
from datetime import datetime

path = os.environ["CV_RECORD_PATH"]
schema = os.environ["CV_RECORD_SCHEMA"]

try:
    with open(path, encoding="utf-8") as handle:
        record = json.load(handle)
except (OSError, ValueError) as exc:
    sys.exit(f"consent record is not readable JSON ({exc})")

if not isinstance(record, dict):
    sys.exit("consent record is not a JSON object")
if record.get("schema") != schema:
    sys.exit(f"unknown consent record schema {record.get('schema')!r} (expected {schema!r})")

accepted = record.get("eula_accepted")
if not isinstance(accepted, str) or accepted.strip().lower() in {"", "no", "n", "false", "0"}:
    sys.exit("record carries no affirmative eula_accepted value")

fields = {}
for name in ("eula_operator_identity", "eula_consented_at"):
    value = record.get(name)
    if not isinstance(value, str) or not value.strip():
        sys.exit(f"record field {name} is missing or empty")
    fields[name] = value.strip()

try:  # a timestamp that cannot be parsed is not an audit trail
    datetime.fromisoformat(fields["eula_consented_at"])
except ValueError:
    sys.exit(f"eula_consented_at {fields['eula_consented_at']!r} is not an ISO-8601 timestamp")

print(f"{fields['eula_operator_identity']}\t{fields['eula_consented_at']}")
PY
)"; then
  err "consent record is INVALID: $CV_CONSENT_RECORD"
  err "A malformed record is treated exactly like no record at all (fail-closed)."
  err "Fix:  bash $SCRIPT_DIR/accept_eula.sh"
  exit 3
fi

identity="${summary%%$'\t'*}"
consented_at="${summary#*$'\t'}"
((QUIET)) || log "consent recorded by '${identity}' at ${consented_at} (record: $CV_CONSENT_RECORD)"
exit 0
