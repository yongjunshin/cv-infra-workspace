# docker/runner/Dockerfile — cv-infra runner image (M5 §3.1, Phase 2).
#
# Multi-stage layering (LOCKED §2/§15; REQ-DEPLOY-004/005, NFR-EXEC-002): the wheel is
# built in a slim builder so the Isaac Sim base is never polluted with build tooling,
# then installed into the BUNDLED interpreter with --no-deps (clobber prevention, R20).
#
# Build (from the repo root; workstation, self-hosted GPU host):
#   docker build -f docker/runner/Dockerfile -t cv-infra-runner:<tag> .
#
# Image-as-artifact (decision 2026-07-07-fu10): build ONCE, record the image Id/digest,
# reuse that artifact across cycles; rebuild only on an explicit recorded decision.
#
# NOTE (NEG-2, LOCKED §8): no EULA/privacy acceptance is baked into this image.
# Consent is a runtime operator input (decision 2026-07-03-p1-eula-runtime-consent);
# M3 injects the acceptance env per run only when the consent record is valid, and the
# runner boot guard refuses to start Isaac without it.

# --------------------------------------------------------------------------- #
# (1) builder — wheel built OUTSIDE the Isaac base (slim, pinned; CLAUDE §2-7).
# Tag+digest locked 2026-07-07 at first pull on etri6000 (= Python 3.11.15,
# Debian trixie; 2-stage pin pattern of scripts/workstation_setup/common.sh).
# --------------------------------------------------------------------------- #
FROM python:3.11-slim@sha256:c20888b6acdd1e63e1c433a185bf3ad162c0288fe484616ce062e0d28add2900 AS builder

COPY . /src

# uv==0.11.13 = the exact version used for the repo's uv.lock workflows (pinned).
# The wheel build backend (hatchling==1.30.1) is pinned in pyproject [build-system].
RUN pip install --no-cache-dir uv==0.11.13 \
 && uv build --wheel --project /src --out-dir /dist

# Clobber-prevention guard (D-C, R20): the runtime stage installs the wheel with
# --no-deps, which is sound ONLY while cv-infra's runtime dependency set is empty
# (measured 2026-07-07: `uv export --frozen --no-dev --no-emit-project` emits nothing).
# If a runtime dependency is ever added, fail the build LOUDLY so the uv.lock-based
# constraints install step (M5 §3.1) gets added instead of silently clobbering the
# Isaac bundle pins.
RUN test -z "$(uv export --frozen --no-dev --no-emit-project --project /src | grep -vE '^(#|[[:space:]]*$)')" \
 || { echo "ERROR: cv-infra runtime deps are no longer empty — add the uv.lock" \
             "constraints install step before --no-deps (M5 §3.1 D-C / R20)"; exit 1; }

# --------------------------------------------------------------------------- #
# (2) runtime — one thin layer on the pinned Isaac Sim base (no latest; LOCKED §2).
# Digest = CV_ISAAC_DIGEST in scripts/workstation_setup/common.sh (pin SoT, locked
# 2026-06-26 at first anonymous NGC pull).
# --------------------------------------------------------------------------- #
FROM nvcr.io/nvidia/isaac-sim:5.1.0@sha256:f3563cb2ba0c18af0b2fb321360dcb73a917b899f879e3213623d6bee484fa54

# Measured 2026-07-07 (etri6000, docker inspect + in-container id/ls):
#   USER=isaac-sim (uid 1234), WORKDIR=/isaac-sim, python.sh=/isaac-sim/python.sh,
#   bundled site-packages owned by uid 1234 (pip install works as the image user).
# WORKDIR is set explicitly anyway so the relative ./python.sh below never depends
# on the base's default (R2 / D-O).
WORKDIR /isaac-sim

# RTX off-screen render + NVENC need >= graphics,compute,utility,video (D-A, R19).
# The base already bakes =all (measured); kept explicit so the guarantee survives a
# base change and spawners (M3) propagate it.
ENV NVIDIA_DRIVER_CAPABILITIES=all

COPY --from=builder --chown=1234:1234 /dist/cv_infra-*.whl /tmp/cv-infra-wheel/

# --no-deps: never let the wheel's resolver touch Isaac's bundled pins (D-C, R20).
# No constraints step: runtime dependency set is empty (guarded in the builder stage).
RUN ./python.sh -m pip install --no-cache-dir --no-deps /tmp/cv-infra-wheel/cv_infra-*.whl \
 && rm -rf /tmp/cv-infra-wheel

# Stateless runner (LOCKED §15, NFR-EXEC-002): no job state/scenario/result baked in.
# JOB_SPEC / RESULT_OUT / caches arrive as env + HOST-ABSOLUTE bind mounts (R-mount);
# one job -> exactly one result.json -> exit (M2 owns the entrypoint's content).
ENTRYPOINT ["./python.sh", "-m", "cv_infra.runner.main"]
