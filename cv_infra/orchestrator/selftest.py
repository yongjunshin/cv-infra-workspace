"""Built-in stub self-test INPUT (M7 §3.1/§3.2 — REQ-SELFTEST-001/002).

WHAT THIS MODULE IS: the request the platform supplies to ITSELF, packaged as a
size-1 envelope marked ``is_self_test`` / ``origin=built-in-stub``, so an
operator can prove a fresh deployment is alive WITHOUT any consumer repo, image
or file (``NFR-SELFTEST-001``).

WHAT THIS MODULE IS NOT — 실행 로직 복제 없음 (self-test 그룹 Notes, the binding
constraint of ``REQ-SELFTEST-002``): it drives NOTHING. It imports no
fanout/queue/scheduler/supervisor/allocator/rollup/store/api code, holds no
client, and starts no job. The self-test differs from an ordinary submission in
exactly two ways:

  1. WHICH document is submitted (the built-in stub below instead of a consumer
     scenario file), and
  2. the two ENVELOPE markers that record its provenance.

Everything after ``POST /envelopes`` is the ordinary UC-01 path — the same
admit gate, the same 2-axis fan-out, the same queue/scheduler/supervisor, the
same rollup/report/store, the same operational projection. A self-test-only
execution branch would make the round-trip evidence worthless (it would prove
the self-test path works, not that the platform works).

MARKER PLACEMENT (M1 계약 그대로): ``is_self_test`` and ``origin`` are fields of
``RequestEnvelope`` — NOT of ``VerificationRequest`` (which is ``extra=forbid``,
so an ``origin`` key inside the request document is a 422). REQ-SELFTEST-001's
"빌트인 stub 요청(origin=built-in-stub)" therefore rides at the envelope level of
the size-1 envelope that contains exactly that request; the value string is
verbatim (``SELF_TEST_ORIGIN``).

THE STUB SUT HANDLE (M7 §3.5 option B — decided p5c15): this module supplies the
stub REQUEST; the platform-internal image that plays the SUT side is a deployment
artifact built from ``docker/selftest_stub/`` (recipe in its README), so a given
deployment either has it or must build it. The ref is resolved
from an explicit argument or ``CV_SELFTEST_SUT_IMAGE`` and is NEVER defaulted to
a guess — the same image-as-artifact policy ``CV_RUNNER_IMAGE`` already follows
(FU-10: no hardcoded default). Unresolved = ``SelfTestNotConfigured``, which the
CLI maps to its infra exit code; it never becomes a submission that fails
halfway with a pull error. A consumer image (e.g. the ``carter-sut:*`` of the
fixture) must never be used here — that would be exactly the external-SUT
dependency ``NFR-SELFTEST-001`` forbids.

WIRE: the body is the D-1 internal representation ``api._parse_envelope``
accepts. ``oracle_plugin_dirs`` is deliberately ABSENT — the stub uses only
entry-point oracles, so the self-test sends NO host path (G-69: a client-side
path is a deployment constraint once the control plane runs in a container).
"""

from __future__ import annotations

import os
from collections.abc import Mapping
from copy import deepcopy
from dataclasses import dataclass
from typing import Any

from cv_infra.contract.apiversion import API_VERSION

#: ``RequestEnvelope.origin`` value of the built-in stub — REQ-SELFTEST-001
#: 문면 그대로 ("origin=built-in-stub"). The audit/dashboard handle that separates
#: a platform self-test from a user submission.
SELF_TEST_ORIGIN = "built-in-stub"

#: Env name carrying the platform-internal stub SUT image ref (module docstring).
#: Deployment-level configuration (M5 compose/.env), never a consumer input.
SUT_IMAGE_ENV = "CV_SELFTEST_SUT_IMAGE"

