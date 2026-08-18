#!/usr/bin/env bash
# install_docker.sh — install pinned Docker CE from the official apt repo. Idempotent.
# Thin wrapper around the official Docker apt procedure (do-not-reinvent). Privileged
# steps go through `sudo -n` and are covered 1:1 by /etc/sudoers.d/cv-infra — but each
# one is SKIPPED when a read-only check shows its result already holds, so a host that
# is already correct needs no privilege at all (see provision.sh header).
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
  require_supported_codename   # the codename pin table above must have a row for this host

  local installed
  installed="$(dpkg-query -W -f='${Version}' docker-ce 2>/dev/null || true)"

  if [[ "$installed" == "$CV_DOCKER_CE_VERSION" ]]; then
    log "docker-ce $CV_DOCKER_CE_VERSION already installed (= the preferred pin) — (re)asserting group + service only"
  elif version_in_set "$installed" "${CV_DOCKER_CE_VERIFIED[@]}"; then
    # D-2 assert mode: an already-working host is NOT dragged down to the preferred
    # pin. Loud on purpose — the accepted stack must be visible in the log, because
    # only the docker-ce string was compared and the companions ride along.
    log "ASSERT MODE: installed docker-ce $installed is an element of the VERIFIED set — NOT installing, NOT downgrading (preferred pin here would be $CV_DOCKER_CE_VERSION)"
    log "ASSERT MODE: companion packages ride along UNCOMPARED — recording what is actually installed:"
    local p v
    for p in docker-ce-cli containerd.io docker-buildx-plugin docker-compose-plugin; do
      v="$(dpkg-query -W -f='${Version}' "$p" 2>/dev/null || true)"
      log "ASSERT MODE:   $p = ${v:-<not installed>}"
    done
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

  # --- privileged actions: only if the state they create is NOT already true --------
  # "checked, already true -> skip" and "could not check -> take the privileged path"
  # are DIFFERENT things and the log must say which one happened. Never skip on doubt:
  # a silent skip would hide exactly the defect this step exists to fix.
  if systemctl is-enabled docker >/dev/null 2>&1 && systemctl is-active docker >/dev/null 2>&1; then
    log "SKIP (already true, checked): docker service is enabled AND active — no 'systemctl enable --now docker', no sudo"
  else
    log "enabling docker service (idempotent)"
    "${CV_SUDO[@]}" systemctl enable --now docker
  fi

  # Group membership becomes effective on the NEXT login; a session that does not have
  # it yet falls back to `sudo -n docker` in the passthrough/pull steps (cv_docker).
  local me; me="$(id -un)"
  if id -nG "$me" 2>/dev/null | tr ' ' '\n' | grep -qxF docker; then
    log "SKIP (already true, checked): '$me' is already in the docker group — no 'usermod -aG', no sudo"
  else
    log "adding '$me' to the docker group (effective next login)"
    # NOTE: /usr/sbin/usermod was REMOVED from the sudo whitelist by decision
    # 2026-07-07-fu6-sudo-scope-reduction, so this branch fails loudly by design and
    # is an operator action in an interactive terminal. It is now only REACHED when
    # the group is genuinely missing.
    "${CV_SUDO[@]}" usermod -aG docker "$me"
  fi

  log "Docker CE provisioning done"
}

main "$@"
