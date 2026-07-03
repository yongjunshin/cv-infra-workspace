#!/usr/bin/env bash
# realign_driver_r580.sh — realign the workstation NVIDIA driver R595 -> R580 (M5).
#
# Executes decision 2026-07-03-driver-r580-realignment (binding): Isaac Sim 5.1.0
# (kit 107.3.3) deterministically segfaults in the RTX renderer on R595; the
# certified branch is R580 (LTSB). Stages:
#
#   stage 1 (default)  Ubuntu archive 580.159.03 — prebuilt SIGNED per-kernel
#                      open modules, installed for EVERY installed generic kernel
#                      (GRUB_DEFAULT=0 boots the NEWEST installed kernel, which
#                      may not be the currently running one). The 595-server set
#                      is removed in the SAME apt transaction. Then the apt pin
#                      file is deployed (holds 580 + hard-blocks R595/R610).
#   stage 2 (--stage2) ONLY if stage 1 still crashes RTX: NVIDIA CUDA ubuntu2404
#                      repo 580.65.06 (the exact Isaac-5.1.0-tested build) via
#                      DKMS (Secure Boot is off on etri6000 — unsigned OK).
#
# The reboot is NEVER implicit: pass --reboot to reboot after a successful run.
# Idempotent: an already-aligned host logs a no-op and exits 0.
#
# Deliberately untouched: Docker / NVIDIA Container Toolkit / daemon config, the
# isaac image, host CUDA toolkit 12.0 packages (libcudart12 etc.), sudoers,
# linux-signatures-nvidia-* and kernel packages (apt dependency resolution owns
# those — never purge them by hand).
#
# KNOWN HOST DEBT (discovered 2026-07-03): etri6000 carries an orphan (non-dpkg)
# NVIDIA 595.71.05 userspace from a runfile install (/usr/bin/nvidia-uninstall
# exists; e.g. libnvidia-glcore.so.595.71.05 is owned by no package). These are
# NOT inert: ldconfig points soname links (libGLX_nvidia.so.0, libnvcuvid.so.1,
# libnvoptix.so.1, ...) at the HIGHER-versioned orphans, and nvidia-container-cli
# — which selects host libs by exact driver version — then silently DROPS those
# libs from every container: Isaac boots GPU-less (no Vulkan ICD lib; observed
# 2026-07-03). The sudo whitelist has no file-removal path, so this script cannot
# fix that itself: it DETECTS the hijack (assert_soname_resolution) and fails
# loud with the README cleanup procedure (operator, own terminal).
set -euo pipefail

export CV_STEP=driver-r580
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source-path=SCRIPTDIR
# shellcheck source=common.sh
source "$SCRIPT_DIR/common.sh"

PIN_SRC="$SCRIPT_DIR/apt-preferences-cv-infra-nvidia-r580"
PIN_DST="/etc/apt/preferences.d/cv-infra-nvidia-r580"

# NVIDIA CUDA repo (stage 2 only). Key placed as user-download + `sudo -n install`
# (same pattern as install_docker.sh — no `sudo curl`).
CUDA_REPO_URL="https://developer.download.nvidia.com/compute/cuda/repos/ubuntu2404/x86_64"
CUDA_KEYRING="/usr/share/keyrings/cv-infra-cuda-archive-keyring.gpg"
CUDA_LIST="/etc/apt/sources.list.d/cv-infra-cuda-ubuntu2404.list"
CUDA_REPO_PIN="/etc/apt/preferences.d/cv-infra-cuda-repo"

DO_REBOOT=0
STAGE2=0
for arg in "$@"; do
  case "$arg" in
    --reboot) DO_REBOOT=1 ;;
    --stage2) STAGE2=1 ;;
    *) die "unknown argument: $arg (supported: --reboot --stage2)" ;;
  esac
done

if [[ "$STAGE2" -eq 1 ]]; then
  TARGET_VERSION="$CV_DRIVER_TARGET_STAGE2"
else
  TARGET_VERSION="$CV_DRIVER_TARGET_STAGE1"
fi
TARGET_UPSTREAM="${TARGET_VERSION%%-*}"   # e.g. 580.159.03

