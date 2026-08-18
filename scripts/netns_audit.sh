#!/usr/bin/env bash
# netns_audit.sh — NEG-1 / DoD-P5-05 runtime egress audit for the C-1 boundary.
#
# WHY (NEG-1, 03-definition-of-done §6): cv-infra must NOT enter an external CI/CD
# environment or git-history store to find a baseline — baseline provenance is the
# internal SQLite only. The STRUCTURAL half of that negative is CPU-provable
# (import allow-list, `grep -rE "git (log|clone|fetch)|api.github.com…" cv_infra/report/`,
# monkeypatched unit negatives). The RUNTIME half (DoD-P5-05) needs an observation
# that the process which ACTUALLY executes the baseline/regression path made zero
# external network accesses during a live run.
#
# That process is the orchestrator server: the report/regression/baseline modules run
# inside `cv_infra.orchestrator.serve` (the CLI only GETs the rendered report over
# 127.0.0.1). So the audited blast radius is ONE network namespace — the serve
# container's — and this script counts every packet that leaves it for a non-private
# destination.
#
# DESIGN — arm → (live run) → audit, with NO timing coupling:
#   * `arm`   installs COUNTER-ONLY netfilter rules in the target container's netns and
#             zeroes them. Counters are cumulative and time-independent, so the auditor
#             can read them any time after the run — nobody has to hold hands with the
#             team running the live leg.
#   * `probe` is the positive control (G-35): it makes ONE outbound TCP connect from
#             INSIDE the audited container and the counters must move. A capture that
#             cannot catch anything makes its own zero meaningless.
#   * `read`  prints the counters plus the netns identity, and FAILS CLOSED (exit 3) if
#             the audit chain is absent — "no rules" must never be read as "0 accesses"
#             (G-26 silent no-op / G-35 vacuous negative).
#
# The rules carry NO jump target: netfilter counts the packet and continues traversal,
# so arming cannot change what the serve container can or cannot reach (this is an
# observation, not an enforcement — enforcement would mask a defect instead of showing it).
#
# JUDGEMENT RULE (what counts as "external CI/git history access"):
#   external = any egress whose destination is NOT loopback and NOT a private/link-local/
#   multicast range (RFC1918 10/8, 172.16/12, 192.168/16, 169.254/16, 224/4, 127/8).
#   GitHub (api.github.com / github.com), GHCR and any git remote are public addresses,
#   so any such access lands on the counters. Traffic to the docker bridge gateway (the
#   CLI ↔ serve API on the host) is private and is NOT counted. DNS to docker's embedded
#   resolver (127.0.0.11) is loopback and is NOT counted; DNS to a public resolver IS.
#   ⇒ `external_packets == 0` is the NEG-1 runtime observation. Any non-zero value is a
#   finding to be explained packet by packet, not a rounding error.
#
# Reuse (do-not-reinvent): stock netfilter counters via the distro `iptables` binary in a
# throwaway sidecar that joins the target netns (`--network container:<name>`); no capture
# daemon, no pcap storage, no observation platform.
#
# Inputs — all arg/env, NO host/GPU/container literals baked in (DoD-P5-09 spirit):
#   <container>                       the container whose netns is audited (required arg)
#   CV_NETNS_AUDIT_IMAGE              sidecar image carrying `iptables` (default below).
#                                     Build it once — the whole image is two lines, and the
#                                     base is digest-pinned (CLAUDE.md §2-7):
#                                       FROM python@sha256:<digest of the host's python:3.11-slim>
#                                       RUN apt-get update \
#                                        && apt-get install -y --no-install-recommends iptables \
#                                        && rm -rf /var/lib/apt/lists/*
#                                     (`docker build -t cv-netns-audit:p5c8 -f Dockerfile.audit .`;
#                                      measured 2026-08-03: iptables 1.8.11-2, nf_tables backend)
#   CV_NETNS_AUDIT_CHAIN              audit chain name (default CV_EXT_AUDIT)
#   CV_NETNS_AUDIT_PROBE_HOST/_PORT   positive-control destination (default a public host)
#   CV_NETNS_AUDIT_RECORD_DIR         where `arm` keeps its record + history (default
#                                     ${TMPDIR:-/tmp}). A record lost with the host's /tmp
#                                     means a reboot, and a reboot already voided the netns.
#
# WHY `arm` WRITES A RECORD (G-73, measured p5c15): `docker compose up -d --build` REPLACES
# the control-plane container, and the replacement is a FRESH netns — rules and counters are
# gone. The deployment looks healthy, so nobody re-arms, and the next runs are simply not
# audited (p5c15: 5 runs on the etri plane). The arm-time identity used to live only in the
# operator's terminal scrollback, so `read` could print "external_packets=0" for a netns that
# was never armed at all *after* a later re-arm. `arm` therefore persists container_id +
# started_at, and `read` compares them with the LIVE container and FAILS CLOSED on mismatch.
# No daemon, no poller: one file written by `arm`, read by `read`.
#
# Exit codes: 0 = ok · 2 = usage error · 3 = infra/config error, INCLUDING "audit chain
#             missing on read", "no arm record" and "container replaced since arm"
#             (fail-closed — same class as the plane-skew gate).
set -euo pipefail

