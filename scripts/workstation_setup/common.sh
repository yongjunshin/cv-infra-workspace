#!/usr/bin/env bash
# shellcheck shell=bash
# shellcheck disable=SC2034  # pins are consumed by the scripts that source this lib
# common.sh — shared version pins + helpers for cv-infra workstation provisioning (M5 / Phase 1).
#
# Sourced by: install_docker.sh, install_nvidia_toolkit.sh, test_gpu_passthrough.sh,
#             pull_isaac.sh, provision.sh. Not meant to be executed on its own.
#
# Reproducibility (CLAUDE.md §2-7; decision 2026-06-24-env-reproducibility-pinning):
#   - ALL version/image pins live HERE — single source of truth, no per-script drift.
#   - A pin that cannot be satisfied is a HARD, LOUD failure (no silent fallback).
#   - Items marked [VERIFY] are concrete *starting* pins whose exact apt patch / image
#     @sha256 digest is locked at the EXECUTION stage against the live registries
#     (this author-stage CPU box cannot reach download.docker.com / nvcr.io). The
#     apt madison guard (require_apt_pkg_version) turns any wrong guess into an
#     actionable failure that lists the versions the repo actually offers.

# Idempotent source guard (readonly pins must not be re-declared on re-source).
# This file is only ever sourced; bare top-level `return` is valid in that context.
[[ -z "${_CV_INFRA_COMMON_LOADED:-}" ]] || return 0
_CV_INFRA_COMMON_LOADED=1

# ---------------------------------------------------------------------------
# PINS — single source of truth
# ---------------------------------------------------------------------------

# Host platform. These scripts target exactly ONE OS (recon: etri6000, Ubuntu 24.04.4);
# provision.sh preflight asserts these and refuses to run elsewhere.
readonly CV_REQUIRE_OS_ID="ubuntu"
readonly CV_REQUIRE_OS_CODENAME="noble"          # Ubuntu 24.04 LTS
readonly CV_REQUIRE_ARCH="amd64"

# NVIDIA driver floor (NFR-DEPLOY-005, DoD-P1-01; R580 branch, Isaac 5.1 floor).
# Provisioning NEVER installs or upgrades the driver — it ASSERTS this floor only.
readonly CV_DRIVER_FLOOR="580.65.06"

# Docker CE (official apt repo). Exact apt version strings. [VERIFY] the exact patch
# at execution via `apt-cache madison docker-ce`; require_apt_pkg_version fails loud.
readonly CV_DOCKER_CE_VERSION="5:28.3.3-1~ubuntu.24.04~noble"          # [VERIFY]
readonly CV_CONTAINERD_VERSION="1.7.27-1"                             # [VERIFY]
readonly CV_DOCKER_BUILDX_VERSION="0.26.1-1~ubuntu.24.04~noble"       # [VERIFY]
readonly CV_DOCKER_COMPOSE_VERSION="2.39.2-1~ubuntu.24.04~noble"      # [VERIFY]

# NVIDIA Container Toolkit (official libnvidia-container apt repo). All four packages
# are pinned to the same version (NVIDIA-recommended). [VERIFY] patch via madison guard.
readonly CV_NVIDIA_TOOLKIT_VERSION="1.17.8-1"                         # [VERIFY]

# GPU-passthrough smoke image (DoD-P1-02). CUDA 12.8+ covers Blackwell; the in-container
# nvidia-smi is injected from the HOST driver, so any recent CUDA base suffices for the
# smoke. Tag-pinned now; lock the @sha256 digest at first pull via CV_CUDA_TEST_DIGEST.
readonly CV_CUDA_TEST_IMAGE="nvidia/cuda:12.8.1-base-ubuntu24.04"     # [VERIFY digest]
readonly CV_CUDA_TEST_DIGEST="${CV_CUDA_TEST_DIGEST:-}"              # e.g. sha256:<64hex>

# Isaac Sim base (LOCKED — CLAUDE.md §5, REQ-DEPLOY-005). The 5.1.0 tag IS the locked
# pin; the @sha256 digest is additional hardening locked at first pull (DoD-P1-03).
readonly CV_ISAAC_IMAGE="nvcr.io/nvidia/isaac-sim:5.1.0"
readonly CV_ISAAC_DIGEST="${CV_ISAAC_DIGEST:-}"                      # e.g. sha256:<64hex>

# Isaac host-side cache scaffold (DoD-P1-03 "cache mount dirs"). Lives under $HOME
# (no sudo). The exact in-container mount targets are finalized with the runner image
# (Phase 2; 5.1.0 cache layout = [VERIFY], M5 §3.6 / R2).
readonly CV_ISAAC_CACHE_ROOT="${CV_ISAAC_CACHE_ROOT:-$HOME/docker/isaac-sim}"

# ---------------------------------------------------------------------------
# HELPERS
# ---------------------------------------------------------------------------

# Non-interactive sudo. The /etc/sudoers.d/cv-infra NOPASSWD drop-in authorizes a
# fixed binary set; `-n` makes any UN-authorized sudo call fail FAST and LOUD instead
# of hanging on a password prompt (G-06: no TTY in non-interactive SSH / agent context).
readonly CV_SUDO=(sudo -n)

log()  { printf '[cv-infra][%s] %s\n' "${CV_STEP:-provision}" "$*"; }
warn() { printf '[cv-infra][%s][WARN] %s\n' "${CV_STEP:-provision}" "$*" >&2; }
err()  { printf '[cv-infra][%s][ERROR] %s\n' "${CV_STEP:-provision}" "$*" >&2; }
die()  { err "$*"; exit 1; }

# Fail loud if a required host tool is absent (avoids unpinned auto-installs of base tools).
require_cmd() {
  command -v "$1" >/dev/null 2>&1 || die \
    "Required host tool missing: '$1'. Install it and re-run (see scripts/workstation_setup/README.md → Prerequisites)."
}

# Fail loud if a pinned apt version is not offered by the configured repositories
# (reproducibility: refuse to drift to a different version).
require_apt_pkg_version() {
  local pkg="$1" want="$2"
  if ! apt-cache madison "$pkg" 2>/dev/null | awk '{print $3}' | grep -qxF "$want"; then
    err "Pinned version not available in apt repo: ${pkg}=${want}"
    err "Versions the repo currently offers for '${pkg}':"
    apt-cache madison "$pkg" 2>/dev/null | awk '{print "    " $3}' >&2 || true
    die "Refusing to install a different/unpinned version. Lock common.sh to an offered version and re-run."
  fi
}
