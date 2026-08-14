# docker/selftest_stub/Dockerfile — built-in stub SUT image (M7 §3.5 option B, Phase 5).
#
# WHY THIS IMAGE EXISTS: `DoD-P5-07` (self-test round-trip green) had exactly one
# missing piece — the platform had no SUT handle of its own, so every self-test job
# died at the runner's readiness barrier (measured: `cv_infra/runner/main.py:476`,
# T3 report §7-1). This is that handle: a platform-internal container that answers
# the SUT side of one job, so `cv-infra selftest` needs NO consumer image and NO
# consumer repo (`NFR-SELFTEST-001`, `REQ-SELFTEST-001`).
#
# WHY A CONTAINER AND NOT AN IN-RUNNER DRIVER (option A): M7 §3.5 D-O pins option B
# FIRST for the deployment/portability gate, because only a separate container
# exercises the thing a redeploy can actually break — container-boundary DDS on the
# per-job bridge network (LOCKED §5/§7). An in-process driver would prove Isaac boots
# and never touch the isolation substrate.
#
# WHY NOT REUSE THE RUNNER IMAGE AS THE SUT: measured dead end (T3 report §7-1) —
# 15.5 GB and it exits immediately in the SUT role, which the supervisor's restart
# limit turns into an infra failure (`supervisor.py:997`). Lightweight is a
# requirement, not a preference.
#
# BUILD (context = this directory; it needs nothing from the repo root, and the root
# .dockerignore excludes docker/ from the other two contexts anyway):
#   CV_SOURCE_REVISION="$(git rev-parse HEAD)" \
#     docker build -f docker/selftest_stub/Dockerfile \
#       --build-arg CV_SOURCE_REVISION="$CV_SOURCE_REVISION" \
#       -t cv-infra-selftest-stub:<tag> docker/selftest_stub
# Then point the self-test at it (the handle is never guessed — FU-10):
#   CV_SELFTEST_SUT_IMAGE=cv-infra-selftest-stub:<tag>
# Full operator recipe + what it proves: docker/selftest_stub/README.md
#
# NOTE (NEG-2, LOCKED §8): no EULA/privacy acceptance value is baked here either.
# This image boots no Isaac; the runner it talks to is the one that needs consent.

# --------------------------------------------------------------------------- #
# Base — the ros:jazzy the repo ALREADY pinned for the DoD-P1-05 container-boundary
# DDS handshake (`scripts/workstation_setup/common.sh`: CV_ROS_JAZZY_IMAGE +
# CV_ROS_JAZZY_DIGEST, 2-stage pin locked at first pull). Reused verbatim rather
# than introducing a second ROS base: the peer that proved DDS across containers on
# this platform is the right thing to build the stub SUT out of, and one pin is one
# thing to re-pin. Tag kept in the comment because the digest is the actual pin
# (LOCKED §2: no floating tags).
#   ros:jazzy = ros-base-noble; measured to already ship rclpy, std_srvs,
#   rcl_interfaces, action_msgs, geometry_msgs (2026-08-14) — see README §pins.
# --------------------------------------------------------------------------- #
FROM ros@sha256:31daab66eef9139933379fb67159449944f4e2dcf2e22c2d12cc715f29873e0f