# Userspace component set — mirrors what nvidia-container-cli must inject for
# Isaac (compute + GL/RTX/OptiX + encode/decode + cfg/fbc/extra). Same names in
# the Ubuntu archive and the NVIDIA CUDA repo; only versions differ per stage.
USERSPACE_PKGS=(
  libnvidia-compute-580
  libnvidia-gl-580
  libnvidia-decode-580
  libnvidia-encode-580
  libnvidia-extra-580
  libnvidia-cfg1-580
  libnvidia-fbc1-580
  nvidia-utils-580
  nvidia-kernel-common-580
)
FIRMWARE_PKG="nvidia-firmware-580-${TARGET_UPSTREAM}"   # GSP firmware; name embeds the version

# Graphics/video sonames the container toolkit must be able to inject for Isaac.
# nvidia-container-cli resolves each soname via the ldcache and REFUSES any lib
# whose resolved file version differs from the driver version — a stale orphan
# that hijacks a soname silently drops that lib from every container.
GRAPHICS_SONAMES=(
  libGLX_nvidia.so.0 libEGL_nvidia.so.0 libnvoptix.so.1 libnvcuvid.so.1
  libnvidia-encode.so.1 libnvidia-allocator.so.1 libnvidia-cfg.so.1
  libnvidia-ngx.so.1 libnvidia-opticalflow.so.1 libnvidia-fbc.so.1
)
LIBDIR="/usr/lib/x86_64-linux-gnu"

MODINFO="$(command -v modinfo || echo /usr/sbin/modinfo)"

installed_generic_kernels() {
  dpkg-query -W -f '${Package}\n' 'linux-image-[0-9]*-generic' 2>/dev/null \
    | sed 's/^linux-image-//' | sort -V
}

installed_595_pkgs() {
  dpkg-query -W -f '${Package} ${db:Status-Abbrev}\n' '*nvidia*595*' 2>/dev/null \
    | awk '$2 == "ii" {print $1}'
}

# Lists every graphics soname that does NOT resolve to the target version
# (empty output = injection-clean host).
hijacked_sonames() {
  local s t
  for s in "${GRAPHICS_SONAMES[@]}"; do
    t="$(readlink -f "$LIBDIR/$s" 2>/dev/null || true)"
    [[ "$t" == *".so.$TARGET_UPSTREAM" ]] || printf '%s -> %s\n' "$s" "${t:-<missing>}"
  done
}

assert_soname_resolution() {
  local bad
  bad="$(hijacked_sonames)"
  if [[ -n "$bad" ]]; then
    err "graphics sonames do NOT resolve to $TARGET_UPSTREAM — nvidia-container-cli will drop them from containers (Isaac boots GPU-less):"
    printf '%s\n' "$bad" >&2
    err "cause: orphan (non-dpkg) driver libs hijack the ldconfig soname links (runfile leftovers)."
    die "operator cleanup required (own terminal) — README -> 'Driver branch realignment' cleanup; then re-run this script."
  fi
  log "OK: all graphics sonames resolve to $TARGET_UPSTREAM (container injection complete)"
}

aligned() {
  local v k
  v="$(dpkg-query -W -f '${Version}' libnvidia-compute-580 2>/dev/null || true)"
  [[ "$v" == "$TARGET_VERSION" ]] || return 1
  [[ -z "$(installed_595_pkgs)" ]] || return 1
  [[ -f "$PIN_DST" ]] || return 1
  [[ -z "$(hijacked_sonames)" ]] || return 1
  if [[ "$STAGE2" -eq 0 ]]; then
    for k in $(installed_generic_kernels); do
      dpkg-query -W "linux-modules-nvidia-580-open-${k}" >/dev/null 2>&1 || return 1
    done
  else
    dpkg-query -W nvidia-dkms-580-open >/dev/null 2>&1 || return 1
  fi
}

snapshot() {
  log "dpkg snapshot (nvidia + kernel module packages) — rollback reference:"
  dpkg -l | grep -iE 'nvidia|linux-(modules|objects|signatures)' \
    | sed 's/^/[cv-infra][driver-r580][dpkg] /' || true
}

# Simulate the transaction and refuse if apt would remove anything outside the
# allowlist regex (fail loud BEFORE touching the driver).
guard_dry_run() {
  local allowed_removals="$1"; shift
  local sim bad
  if ! sim="$(apt-get install -s --no-install-recommends --allow-downgrades "$@" 2>&1)"; then
    printf '%s\n' "$sim" >&2
    die "apt simulation failed — refusing to touch the driver (see output above)"
  fi
  bad="$(printf '%s\n' "$sim" | awk '/^Remv /{print $2}' | grep -vE "$allowed_removals" || true)"
  if [[ -n "$bad" ]]; then
    printf '%s\n' "$sim" >&2
    die "apt wants to remove packages outside the allowlist: ${bad//$'\n'/ } — refusing"
  fi
  # `|| true`: a converged re-run simulates to zero Inst/Remv lines and a
  # non-matching grep must not kill the script (set -e + pipefail).
  printf '%s\n' "$sim" | grep -E '^(Inst|Remv)' | sed 's/^/[cv-infra][driver-r580][sim] /' || true
}

