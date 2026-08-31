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

# Clobber-prevention guard (D-C, R20, D-4' 2026-07-10): the runtime stage installs
# the wheel with --no-deps, which is sound ONLY while every runtime dependency
# falls into one of two audited categories:
#   * HOST_ONLY_DEPS      — host-side control plane (docker SDK = M3 supervisor
#     spawn, pyyaml = M8 CLI); the runner, DDS-only and without docker.sock,
#     never imports them. p4c1~c3 additions audited host-only 2026-07-13 (import
#     sites: orchestrator/{serve,api,scheduler}.py + cli/batch.py only) — image
#     policy ruling: agent-comms question deployment-2026-07-13-p4-gpu-parallel.
#   * BUNDLE_SUPPLIED_DEPS — deps the kit-prebundled interpreter ALREADY ships
#     (D-4': pydantic stack, measured 2.11.7/core 2.33.2 on image #5, p3c1 probe
#     bundled-pydantic.txt); the runner consumes contract.schema on that bundle.
#     Version skew bundle<->pyproject is a loud BUILD failure — see the assert in
#     the runtime stage below (the pin value is derived from the wheel metadata,
#     never re-recorded here — G-25/G-27).
# Both lists cover the deps + ALL their transitives, measured from
# `uv export --frozen --no-dev --no-emit-project`. Any runtime dep NOT on either
# allowlist fails the build LOUDLY: adding one requires an explicit image policy
# decision (extend an allowlist vs add a uv.lock-based constraints install step
# before --no-deps) instead of silently clobbering the Isaac bundle pins (M5 §3.1).
RUN set -eu; \
    HOST_ONLY_DEPS='annotated-doc anyio certifi charset-normalizer click colorama docker fastapi h11 httpcore httpx idna nvidia-ml-py pywin32 pyyaml requests starlette urllib3 uvicorn'; \
    BUNDLE_SUPPLIED_DEPS='annotated-types pydantic pydantic-core typing-extensions typing-inspection'; \
    uv export --frozen --no-dev --no-emit-project --project /src > /tmp/runtime-deps.txt; \
    leftover=''; \
    for dep in $(sed -n 's/^\([A-Za-z0-9._-]*\)==.*/\1/p' /tmp/runtime-deps.txt \
                 | tr '[:upper:]' '[:lower:]'); do \
        case " $HOST_ONLY_DEPS $BUNDLE_SUPPLIED_DEPS " in \
            *" $dep "*) ;; \
            *) leftover="$leftover $dep" ;; \
        esac; \
    done; \
    rm -f /tmp/runtime-deps.txt; \
    test -z "$leftover" \
    || { echo "ERROR: runtime dep(s) not in the host-only/bundle-supplied allowlists:$leftover —" \
               "a new runtime dep needs an image-policy decision (extend" \
               "an allowlist or add the uv.lock constraints install step" \
               "before --no-deps; M5 §3.1 D-C / R20 / D-4')"; exit 1; }

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
# (measured, T2 report §4). GNUPGHOME is a throwaway tmpdir so root's gpg call
# never creates files under the uid-1234 HOME (G-15 hazard).
# T3.5 (PM ruling A): + nav-msgs/geometry-msgs typesupport — T3 bringup measured
# `ros2 bag record` dropping /chassis/odom·/cmd_vel as "unknown type" without
# them (T3 report §6-1; recommends-off keeps them typesupport-only).
# go2 C4 (PM ruling A on questions/runner-2026-09-01-go2-c5-batch.md): + tf2-msgs,
# same failure mode measured again on the workstation — the recorder logged
# "Topic '/tf' has unknown type 'tf2_msgs/msg/TFMessage' … package 'tf2_msgs' not
# found" and wrote a bag WITHOUT /tf·/tf_static while exiting 0. The runner already
# requests both topics (M2 recording.py); only this layer was missing.
#
# ── apt VERSION PINS (CLAUDE §2-7; binding precondition of decision D-6) ──────
# Shipping path = (C) REBUILD ON THE NEW HOST (decision 2026-08-14 D-6). Registry
# push / save+load were not adopted because they redistribute the NVIDIA base. C
# only holds if "a rebuild makes the same thing", so D-6 also RESTATES what FU-10
# pins: the INPUT SET (base digest + uv.lock + apt versions + source commit), NOT
# the output image digest. That retires the previous rationale here ("the ROS repo
# is rolling, so the FU-10 image Id is the anchor") — with C there is no output
# anchor left, so the apt versions must be pinned like every other input.
#
# WHAT IS PINNED: the 9 packages this layer NAMES. Their transitive closure is NOT
# pinned (247 more in the ROS install's manifest below + 15 in the keyring install).
# Pinning the whole closure was rejected: those are mostly ubuntu-archive packages
# whose superseded versions LEAVE the index on every security update, so a 270-pin
# build would be permanently broken within weeks — a reproducibility claim that
# never builds is worse than an honest partial pin. (The closure-wide fix is an
# archive snapshot service, not more pins; packages.ros.org has none — out of scope.)
#
# SOURCE OF THE VALUES (never invented — §2-4 / G-64). These no longer share one
# provenance, so they are listed per value rather than under one sentence (G-79: when
# one value moves, the prose that described all of them stops being true):
#
#   * ca-certificates=20260601~24.04.1, gnupg=2.4.4-2ubuntu17.4, and the 5 ros-jazzy
#     versions below — read out of the image then in service, cv-infra-runner:p5c15,
#     on the workstation over SSH, read-only, 2026-08-14, with the same dpkg-query
#     call this layer prints. ⚠ That image no longer exists (deleted 2026-08-20 in the
#     production cutover — docs/evidence-anchors.md); the measurement stands, the
#     path to re-run it does not. All 7 were RE-CONFIRMED as still served by their
#     archives on 2026-08-21 (per-pin dry-run, p5c20 F-7).
#
#   * ros-jazzy-tf2-msgs=0.36.21-1noble.20260615.112712 — measured 2026-09-01 (go2 C4)
#     by ASKING THE ARCHIVE, because this package was NEVER installed here: reading a
#     "last good image" (instrument A below) cannot answer for a package no image has.
#     Query, verbatim (any image that already carries this layer's ros2.list works —
#     the point is the fresh `apt-get update`, not the image):
#       docker run --rm --user 0 --entrypoint bash cv-infra-runner:prod-b257ed3 -c \
#         'apt-get update -qq && apt-cache policy ros-jazzy-tf2-msgs'
#       -> Installed: (none); Candidate: 0.36.21-1noble.20260615.112712 (packages.ros.org
#          noble/main amd64, the ONLY version in the table). Note the geometry2 stack
#          carries its own version line (0.36.x), not the 5.3.8 of the *_msgs above.
#     The same call re-confirmed ros-jazzy-geometry-msgs / ros-jazzy-ros2bag as still
#     served at the pinned versions on this date.
#
#   * curl=8.5.0-2ubuntu10.13 — measured 2026-08-27 (p6c5) by ASKING THE ARCHIVE from
#     this image's own stage-2 base, because the previous pin (8.5.0-2ubuntu10.12) had
#     been WITHDRAWN and the release-gate --no-cache build failed on it ("E: Version
#     '8.5.0-2ubuntu10.12' for 'curl' was not found"). Query, verbatim:
#       docker run --rm --user 0 --entrypoint bash \
#         nvcr.io/nvidia/isaac-sim@sha256:f3563cb2ba0c18af0b2fb321360dcb73a917b899f879e3213623d6bee484fa54 \
#         -c 'apt-get update -qq && apt-cache policy ca-certificates curl gnupg'
#       -> base: Ubuntu 24.04.2 LTS (noble); curl Candidate: 8.5.0-2ubuntu10.13 (served
#          by noble-updates AND noble-security). The table holds only .13, the noble
#          release 8.5.0-2ubuntu10, and the installed 8.5.0-2ubuntu10.6 — .12 is GONE,
#          exactly as .11 was. The same query re-confirmed the other two ubuntu pins on
#          this date: ca-certificates=20260601~24.04.1 and gnupg=2.4.4-2ubuntu17.4.
#     Verified by a --no-cache build of this file the same day (see the report).
#     ⚠ THIS ONE PIN ROTS FASTER THAN WE RELEASE: .11 was dead by 2026-08-21, .12 by
#     2026-08-27 — SIX DAYS. Expect the pre-release --no-cache gate (G-99 ①) to go red
#     on curl most cycles; that is the gate working, and the fix is this recipe, not a
#     new investigation. (A standing fix would be an archive snapshot service —
#     snapshot.ubuntu.com does exist for Ubuntu, unlike packages.ros.org; not adopted
#     here, see WHAT IS PINNED above. Decide it deliberately, not mid-release.)
#
# Base-provided vs installed here (2026-08-14, same method, against the digest-pinned
# base): ca-certificates 20240203 and curl 8.5.0-2ubuntu10.6 ship IN the base and
# are UPGRADED by this layer; gnupg is absent from the base and is NEW here.
#
# WHEN A PIN DISAPPEARS the build fails loudly (both installs print the re-pin
# recipe). That is the intended §2-7 trade: a loud break beats silent drift.
#
# ★ RE-PINNING HAS TWO FAILURE MODES AND THEY NEED DIFFERENT INSTRUMENTS. Reaching
# for the wrong one is not a slow path, it is a CIRCULAR one — measured 2026-08-21
# (p5c20 F-7/F-8), which is why this block now spells both out:
#
#   (A) THE IMAGE IS STALE — you want the versions a known-good image was built
#       with (e.g. reproducing an old build, or answering "what shipped?").
#       INSTRUMENT: the last good image.
#         docker run --rm --entrypoint dpkg-query <last-good-runner-image> -W \
#           -f='${binary:Package}=${Version}\n' <the pkg names>
#       (the dpkg-query manifest this layer prints is the same instrument — keep it)
#
#   (B) THE ARCHIVE WITHDREW THE VERSION — the build just failed with
#       "E: Version 'X' for 'pkg' was not found". Ubuntu drops superseded versions
#       out of noble-updates/-security on every security bump, so the version is
#       simply GONE while the image that has it is fine.
#       ⚠ INSTRUMENT (A) IS USELESS HERE AND LOOKS LIKE IT WORKS: the last good
#       image dutifully reports the very version the archive just said does not
#       exist — of course it does, it was alive when that image was built. Applying
#       it re-pins the dead value and the next build fails identically.
#       INSTRUMENT: ASK THE ARCHIVE, from this image's own base:
#         docker run --rm --user 0 --entrypoint bash <base image> -c \
#           'apt-get update -qq && apt-cache policy curl ca-certificates gnupg'
#       Take the Candidate line. (docker/netns_audit/Dockerfile already said this;
#       this file did not, and that gap cost a cycle.)
#
# Either way: replace the versions below and RECORD the measurement — what was
# queried, from which base, on what date (G-24) — in the report and beside the pin.
USER root
RUN set -eu; \
    export DEBIAN_FRONTEND=noninteractive; \
    apt-get update; \
    apt-get install -y --no-install-recommends \
        ca-certificates=20260601~24.04.1 \
        curl=8.5.0-2ubuntu10.13 \
        gnupg=2.4.4-2ubuntu17.4 \
    || { echo "ERROR: a pinned ubuntu-archive version above is no longer in the index" \
              "(superseded versions LEAVE noble-updates/-security on every security bump)." \
              "RE-PIN by ASKING THE ARCHIVE what it serves now — NOT by reading the last" \
              "good image, which still holds the version that just failed and would send" \
              "you in a circle (measured 2026-08-21):" \
              "  docker run --rm --user 0 --entrypoint bash <this image's base> -c" \
              "    'apt-get update -qq && apt-cache policy ca-certificates curl gnupg'" \
              "Take the Candidate line, update the apt VERSION PINS block in" \
              "docker/runner/Dockerfile, record what/when/from-where (G-24), and re-verify" \
              "with a --no-cache build (D-6, CLAUDE §2-7)."; \
         exit 1; }; \
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
        ros-jazzy-ros2bag=0.26.11-1noble.20260616.083858 \
        ros-jazzy-rosbag2-storage-mcap=0.26.11-1noble.20260616.074830 \
        ros-jazzy-nav-msgs=5.3.8-1noble.20260615.112021 \
        ros-jazzy-geometry-msgs=5.3.8-1noble.20260615.110340 \
        ros-jazzy-sensor-msgs=5.3.8-1noble.20260615.112429 \
        ros-jazzy-tf2-msgs=0.36.21-1noble.20260615.112712 \
    || { echo "ERROR: a pinned ros-jazzy version above is no longer served by" \
              "packages.ros.org (that repo is rolling and withdraws superseded" \
              "versions). RE-PIN: read the versions off the last good runner image" \
              "with 'dpkg-query -W' and update the apt VERSION PINS block in" \
              "docker/runner/Dockerfile, then re-verify the recorder topics (D-6)."; \
         exit 1; }; \
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
# (HOST_ONLY_DEPS) or bundle-supplied (BUNDLE_SUPPLIED_DEPS — pydantic stack the
# kit interpreter already ships, D-4'), both guarded in the builder stage;
# host-only deps are excluded from this image on purpose — the wheel must stay
# importable on the bundle alone, which the runtime import smoke proves
# (DoD-P2-12); supervisor (M3) and CLI (M8) lazy-import docker/yaml.
#
# D-4' skew guard (G-27 mechanized): the bundled pydantic the runner will import
# must EQUAL the pyproject pin. The pin is DERIVED from the just-installed wheel's
# own metadata (Requires-Dist, generated from pyproject) — never re-recorded in
# this file (G-25: a copied pin drifts silently). Skew = loud build failure.
RUN ./python.sh -m pip install --no-cache-dir --no-deps /tmp/cv-infra-wheel/cv_infra-*.whl \
 && ./python.sh -c "\
import pydantic; from importlib.metadata import requires; \
pins = [r.split('==', 1)[1].split(';')[0].strip() \
        for r in requires('cv-infra') if r.startswith('pydantic==')]; \
assert len(pins) == 1, 'expected exactly one pydantic pin in the cv-infra wheel metadata: %r' % pins; \
assert pydantic.VERSION == pins[0], \
    'bundled pydantic %s != pyproject pin %s — D-4 skew: re-pin pyproject to the ' \
    'measured bundle (G-27) or record a new image-policy decision' % (pydantic.VERSION, pins[0]); \
print('[guard] bundled pydantic %s == pyproject pin (D-4 aligned)' % pydantic.VERSION)" \
 && rm -rf /tmp/cv-infra-wheel

