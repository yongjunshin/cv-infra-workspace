#!/usr/bin/env bash
# check_plane_skew.sh — G-43 deployment-plane skew gate (read-only).
#
# WHY (GOTCHAS G-43, G-66): the platform ships across deployment planes that a
# release re-tag does NOT keep together:
#   (1) YAML plane   — the reusable workflow / composite action. A release tag
#                      `@vN` MOVES this plane (consumers pin `uses: …@vN`).
#   (2) runtime plane — the code a GPU job actually executes: the runner venv's
#                      editable install + the pre-installed serve/CLI container.
#                      GPU jobs do NOT `actions/checkout` (R10), so this plane is
#                      updated ONLY by a checkout + reinstall + container restart.
#  (2') control-plane image — since p5c14 the control plane IS a container built
#                      from docker/orchestrator/Dockerfile, so the code it runs is
#                      the wheel BAKED INTO that image, not the checkout. Measured
#                      hazard (G-66): `docker compose build` inherits no
#                      --build-arg/--label, so the documented product path used to
#                      emit an image with an EMPTY revision label while this gate
#                      looked only at the runner image — a plane the gate did not
#                      see, exactly the failure mode G-43 names. Moved only by a
#                      rebuild (`up -d --build`).
#   (3) runner-image plane — the wheel BAKED INTO `cv-infra-runner:<tag>`. The job
#                      body runs THAT wheel, never the checkout, so only an image
#                      REBUILD moves it. Measured cost of not looking (p5c12): the
#                      control plane was fresh but the image was still `p4c5`, so a
#                      job declaring `scenario.initial_pose` was rejected inside the
#                      container ("Extra inputs are not permitted") — the standard
#                      path was blocked while every plane the gate DID look at was
#                      green (G-43's p5c8 reinforcement, third-plane clause).
# A re-tag moves plane (1) but leaves (2), (2') and (3) untouched → they silently
# skew, and the live leg runs stale code. This gate is the pre-live-leg check from
# G-43's agreed response ②/③: compare the runtime-plane checkout commit AND both
# images' baked wheel commits against the release tag peel; loud-fail (fail-closed)
# on any mismatch — including an image that carries no stamp at all, because a
# plane the gate cannot read must never be read as agreement (G-66 ③).
#
# The remediation (how to re-sync the runtime plane) lives in the C-2 deploy
# manual seed: docs/deploy/plane-sync.md. This script only DETECTS; it never
# mutates the workstation, the checkout, or any git ref (read-only compare).
#
# Reuse (do-not-reinvent): the whole comparison is stock `git rev-parse` plus, for
# planes (2') and (3), stock `docker image inspect` of the OCI-standard revision label.
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
#   --orchestrator-image REF               control-plane image whose baked wheel is plane
#                     / CV_PLANE_ORCH_IMAGE (2'). **REQUIRED — no default**, same two
#                                          reasons as --image: a gate that skips a plane
#                                          emits false green, and a default ref would bake
#                                          a host-side literal in here. Read from the same
#                                          OCI label, stamped by docker/orchestrator/
#                                          Dockerfile via the compose `build.args` block.
#                                          Compose-built images from before that wiring
#                                          are unstamped → fail-closed (exit 3).
#
# STALE-LOCAL-TAG HAZARD (read this): the tag is peeled from --tag-repo's LOCAL
# refs. If that repo has not fetched the moved release tag, the peel is stale and
# the gate can FALSELY pass (measured 2026-07-24: the workstation checkout's
# local `v1` still peeled to the stale 0e9ec21). Before trusting a pass, make the
# tag authoritative on the --tag-repo side: `git -C <tag-repo> fetch --tags --force`
#   ^^^^^^^ --force is REQUIRED, not optional. Plain `git fetch --tags` SILENTLY
#   REFUSES a moved tag ("! [rejected] v1 -> v1 (would clobber existing tag)") and
#   exits 0, so the local ref keeps pointing at the OLD release. Measured 2026-08-21
#   (p5c19 F-6): the deployment host's `v1` stayed four weeks behind through a plain
#   fetch, and the DEFAULT gate invocation (--tag v1) therefore judged the planes
#   against a July commit. It surfaced as a false FAIL only because the plane happened
#   to be AHEAD of the stale tag — the false PASS direction is the same defect with
#   luckier timing, and that one is silent.
# (do that on YOUR side), OR pass the verified release-target commit explicitly
# (`--tag <sha>`), OR peel from a fresh clone. `git ls-remote --tags <remote> vN`
# reads the pushed tag without touching local refs (see plane-sync.md).
#
# Exit codes: 0 = every plane in sync (safe to run the live leg) ·
#             2 = usage error (incl. a REQUIRED image argument not given) ·
#             3 = SKEW DETECTED on any plane, or a required rev/repo/image could not
#                 be resolved, or an image carries no revision stamp
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
  # Header block = every comment line after the shebang, up to the first code line.
  # Derived, not a hardcoded line range: the range went stale the moment this header
  # grew a plane (measured while adding ②' — the old '2,73p' silently truncated).
  sed -n '2,${/^[^#]/q; s/^# \{0,1\}//p;}' "${BASH_SOURCE[0]}"
}