# --- the built-in stub asset (M7 §3.3 minimal_scenario / trivial_criteria) ---
#
# PROVENANCE of every value below (no invented numbers — CLAUDE §2-4):
#
# * scene / robot: the NVIDIA-shipped sample the platform itself boots in its own
#   GPU harnesses (scripts/measure/common.sh ``CV_MEASURE_SCENE_REL`` =
#   /Isaac/Samples/ROS2/Scenario/carter_warehouse_navigation.usd) and names in its
#   own canonical fixture (tests/fixtures/nova_carter_warehouse_goal.yaml). It ships
#   with Isaac Sim — NOT a consumer asset, so using it keeps the external
#   dependency count at 0.
# * initial_pose / goal: the AMCL start pose RECORDED in that same fixture's
#   comment ("AMCL start pose is (-6.0, -1.0, yaw=pi)") and carried as the M1
#   ``InitialPose`` example. Spawn pose == goal pose is the whole trick of the
#   trivial criterion: the robot is AT the goal at t=0 by construction, so the
#   judgement needs no SUT motion and no tuned threshold.
# * acceptance_criteria: ``reached_goal`` with NO params — the oracle default
#   tolerance applies to a residual that is ~0 by construction. A threshold typed
#   here would be a fabricated measurement (M7 §8 open item 2); this stub avoids
#   needing one at all — plus the ``no_collision`` criterion below (its own
#   provenance note).
# * seed: the platform fixture's determinism seed (42), unchanged.
#
# ASSUMPTION SURFACED (verified LIVE only by the Wave-C round-trip, not here):
# the reached_goal oracle judges the GT pose against a map-frame goal, i.e. this
# scene's map frame and world frame coincide — which is how every existing live
# run of the canonical fixture has been judged (p2c5 bringup residuals). If that
# is false for the spawn pose, the self-test fails LOUDLY rather than silently.
_STUB_SCENE = "nova_carter_warehouse"
_STUB_ROBOT = "nova_carter"
_STUB_POSE = {"x": -6.0, "y": -1.0, "yaw": 3.1416}
_STUB_SEED = 42

#: ``no_collision`` params of the stub — WHY the criterion is here at all: the runner
#: builds the physics telemetry sampler and binds it PRE-reset UNCONDITIONALLY
#: (``runner/main.py`` step 2->3), and ``telemetry.bind()`` raises when it has no
#: ``chassis_path``. That key lives ONLY on ``NoCollisionParams`` (M1
#: ``contract/schema.py``; ``ReachedGoalParams`` is ``extra=forbid``), so a stub
#: declaring reached_goal ALONE dies before the readiness barrier. 판정 기준은 그 자체가
#: INPUT이므로 (REQ-INTAKE-007) 고치는 것도 입력이다 — 실행 평면 코드는 건드리지 않는다.
#:
#: PROVENANCE (values NOT invented here — CLAUDE §2-4): both prim paths are the
#: workstation measurements carried by the platform fixture, which is their source of
#: truth: ``tests/fixtures/nova_carter_warehouse_goal.yaml``,
#: ``acceptance_criteria[no_collision].params`` (today lines 104 / 107-109 — comments
#: "measured (bringup probe-01)" for the chassis body and "measured contact partners
#: (bringup run1, D-E reduction)" for the exclusions). They apply verbatim because the
#: stub resolves to the SAME asset as that fixture: scene ``nova_carter_warehouse`` ->
#: ``/Isaac/Samples/ROS2/Scenario/carter_warehouse_navigation.usd`` with robot prim
#: ``/World/Nova_Carter_ROS`` (``runner/sim_runtime.SCENE_ASSETS``).
#:
#: The exclusions are NOT decoration: the stub robot sits parked ON the ground, and the
#: measured contact partners of the chassis are exactly its own self-body subtree and
#: that floor plane — unfiltered, a motionless robot false-FAILs (the reduction
#: ``oracles/no_collision.py`` documents). With them, "no collision" is trivially true
#: for a parked robot while still proving the verdict path ran for a SECOND oracle.
_STUB_COLLISION_PARAMS: dict[str, Any] = {
    "chassis_path": "/World/Nova_Carter_ROS/chassis_link",
    "collision_excluded_paths": [
        "/World/Nova_Carter_ROS",
        "/World/warehouse_with_forklifts/GroundPlane",
    ],
}

#: Sim-time budget of the stub mission (``Scenario.timeout_s``) — an UPPER BOUND,
#: not a pass threshold: the verdict comes from where the robot IS, and it is at
#: the goal from the first frame. PROVISIONAL (M7 §8 open item 2 — "미션 지속 T ...
#: Phase 5 실측"): kept small so a health check is fast, and deliberately far below
#: the canonical fixture's 120 s mission. The Wave-C live round-trip measures what
#: the stub actually costs and confirms/adjusts this ONE number.
_STUB_TIMEOUT_S = 30.0


class SelfTestNotConfigured(RuntimeError):
    """This deployment has no built-in stub SUT handle configured (module docstring).

    A configuration condition, not a verification outcome: the CLI maps it to the
    infrastructure exit code, exactly like an unreachable orchestrator. Raised
    BEFORE anything is submitted, so no half-run envelope is left behind.
    """


