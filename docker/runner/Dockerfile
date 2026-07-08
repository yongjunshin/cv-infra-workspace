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
# --no-deps, which is sound ONLY while every runtime dependency is host-only
# control plane (docker SDK = M3 supervisor spawn, pyyaml = M8 CLI — the runner,
# DDS-only and without docker.sock, never imports them). HOST_ONLY_DEPS is the
# explicit allowlist of those deps + ALL their transitives, measured 2026-07-08
# from `uv export --frozen --no-dev --no-emit-project`. Any runtime dep NOT on
# the allowlist fails the build LOUDLY: adding one requires an explicit image
# policy decision (extend the allowlist vs add a uv.lock-based constraints
# install step before --no-deps) instead of silently clobbering the Isaac
# bundle pins (M5 §3.1).
RUN set -eu; \
    HOST_ONLY_DEPS='certifi charset-normalizer docker idna pywin32 pyyaml requests urllib3'; \
    uv export --frozen --no-dev --no-emit-project --project /src > /tmp/runtime-deps.txt; \
    leftover=''; \
    for dep in $(sed -n 's/^\([A-Za-z0-9._-]*\)==.*/\1/p' /tmp/runtime-deps.txt \
                 | tr '[:upper:]' '[:lower:]'); do \
        case " $HOST_ONLY_DEPS " in \
            *" $dep "*) ;; \
            *) leftover="$leftover $dep" ;; \
        esac; \
    done; \
    rm -f /tmp/runtime-deps.txt; \
    test -z "$leftover" \
    || { echo "ERROR: runtime dep(s) not in the host-only allowlist:$leftover —" \
               "a new runtime dep needs an image-policy decision (extend" \
               "HOST_ONLY_DEPS or add the uv.lock constraints install step" \
               "before --no-deps; M5 §3.1 D-C / R20)"; exit 1; }

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

# rosbag2/MCAP apt layer (PM ruling 2026-07-08, T2.5 — option A: reuse genuine
# rosbag2 per CLAUDE §5 do-not-reinvent; the runner records via a
# `ros2 bag record --storage mcap` subprocess, glue owned by M2 recording.py).
# Placed BEFORE the wheel layer on purpose: this layer changes rarely while the
# wheel changes every rebuild, so wheel rebuilds keep this layer cached.
# Coexistence safety: this is a separate apt layer targeting the SYSTEM
# python3.12 under /opt/ros/jazzy — Isaac's bundled py3.11 interpreter and its
# pinned site-packages are never touched (ABI isolation); the recorder joins DDS
# from a subprocess on the same ROS_DOMAIN_ID/RMW (env separation = M2 glue).
# Repo setup is the standard ros-archive-keyring method (no curl|bash): the key
# is written to the fixed keyring path and its fingerprint is asserted against
# the fixed Open Robotics fingerprint (loud fail on mismatch); `noble` and
# `amd64` are hardcoded because the digest-pinned base IS ubuntu 24.04/amd64
# (measured, T2 report §4). Reproducibility (§2-7): the ROS apt repo is rolling
# — exact-version pins would break future rebuilds — so the layer prints a
# dpkg-query manifest of every package it installs into the build log
# (evidence); the artifact reproducibility anchor is the FU-10 image Id pin
# (decision 2026-07-07-fu10). GNUPGHOME is a throwaway tmpdir so root's gpg
# call never creates files under the uid-1234 HOME (G-15 hazard).
USER root
RUN set -eu; \
    export DEBIAN_FRONTEND=noninteractive; \
    apt-get update; \
    apt-get install -y --no-install-recommends ca-certificates curl gnupg; \
    curl -fsSL https://raw.githubusercontent.com/ros/rosdistro/master/ros.key \
         -o /usr/share/keyrings/ros-archive-keyring.gpg; \
    GNUPGHOME="$(mktemp -d)"; export GNUPGHOME; \
    fpr="$(gpg --show-keys --with-colons /usr/share/keyrings/ros-archive-keyring.gpg \
           | awk -F: '/^fpr:/ {print $10; exit}')"; \
    rm -rf "$GNUPGHOME"; unset GNUPGHOME; \
    test "$fpr" = "C1CF6E31E6BADE8868B172B4F42ED6FBAB17C654" \
      || { echo "ERROR: ROS apt key fingerprint mismatch: got '$fpr'" \
                "(expected Open Robotics C1CF6E31E6BADE8868B172B4F42ED6FBAB17C654)"; \
           exit 1; }; \
    echo "deb [arch=amd64 signed-by=/usr/share/keyrings/ros-archive-keyring.gpg]" \
         "http://packages.ros.org/ros2/ubuntu noble main" \
         > /etc/apt/sources.list.d/ros2.list; \
    dpkg-query -W -f='${binary:Package}=${Version}\n' | sort > /tmp/pkgs.before; \
    apt-get update; \
    apt-get install -y --no-install-recommends \
        ros-jazzy-ros2bag ros-jazzy-rosbag2-storage-mcap; \
    dpkg-query -W -f='${binary:Package}=${Version}\n' | sort > /tmp/pkgs.after; \
    echo '=== rosbag2 apt layer manifest (every package this layer installed) ==='; \
    comm -13 /tmp/pkgs.before /tmp/pkgs.after; \
    echo '=== end rosbag2 apt layer manifest ==='; \
    rm -f /tmp/pkgs.before /tmp/pkgs.after; \
    rm -rf /var/lib/apt/lists/*
# Back to the base's measured user (isaac-sim, uid 1234 — R2 note above); the
# apt layer above is the only root step in this image.
USER isaac-sim

COPY --from=builder --chown=1234:1234 /dist/cv_infra-*.whl /tmp/cv-infra-wheel/

# --no-deps: never let the wheel's resolver touch Isaac's bundled pins (D-C, R20).
# No constraints step BY POLICY: runtime deps are host-only control plane
# (HOST_ONLY_DEPS, guarded in the builder stage) and are excluded from this image
# on purpose — the wheel must stay importable dep-less, which the runtime import
# smoke proves (DoD-P2-12); supervisor (M3) and CLI (M8) lazy-import docker/yaml.
RUN ./python.sh -m pip install --no-cache-dir --no-deps /tmp/cv-infra-wheel/cv_infra-*.whl \
 && rm -rf /tmp/cv-infra-wheel

# Stateless runner (LOCKED §15, NFR-EXEC-002): no job state/scenario/result baked in.
# JOB_SPEC / RESULT_OUT / caches arrive as env + HOST-ABSOLUTE bind mounts (R-mount);
# one job -> exactly one result.json -> exit (M2 owns the entrypoint's content).
ENTRYPOINT ["./python.sh", "-m", "cv_infra.runner.main"]
