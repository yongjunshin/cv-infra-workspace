#!/usr/bin/env bash
# test_gpu_passthrough.sh — DoD-P1-02 gate command, emitted verbatim:
#   docker run --rm --gpus all <pinned CUDA base> nvidia-smi  -> exit 0
# Proves the host driver + NVIDIA Container Toolkit pass the GPU through to a
# container WITHOUT REQUIRING a host CUDA / Isaac install.
#
# ⚠ It does NOT prove the host has none. Measured 2026-08-19 (p5c17 QA): BOTH
# GPU hosts this project has ever deployed to ship a host CUDA toolkit — one of
# them is recorded in realign_driver_r580.sh's untouched-package list. So an
# absence claim has never been true here, and NFR-DEPLOY-005 does not rest on
# one. What it rests on is NON-USE: the deploy plane holds zero host-toolkit
# references, the runner's 8 mounts are all cv-infra paths, and the container's
# CUDA major differs from the host's. Evidence + the exact versions:
# agent-comms/findings/2026-08-19-p5c17.md §12-6.
#
# (This comment is itself scanned by tests/negative/
# test_deployment_identity_hardcoding.py::test_deploy_plane_requires_no_host_cuda_or_isaac_install
# — spelling the host paths out here trips that guard, correctly. Cite the
# evidence file instead; do not widen the guard to admit prose.)
#
# Read-only smoke (no state change beyond pulling the pinned test image).
set -euo pipefail

export CV_STEP=gpu-test
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source-path=SCRIPTDIR
# shellcheck source=common.sh
source "$SCRIPT_DIR/common.sh"

require_cmd docker

main() {
  local img="$CV_CUDA_TEST_IMAGE"
  if [[ -n "$CV_CUDA_TEST_DIGEST" ]]; then
    img="${CV_CUDA_TEST_IMAGE%:*}@${CV_CUDA_TEST_DIGEST}"
  else
    warn "CUDA test image digest not locked yet — using tag pin '$CV_CUDA_TEST_IMAGE'."
    warn "Lock CV_CUDA_TEST_DIGEST after first pull for full reproducibility (see README → Locking digests)."
  fi

  log "DoD-P1-02 gate -> docker run --rm --gpus all $img nvidia-smi"
  # cv_docker: plain `docker` when this session already reaches the daemon, else the
  # `sudo -n docker` channel (e.g. an SSH session where the group is not yet effective).
  if cv_docker run --rm --gpus all "$img" nvidia-smi; then
    log "GPU passthrough PASS (exit 0) — DoD-P1-02 command succeeded"
  else
    die "GPU passthrough FAILED: '--gpus all' container could not run nvidia-smi. Check toolkit runtime config (install_nvidia_toolkit.sh) and driver."
  fi
}

main "$@"