# --- inputs (env defaults; args override) ---
CV_PLANE_SRC="${CV_PLANE_SRC:-$HOME/cv-infra-p2-src/cv-infra-workspace}"
CV_PLANE_SRC_REV="${CV_PLANE_SRC_REV:-HEAD}"
CV_PLANE_TAG="${CV_PLANE_TAG:-v1}"
CV_PLANE_TAG_REPO="${CV_PLANE_TAG_REPO:-}"   # resolved to CV_PLANE_SRC after arg parse
CV_PLANE_IMAGE="${CV_PLANE_IMAGE:-}"         # plane (3)  — required, no default
CV_PLANE_ORCH_IMAGE="${CV_PLANE_ORCH_IMAGE:-}"  # plane (2') — required, no default

while [[ $# -gt 0 ]]; do
  case "$1" in
    --src)      CV_PLANE_SRC="${2:?--src needs a path}"; shift 2 ;;
    --src-rev)  CV_PLANE_SRC_REV="${2:?--src-rev needs a rev}"; shift 2 ;;
    --tag)      CV_PLANE_TAG="${2:?--tag needs a ref}"; shift 2 ;;
    --tag-repo) CV_PLANE_TAG_REPO="${2:?--tag-repo needs a path}"; shift 2 ;;
    --image)    CV_PLANE_IMAGE="${2:?--image needs a runner image ref}"; shift 2 ;;
    --orchestrator-image)
                CV_PLANE_ORCH_IMAGE="${2:?--orchestrator-image needs a control-plane image ref}"
                shift 2 ;;
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
  err "        $(basename "${BASH_SOURCE[0]}") --tag <release-sha> --image <runner-image-ref> \\"
  err "            --orchestrator-image <control-plane-image-ref>"
  err "    (that ref is the orchestrator's CV_RUNNER_IMAGE for the leg — see docs/deploy/plane-sync.md)"
  exit "$EXIT_USAGE"
fi
if [[ -z "$CV_PLANE_ORCH_IMAGE" ]]; then
  err "--orchestrator-image / CV_PLANE_ORCH_IMAGE is REQUIRED (control-plane image plane ②', G-66)."
  err "    The control plane runs the wheel baked into ITS image too; compose builds it and"
  err "    inherits no --build-arg/--label, so that plane silently lost its revision stamp"
  err "    while this gate looked only at the runner image (measured 2026-08-14). A gate that"
  err "    does not look at a plane emits false green (G-43). Pass the running image, e.g.:"
  err "        $(basename "${BASH_SOURCE[0]}") --tag <release-sha> --image <runner-image-ref> \\"
  err "            --orchestrator-image <control-plane-image-ref>"
  err "    (that ref is compose's CV_ORCHESTRATOR_IMAGE — see docs/deploy/plane-sync.md)"
  exit "$EXIT_USAGE"