# Every installed generic kernel must RESOLVE (depmod — what modprobe will load
# at boot) to an nvidia module at the target version. Other nvidia.ko* files that
# are merely shadowed (e.g. the orphan runfile module under kernel/drivers/video/
# on etri6000 — non-dpkg, not removable via the sudo whitelist) only warn.
assert_module_versions() {
  local k ko resolved ver
  for k in $(installed_generic_kernels); do
    ver="$("$MODINFO" -k "$k" nvidia -F version 2>/dev/null || true)"
    resolved="$("$MODINFO" -k "$k" nvidia -F filename 2>/dev/null || true)"
    [[ "$ver" == "$TARGET_UPSTREAM" ]] \
      || die "kernel $k resolves nvidia to '${resolved:-<none>}' version '${ver:-<none>}' (want $TARGET_UPSTREAM)"
    while IFS= read -r ko; do
      [[ -n "$ko" && "$ko" != "$resolved" ]] || continue
      warn "shadowed stale nvidia module left on disk (inert; operator cleanup — see README): $ko ($("$MODINFO" -F version "$ko" 2>/dev/null || echo '?'))"
    done < <(find "/lib/modules/$k" -name 'nvidia.ko*' 2>/dev/null)
  done
  log "OK: every installed generic kernel resolves nvidia to $TARGET_UPSTREAM"
}

deploy_pin() {
  local src="$PIN_SRC"
  [[ -f "$PIN_SRC" ]] || die "pin file source missing: $PIN_SRC"
  if [[ "$STAGE2" -eq 1 ]]; then
    src="$CV_TMPDIR/cv-infra-nvidia-r580"
    sed "s/^Pin: version 580\.159\.03\*/Pin: version ${TARGET_UPSTREAM}*/" "$PIN_SRC" > "$src"
  fi
  "${CV_SUDO[@]}" install -m 0644 -o root -g root "$src" "$PIN_DST"
  log "apt pin deployed -> $PIN_DST; candidate policy now:"
  apt-cache policy libnvidia-compute-580 | sed 's/^/[cv-infra][driver-r580][policy] /'
}

setup_cuda_repo() {
  require_cmd curl
  require_cmd gpg
  log "configuring NVIDIA CUDA ubuntu2404 apt repo (stage 2 fallback source)"
  curl -fsSL "$CUDA_REPO_URL/3bf863cc.pub" | gpg --dearmor > "$CV_TMPDIR/cuda.gpg"
  "${CV_SUDO[@]}" install -D -m 0644 -o root -g root "$CV_TMPDIR/cuda.gpg" "$CUDA_KEYRING"
  printf 'deb [signed-by=%s] %s/ /\n' "$CUDA_KEYRING" "$CUDA_REPO_URL" > "$CV_TMPDIR/cuda.list"
  "${CV_SUDO[@]}" install -D -m 0644 -o root -g root "$CV_TMPDIR/cuda.list" "$CUDA_LIST"
  # Keep the (large) CUDA repo from influencing anything we did not explicitly
  # pin: repo-wide priority 100 (< 500); the 580 set is raised to 1001 by the
  # branch pin file, so ONLY the driver packages are ever taken from it.
  printf 'Package: *\nPin: origin developer.download.nvidia.com\nPin-Priority: 100\n' > "$CV_TMPDIR/cuda.pref"
  "${CV_SUDO[@]}" install -m 0644 -o root -g root "$CV_TMPDIR/cuda.pref" "$CUDA_REPO_PIN"
  "${CV_SUDO[@]}" apt-get update
}

finish() {
  if [[ "$DO_REBOOT" -eq 1 ]]; then
    log "rebooting NOW (approved: decision 2026-07-03-driver-r580-realignment) — the new module loads on boot"
    exec "${CV_SUDO[@]}" systemctl reboot
  fi
  log "DONE — REBOOT REQUIRED to load the $TARGET_UPSTREAM module: sudo -n systemctl reboot"
}

