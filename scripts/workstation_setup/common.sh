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

# Host platform. Supported OS is a SET, not one machine's distro (decision
# 2026-08-19-p5c17-os-set-pin-set-and-host-purity, D-1). Everything that actually
# DEPENDS on the codename is the three apt version strings below, so supporting a
# release = adding a row to that table + a member here; consumers are untouched.
# provision.sh preflight asserts membership and refuses to run outside the set.
readonly CV_REQUIRE_OS_ID="ubuntu"
readonly CV_SUPPORTED_OS_CODENAMES=(noble jammy)  # Ubuntu 24.04 LTS · 22.04 LTS
readonly CV_REQUIRE_ARCH="amd64"

# The LIVE host codename ("" if /etc/os-release does not state one). Read HERE because
# the pin table below keys on it; the ASSERT that it is a supported member stays in
# provision.sh preflight (assert ORDER is load-bearing) via require_supported_codename.
# Guarded on purpose: this lib is also sourced by scripts that run INSIDE containers
# (scripts/consent/*, scripts/measure/*) under `set -euo pipefail`, where an assignment
# from a failing command substitution kills the calling script with no message at all.
# An unreadable/odd /etc/os-release must degrade to "" here and be reported by the
# codename assert, not abort a consent check.
CV_HOST_CODENAME=""
if [[ -r /etc/os-release ]]; then
  CV_HOST_CODENAME="$(. /etc/os-release && printf '%s' "${VERSION_CODENAME:-}")" \
    || CV_HOST_CODENAME=""
fi
readonly CV_HOST_CODENAME

# NVIDIA driver floor (NFR-DEPLOY-005, DoD-P1-01; R580 branch, Isaac 5.1 floor).
# Provisioning NEVER installs or upgrades the driver — it ASSERTS this floor only.
readonly CV_DRIVER_FLOOR="580.65.06"

# Docker CE (official apt repo) — the PREFERRED pin, i.e. what we INSTALL when we
# install. Three of the five strings embed the distro codename, so they are a TABLE
# keyed by the live codename (D-1). Same upstream versions on both rows: measured
# 2026-08-19 with `apt-cache madison` on the jammy host, the noble row's versions are
# offered for jammy too — the barrier was the suffix, never availability.
case "$CV_HOST_CODENAME" in
  noble)  # confirmed 2026-06-26 on etri6000 against the live download.docker.com repo
    _cv_docker_ce="5:28.3.3-1~ubuntu.24.04~noble"
    _cv_buildx="0.26.1-1~ubuntu.24.04~noble"
    _cv_compose="2.39.2-1~ubuntu.24.04~noble"
    ;;
  jammy)  # offer confirmed 2026-08-19 (madison, CEO local host); NOT yet installed by us
    _cv_docker_ce="5:28.3.3-1~ubuntu.22.04~jammy"
    _cv_buildx="0.26.1-1~ubuntu.22.04~jammy"
    _cv_compose="2.39.2-1~ubuntu.22.04~jammy"
    ;;
  *)      # unsupported/unknown host: leave the pins EMPTY and let the codename assert
    _cv_docker_ce=""; _cv_buildx=""; _cv_compose="" ;;
esac
readonly CV_DOCKER_CE_VERSION="$_cv_docker_ce"
readonly CV_DOCKER_BUILDX_VERSION="$_cv_buildx"
readonly CV_DOCKER_COMPOSE_VERSION="$_cv_compose"
unset _cv_docker_ce _cv_buildx _cv_compose

# containerd.io carries NO codename suffix AT THIS VERSION (measured: the same
# `1.7.27-1` string is offered on both noble and jammy), so it stays a scalar — a
# one-row table would be over-engineering. ★ That property belongs to the VERSION,
# not to the package: the current containerd.io line on this repo is
# `2.3.3-1~ubuntu.22.04~jammy` (measured 2026-08-19), i.e. it DOES carry a suffix.
# Moving this pin into the 2.x era means moving it into the table above.
readonly CV_CONTAINERD_VERSION="1.7.27-1"                             # confirmed 2026-06-26

