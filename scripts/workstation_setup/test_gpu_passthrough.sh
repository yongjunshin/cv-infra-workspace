#!/usr/bin/env bash
# test_gpu_passthrough.sh — DoD-P1-02 gate command, emitted verbatim:
#   docker run --rm --gpus all <pinned CUDA base> nvidia-smi  -> exit 0
# Proves the host driver + NVIDIA Container Toolkit pass the GPU through to a
# container, with NO host CUDA / Isaac installed. Read-only smoke (no state change
# beyond pulling the pinned test image).
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
  # `sudo -n docker`: the docker group is not yet effective in this SSH session.
  if "${CV_SUDO[@]}" docker run --rm --gpus all "$img" nvidia-smi; then
    log "GPU passthrough PASS (exit 0) — DoD-P1-02 command succeeded"
  else
    die "GPU passthrough FAILED: '--gpus all' container could not run nvidia-smi. Check toolkit runtime config (install_nvidia_toolkit.sh) and driver."
  fi
}

main "$@"
