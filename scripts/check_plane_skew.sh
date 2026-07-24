#!/usr/bin/env bash
# check_plane_skew.sh — G-43 two-plane deployment skew gate (read-only).
#
# WHY (GOTCHAS G-43): the platform ships across TWO deployment planes that a
# release re-tag does NOT keep together:
#   (1) YAML plane   — the reusable workflow / composite action. A release tag
#                      `@vN` MOVES this plane (consumers pin `uses: …@vN`).
#   (2) runtime plane — the code a GPU job actually executes: the runner venv's
#                      editable install + the pre-installed serve/CLI container.
#                      GPU jobs do NOT `actions/checkout` (R10), so this plane is
#                      updated ONLY by a checkout + reinstall + container restart.
# A re-tag moves plane (1) but leaves plane (2) untouched → the two silently
# skew, and the live leg runs stale code. This gate is the pre-live-leg check
# from G-43's agreed response ②: compare the runtime-plane checkout commit
# against the release tag peel; loud-fail (fail-closed) on any mismatch.
#
# The remediation (how to re-sync the runtime plane) lives in the C-2 deploy
# manual seed: docs/deploy/plane-sync.md. This script only DETECTS; it never
# mutates the workstation, the checkout, or any git ref (read-only compare).
#
# Reuse (do-not-reinvent): the whole comparison is stock `git rev-parse`.
#
# Inputs — all arg/env, NO host/GPU literals baked in (DoD-P5-09 spirit):
#   --src PATH        / CV_PLANE_SRC       runtime-plane checkout dir
#                                          (default: $HOME/cv-infra-p2-src/cv-infra-workspace,
#                                           env-overridable — same house pattern as common.sh)
#   --src-rev REV     / CV_PLANE_SRC_REV   rev read as the runtime-plane commit
#                                          (default: HEAD = the live checkout;
#                                           overridable for what-if / self-test)
#   --tag REF         / CV_PLANE_TAG       release tag / ref of the YAML plane
#                                          (default: v1). Peeled via `REF^{commit}`
#                                          so annotated OR lightweight tags — and
#                                          a bare commit — all work.
#   --tag-repo PATH   / CV_PLANE_TAG_REPO  repo the tag is peeled FROM
#                                          (default: = --src)
#
# STALE-LOCAL-TAG HAZARD (read this): the tag is peeled from --tag-repo's LOCAL
# refs. If that repo has not fetched the moved release tag, the peel is stale and
# the gate can FALSELY pass (measured 2026-07-24: the workstation checkout's
# local `v1` still peeled to the stale 0e9ec21). Before trusting a pass, make the
# tag authoritative on the --tag-repo side: `git -C <tag-repo> fetch --tags`
# (do that on YOUR side), OR pass the verified release-target commit explicitly
# (`--tag <sha>`), OR peel from a fresh clone. `git ls-remote --tags <remote> vN`
# reads the pushed tag without touching local refs (see plane-sync.md).
#
# Exit codes: 0 = in sync (safe to run the live leg) · 2 = usage error ·
#             3 = SKEW DETECTED or a required rev/repo could not be resolved
#                 (fail-closed, infra/config class — same class as the consent
#                  gate and the D-2 pull-timeout infra_error).
set -euo pipefail

readonly CV_STEP=plane-skew
readonly EXIT_USAGE=2
readonly EXIT_SKEW=3

log() { printf '[cv-infra][%s] %s\n' "$CV_STEP" "$*"; }
err() { printf '[cv-infra][%s][ERROR] %s\n' "$CV_STEP" "$*" >&2; }
die() { err "$*"; exit "$EXIT_SKEW"; }

usage() {
  sed -n '2,60p' "${BASH_SOURCE[0]}" | sed 's/^# \{0,1\}//'
}