@dataclass(frozen=True)
class SelfTestSubmission:
    """What to submit + what to tell the operator (the single entry point's return).

    ``body`` is POSTed VERBATIM to ``/envelopes`` by the caller (M8) using its own
    client — this module owns no HTTP. ``origin`` / ``sut_image_ref`` are the
    provenance the CLI prints so an operator sees WHICH stub handle ran.
    """

    body: dict[str, Any]
    origin: str
    sut_image_ref: str


def _stub_request_document(sut_image_ref: str) -> dict[str, Any]:
    """The built-in stub Verification Request (REQ-SELFTEST-001) as a fresh dict.

    A plain document — it is admitted by the SERVER through the real M1 6-stage
    gate like any other request (no contract bypass, no pre-admitted shortcut).
    ``interface`` is omitted so the M1 defaults apply (ros2 + the LOCKED Nav2
    pins); no sensors/odom are declared because the stub judges a parked robot.
    """
    return {
        "apiVersion": API_VERSION,
        "scenario": {
            "scene": _STUB_SCENE,
            "robot": _STUB_ROBOT,
            "initial_pose": dict(_STUB_POSE),
            "goal": {**_STUB_POSE, "frame": "map"},
            "seed": _STUB_SEED,
            "timeout_s": _STUB_TIMEOUT_S,
        },
        "sut": {"image_ref": sut_image_ref},
        "acceptance_criteria": [
            {"oracle": "reached_goal"},
            {"oracle": "no_collision", "params": deepcopy(_STUB_COLLISION_PARAMS)},
        ],
    }


def _resolve_sut_image(sut_image_ref: str | None, environ: Mapping[str, str]) -> str:
    """explicit arg > ``CV_SELFTEST_SUT_IMAGE`` > loud refusal (no invented default).

    Set-but-empty falls through to the refusal: an empty string never silently
    means "unset" and never becomes an empty image ref (G-26).
    """
    resolved = (sut_image_ref or environ.get(SUT_IMAGE_ENV) or "").strip()
    if resolved:
        return resolved
    raise SelfTestNotConfigured(
        "no built-in stub SUT handle on this deployment: set"
        f" {SUT_IMAGE_ENV}=<platform-internal stub image ref> (or pass one explicitly)."
        " The self-test never falls back to a consumer image — that would be the"
        " external-SUT dependency NFR-SELFTEST-001 forbids — and never guesses an"
        " image ref (image-as-artifact, FU-10). Build the stub image first —"
        " recipe: docker/selftest_stub/README.md."
    )


def build_self_test_submission(
    *,
    sut_image_ref: str | None = None,
    trigger_source: str | None = None,
    environ: Mapping[str, str] | None = None,
) -> SelfTestSubmission:
    """Build the size-1 self-test envelope submission (REQ-SELFTEST-001/002).

    THE single public entry point of this module (M8 wraps it as ``cv-infra
    selftest``): it returns WHAT to submit; submitting it, waiting for the
    terminal ``report_outcome`` and folding that into an exit code are the
    caller's — the ordinary ``submit --wait`` machinery, unchanged.

    Args:
        sut_image_ref: platform-internal stub SUT image ref; None = read
            ``CV_SELFTEST_SUT_IMAGE`` from ``environ``.
        trigger_source: optional envelope provenance (REQ-INTAKE-003) ridden
            verbatim; None = omitted, so the server's documented default applies
            (same omission rule as the batch CLI).
        environ: env mapping to read (defaults to ``os.environ``) — injectable so
            the resolution is unit-testable without touching the process env.

    Returns:
        ``SelfTestSubmission`` — ``body`` goes to ``POST /envelopes`` verbatim.

    Raises:
        SelfTestNotConfigured: no stub SUT handle is configured (exit-3 territory).
    """
    resolved = _resolve_sut_image(sut_image_ref, os.environ if environ is None else environ)
    body: dict[str, Any] = {
        "requests": [_stub_request_document(resolved)],  # 크기 1 (REQ-SELFTEST-002)
        "is_self_test": True,
        "origin": SELF_TEST_ORIGIN,
    }
    if trigger_source is not None:
        body["trigger_source"] = trigger_source
    return SelfTestSubmission(body=body, origin=SELF_TEST_ORIGIN, sut_image_ref=resolved)
