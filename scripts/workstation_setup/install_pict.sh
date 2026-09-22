#!/usr/bin/env bash
# install_pict.sh — build Microsoft PICT from a PINNED commit and install it on this host.
#
# PICT is the covering-array generator cv-infra REUSES (do-not-reinvent): it turns the
# consumer's `param_space.pict` into the pairwise case array. Upstream ships no release
# binary, so the only way to have it is to build it — which is also why the pin is a
# COMMIT and not a tag: a tag can be moved, and a different generator is a different set
# of cases for the same declared input space.
#
# The same commit is built by .github/workflows/ci.yml; tests/test_gh_wiring_static.py
# asserts the two agree, so this file and CI cannot drift into two different PICTs.
#
# Idempotent: an existing binary at the destination is left alone unless CV_PICT_FORCE=1.
# Prints the `export CV_PICT_BIN=...` line the runner service environment needs — the
# CLI refuses to start (exit 3) without it rather than guessing a generator.
set -euo pipefail

export CV_STEP=pict
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source-path=SCRIPTDIR
# shellcheck source=common.sh
source "$SCRIPT_DIR/common.sh"

# The pin (CLAUDE.md §2-3). Bumping it = new commit here AND in ci.yml, in one change.
readonly CV_PICT_REPO="https://github.com/microsoft/pict"
readonly CV_PICT_COMMIT="0c66a8e332655cd64802e18de962dacbbe3882bd"
# Under $HOME on purpose: no sudo, and the same path CI's cache uses, so an operator
# reading either place recognises the other.
CV_PICT_PREFIX="${CV_PICT_PREFIX:-$HOME/.cache/cv-infra/pict}"

main() {
  require_cmd git
  require_cmd make
  require_cmd g++

  local dest="$CV_PICT_PREFIX/pict"
  if [[ -x "$dest" && "${CV_PICT_FORCE:-0}" != "1" ]]; then
    log "PICT already installed at $dest — skipping build (CV_PICT_FORCE=1 to rebuild)"
    print_export "$dest"
    return 0
  fi

  # Build in a scratch tree we own and remove; nothing about the build is kept, only the
  # one binary that comes out of it.
  local work
  work="$(mktemp -d)"
  # shellcheck disable=SC2064  # expand $work now: that is the directory to clean up
  trap "rm -rf '$work'" EXIT

  log "cloning $CV_PICT_REPO at $CV_PICT_COMMIT"
  git clone --no-checkout "$CV_PICT_REPO" "$work/pict" \
    || die "git clone failed for $CV_PICT_REPO (network/proxy?)"
  git -C "$work/pict" checkout --quiet "$CV_PICT_COMMIT" \
    || die "commit $CV_PICT_COMMIT is not in $CV_PICT_REPO — the pin is wrong, not the host"

  log "building (make)"
  make -C "$work/pict" >/dev/null || die "PICT build failed — see the make output above"
  [[ -x "$work/pict/pict" ]] || die "build produced no executable at $work/pict/pict"

  mkdir -p "$CV_PICT_PREFIX"
  install -m 0755 "$work/pict/pict" "$dest"
  log "installed $dest"
  print_export "$dest"
}

# What the operator still has to do: the binary is useless to the runner until its
# service environment names it (systemd `Environment=`, or ~/.bashrc for a manual run).
print_export() {
  local dest="$1"
  log "add this to the runner's environment:"
  printf 'export CV_PICT_BIN=%s\n' "$dest"
}

main "$@"
