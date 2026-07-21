#!/usr/bin/env bash
# register_gh_runner.sh — DoD-P1-07: register this workstation as a self-hosted
# GitHub Actions runner (repo-level, labels [self-hosted, cv-infra-gpu]) and
# persist it as a systemd service.
# Policy: decision 2026-07-03-self-hosted-runner-policy (binding) — exact-version
# pin + tarball sha256 verification + --disableupdate; hardening applied at
# registration time (no workflow consumes the self-hosted label until P5).
#
# Registration TARGET is parameterized via env (decision
# 2026-07-21-e2e-user-runner-provisioning): CV_GH_RUNNER_REPO_URL /
# CV_GH_RUNNER_NAME / CV_GH_RUNNER_HOME / CV_GH_RUNNER_SERVICE, all defaulting
# in common.sh to the original cv-infra-workspace runner — a plain no-env run
# is exactly the pre-parameterization behavior. Second-runner example
# (cv-infra-user, same machine, own home + own unit): see README →
# "Second runner". Version/sha256 pins and the label set are NOT parameters.
#
# Idempotent: re-run skips download (version marker), skips config (.runner
# present), reinstalls the unit only on content drift. A pin refresh (bump the
# CV_GH_RUNNER_* pins in common.sh) is also just a re-run.
#
# Registration token: injected via env RUNNER_REG_TOKEN ONLY (issued locally with
#   gh api -X POST repos/<owner>/<repo>/actions/runners/registration-token --jq .token
# for the TARGET repo). The token is never logged, never echoed, never written
# to disk by this script.
#
# sudo surface: `sudo -n install` + `sudo -n systemctl` only (both in the
# /etc/sudoers.d/cv-infra whitelist). GitHub's ./svc.sh is deliberately NOT used —
# it shells out to sudo paths outside the whitelist.
set -euo pipefail

export CV_STEP=runner
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source-path=SCRIPTDIR
# shellcheck source=common.sh
source "$SCRIPT_DIR/common.sh"

require_cmd curl
require_cmd tar
require_cmd sha256sum
require_cmd systemctl

readonly TARBALL="actions-runner-linux-x64-${CV_GH_RUNNER_VERSION}.tar.gz"
readonly TARBALL_URL="https://github.com/actions/runner/releases/download/v${CV_GH_RUNNER_VERSION}/${TARBALL}"
readonly VERSION_MARKER="$CV_GH_RUNNER_HOME/.cv_runner_version"
readonly UNIT_PATH="/etc/systemd/system/${CV_GH_RUNNER_SERVICE}.service"
# owner/repo slug (die/log messages) + bare repo name (unit Description) derived
# from the target URL. The Description uses the bare NAME so a default (no-env)
# re-run renders the unit byte-identical to the pre-parameterization file —
# content-drift reinstall stays a genuine no-op for the existing runner.
readonly RUNNER_REPO_PATH="${CV_GH_RUNNER_REPO_URL#https://github.com/}"
readonly RUNNER_REPO_NAME="${CV_GH_RUNNER_REPO_URL##*/}"

# ① Download the PINNED runner release and verify the tarball sha256 (mismatch = die).
install_runner_binaries() {
  if [[ -f "$VERSION_MARKER" && "$(cat "$VERSION_MARKER")" == "$CV_GH_RUNNER_VERSION" ]]; then
    log "runner binaries already at pinned version ${CV_GH_RUNNER_VERSION} — skipping download (idempotent)"
    return 0
  fi

  # Pin refresh path: stop the service before replacing binaries under it.
  if systemctl is-active --quiet "$CV_GH_RUNNER_SERVICE" 2>/dev/null; then
    log "stopping ${CV_GH_RUNNER_SERVICE} before binary upgrade (pin refresh)"
    "${CV_SUDO[@]}" systemctl stop "$CV_GH_RUNNER_SERVICE"
  fi

  local tmp got
  tmp="$(mktemp -d)"
  log "downloading pinned runner ${CV_GH_RUNNER_VERSION}: $TARBALL_URL"
  curl -fsSL -o "$tmp/$TARBALL" "$TARBALL_URL"

  got="$(sha256sum "$tmp/$TARBALL" | awk '{print $1}')"
  if [[ "$got" != "$CV_GH_RUNNER_TARBALL_SHA256" ]]; then
    rm -rf "$tmp"
    err "tarball sha256 MISMATCH for ${TARBALL}:"
    err "    expected ${CV_GH_RUNNER_TARBALL_SHA256} (official release-notes checksum, see common.sh)"
    err "    got      ${got}"
    die "Refusing to install an unverified runner binary (supply-chain guard; re-check the pin in common.sh)."
  fi
  log "tarball sha256 verified OK"

  mkdir -p "$CV_GH_RUNNER_HOME"
  tar xzf "$tmp/$TARBALL" -C "$CV_GH_RUNNER_HOME"
  rm -rf "$tmp"
  printf '%s\n' "$CV_GH_RUNNER_VERSION" > "$VERSION_MARKER"
  log "runner ${CV_GH_RUNNER_VERSION} unpacked into $CV_GH_RUNNER_HOME"
}