# NVIDIA Container Toolkit (official libnvidia-container apt repo). All four packages
# are pinned to the same version (NVIDIA-recommended). CONFIRMED 2026-06-26 via madison
# guard. Codename-independent (measured on both hosts).
readonly CV_NVIDIA_TOOLKIT_VERSION="1.17.8-1"                         # confirmed 2026-06-26

# --- VERIFIED version SETS (assert mode) -----------------------------------------
# Decision 2026-08-19-p5c17-os-set-pin-set-and-host-purity, D-2: provisioning does NOT
# drag an already-working host DOWN to the preferred pin. If the INSTALLED version is
# an element of the set below, the install step is SKIPPED and only asserted (loudly).
# Anything else -> the preferred pin above is installed (previous behaviour).
#
# ★ These are SETS, never floors. `dpkg --compare-versions ... ge` would admit every
# future version, which is exactly the shape G-12 caught (a floor-only "R580+" assert
# admitted R595 and Isaac's RTX renderer segfaulted). A set has an upper bound by
# construction, and every element carries the evidence that put it there (G-24).
# Keep the sets SMALL — each element is a stack we promise still works.
#
# The membership KEY is the docker-ce version. The companion packages (containerd.io /
# buildx / compose) ride along and are LOGGED, not compared: enumerating 5-tuples buys
# nothing, because what makes an element trustworthy is a cycle that ran green on the
# whole stack as installed. install_docker.sh prints the companions in assert mode so
# an audit can see exactly what was accepted.
readonly CV_DOCKER_CE_VERIFIED=(
  "5:28.3.3-1~ubuntu.24.04~noble"   # verified: etri6000, every GPU cycle P1..p5c16 (evidence: implementation-plan/nfr-measurement-notes.md)
  # VERIFIED 2026-08-19 by p5c17 T4: the full C-2 walkthrough (①provision → ②consent →
  # ③compose up --build → ④selftest exit 0) ran green on CEO local RTX 4080 / ubuntu jammy
  # with this STACK AS INSTALLED — docker-ce 5:29.7.2-1~ubuntu.22.04~jammy · docker-ce-cli
  # 5:29.7.2-1~ubuntu.22.04~jammy · containerd.io 2.3.3-1~ubuntu.22.04~jammy · docker-buildx-plugin
  # 0.36.1-1~ubuntu.22.04~jammy · docker-compose-plugin 5.4.0-1~ubuntu.22.04~jammy (Compose v5,
  # three majors past the preferred pin — the project's FIRST Compose v5 run, no schema change
  # needed). Evidence: agent-comms/reports/deployment-2026-08-19-p5c17-T4-c2-walkthrough.md;
  # raw logs on that host at ~/cv-infra-p5c17-t4-evidence/.
  "5:29.7.2-1~ubuntu.22.04~jammy"
)
readonly CV_NVIDIA_TOOLKIT_VERIFIED=(
  "1.17.8-1"                        # verified: etri6000, every GPU cycle P1..p5c16 (evidence: implementation-plan/nfr-measurement-notes.md)
  # VERIFIED 2026-08-19 by p5c17 T4 (same walkthrough, same host): nvidia-container-toolkit
  # 1.19.1-1 with -base / libnvidia-container-tools / libnvidia-container1 all 1.19.1-1,
  # driver 580.178.04 open KMD. GPU passthrough smoke exit 0 and a live Isaac Sim 5.1.0 job
  # (runner + stub SUT on a per-job bridge) ran to verdict=pass on it.
  # Evidence: agent-comms/reports/deployment-2026-08-19-p5c17-T4-c2-walkthrough.md.
  "1.19.1-1"
)

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

# Is the live host codename one of the supported ones? (D-1 — the assert itself lives
# in provision.sh preflight; install_docker.sh calls it too, since it can run alone.)
require_supported_codename() {
  local c
  for c in "${CV_SUPPORTED_OS_CODENAMES[@]}"; do
    [[ "$c" == "$CV_HOST_CODENAME" ]] && return 0
  done
  die "Unsupported codename '${CV_HOST_CODENAME:-?}' — these scripts support: ${CV_SUPPORTED_OS_CODENAMES[*]}. Adding one = a row in the codename pin table of common.sh (the apt versions must be OFFERED there — check with 'apt-cache madison docker-ce')."
}