readonly CV_STEP=netns-audit
readonly EXIT_USAGE=2
readonly EXIT_INFRA=3

CV_NETNS_AUDIT_IMAGE="${CV_NETNS_AUDIT_IMAGE:-cv-netns-audit:p5c8}"
CV_NETNS_AUDIT_CHAIN="${CV_NETNS_AUDIT_CHAIN:-CV_EXT_AUDIT}"
CV_NETNS_AUDIT_PROBE_HOST="${CV_NETNS_AUDIT_PROBE_HOST:-api.github.com}"
CV_NETNS_AUDIT_PROBE_PORT="${CV_NETNS_AUDIT_PROBE_PORT:-443}"
CV_NETNS_AUDIT_RECORD_DIR="${CV_NETNS_AUDIT_RECORD_DIR:-${TMPDIR:-/tmp}}"

log() { printf '[cv-infra][%s] %s\n' "$CV_STEP" "$*"; }
err() { printf '[cv-infra][%s][ERROR] %s\n' "$CV_STEP" "$*" >&2; }
die() { err "$*"; exit "$EXIT_INFRA"; }

usage() {
  sed -n '2,76p' "${BASH_SOURCE[0]}" | sed 's/^# \{0,1\}//'
  printf '\nUsage: %s {arm|read|probe} <container>\n' "${BASH_SOURCE[0]##*/}"
}

# --- helpers ---------------------------------------------------------------

require_running() {
  local c="$1"
  command -v docker >/dev/null 2>&1 || die "required tool missing: docker"
  local state
  state="$(docker inspect -f '{{.State.Running}}' "$c" 2>/dev/null)" \
    || die "no such container: '$c' (the audited netns must exist)."
  [[ "$state" == "true" ]] || die "container '$c' is not running — its netns is gone."
}

# Run `iptables …` inside the TARGET's netns via a throwaway sidecar.
in_netns() {
  local c="$1"; shift
  docker run --rm --network "container:${c}" --cap-add NET_ADMIN \
    "$CV_NETNS_AUDIT_IMAGE" "$@"
}

netns_identity() {
  local c="$1"
  docker inspect -f 'container_id={{.Id}} started_at={{.State.StartedAt}}' "$c"
}

# The arm record: one file per (chain, container). Written by `arm`, verified by `read`.
record_path() { printf '%s/cv-netns-audit.%s.%s.arm' "$CV_NETNS_AUDIT_RECORD_DIR" "$CV_NETNS_AUDIT_CHAIN" "$1"; }

write_record() {
  local c="$1" rec; rec="$(record_path "$c")"
  [[ -d "$CV_NETNS_AUDIT_RECORD_DIR" ]] \
    || die "record dir not found: '$CV_NETNS_AUDIT_RECORD_DIR' (set CV_NETNS_AUDIT_RECORD_DIR)."
  { printf 'armed_at=%s\nchain=%s\ncontainer=%s\n%s\n' \
      "$(date -Is)" "$CV_NETNS_AUDIT_CHAIN" "$c" "$(netns_identity "$c")"; } > "$rec" \
    || die "cannot write the arm record '$rec' — refusing to arm without one (a capture nobody can verify is not a capture)."
  # Append-only history: every arm of this deployment, in order (the "arm log" of G-73).
  printf '%s armed %s\n' "$(date -Is)" "$(netns_identity "$c")" >> "${rec}.history" || true
  printf '%s' "$rec"
}

# --- commands --------------------------------------------------------------

