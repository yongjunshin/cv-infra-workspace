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
#   - The apt patch versions + image @sha256 digests below were CONFIRMED/LOCKED at the
#     EXECUTION stage (2026-06-26, host etri6000) against the live download.docker.com /
#     nvidia.github.io / nvcr.io registries — every author-stage guess matched exactly,
#     no correction was needed. The apt madison guard (require_apt_pkg_version) still
#     turns any wrong/unavailable pin into an actionable failure (listing the offered
#     versions) when re-provisioning a different host.

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

# Docker CE (official apt repo). Exact apt version strings, CONFIRMED 2026-06-26 against
# the live download.docker.com noble repo via the madison guard (installed cleanly, no drift).
readonly CV_DOCKER_CE_VERSION="5:28.3.3-1~ubuntu.24.04~noble"          # confirmed 2026-06-26
readonly CV_CONTAINERD_VERSION="1.7.27-1"                             # confirmed 2026-06-26
readonly CV_DOCKER_BUILDX_VERSION="0.26.1-1~ubuntu.24.04~noble"       # confirmed 2026-06-26
readonly CV_DOCKER_COMPOSE_VERSION="2.39.2-1~ubuntu.24.04~noble"      # confirmed 2026-06-26

# NVIDIA Container Toolkit (official libnvidia-container apt repo). All four packages
# are pinned to the same version (NVIDIA-recommended). CONFIRMED 2026-06-26 via madison guard.
readonly CV_NVIDIA_TOOLKIT_VERSION="1.17.8-1"                         # confirmed 2026-06-26

# GPU-passthrough smoke image (DoD-P1-02). CUDA 12.8+ covers Blackwell; the in-container
# nvidia-smi is injected from the HOST driver, so any recent CUDA base suffices for the
# smoke. Tag + @sha256 digest LOCKED 2026-06-26 at first pull (RepoDigest of the manifest
# list; resolves to the amd64 platform on this host). Env-overridable for a re-lock.
readonly CV_CUDA_TEST_IMAGE="nvidia/cuda:12.8.1-base-ubuntu24.04"
readonly CV_CUDA_TEST_DIGEST="${CV_CUDA_TEST_DIGEST:-sha256:133c78a0575303be34164d0b90137a042172bdf60696af01a3c424ab402d86e2}"

# Isaac Sim base (LOCKED — CLAUDE.md §5, REQ-DEPLOY-005). The 5.1.0 tag IS the locked
# pin; the @sha256 digest is additional hardening LOCKED 2026-06-26 at first anonymous
# NGC pull (DoD-P1-03; RepoDigest of the manifest list). Env-overridable for a re-lock.
readonly CV_ISAAC_IMAGE="nvcr.io/nvidia/isaac-sim:5.1.0"
readonly CV_ISAAC_DIGEST="${CV_ISAAC_DIGEST:-sha256:f3563cb2ba0c18af0b2fb321360dcb73a917b899f879e3213623d6bee484fa54}"

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

# --- P1-04/05 isaac smoke + DDS pins (M2) ---
# Sourced by scripts/isaac_smoke/{run_smoke.sh,run_dds_handshake.sh}. Same rules as
# above: pins live here only; env-overridable defaults follow the CV_ISAAC_DIGEST
# 2-stage pattern (pull by exact tag once -> lock @sha256 here -> reference by digest).

# ros:jazzy DDS-handshake peer image (DoD-P1-05). Exact tag pin; digest locked after
# the first pull on the workstation (run_dds_handshake.sh prints the RepoDigest).
readonly CV_ROS_JAZZY_IMAGE="ros:jazzy"
readonly CV_ROS_JAZZY_DIGEST="${CV_ROS_JAZZY_DIGEST:-}"      # lock after first pull

# Smoke/handshake runtime knobs (DoD-P1-04/05).
readonly CV_SMOKE_NET="${CV_SMOKE_NET:-cv-smoke-net}"        # dedicated bridge net (non-host, R8)
readonly CV_SMOKE_DOMAIN_ID="${CV_SMOKE_DOMAIN_ID:-42}"      # fixed ROS_DOMAIN_ID (safe range 0..101)
# Kit/Isaac needs a real /dev/shm (docker default 64m is too small for Kit workloads).
# This is separate from the DDS SHM *transport*, which stays disabled via the UDPv4
# profile (R8). Value [VERIFY]: measured in-run usage is recorded by run_smoke.sh.
readonly CV_SMOKE_SHM_SIZE="${CV_SMOKE_SHM_SIZE:-1g}"
readonly CV_SMOKE_TIMEOUT_S="${CV_SMOKE_TIMEOUT_S:-2400}"    # smoke wall guard (cold shader compile)
readonly CV_HANDSHAKE_BOOT_TIMEOUT_S="${CV_HANDSHAKE_BOOT_TIMEOUT_S:-1200}"
readonly CV_HANDSHAKE_WAIT_S="${CV_HANDSHAKE_WAIT_S:-240}"   # in-sim wall wait for reverse /cmd_vel
readonly CV_HANDSHAKE_ECHO_TIMEOUT_S="${CV_HANDSHAKE_ECHO_TIMEOUT_S:-60}"