# Exact-string membership in a verified version SET (D-2). NOT a floor comparison:
# `dpkg --compare-versions ... ge` admits every future version and that is the exact
# shape G-12 caught. Callers pass the set expanded: version_in_set "$v" "${SET[@]}".
version_in_set() {
  local want="$1" v
  shift
  for v in "$@"; do
    [[ "$v" == "$want" ]] && return 0
  done
  return 1
}

# Run docker through the channel this session can actually use, resolved ONCE by
# OBSERVATION (not by assumption): plain `docker` when the daemon is already reachable
# (docker group effective), else the `sudo -n docker` channel. A host where the
# operator is already in the docker group must not be forced to install a NOPASSWD
# drop-in just to run a read-only smoke.
cv_docker() {
  if [[ -z "${_CV_DOCKER_MODE:-}" ]]; then
    if docker info >/dev/null 2>&1; then
      _CV_DOCKER_MODE=plain
      log "docker is reachable WITHOUT sudo (verified: 'docker info' succeeded) — using plain 'docker'"
    else
      _CV_DOCKER_MODE=sudo
      log "docker is NOT reachable without sudo ('docker info' failed) — using the '${CV_SUDO[*]} docker' channel (needs the NOPASSWD drop-in)"
    fi
  fi
  if [[ "$_CV_DOCKER_MODE" == "plain" ]]; then
    docker "$@"
  else
    "${CV_SUDO[@]}" docker "$@"
  fi
}

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

# ---------------------------------------------------------------------------
# --- P1-07 self-hosted runner pins (M8) ---
# ---------------------------------------------------------------------------
# Consumed by register_gh_runner.sh only (DoD-P1-07; decision
# 2026-07-03-self-hosted-runner-policy — binding). Appended by M8/DX; the M5
# provisioning pins above are untouched.
#
# Pin refresh is ACCEPTED MAINTENANCE: GitHub may refuse jobs from runners more
# than ~30 days behind the minimum version (self-update is disabled via
# --disableupdate). Refresh = re-resolve the latest release
# (`gh api repos/actions/runner/releases/latest`), bump the two pins below, and
# re-run register_gh_runner.sh (idempotent). See README → runner pin refresh.
readonly CV_GH_RUNNER_VERSION="2.335.1"       # pinned 2026-07-03 (then-latest official release)
# Official linux-x64 tarball sha256 published in the v2.335.1 release notes
# (`<!-- BEGIN SHA linux-x64 -->` marker) — an UPSTREAM-stated checksum, not a
# first-download measurement. Mismatch at install time = hard die.
readonly CV_GH_RUNNER_TARBALL_SHA256="4ef2f25285f0ae4477f1fe1e346db76d2f3ebf03824e2ddd1973a2819bf6c8cf"
# Registration TARGET params (decision 2026-07-21-e2e-user-runner-provisioning:
# a SECOND same-machine repo-level runner for cv-infra-user). Env-overridable,
# defaulting to the original WORKSPACE runner — a plain no-env re-run is
# byte-identical to the pre-parameterization behavior (idempotent no-op on the
# existing runner). The version/sha256 pins above and the label set below are
# deliberately NOT parameters: every runner on this host runs the same pinned
# binary with the same `cv-infra-gpu` label (decision 2026-07-03 §2/§3 hardening
# applies identically to each registration).
readonly CV_GH_RUNNER_REPO_URL="${CV_GH_RUNNER_REPO_URL:-https://github.com/yongjunshin/cv-infra-workspace}"  # repo-level target (decision §1)
# Runner name = LIVE HOST identity + role, derived at run time (DoD-P5-09: no machine
# hardcoded into the deployment). Provisioning a second host used to silently propose
# the first host's runner name; GitHub runner names are per-repo unique, so that is a
# portability defect, not cosmetics. On the original workstation this is byte-identical
# to the previous literal (`hostname` there is measured as `etri6000` — decision
# 2026-07-07-workstation-access-ssh-first-alpacon-fallback §"동일 호스트 실측 확증"),
# and register_gh_runner.sh skips an already-configured runner anyway (.runner marker).
readonly CV_GH_RUNNER_NAME="${CV_GH_RUNNER_NAME:-$(hostname -s)-cv-infra}"
readonly CV_GH_RUNNER_LABELS="cv-infra-gpu"   # effective label set: [self-hosted, Linux, X64, cv-infra-gpu]
readonly CV_GH_RUNNER_HOME="${CV_GH_RUNNER_HOME:-$HOME/cv-infra-gh-runner}"
readonly CV_GH_RUNNER_SERVICE="${CV_GH_RUNNER_SERVICE:-cv-infra-gh-runner}"