# ── apt VERSION PINS (CLAUDE §2-7; same discipline as docker/runner/Dockerfile) ──
# The stub needs exactly ONE package the base lacks: nav2_msgs (the NavigateToPose
# action type the runner's goal interface is pinned to). Unlike the runner's layer,
# the transitive closure here is small enough to pin ENTIRELY: measured
# `apt-get install --no-install-recommends --dry-run ros-jazzy-nav2-msgs` on the
# digest-pinned base (2026-08-14) = 2 packages total, the direct one plus
# geographic-msgs. Naming both means this layer's apt input set is 100 % pinned —
# the runner could not do that (247 unpinned transitives) but this one can, so it
# does.
#
# SOURCE OF THE VALUES (never invented — §2-4 / G-64): `apt-cache policy` /
# `--dry-run` against packages.ros.org on the digest-pinned base, 2026-08-14.
#
# WHEN A PIN DISAPPEARS the build fails loudly (packages.ros.org is rolling and
# withdraws superseded versions). That is the intended §2-7 trade. RE-PIN recipe is
# in the failure message, and the layer prints its own dpkg manifest so the next
# person can read the values off the last good image.
RUN set -eu; \
    export DEBIAN_FRONTEND=noninteractive; \
    dpkg-query -W -f='${binary:Package}=${Version}\n' | sort > /tmp/pkgs.before; \
    apt-get update; \
    apt-get install -y --no-install-recommends \
        ros-jazzy-nav2-msgs=1.3.12-1noble.20260615.145957 \
        ros-jazzy-geographic-msgs=1.0.6-2noble.20260615.111621 \
    || { echo "ERROR: a pinned ros-jazzy version above is no longer served by" \
              "packages.ros.org (that repo is rolling and withdraws superseded" \
              "versions). RE-PIN: read the versions off the last good stub image" \
              "with 'dpkg-query -W', or measure the candidates with" \
              "'apt-cache policy ros-jazzy-nav2-msgs', update the apt VERSION PINS" \
              "block in docker/selftest_stub/Dockerfile, and re-run the readiness" \
              "probe (scripts/selftest_stub/probe_readiness.sh)."; \
         exit 1; }; \
    dpkg-query -W -f='${binary:Package}=${Version}\n' | sort > /tmp/pkgs.after; \
    echo '=== selftest stub apt layer manifest (every package this layer installed) ==='; \
    comm -13 /tmp/pkgs.before /tmp/pkgs.after; \
    echo '=== end selftest stub apt layer manifest ==='; \
    rm -f /tmp/pkgs.before /tmp/pkgs.after; \
    rm -rf /var/lib/apt/lists/*

COPY stub_sut.py /opt/cv-infra/stub_sut.py

# Unbuffered so `docker logs <job>-sut` shows the readiness/goal lines AS THEY
# HAPPEN — during a live self-test those lines are the only window into the SUT
# side of the barrier.
ENV PYTHONUNBUFFERED=1

# The stub's knobs are BUILD-time in production. M3 spawns the SUT with exactly one
# env var (ROS_DOMAIN_ID) — deliberately, because a SUT is a blackbox (REQ-EXEC-005)
# — so there is no runtime channel to hand it anything else. This ARG is that
# channel: the ONLY knob expected to ever move is the goal hold (stub_sut.py
# DEFAULT_GOAL_HOLD_S documents when and how to measure it), so it gets a build arg
# instead of forcing a source edit. Default 0 = succeed as soon as the goal arrives.
ARG CV_STUB_GOAL_HOLD_S=0
ENV CV_STUB_GOAL_HOLD_S=${CV_STUB_GOAL_HOLD_S}

# Fail the build if the node cannot even import its contract types (a missing
# nav2_msgs would otherwise surface as a container that exits and gets restarted
# into an infra failure, three layers away from the cause).
RUN /ros_entrypoint.sh python3 -c \
    "import rclpy, std_srvs.srv, rcl_interfaces.srv, nav2_msgs.action; print('[guard] stub SUT imports OK')"

# Plane stamp (G-66): every image-producing path in this repo records the commit it
# was built from, so "which stub is running" is answerable with `docker image
# inspect`. NOTE: `scripts/check_plane_skew.sh` does not read this plane yet — it
# takes the runner + control-plane images and making a 4th plane OPTIONAL would be
# the false-green the gate exists to prevent. Wiring it in is a gate change (DX/QA
# owns those tests); stamping now costs nothing and keeps the option open.
ARG CV_SOURCE_REVISION
RUN test -n "${CV_SOURCE_REVISION}" \
    || { echo "ERROR: --build-arg CV_SOURCE_REVISION=<source commit sha> is required" \
              "(stub SUT image stamp, G-66). Recipe: docker/selftest_stub/README.md"; exit 1; }
LABEL org.opencontainers.image.revision=$CV_SOURCE_REVISION

# The base ENTRYPOINT (/ros_entrypoint.sh) sources /opt/ros/$ROS_DISTRO/setup.bash
# and execs the command — reused rather than re-implemented (CLAUDE §5). Keeping it
# also means `docker run <this image> ros2 service call …` works, which is exactly
# how the readiness probe drives the peer side.
#
# CMD (not ENTRYPOINT) is what M3 runs: the supervisor spawns the SUT with NO
# command override (`supervisor.py`), so this line IS the production command.
CMD ["python3", "/opt/cv-infra/stub_sut.py"]
