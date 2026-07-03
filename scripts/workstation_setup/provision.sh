#!/usr/bin/env bash
# provision.sh — idempotent Phase-1 host provisioning orchestrator (M5).
#
#   preflight (read-only) -> 1) Docker CE -> 2) NVIDIA Container Toolkit
#                         -> 3) GPU passthrough smoke (DoD-P1-02)
#                         -> 4) Isaac Sim image pull (DoD-P1-03)
#
# Every step is safe to re-run. The GPU driver is NEVER installed or upgraded — only
# asserted against the floor. Requires the /etc/sudoers.d/cv-infra NOPASSWD drop-in
# (see README); without it `sudo -n` fails fast and loud rather than hanging (G-06).
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source-path=SCRIPTDIR
# shellcheck source=common.sh
source "$SCRIPT_DIR/common.sh"

preflight() {
  export CV_STEP=preflight
  log "host preflight — read-only assertions (the driver is never modified)"

  require_cmd dpkg
  require_cmd nvidia-smi

  # OS + arch must match what these scripts target.
  # shellcheck disable=SC1091
  source /etc/os-release
  [[ "${ID:-}" == "$CV_REQUIRE_OS_ID" ]] \
    || die "Unsupported OS ID '${ID:-?}' (these scripts target $CV_REQUIRE_OS_ID)"
  [[ "${VERSION_CODENAME:-}" == "$CV_REQUIRE_OS_CODENAME" ]] \
    || die "Unsupported codename '${VERSION_CODENAME:-?}' (these scripts target $CV_REQUIRE_OS_CODENAME)"
  local arch; arch="$(dpkg --print-architecture)"
  [[ "$arch" == "$CV_REQUIRE_ARCH" ]] \
    || die "Unsupported arch '$arch' (these scripts target $CV_REQUIRE_ARCH)"

  # Driver floor + branch — assert only (NFR-DEPLOY-005, DoD-P1-01; decision
  # 2026-07-03-driver-r580-realignment). Floor alone admitted R595, which
  # segfaults the Isaac 5.1.0 RTX renderer — the branch must ALSO match.
  local drv
  drv="$(nvidia-smi --query-gpu=driver_version --format=csv,noheader | head -n1 | tr -d '[:space:]')"
  [[ -n "$drv" ]] || die "Could not read NVIDIA driver version (nvidia-smi)"
  if ! dpkg --compare-versions "$drv" ge "$CV_DRIVER_FLOOR"; then
    die "Driver $drv is below the floor $CV_DRIVER_FLOOR (NFR-DEPLOY-005). Provisioning does NOT touch the driver — realign it out-of-band (realign_driver_r580.sh), then re-run."
  fi
  if [[ "${drv%%.*}" != "$CV_DRIVER_BRANCH" ]]; then
    die "Driver $drv is not on the R${CV_DRIVER_BRANCH} branch (Isaac Sim 5.1.0 certified branch; R595+ segfaults the RTX renderer — decision 2026-07-03-driver-r580-realignment). Run scripts/workstation_setup/realign_driver_r580.sh, then re-run."
  fi

  log "OK: $ID/$VERSION_CODENAME arch=$arch driver=$drv (>= $CV_DRIVER_FLOOR, R$CV_DRIVER_BRANCH branch)"
}

main() {
  preflight

  export CV_STEP=provision
  log "=== step 1/4: Docker CE ==="
  bash "$SCRIPT_DIR/install_docker.sh"

  log "=== step 2/4: NVIDIA Container Toolkit ==="
  bash "$SCRIPT_DIR/install_nvidia_toolkit.sh"

  log "=== step 3/4: GPU passthrough smoke (DoD-P1-02) ==="
  bash "$SCRIPT_DIR/test_gpu_passthrough.sh"

  log "=== step 4/4: Isaac Sim image pull (DoD-P1-03) ==="
  bash "$SCRIPT_DIR/pull_isaac.sh"

  export CV_STEP=provision
  log "ALL STEPS COMPLETE — host provisioned (driver untouched). DoD-P1-02 / DoD-P1-03 gate commands ran above."
}

main "$@"
