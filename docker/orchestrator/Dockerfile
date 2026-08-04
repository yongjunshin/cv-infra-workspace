# docker/orchestrator/Dockerfile — cv-infra CONTROL PLANE image (M5 §3.2, REQ-DEPLOY-002).
#
# NOT the runner. No Isaac base, no render stack, no GPU compute: this image holds the
# long-lived process that serves the M3 submit/scheduler API + M6 operational read model
# and spawns runner containers as SIBLINGS through the host docker daemon (LOCKED §3).
# Isaac Sim's tens of GB belong to the data plane only (R18).
#
# Same layering idioms as docker/runner/Dockerfile — multi-stage (build tooling never
# reaches the runtime layer), digest-pinned base, lock-derived pins (CLAUDE §2-7) — with
# one deliberate difference: the runner installs the wheel `--no-deps` because the kit
# interpreter already ships pinned packages that must not be clobbered (D-C, R20). Nothing
# is pre-bundled here, so this image installs the FULL uv.lock closure, hash-verified.
#
# Build (from the repo root; `docker compose -f docker/compose.yaml build` does exactly this):
#   docker build -f docker/orchestrator/Dockerfile -t cv-infra-orchestrator:<tag> .
#
# Run: see docker/compose.yaml — the process needs /var/run/docker.sock, host-absolute
# bind mounts at IDENTICAL paths, and the operator's runtime consent env (never baked).
#
# NOTE (NEG-2, LOCKED §8): no EULA/privacy acceptance is baked into this image. The
# control plane only FORWARDS the operator's runtime consent env to the runners it spawns.

# --------------------------------------------------------------------------- #
# (1) builder — wheel + lock export, in a throwaway stage.
# Same digest as docker/runner/Dockerfile's builder (= Python 3.11.15, Debian trixie,
# locked 2026-07-07 at first pull on etri6000); keep the two in sync.
# --------------------------------------------------------------------------- #
FROM python:3.11-slim@sha256:c20888b6acdd1e63e1c433a185bf3ad162c0288fe484616ce062e0d28add2900 AS builder

COPY . /src

# uv==0.11.13 = the exact version used for this repo's uv.lock workflows (pinned).
# `uv export --frozen` fails loudly if uv.lock is out of date w.r.t. pyproject.toml, so a
# drifted lock can never be papered over at image build time. The export carries per-artifact
# hashes; the runtime stage installs it with --require-hashes.
RUN pip install --no-cache-dir uv==0.11.13 \
 && uv build --wheel --project /src --out-dir /dist \
 && uv export --frozen --no-dev --no-emit-project --project /src > /dist/requirements.txt

# --------------------------------------------------------------------------- #
# (2) runtime — slim python + the locked dependency closure + the cv_infra wheel.
# --------------------------------------------------------------------------- #
FROM python:3.11-slim@sha256:c20888b6acdd1e63e1c433a185bf3ad162c0288fe484616ce062e0d28add2900

# Structured boot lines (`serve-config`, `runner-mounts`, `cache-seed`) are the observable
# contract of this deployment — they must reach `docker logs` unbuffered.
ENV PYTHONUNBUFFERED=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

COPY --from=builder /dist /tmp/dist

# --require-hashes: refuse any artifact whose bytes are not the ones uv.lock recorded
# (reproducibility down to the wheel, CLAUDE §2-7). The cv_infra wheel itself is installed
# --no-deps afterwards: its dependency closure is ALREADY installed from the lock above, so
# --no-deps here means "never let a second resolver re-decide a pinned version".
# The import smoke is a build-time guard, not decoration: an image whose control-plane
# entrypoint cannot import is a deployment that fails at `up` instead of at `build`.
RUN pip install --no-cache-dir --require-hashes -r /tmp/dist/requirements.txt \
 && pip install --no-cache-dir --no-deps /tmp/dist/cv_infra-*.whl \
 && rm -rf /tmp/dist \
 && python -c "import cv_infra.orchestrator.serve, docker, uvicorn, fastapi, pynvml; \
print('[guard] control-plane imports OK')"

# Root by measurement, not by laziness: seeding a per-job cache with `cp -a` must preserve
# the runner's uid-1234 ownership, and only a privileged copier can. A non-privileged copy
# yields a cache the runner cannot write == a silently DISABLED cache (47 s/job, measured
# p4c5 — G-34; supervisor.py::_assert_runner_writable turns it into a loud failure).
# Docker socket access is host-root-equivalent anyway (R11, accepted for single-tenant).
# The Debian base also gives GNU `cp` (--archive ownership preservation); a busybox/distroless
# base would break that seeding silently.
USER root

# Stateless (LOCKED §15): no store, artifacts or cache in the image — the store path, out
# dir and cache roots all arrive as env + host-absolute bind mounts (docker/compose.yaml).
#
# CMD, not ENTRYPOINT (G-14): an ENTRYPOINT would swallow ad-hoc arguments, and this image
# doubles as the pinned CLI surface (`docker compose run --rm orchestrator cv-infra --help`).
CMD ["python", "-m", "cv_infra.orchestrator.serve"]