cmd_arm() {
  local c="$1"
  require_running "$c"
  local chain="$CV_NETNS_AUDIT_CHAIN"
  # Idempotent: drop a previous arming first, then (re)install and zero.
  in_netns "$c" sh -c "
    set -e
    iptables -D OUTPUT -j ${chain} 2>/dev/null || true
    iptables -F ${chain} 2>/dev/null || true
    iptables -X ${chain} 2>/dev/null || true
    iptables -N ${chain}
    for net in 127.0.0.0/8 10.0.0.0/8 172.16.0.0/12 192.168.0.0/16 169.254.0.0/16 224.0.0.0/4; do
      iptables -A ${chain} -d \$net -j RETURN
    done
    # counter-only rules (no -j): netfilter counts and continues — zero behaviour change.
    iptables -A ${chain} -p tcp --syn
    iptables -A ${chain}
    iptables -A OUTPUT -j ${chain}
    iptables -Z ${chain}
  " >/dev/null
  local rec; rec="$(write_record "$c")"
  log "ARMED chain=${chain} container=${c} at=$(date -Is)"
  log "$(netns_identity "$c")"
  log "arm record: ${rec} (+ .history) — 'read' verifies the live container against it"
  log "counters zeroed; read them later with: $0 read ${c}"
}

cmd_read() {
  local c="$1"
  require_running "$c"
  local chain="$CV_NETNS_AUDIT_CHAIN" out syn all
  out="$(in_netns "$c" iptables -L "$chain" -v -n -x 2>/dev/null)" \
    || die "audit chain '${chain}' is ABSENT in container '${c}' — the run was NOT audited."
  # The two counter-only rules are the LAST two lines (target column empty).
  syn="$(printf '%s\n' "$out" | awk '$0 ~ /tcp/ && $0 ~ /flags:0x17\/0x02/ {print $1}' | tail -1)"
  all="$(printf '%s\n' "$out" | awk 'NF>=8 && $3=="all" && $4=="--" {print $1}' | tail -1)"
  printf '%s\n' "$out"
  local live rec recorded
  live="$(netns_identity "$c")"
  log "$live"
  log "external_syn=${syn:-unparsed} external_packets=${all:-unparsed}"
  # The identity check the header used to leave to the operator's scrollback (G-73).
  rec="$(record_path "$c")"
  [[ -f "$rec" ]] \
    || die "no arm record at '$rec' — these counters cannot be attributed to a known arming. Re-arm ($0 arm ${c}) BEFORE the run you want audited; runs made before that arming are NOT audited."
  recorded="$(grep -m1 '^container_id=' "$rec" || true)"
  [[ -n "$recorded" ]] || die "arm record '$rec' is malformed (no container_id) — re-arm."
  if [[ "$recorded" != "$live" ]]; then
    err "recorded at arm time : $recorded"
    err "live now             : $live"
    die "the audited container was REPLACED or RESTARTED since arm (\`up --build\` does exactly this, G-73) — this capture is VOID, not zero. Re-arm and re-run whatever you needed audited."
  fi
  log "arm record verified: $rec (armed_at=$(sed -n 's/^armed_at=//p' "$rec" | head -1))"
  log "NEG-1 runtime observation holds iff external_packets == 0 AND the identity above"
  log "matches the arm-time record (verified just now — a restarted container = fresh netns"
  log "= counters and rules lost = the capture is VOID, not zero)."
}

cmd_probe() {
  local c="$1"
  require_running "$c"
  log "positive control: ONE TCP connect from inside '${c}' to ${CV_NETNS_AUDIT_PROBE_HOST}:${CV_NETNS_AUDIT_PROBE_PORT}"
  docker exec "$c" python3 -c "
import socket, sys
try:
    socket.create_connection(('${CV_NETNS_AUDIT_PROBE_HOST}', ${CV_NETNS_AUDIT_PROBE_PORT}), timeout=5).close()
    print('connect: reached')
except OSError as exc:
    print(f'connect: failed ({exc}) — the SYN still left the netns, which is what we count')
" || true
  log "counters are now POLLUTED on purpose — re-arm ($0 arm ${c}) to restore a zero baseline."
}

# --- dispatch --------------------------------------------------------------

[[ $# -ge 1 ]] || { usage >&2; exit "$EXIT_USAGE"; }
case "$1" in
  -h|--help) usage; exit 0 ;;
  arm|read|probe)
    [[ $# -eq 2 ]] || { err "'$1' needs exactly one <container>"; usage >&2; exit "$EXIT_USAGE"; }
    "cmd_$1" "$2"
    ;;
  *) err "unknown command: $1"; usage >&2; exit "$EXIT_USAGE" ;;
esac
