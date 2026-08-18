#!/usr/bin/env bash
# install_nvidia_toolkit.sh — install pinned NVIDIA Container Toolkit + wire it into
# the docker runtime. Idempotent. Thin wrapper around the official NVIDIA procedure
# (do-not-reinvent). Privileged steps go through `sudo -n`, covered 1:1 by the drop-in.
# Requires Docker to be installed first (run via provision.sh, or after install_docker.sh).
set -euo pipefail

export CV_STEP=toolkit
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source-path=SCRIPTDIR
# shellcheck source=common.sh
source "$SCRIPT_DIR/common.sh"

require_cmd curl
require_cmd gpg
require_cmd dpkg
require_cmd docker   # must exist before configuring the docker runtime

# Temp dir for downloaded apt key/list. Top-level global so the EXIT trap (which
# fires after main() returns) can still clean it up.
CV_TMPDIR="$(mktemp -d)"
trap 'rm -rf "$CV_TMPDIR"' EXIT

main() {
  local installed
  installed="$(dpkg-query -W -f='${Version}' nvidia-container-toolkit 2>/dev/null || true)"

  if [[ "$installed" == "$CV_NVIDIA_TOOLKIT_VERSION" ]]; then
    log "nvidia-container-toolkit $CV_NVIDIA_TOOLKIT_VERSION already installed (= the preferred pin) — re-asserting docker runtime config"
  elif version_in_set "$installed" "${CV_NVIDIA_TOOLKIT_VERIFIED[@]}"; then
    # D-2 assert mode (see common.sh): an element of the verified SET is accepted as
    # is — no downgrade to the preferred pin. Loud, and it prints the whole quartet.
    log "ASSERT MODE: installed nvidia-container-toolkit $installed is an element of the VERIFIED set — NOT installing, NOT downgrading (preferred pin here would be $CV_NVIDIA_TOOLKIT_VERSION)"
    local p v
    for p in nvidia-container-toolkit-base libnvidia-container-tools libnvidia-container1; do
      v="$(dpkg-query -W -f='${Version}' "$p" 2>/dev/null || true)"
      log "ASSERT MODE:   $p = ${v:-<not installed>}"
    done
  else
    log "configuring NVIDIA Container Toolkit official apt repository (pinned)"
    # Dearmor the key as the user, then place key + list with bounded `install` ops
    # (avoids whitelisting `sudo gpg` / `sudo tee`).
    curl -fsSL https://nvidia.github.io/libnvidia-container/gpgkey \
      | gpg --dearmor > "$CV_TMPDIR/nvidia-container-toolkit-keyring.gpg"
    "${CV_SUDO[@]}" install -D -m 0644 -o root -g root \
      "$CV_TMPDIR/nvidia-container-toolkit-keyring.gpg" \
      /usr/share/keyrings/nvidia-container-toolkit-keyring.gpg

    curl -fsSL https://nvidia.github.io/libnvidia-container/stable/deb/nvidia-container-toolkit.list \
      | sed 's#deb https://#deb [signed-by=/usr/share/keyrings/nvidia-container-toolkit-keyring.gpg] https://#g' \
      > "$CV_TMPDIR/nvidia-container-toolkit.list"
    "${CV_SUDO[@]}" install -D -m 0644 -o root -g root \
      "$CV_TMPDIR/nvidia-container-toolkit.list" \
      /etc/apt/sources.list.d/nvidia-container-toolkit.list

    "${CV_SUDO[@]}" apt-get update

    local p
    for p in nvidia-container-toolkit nvidia-container-toolkit-base \
             libnvidia-container-tools libnvidia-container1; do
      require_apt_pkg_version "$p" "$CV_NVIDIA_TOOLKIT_VERSION"
    done

    log "installing pinned NVIDIA Container Toolkit packages"
    "${CV_SUDO[@]}" apt-get install -y \
      "nvidia-container-toolkit=$CV_NVIDIA_TOOLKIT_VERSION" \
      "nvidia-container-toolkit-base=$CV_NVIDIA_TOOLKIT_VERSION" \
      "libnvidia-container-tools=$CV_NVIDIA_TOOLKIT_VERSION" \
      "libnvidia-container1=$CV_NVIDIA_TOOLKIT_VERSION"
  fi

  # Declarative + idempotent: writes the nvidia runtime into /etc/docker/daemon.json,
  # then restarts docker to pick it up — UNLESS the daemon already reports the runtime,
  # in which case there is nothing to write and no reason to restart (a restart would
  # bounce every running container on someone's host for no gain).
  # The three outcomes are logged DIFFERENTLY on purpose: already-true / could-not-read
  # / done. "Could not read" takes the privileged path — never skip on doubt.
  local runtimes rc=0
  runtimes="$(docker info --format '{{json .Runtimes}}' 2>/dev/null)" || rc=$?
  if (( rc != 0 )) || [[ -z "$runtimes" ]]; then
    warn "could not read the docker runtime list ('docker info' failed) — taking the privileged path rather than assuming"
    log "wiring NVIDIA runtime into docker (nvidia-ctk) + restarting docker"
    "${CV_SUDO[@]}" nvidia-ctk runtime configure --runtime=docker
    "${CV_SUDO[@]}" systemctl restart docker
  elif grep -q '"nvidia"' <<<"$runtimes"; then
    log "SKIP (already true, checked): docker reports the 'nvidia' runtime — no 'nvidia-ctk runtime configure', no 'systemctl restart docker', no sudo"
  else
    log "wiring NVIDIA runtime into docker (nvidia-ctk) + restarting docker"
    # NOTE: /usr/bin/nvidia-ctk was REMOVED from the sudo whitelist by decision
    # 2026-07-07-fu6-sudo-scope-reduction — this branch is an operator action in an
    # interactive terminal, and it is now only REACHED when the runtime is missing.
    "${CV_SUDO[@]}" nvidia-ctk runtime configure --runtime=docker
    "${CV_SUDO[@]}" systemctl restart docker
  fi

  log "NVIDIA Container Toolkit provisioning done"
}

main "$@"