# ② Configure (register) the runner — repo-level, unattended, pinned (no self-update).
configure_runner() {
  if [[ -f "$CV_GH_RUNNER_HOME/.runner" ]]; then
    log "runner already configured (.runner present) — skipping registration (idempotent)."
    log "To re-register from scratch, see README → runner teardown (config.sh remove)."
    return 0
  fi
  if [[ -z "${RUNNER_REG_TOKEN:-}" ]]; then
    die "RUNNER_REG_TOKEN is not set. Issue a short-lived registration token LOCALLY (never commit/log it):
    gh api -X POST repos/${RUNNER_REPO_PATH}/actions/runners/registration-token --jq .token
then re-run with the token in the environment (see README → runner registration)."
  fi
  log "registering runner '${CV_GH_RUNNER_NAME}' (labels: self-hosted + ${CV_GH_RUNNER_LABELS}) with ${CV_GH_RUNNER_REPO_URL}"
  (
    cd "$CV_GH_RUNNER_HOME"
    ./config.sh \
      --url "$CV_GH_RUNNER_REPO_URL" \
      --token "$RUNNER_REG_TOKEN" \
      --name "$CV_GH_RUNNER_NAME" \
      --labels "$CV_GH_RUNNER_LABELS" \
      --unattended \
      --disableupdate \
      --replace
  )
  log "runner registered"
}

# ③ Persist via a systemd system unit placed with whitelisted `install`/`systemctl`.
install_service() {
  local unit_src
  unit_src="$(mktemp)"
  cat > "$unit_src" <<EOF
[Unit]
Description=cv-infra self-hosted GitHub Actions runner (${CV_GH_RUNNER_NAME} -> ${RUNNER_REPO_NAME})
After=network-online.target
Wants=network-online.target

[Service]
User=etri
WorkingDirectory=${CV_GH_RUNNER_HOME}
ExecStart=${CV_GH_RUNNER_HOME}/run.sh
Restart=always
RestartSec=10
KillMode=process
KillSignal=SIGINT
TimeoutStopSec=5min

[Install]
WantedBy=multi-user.target
EOF

  if cmp -s "$unit_src" "$UNIT_PATH" 2>/dev/null; then
    log "systemd unit already up to date at $UNIT_PATH (idempotent)"
  else
    log "installing systemd unit -> $UNIT_PATH"
    "${CV_SUDO[@]}" install -m 0644 -o root -g root "$unit_src" "$UNIT_PATH"
    "${CV_SUDO[@]}" systemctl daemon-reload
  fi
  rm -f "$unit_src"

  "${CV_SUDO[@]}" systemctl enable --now "$CV_GH_RUNNER_SERVICE"
  log "service state: active=$(systemctl is-active "$CV_GH_RUNNER_SERVICE" || true), enabled=$(systemctl is-enabled "$CV_GH_RUNNER_SERVICE" || true)"
}

main() {
  install_runner_binaries
  configure_runner
  install_service
  log "registration complete — verify online/idle from a gh-enabled host:"
  log "    gh api repos/${RUNNER_REPO_PATH}/actions/runners --jq '.runners[] | {name,status,busy}'"
}

main "$@"