fi

command -v git >/dev/null 2>&1 || die "required tool missing: git"
command -v docker >/dev/null 2>&1 \
  || die "required tool missing: docker (needed to read the image planes off" \
         "'$CV_PLANE_IMAGE' / '$CV_PLANE_ORCH_IMAGE')."

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

# Resolve an image plane: the commit the image's baked wheel was built from, stamped as
# the OCI revision label at build time. Sets IMAGE_PLANE_REV to the resolved commit, or
# to "" for an UNSTAMPED image (`index` prints "<no value>" when the key is absent; an
# empty value means the same) — a migration state reported below, never a pass.
# A global rather than a $(...) capture on purpose: `die` inside a command substitution
# would only kill the subshell.
IMAGE_PLANE_REV=""
resolve_image_plane() {   # $1 = image ref, $2 = human label used in the messages
  local ref="$1" label="$2" stamped=""
  IMAGE_PLANE_REV=""
  if ! stamped="$(docker image inspect "$ref" \
        --format "{{index .Config.Labels \"${REVISION_LABEL}\"}}" 2>/dev/null)"; then
    die "cannot inspect ${label} image '${ref}' (build/pull it, or fix the image argument)."
  fi
  if [[ -z "$stamped" || "$stamped" == "<no value>" ]]; then
    return 0
  fi
  if ! IMAGE_PLANE_REV="$(git -C "$CV_PLANE_SRC" rev-parse --verify "${stamped}^{commit}" 2>/dev/null)"; then
    die "${label} image '${ref}' claims revision '${stamped}', which does not resolve in '$CV_PLANE_SRC' (fetch it, or re-check the stamp)."
  fi
}

resolve_image_plane "$CV_PLANE_IMAGE" "runner"
image="$IMAGE_PLANE_REV"
resolve_image_plane "$CV_PLANE_ORCH_IMAGE" "control-plane"
orchestrator="$IMAGE_PLANE_REV"

log "runtime plane : ${CV_PLANE_SRC} @ ${CV_PLANE_SRC_REV} -> ${runtime}"
log "release tag   : ${CV_PLANE_TAG_REPO} @ ${CV_PLANE_TAG} -> ${expected}"
log "control image : ${CV_PLANE_ORCH_IMAGE} @ ${REVISION_LABEL} -> ${orchestrator:-<unstamped>}"
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
if [[ -z "$orchestrator" ]]; then
  skew=1
  err "CONTROL-PLANE IMAGE UNSTAMPED (②') — '${CV_PLANE_ORCH_IMAGE}' carries no ${REVISION_LABEL} label."
  err "    Compose builds from before the stamp wiring cannot be compared, so this gate"
  err "    REFUSES to pass rather than skip the plane (fail-closed, G-66)."
  err "    Migration: rebuild the control plane WITH the revision in the environment —"
  err "        CV_SOURCE_REVISION=\"\$(git rev-parse HEAD)\" docker compose -f docker/compose.yaml up -d --build"
  err "    (compose inherits no --build-arg/--label of its own — that is why the arg is wired"
  err "     into docker/compose.yaml; full recipe: docs/deploy/plane-sync.md ②')"
elif [[ "$orchestrator" != "$expected" ]]; then
  skew=1
  err "PLANE SKEW DETECTED (②') — the control-plane image's baked wheel does NOT match the release ref."
  err "    control image (${CV_PLANE_ORCH_IMAGE}) : ${orchestrator}"
  err "    release ref (${CV_PLANE_TAG})  : ${expected}"
  delta_line "$orchestrator" "control-plane image"
  err "    Only a REBUILD moves this plane — a checkout update alone does not."
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
  log "IN SYNC — runtime plane, control-plane image AND runner image all match the release ref."
  log "(reminder: this confirms every plane == the ref you passed; verify that ref itself is the"
  log " intended release commit — see the stale-local-tag hazard in this header.)"
  exit 0
fi
die "Re-sync every skewed plane to the release commit before the live leg — procedure: docs/deploy/plane-sync.md"