# Exit-code fidelity at the container boundary (Q2 = decision
# 2026-08-06-p5c12-runner-image-plane-rulings; exit contract 0/1/2/3 = DoD-P2-07 /
# DoD-P5-13). MEASURED DEFECT: the pinned base's ./python.sh ends every interpreter
# invocation with `$python_exe … || error_exit`, and its error_exit() does a flat
# `exit 1` — so the runner's 2 (bad JOB_SPEC) and 3 (EULA boot guard / platform) are
# COLLAPSED TO 1 before `docker wait` can see them (2026-08-06 on p4c5 and 2026-08-10
# on p5c13: N=2 -> 1, N=3 -> 1; the real ENTRYPOINT's BadJobSpec 2 -> 1 too).
# REPAIR: drop that one `|| error_exit` guard (2 call sites — the normal and the
# --lldb-debug branch). The vendor script already runs under `set -e`, so it then
# exits with the interpreter's own status; the loud line error_exit printed ("There
# was an error running python") was misleading anyway — a runner exit of 2/3 is a
# CONTRACT signal, not an error. Verified in a throwaway container on this very image
# before landing (evidence: ~/cv-infra-p2-out/p5c13/exit/02,03*.log): 0/1/2/3 all
# propagate and the EULA refusal reaches `docker run` as 3.
# The call-site count is asserted BEFORE and AFTER, so a base upgrade that rewrites
# those lines fails the build LOUDLY instead of silently un-patching us (G-25/G-27
# pattern, same as the pydantic pin assert above). Patching a vendor file is the
# minimal repair available: python.sh sources the base's own setup_python_env.sh, and
# re-implementing that env glue in our layer would be reinventing (CLAUDE §5).
RUN set -eu; \
    before="$(grep -c ' || error_exit$' ./python.sh || true)"; \
    test "$before" = "2" \
    || { echo "ERROR: ./python.sh no longer has the 2 expected '|| error_exit' call sites" \
              "(found $before) — the pinned Isaac base changed its launcher: re-measure" \
              "python.sh and re-derive this exit-code patch (M5 §3.1, Q2 exit contract)"; exit 1; }; \
    sed -i 's/ || error_exit$//' ./python.sh; \
    after="$(grep -c ' || error_exit$' ./python.sh || true)"; \
    test "$after" = "0" \
    || { echo "ERROR: exit-collapse patch did not apply ($after call site(s) left)"; exit 1; }; \
    echo "[guard] python.sh exit-code collapse patched — runner 0/1/2/3 reach docker wait"