# ---------------------------------------------------------------------------
# --- driver R580 realignment pins (M5) ---
# ---------------------------------------------------------------------------
# Decision 2026-07-03-driver-r580-realignment (binding): Isaac Sim 5.1.0
# (kit 107.3.3) deterministically segfaults in the RTX renderer on the R595
# branch (known NVIDIA issue, no workaround; certified branch = R580 LTSB).
# The provisioning preflight therefore asserts BRANCH == CV_DRIVER_BRANCH in
# addition to the CV_DRIVER_FLOOR above — the floor-only assert is what let
# 595.71.05 through. Consumed by realign_driver_r580.sh and provision.sh.
readonly CV_DRIVER_BRANCH="580"                                # driver major MUST equal this (branch floor AND ceiling)
readonly CV_DRIVER_TARGET_STAGE1="580.159.03-0ubuntu0.24.04.1" # Ubuntu noble archive (prebuilt signed per-kernel open modules); confirmed 2026-07-03
readonly CV_DRIVER_TARGET_STAGE2="580.65.06-0ubuntu1"          # NVIDIA CUDA ubuntu2404 repo (DKMS) — fallback ONLY if stage 1 still crashes RTX

# ---------------------------------------------------------------------------
# --- P1-04/05 isaac smoke + DDS pins (M2) ---
# ---------------------------------------------------------------------------
# Sourced by scripts/isaac_smoke/{run_smoke.sh,run_dds_handshake.sh}. Same rules as
# above: pins live here only; env-overridable defaults follow the CV_ISAAC_DIGEST
# 2-stage pattern (pull by exact tag once -> lock @sha256 here -> reference by digest).

# ros:jazzy DDS-handshake peer image (DoD-P1-05). Exact tag pin; @sha256 digest
# LOCKED 2026-07-03 from the first pull's RepoDigests on etri6000 (manifest-list
# digest; same 2-stage pattern as CV_ISAAC_DIGEST). Env-overridable for a re-lock.
readonly CV_ROS_JAZZY_IMAGE="ros:jazzy"
readonly CV_ROS_JAZZY_DIGEST="${CV_ROS_JAZZY_DIGEST:-sha256:31daab66eef9139933379fb67159449944f4e2dcf2e22c2d12cc715f29873e0f}"

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

# ---------------------------------------------------------------------------
# --- P5 EULA consent gate (M5 §3.7) ---
# ---------------------------------------------------------------------------
# Sourced by scripts/consent/{accept_eula.sh,check_consent.sh}. Same rule as above:
# the paths/URLs live HERE only, so the writer and the gate can never drift apart.
#
# The consent RECORD (identity + timestamp, REQ-DEPLOY-010) is the host-side audit
# + gate artifact, deliberately SEPARATE from the runtime .env: the record answers
# "did an operator consent, who, when", while the runtime boot gate's single source
# of truth stays the env the runner receives (M5 §3.7 D-O/F7). It lives under $HOME
# (no sudo, survives re-deploys of the repo checkout).
readonly CV_CONSENT_RECORD="${CV_CONSENT_RECORD:-$HOME/.cv-infra/eula-consent.json}"
readonly CV_CONSENT_RECORD_SCHEMA="cv-infra/eula-consent/v1"

# What the operator is asked to accept. The license URL is the one the P1 smoke
# wrapper already shows (single wording across the deployment).
readonly CV_EULA_URL="https://www.nvidia.com/en-us/agreements/enterprise-software/isaac-sim-additional-software-and-materials-license/"
# NVIDIA privacy policy (stable official URL). The Omniverse/Kit data-collection
# notice itself is presented by the Isaac Sim container at boot; the deployment's
# job is to make the operator's decision explicit and recorded, not to restate it.
readonly CV_PRIVACY_URL="https://www.nvidia.com/en-us/about-nvidia/privacy-policy/"
