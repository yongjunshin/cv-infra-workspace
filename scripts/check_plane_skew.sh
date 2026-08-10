#!/usr/bin/env bash
# check_plane_skew.sh — G-43 three-plane deployment skew gate (read-only).
#
# WHY (GOTCHAS G-43): the platform ships across THREE deployment planes that a
# release re-tag does NOT keep together:
#   (1) YAML plane   — the reusable workflow / composite action. A release tag
#                      `@vN` MOVES this plane (consumers pin `uses: …@vN`).
#   (2) runtime plane — the code a GPU job actually executes: the runner venv's
#                      editable install + the pre-installed serve/CLI container.
#                      GPU jobs do NOT `actions/checkout` (R10), so this plane is
#                      updated ONLY by a checkout + reinstall + container restart.
#   (3) runner-image plane — the wheel BAKED INTO `cv-infra-runner:<tag>`. The job
#                      body runs THAT wheel, never the checkout, so only an image
#                      REBUILD moves it. Measured cost of not looking (p5c12): the
#                      control plane was fresh but the image was still `p4c5`, so a
#                      job declaring `scenario.initial_pose` was rejected inside the
#                      container ("Extra inputs are not permitted") — the standard
#                      path was blocked while every plane the gate DID look at was
#                      green (G-43's p5c8 reinforcement, third-plane clause).
# A re-tag moves plane (1) but leaves (2) and (3) untouched → they silently skew,
# and the live leg runs stale code. This gate is the pre-live-leg check from
# G-43's agreed response ②/③: compare the runtime-plane checkout commit AND the
# runner image's baked wheel commit against the release tag peel; loud-fail
# (fail-closed) on any mismatch.
#
# The remediation (how to re-sync the runtime plane) lives in the C-2 deploy
# manual seed: docs/deploy/plane-sync.md. This script only DETECTS; it never
# mutates the workstation, the checkout, or any git ref (read-only compare).
#
# Reuse (do-not-reinvent): the whole comparison is stock `git rev-parse` plus, for
# plane (3), stock `docker image inspect` of the OCI-standard revision label.
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
#   --image REF       / CV_PLANE_IMAGE     runner image whose BAKED wheel commit is
#                                          plane (3). **REQUIRED — no default**: a
#                                          gate that does not look at a plane emits
#                                          false green (G-43), and defaulting the ref
#                                          would bake a host-side literal in here.
#                                          Read from the image's OCI label
#                                          `org.opencontainers.image.revision`, which
#                                          docker/runner/Dockerfile stamps from
#                                          `--build-arg CV_SOURCE_REVISION=<sha>`.
#                                          An image built BEFORE that wiring carries no
#                                          such label → fail-closed (exit 3) with a
#                                          rebuild/migration message, never a pass.
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
# Exit codes: 0 = all three planes in sync (safe to run the live leg) ·
#             2 = usage error (incl. the REQUIRED --image not given) ·
#             3 = SKEW DETECTED on any plane, or a required rev/repo/image could not
#                 be resolved, or the image carries no revision stamp
#                 (fail-closed, infra/config class — same class as the consent
#                  gate and the D-2 pull-timeout infra_error).
set -euo pipefail

readonly CV_STEP=plane-skew
readonly EXIT_USAGE=2
readonly EXIT_SKEW=3
readonly REVISION_LABEL=org.opencontainers.image.revision

log() { printf '[cv-infra][%s] %s\n' "$CV_STEP" "$*"; }
err() { printf '[cv-infra][%s][ERROR] %s\n' "$CV_STEP" "$*" >&2; }
die() { err "$*"; exit "$EXIT_SKEW"; }

usage() {
  sed -n '2,73p' "${BASH_SOURCE[0]}" | sed 's/^# \{0,1\}//'
}

# --- inputs (env defaults; args override) ---
CV_PLANE_SRC="${CV_PLANE_SRC:-$HOME/cv-infra-p2-src/cv-infra-workspace}"
CV_PLANE_SRC_REV="${CV_PLANE_SRC_REV:-HEAD}"
CV_PLANE_TAG="${CV_PLANE_TAG:-v1}"
CV_PLANE_TAG_REPO="${CV_PLANE_TAG_REPO:-}"   # resolved to CV_PLANE_SRC after arg parse
CV_PLANE_IMAGE="${CV_PLANE_IMAGE:-}"         # plane (3) — required, no default

while [[ $# -gt 0 ]]; do
  case "$1" in
    --src)      CV_PLANE_SRC="${2:?--src needs a path}"; shift 2 ;;
    --src-rev)  CV_PLANE_SRC_REV="${2:?--src-rev needs a rev}"; shift 2 ;;
    --tag)      CV_PLANE_TAG="${2:?--tag needs a ref}"; shift 2 ;;
    --tag-repo) CV_PLANE_TAG_REPO="${2:?--tag-repo needs a path}"; shift 2 ;;
    --image)    CV_PLANE_IMAGE="${2:?--image needs a runner image ref}"; shift 2 ;;
    -h|--help)  usage; exit 0 ;;
    *) err "unknown argument: $1"; usage >&2; exit "$EXIT_USAGE" ;;
  esac