# Runner-image plane stamp (plane ③, G-43 third-plane clause): the commit this image's
# wheel was built from, as the OCI-standard label `scripts/check_plane_skew.sh --image`
# reads. Needed because the wheel carries no commit (`__version__` is pinned 0.0.0) and
# the rebuild recipe exports the build context with `git archive` (no .git in context),
# so the value can only come in as a build arg. Asserted non-empty: an unstamped image
# would be caught by the gate anyway (fail-closed), but failing at BUILD time is louder
# and cheaper. Placed last so re-stamping never invalidates the apt/wheel layers.
#   docker build --build-arg CV_SOURCE_REVISION="$(git rev-parse <X>)" …
ARG CV_SOURCE_REVISION
RUN test -n "${CV_SOURCE_REVISION}" \
    || { echo "ERROR: --build-arg CV_SOURCE_REVISION=<source commit sha> is required" \
              "(runner-image plane stamp; recipe: docs/deploy/plane-sync.md ③)"; exit 1; }
LABEL org.opencontainers.image.revision=$CV_SOURCE_REVISION

# Hash-seed pin (CLAUDE §2-7 reproducibility; G-72). CPython randomizes str hashing
# PER PROCESS, so every set/dict iteration order differs run to run — p5c15 shipped an
# instrument (telemetry.contact_partners) whose output moved with it, and the spread was
# nearly attributed to engine/scene non-determinism. The value is read at INTERPRETER
# STARTUP only, so it cannot be set from inside cv_infra; it must be in the process env.
# WHY HERE and nowhere else:
#   * the image ENV covers EVERY interpreter in this container regardless of launcher
#     (M3 supervisor, plain `docker run`, self-test), including the LD_LIBRARY_PATH
#     re-exec (os.execv inherits environ) and the `ros2 bag record` subprocess;
#   * NOT in the orchestrator's per-run env: that would cover supervisor-launched jobs
#     only, and it is M3-owned (a `docker run -e` still overrides this if ever needed);
#   * deliberately NOT in the test env — pytest must keep the randomized default so
#     hash-order defects stay catchable instead of masked (tests pin the seed themselves
#     where they assert stability: tests/test_runner_core.py).
# Placed after the apt/wheel layers (same rationale as the stamp above) so adding it
# does not invalidate them. Hash randomization is a DoS hardening for network servers;
# this container is a batch job runner with no untrusted-input hashing path.
ENV PYTHONHASHSEED=0

# Stateless runner (LOCKED §15, NFR-EXEC-002): no job state/scenario/result baked in.
# JOB_SPEC / RESULT_OUT / caches arrive as env + HOST-ABSOLUTE bind mounts (R-mount);
# one job -> exactly one result.json -> exit (M2 owns the entrypoint's content).
ENTRYPOINT ["./python.sh", "-m", "cv_infra.runner.main"]