main() {
  require_cmd dpkg
  require_cmd apt-cache
  [[ -x "$MODINFO" ]] || die "modinfo not found (kmod package)"

  local stage_label="stage 1 (Ubuntu archive)"
  [[ "$STAGE2" -eq 1 ]] && stage_label="stage 2 (NVIDIA CUDA repo, DKMS)"
  log "driver R580 realignment — $stage_label, target $TARGET_VERSION"

  if aligned; then
    log "NO-OP: userspace already at $TARGET_VERSION, no 595 packages, pin file present."
    finish
    return 0
  fi

  snapshot

  local k pkgs=() removals=() allowed='595'
  # 595 packages still installed -> removed in the same transaction ("pkg-").
  while IFS= read -r k; do
    [[ -n "$k" ]] && removals+=("${k}-")
  done < <(installed_595_pkgs)

  if [[ "$STAGE2" -eq 1 ]]; then
    setup_cuda_repo
    # Prebuilt LRM modules (Ubuntu archive builds) are replaced by DKMS builds;
    # remove them so exactly ONE module per kernel remains.
    while IFS= read -r k; do
      [[ -n "$k" ]] && removals+=("${k}-")
    done < <(dpkg-query -W -f '${Package} ${db:Status-Abbrev}\n' \
               'linux-modules-nvidia-580-open-*' 'linux-objects-nvidia-580-open-*' 2>/dev/null \
               | awk '$2 == "ii" {print $1}')
    allowed='595|^linux-(modules|objects)-nvidia-580-open-'
    require_apt_pkg_version nvidia-dkms-580-open "$TARGET_VERSION"
    pkgs+=("nvidia-dkms-580-open=$TARGET_VERSION")
    # DKMS needs headers for every installed generic kernel (kernel-versioned,
    # branch fixed by name — same policy as the LRM module packages).
    while IFS= read -r k; do
      [[ -n "$k" ]] && pkgs+=("linux-headers-${k}")
    done < <(installed_generic_kernels)
  else
    # Prebuilt signed open modules for EVERY installed generic kernel (the next
    # boot picks the newest kernel; cover them all). Kernel-versioned packages:
    # the branch is fixed by the name, the version follows the kernel.
    while IFS= read -r k; do
      [[ -n "$k" ]] || continue
      # NB: command substitution, NOT `madison | grep -q` — grep -q exits at the
      # first match and SIGPIPEs apt-cache under pipefail (observed exit 141).
      [[ -n "$(apt-cache madison "linux-modules-nvidia-580-open-${k}" 2>/dev/null)" ]] \
        || die "no prebuilt 580-open module package for installed kernel $k (linux-modules-nvidia-580-open-${k}) — use --stage2 (DKMS)"
      pkgs+=("linux-modules-nvidia-580-open-${k}")
    done < <(installed_generic_kernels)
  fi

  local p
  for p in "${USERSPACE_PKGS[@]}"; do
    require_apt_pkg_version "$p" "$TARGET_VERSION"
    pkgs+=("${p}=${TARGET_VERSION}")
  done
  require_apt_pkg_version "$FIRMWARE_PKG" "$TARGET_VERSION"
  pkgs+=("${FIRMWARE_PKG}=${TARGET_VERSION}")

  log "single apt transaction: ${#pkgs[@]} installs, ${#removals[@]} removals"
  guard_dry_run "$allowed" "${pkgs[@]}" "${removals[@]}"
  "${CV_SUDO[@]}" apt-get install -y --no-install-recommends --allow-downgrades \
    "${pkgs[@]}" "${removals[@]}"

  # Purge leftover config state of the removed 595 set (they are gone already;
  # this only clears "rc" residue). Never touches linux-signatures-nvidia-*.
  local rc_pkgs
  rc_pkgs="$(dpkg-query -W -f '${Package} ${db:Status-Abbrev}\n' '*nvidia*595*' 2>/dev/null \
    | awk '$2 == "rc" {print $1}' || true)"
  if [[ -n "$rc_pkgs" ]]; then
    # shellcheck disable=SC2086  # intentional word-splitting of the package list
    "${CV_SUDO[@]}" apt-get purge -y $rc_pkgs
  fi

  assert_module_versions
  deploy_pin
  assert_soname_resolution
  log "NOTE: nvidia-smi will report a userspace/kernel mismatch until the reboot — expected."
  finish
}

CV_TMPDIR="$(mktemp -d)"
trap 'rm -rf "$CV_TMPDIR"' EXIT

main