# --- inputs (env defaults; args override) ---
CV_PLANE_SRC="${CV_PLANE_SRC:-$HOME/cv-infra-p2-src/cv-infra-workspace}"
CV_PLANE_SRC_REV="${CV_PLANE_SRC_REV:-HEAD}"
CV_PLANE_TAG="${CV_PLANE_TAG:-v1}"
CV_PLANE_TAG_REPO="${CV_PLANE_TAG_REPO:-}"   # resolved to CV_PLANE_SRC after arg parse

while [[ $# -gt 0 ]]; do
  case "$1" in
    --src)      CV_PLANE_SRC="${2:?--src needs a path}"; shift 2 ;;
    --src-rev)  CV_PLANE_SRC_REV="${2:?--src-rev needs a rev}"; shift 2 ;;
    --tag)      CV_PLANE_TAG="${2:?--tag needs a ref}"; shift 2 ;;
    --tag-repo) CV_PLANE_TAG_REPO="${2:?--tag-repo needs a path}"; shift 2 ;;
    -h|--help)  usage; exit 0 ;;
    *) err "unknown argument: $1"; usage >&2; exit "$EXIT_USAGE" ;;
  esac
done
: "${CV_PLANE_TAG_REPO:=$CV_PLANE_SRC}"

command -v git >/dev/null 2>&1 || die "required tool missing: git"

# Both dirs must be real git repos (a missing/typo'd path is a config error, not a
# reason to silently pass — fail-closed, G-26).
git -C "$CV_PLANE_SRC" rev-parse --git-dir >/dev/null 2>&1 \
  || die "runtime-plane path is not a git repo: '$CV_PLANE_SRC' (set --src / CV_PLANE_SRC)."
git -C "$CV_PLANE_TAG_REPO" rev-parse --git-dir >/dev/null 2>&1 \
  || die "tag-repo path is not a git repo: '$CV_PLANE_TAG_REPO' (set --tag-repo / CV_PLANE_TAG_REPO)."

# Resolve the runtime-plane commit (checkout HEAD, or an explicit what-if rev).
if ! runtime="$(git -C "$CV_PLANE_SRC" rev-parse --verify "${CV_PLANE_SRC_REV}^{commit}" 2>/dev/null)"; then
  die "cannot resolve runtime rev '${CV_PLANE_SRC_REV}' in '$CV_PLANE_SRC'."
fi
# Resolve the release-tag peel (the YAML plane the live leg would run under).
if ! expected="$(git -C "$CV_PLANE_TAG_REPO" rev-parse --verify "${CV_PLANE_TAG}^{commit}" 2>/dev/null)"; then
  die "cannot resolve tag/ref '${CV_PLANE_TAG}' in '$CV_PLANE_TAG_REPO' (fetch --tags, or pass --tag <sha>)."
fi

log "runtime plane : ${CV_PLANE_SRC} @ ${CV_PLANE_SRC_REV} -> ${runtime}"
log "release tag   : ${CV_PLANE_TAG_REPO} @ ${CV_PLANE_TAG} -> ${expected}"

if [[ "$runtime" == "$expected" ]]; then
  log "IN SYNC — runtime plane matches the release tag. Safe to run the live leg."
  log "(reminder: this confirms runtime == tag; verify the tag itself points at the"
  log " intended release commit — see the stale-local-tag hazard in this header.)"
  exit 0
fi

err "PLANE SKEW DETECTED — the runtime plane does NOT match the release tag (G-43)."
err "    runtime (checkout) : ${runtime}"
err "    release tag (${CV_PLANE_TAG})  : ${expected}"
# Best-effort ahead/behind detail (only meaningful when both live in one history).
if [[ "$CV_PLANE_SRC" == "$CV_PLANE_TAG_REPO" ]]; then
  if delta="$(git -C "$CV_PLANE_SRC" rev-list --left-right --count "${expected}...${runtime}" 2>/dev/null)"; then
    behind="${delta%%$'\t'*}"; ahead="${delta##*$'\t'}"
    err "    runtime is ${behind} commit(s) behind / ${ahead} commit(s) ahead of the tag."
  fi
fi
die "Re-sync the runtime plane to the release commit before the live leg — procedure: docs/deploy/plane-sync.md"