done
: "${CV_PLANE_TAG_REPO:=$CV_PLANE_SRC}"

if [[ -z "$CV_PLANE_IMAGE" ]]; then
  err "--image / CV_PLANE_IMAGE is REQUIRED (runner-image plane ③, G-43 third-plane clause)."
  err "    The live leg's job body runs the wheel baked into that image, NOT the checkout;"
  err "    a gate that skips this plane goes green while the job dies inside the container"
  err "    (measured p5c12). Pass the image the live leg will spawn, e.g.:"
  err "        $(basename "${BASH_SOURCE[0]}") --tag <release-sha> --image <runner-image-ref>"
  err "    (that ref is the orchestrator's CV_RUNNER_IMAGE for the leg — see docs/deploy/plane-sync.md)"
  exit "$EXIT_USAGE"
fi

command -v git >/dev/null 2>&1 || die "required tool missing: git"
command -v docker >/dev/null 2>&1 \
  || die "required tool missing: docker (needed to read plane ③ off '$CV_PLANE_IMAGE')."

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

# Resolve the runner-image plane: the commit the image's baked wheel was built from,
# stamped as the OCI revision label at build time. `index` prints "<no value>" when
# the key is absent — that (and an empty value) is an UNSTAMPED image, which is a
# migration state, never a pass.
if ! image_rev="$(docker image inspect "$CV_PLANE_IMAGE" \
      --format "{{index .Config.Labels \"${REVISION_LABEL}\"}}" 2>/dev/null)"; then
  die "cannot inspect runner image '${CV_PLANE_IMAGE}' (build/pull it, or fix --image)."
fi
image=""                        # "" = unstamped (reported below, never a pass)
if [[ -z "$image_rev" || "$image_rev" == "<no value>" ]]; then
  :
elif ! image="$(git -C "$CV_PLANE_SRC" rev-parse --verify "${image_rev}^{commit}" 2>/dev/null)"; then
  die "runner image '${CV_PLANE_IMAGE}' claims revision '${image_rev}', which does not resolve in '$CV_PLANE_SRC' (fetch it, or re-check the stamp)."
fi

log "runtime plane : ${CV_PLANE_SRC} @ ${CV_PLANE_SRC_REV} -> ${runtime}"
log "release tag   : ${CV_PLANE_TAG_REPO} @ ${CV_PLANE_TAG} -> ${expected}"
log "runner image  : ${CV_PLANE_IMAGE} @ ${REVISION_LABEL} -> ${image:-<unstamped>}"

# Best-effort ahead/behind detail (only meaningful when both live in one history).
delta_line() {  # $1 = the plane's commit, $2 = label for the message
  local delta behind ahead
  delta="$(git -C "$CV_PLANE_SRC" rev-list --left-right --count "${expected}...$1" 2>/dev/null)" || return 0
  behind="${delta%%$'\t'*}"; ahead="${delta##*$'\t'}"
  err "    $2 is ${behind} commit(s) behind / ${ahead} commit(s) ahead of the release ref."
}

skew=0
if [[ "$runtime" != "$expected" ]]; then
  skew=1
  err "PLANE SKEW DETECTED (②) — the runtime plane does NOT match the release ref (G-43)."
  err "    runtime (checkout) : ${runtime}"
  err "    release ref (${CV_PLANE_TAG})  : ${expected}"
  # ahead/behind is only meaningful when tag and checkout share one history.
  [[ "$CV_PLANE_SRC" == "$CV_PLANE_TAG_REPO" ]] && delta_line "$runtime" "runtime"
fi
if [[ -z "$image" ]]; then
  skew=1
  err "RUNNER-IMAGE PLANE UNSTAMPED (③) — '${CV_PLANE_IMAGE}' carries no ${REVISION_LABEL} label."
  err "    Images built before the p5c13 stamp wiring cannot be compared, so this gate"
  err "    REFUSES to pass rather than skip the plane (fail-closed)."
  err "    Migration: rebuild the runner image with the stamp —"
  err "        docker build -f docker/runner/Dockerfile --build-arg CV_SOURCE_REVISION=<sha> -t <ref> ."
  err "    (full recipe, including the git-archive build context: docs/deploy/plane-sync.md ③)"
elif [[ "$image" != "$expected" ]]; then
  skew=1
  err "PLANE SKEW DETECTED (③) — the runner image's baked wheel does NOT match the release ref."
  err "    runner image (${CV_PLANE_IMAGE}) : ${image}"
  err "    release ref (${CV_PLANE_TAG})  : ${expected}"
  delta_line "$image" "runner image"
  err "    Only a REBUILD moves this plane — checkout/reinstall/serve restart do not."
fi

if [[ "$skew" -eq 0 ]]; then
  log "IN SYNC — runtime plane AND runner image both match the release ref. Safe to run the live leg."
  log "(reminder: this confirms both planes == the ref you passed; verify that ref itself is the"
  log " intended release commit — see the stale-local-tag hazard in this header.)"
  exit 0
fi
die "Re-sync every skewed plane to the release commit before the live leg — procedure: docs/deploy/plane-sync.md"
