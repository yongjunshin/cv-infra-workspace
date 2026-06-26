#!/usr/bin/env bash
# install_docker.sh — install pinned Docker CE from the official apt repo. Idempotent.
# Thin wrapper around the official Docker apt procedure (do-not-reinvent). Privileged
# steps go through `sudo -n` and are covered 1:1 by /etc/sudoers.d/cv-infra.
set -euo pipefail

export CV_STEP=docker
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source-path=SCRIPTDIR
# shellcheck source=common.sh
source "$SCRIPT_DIR/common.sh"

require_cmd curl
require_cmd dpkg

# Temp dir for downloaded apt key/list. Top-level global so the EXIT trap (which
# fires after main() returns) can still clean it up.
CV_TMPDIR="$(mktemp -d)"
trap 'rm -rf "$CV_TMPDIR"' EXIT

main() {
  if dpkg-query -W -f='${Version}' docker-ce 2>/dev/null | grep -qxF "$CV_DOCKER_CE_VERSION"; then
    log "docker-ce $CV_DOCKER_CE_VERSION already installed — (re)asserting group + service only"
  else
    log "configuring Docker CE official apt repository (pinned)"
    # Download the (ASCII-armored) key as the user, then place it with one bounded
    # privileged op (install) — avoids whitelisting `sudo curl`.
    curl -fsSL https://download.docker.com/linux/ubuntu/gpg -o "$CV_TMPDIR/docker.asc"
    "${CV_SUDO[@]}" install -D -m 0644 -o root -g root "$CV_TMPDIR/docker.asc" /etc/apt/keyrings/docker.asc

    local codename arch
    # shellcheck disable=SC1091
    codename="$(. /etc/os-release && echo "$VERSION_CODENAME")"
    arch="$(dpkg --print-architecture)"
    printf 'deb [arch=%s signed-by=/etc/apt/keyrings/docker.asc] https://download.docker.com/linux/ubuntu %s stable\n' \
      "$arch" "$codename" > "$CV_TMPDIR/docker.list"
    "${CV_SUDO[@]}" install -D -m 0644 -o root -g root "$CV_TMPDIR/docker.list" /etc/apt/sources.list.d/docker.list

    "${CV_SUDO[@]}" apt-get update

    # Reproducibility gate: every pin must be on offer, else fail loud.
    require_apt_pkg_version docker-ce              "$CV_DOCKER_CE_VERSION"
    require_apt_pkg_version docker-ce-cli          "$CV_DOCKER_CE_VERSION"
    require_apt_pkg_version containerd.io          "$CV_CONTAINERD_VERSION"
    require_apt_pkg_version docker-buildx-plugin   "$CV_DOCKER_BUILDX_VERSION"
    require_apt_pkg_version docker-compose-plugin  "$CV_DOCKER_COMPOSE_VERSION"

    log "installing pinned Docker CE packages"
    "${CV_SUDO[@]}" apt-get install -y \
      "docker-ce=$CV_DOCKER_CE_VERSION" \
      "docker-ce-cli=$CV_DOCKER_CE_VERSION" \
      "containerd.io=$CV_CONTAINERD_VERSION" \
      "docker-buildx-plugin=$CV_DOCKER_BUILDX_VERSION" \
      "docker-compose-plugin=$CV_DOCKER_COMPOSE_VERSION"
  fi

  log "enabling docker service (idempotent)"
  "${CV_SUDO[@]}" systemctl enable --now docker

  # Group membership becomes effective on the NEXT login; this session still uses
  # `sudo -n docker` for the passthrough test and isaac pull (see test/pull scripts).
  local me; me="$(id -un)"
  log "adding '$me' to the docker group (effective next login)"
  "${CV_SUDO[@]}" usermod -aG docker "$me"

  log "Docker CE provisioning done"
}

main "$@"
