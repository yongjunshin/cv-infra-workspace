"""CPU unit tests — ``no_collision.collision_scope``, the opt-in that decides WHAT
counts as "the robot" in the contact reduction (AR-25, 2026-09-01).

Why the knob exists at all — two MEASURED failures pulling in opposite directions:

* pre-AR-12 (chassis prim only) answered ``collision_count == 0`` for a go2 that
  was dropped on a box, tripped, logged 263 leg<->box contacts and ended upside
  down (roll 3.14): every actor was a foot or a calf, never ``/World/Go2/base``.
* AR-12 (chassis SUBTREE) then broke the wheeled robot the other way: C4's A/B on
  one host, one scenario, one warm cache, with the runner image as the only
  variable, measured **pass / 0 collisions -> fail / 3,780** on the canonical
  carter run, ``0/5`` on carter goal_random, and the built-in self-test failing
  with ``12 chassis collision(s)`` for a robot that never moves.

So neither meaning is "the" meaning, and the scenario declares which one it wants
(``"chassis"`` default = the pre-AR-12 semantics, ``"robot"`` = the subtree).

The load-bearing test in this file is
``test_the_default_scope_is_byte_identical_to_the_pre_ar12_reduction``: the
pre-AR-12 body is reproduced VERBATIM from git (``30082de``'s form, unchanged
until AR-12's ``5d3f36e``) and the two are compared over every ordered actor pair
of a 6-path alphabet x 6 exclusion sets. "Restores the old meaning" is a claim
about a judgement, so it is checked as one instead of by reading the diff.
"""

from __future__ import annotations

import copy
import itertools
import pathlib

import pytest
from pydantic import ValidationError

from cv_infra.contract.errors import from_validation_error
from cv_infra.contract.schema import NoCollisionParams
from cv_infra.oracles.no_collision import (
    DEFAULT_COLLISION_SCOPE,
    NoCollisionOracle,
    resolve_collision_scope,
)
from cv_infra.runner import main
from cv_infra.runner.telemetry import (
    COLLISION_SCOPES,
    SCOPE_CHASSIS,
    SCOPE_ROBOT,
    ContactEvent,
    TelemetryRecord,
    _matches,
    count_real_collisions,
)

FIXTURE = pathlib.Path(__file__).parent / "fixtures" / "nova_carter_warehouse_goal.yaml"

# The canonical carter declaration (fixture ``acceptance_criteria[no_collision]``).
CHASSIS = "/World/Nova_Carter_ROS/chassis_link"
CARTER_EXCLUSIONS = [
    "/World/Nova_Carter_ROS",
    "/World/warehouse_with_forklifts/GroundPlane",
]


# --------------------------------------------------------------------------- #
# (1) The default scope IS the pre-AR-12 reduction — proven as a judgement.
# --------------------------------------------------------------------------- #
def _pre_ar12_count(events, chassis_path: str, excluded_paths: list[str]) -> int:
    """The reduction as it stood BEFORE AR-12 — verbatim (git ``30082de`` body).

    Kept as a literal copy on purpose: an "equivalent rewrite" of the reference
    would prove that two things this cycle wrote agree, not that today's default
    answers what shipped. ``_matches`` is the same private helper the old body
    called and AR-12 did not touch it.
    """
    count = 0
    for e in events:
        actors = {e.actor0_path, e.actor1_path}
        if chassis_path not in actors:
            continue  # other-link contact (articulation aggregation artifact)
        others = actors - {chassis_path}
        other = next(iter(others)) if others else chassis_path
        if any(_matches(other, ex) for ex in excluded_paths):
            continue
        count += 1
    return count


#: Every kind of actor path a carter contact report was measured to carry: the
#: chassis itself, something UNDER it, a sibling link of the robot, the robot
#: root, a floor plane, an obstacle.
ACTORS = (
    CHASSIS,
    f"{CHASSIS}/imu",
    "/World/Nova_Carter_ROS/wheel_left",
    "/World/Nova_Carter_ROS",
    "/World/warehouse_with_forklifts/GroundPlane/CollisionPlane",
    "/World/obstacle_box",
)

#: Exclusion lists to judge each pair under — including the canonical fixture's
#: pair and the degenerate ones ("nothing excluded", "the chassis itself").
EXCLUSION_SETS = (
    [],
    ["/World/Nova_Carter_ROS"],
    ["/World/warehouse_with_forklifts/GroundPlane"],
    CARTER_EXCLUSIONS,
    ["/World/obstacle_box"],
    [CHASSIS],
)

ALL_PAIRS = [ContactEvent(0.1, a0, a1) for a0, a1 in itertools.product(ACTORS, ACTORS)]


@pytest.mark.parametrize("excluded", EXCLUSION_SETS)
def test_the_default_scope_is_byte_identical_to_the_pre_ar12_reduction(excluded):
    """Every ordered actor pair (36) x every exclusion set: same verdict as before.

    Both the per-event judgement and the aggregate are compared — an off-by-one
    in the aggregation would survive a per-event-only check.
    """
    for event in ALL_PAIRS:
        assert count_real_collisions([event], CHASSIS, excluded) == _pre_ar12_count(
            [event], CHASSIS, excluded
        ), f"pair {event.actor0_path} <-> {event.actor1_path} under {excluded}"
    assert count_real_collisions(ALL_PAIRS, CHASSIS, excluded) == _pre_ar12_count(
        ALL_PAIRS, CHASSIS, excluded
    )


def test_the_equality_above_is_not_vacuous_the_widened_scope_answers_differently():
    """Positive control: the same corpus, the same exclusions, ``scope="robot"``
    -> a DIFFERENT number. Without this, the equality test would still pass if
    both scopes had collapsed into one meaning."""
    chassis_count = count_real_collisions(ALL_PAIRS, CHASSIS, CARTER_EXCLUSIONS)
    robot_count = count_real_collisions(ALL_PAIRS, CHASSIS, CARTER_EXCLUSIONS, SCOPE_ROBOT)
    assert robot_count > chassis_count
    assert chassis_count == _pre_ar12_count(ALL_PAIRS, CHASSIS, CARTER_EXCLUSIONS)


def test_the_default_argument_is_the_default_scope():
    """The reduction's own fallback and the oracle's resolved default are one
    value — two spellings of "chassis" would drift the metric from the verdict."""
    assert DEFAULT_COLLISION_SCOPE == SCOPE_CHASSIS
    assert count_real_collisions(ALL_PAIRS, CHASSIS, CARTER_EXCLUSIONS) == count_real_collisions(
        ALL_PAIRS, CHASSIS, CARTER_EXCLUSIONS, DEFAULT_COLLISION_SCOPE
    )


# --------------------------------------------------------------------------- #
# (2) The C4 regression, in miniature: a clean carter run and the parked stub.
# --------------------------------------------------------------------------- #
#: A floor prim the carter declarations do NOT name. C4 measured that such a prim
#: exists in this scene (its exclusions "그 바닥 프림을 이름으로 덮지 못한다") but the
#: exact path was not recoverable — the product path preserves no runner stdout
#: (C4 §7-1), so this stands for the CLASS, not for a measured prim path.
UNNAMED_FLOOR = "/World/GroundPlane/CollisionPlane"

#: What a CLEAN carter drive reports: wheel/caster <-> floor, forever (7344 such
#: events on the p2c5 run1 that first documented the articulation aggregation).
CLEAN_DRIVE = [
    ContactEvent(0.1 * i, f"/World/Nova_Carter_ROS/wheel_{i % 4}", UNNAMED_FLOOR) for i in range(12)
]


def test_a_clean_carter_run_counts_zero_again_and_the_widened_scope_is_why_it_did_not():
    """The A/B in one assert pair: old runner pass/0 vs RC runner fail/3,780."""
    assert count_real_collisions(CLEAN_DRIVE, CHASSIS, CARTER_EXCLUSIONS) == 0
    assert count_real_collisions(CLEAN_DRIVE, CHASSIS, CARTER_EXCLUSIONS, SCOPE_ROBOT) == 12


def test_the_selftest_stub_request_needs_no_edit():
    """M7's built-in stub is an INPUT (REQ-INTAKE-007) and this repair must not
    ask it to change: it declares no scope, so it gets the pre-AR-12 meaning, and
    the parked robot that failed live with ``12 chassis collision(s)`` passes."""
    from cv_infra.orchestrator.selftest import _STUB_COLLISION_PARAMS

    assert "collision_scope" not in _STUB_COLLISION_PARAMS  # untouched by AR-25
    assert resolve_collision_scope(_STUB_COLLISION_PARAMS) == SCOPE_CHASSIS

    record = TelemetryRecord(contact_events=list(CLEAN_DRIVE))
    outcome = NoCollisionOracle().evaluate(record, _STUB_COLLISION_PARAMS)
    assert outcome.passed and outcome.detail == "no chassis collisions"


# --------------------------------------------------------------------------- #
# (3) The go2 side: the default re-hides what AR-12 found, the opt-in shows it.
# --------------------------------------------------------------------------- #
GO2_BASE = "/World/Go2/base"
GO2_GROUNDS = [
    "/World/GroundPlane/collisionPlane",
    "/World/Warehouse_Empty_small_realtime/GroundPlane/CollisionPlane",
]
#: C1's measured overturn: leg<->box contacts only, the base never touching.
GO2_OVERTURN = [
    ContactEvent(0.1, "/World/Go2/FL_foot", GO2_GROUNDS[0]),  # walking -> excluded
    ContactEvent(0.2, GO2_GROUNDS[1], "/World/Go2/RR_calf"),  # walking -> excluded
    ContactEvent(0.3, "/World/Go2/FL_calf", "/World/cv_obstacles/box_0"),
    ContactEvent(0.4, "/World/Go2/RL_calf", "/World/cv_obstacles/box_0"),
]


def test_a_legged_robot_must_declare_the_robot_scope_to_be_judged_at_all():
    """Stated as the cost of the default, not hidden by it: under ``"chassis"``
    the overturn reads 0 (the AR-12 defect), under ``"robot"`` it reads 2."""
    assert count_real_collisions(GO2_OVERTURN, GO2_BASE, GO2_GROUNDS) == 0
    assert count_real_collisions(GO2_OVERTURN, GO2_BASE, GO2_GROUNDS, SCOPE_ROBOT) == 2


def test_the_widened_scope_still_excludes_ground_and_self():
    """Opting in does not opt out of the D-E filter — the two walking contacts
    above stay excluded, which is why a go2 scenario must declare its floors."""
    unfiltered = count_real_collisions(GO2_OVERTURN, GO2_BASE, [], SCOPE_ROBOT)
    assert unfiltered == 4  # ...and 2 of those 4 are the robot walking


def test_the_oracle_detail_names_the_scope_it_judged_with():
    record = TelemetryRecord(contact_events=list(GO2_OVERTURN))
    criteria = {
        "chassis_path": GO2_BASE,
        "collision_excluded_paths": GO2_GROUNDS,
        "collision_scope": SCOPE_ROBOT,
    }
    outcome = NoCollisionOracle().evaluate(record, criteria)
    assert not outcome.passed and outcome.reason == "collision"
    assert outcome.detail == "2 robot collision(s) after ground/self filter"
    # ...and under the default the same sentence is the one that always shipped.
    hit = TelemetryRecord(contact_events=[ContactEvent(0.1, CHASSIS, "/World/obstacle_box")])
    failed = NoCollisionOracle().evaluate(hit, {"chassis_path": CHASSIS})
    assert failed.detail == "1 chassis collision(s) after ground/self filter"


# --------------------------------------------------------------------------- #
# (4) Resolution + LOUD refusal of an unusable value.
# --------------------------------------------------------------------------- #
def test_absent_and_null_both_mean_the_default():
    assert resolve_collision_scope({"chassis_path": CHASSIS}) == SCOPE_CHASSIS
    assert resolve_collision_scope({"collision_scope": None}) == SCOPE_CHASSIS


def test_a_declared_scope_is_honored():
    assert resolve_collision_scope({"collision_scope": SCOPE_ROBOT}) == SCOPE_ROBOT
    assert resolve_collision_scope({"collision_scope": SCOPE_CHASSIS}) == SCOPE_CHASSIS


def test_an_unknown_scope_is_refused_pre_boot_not_silently_defaulted():
    """A criteria dict that did not come through the contract (hand-built, custom
    entrypoint) must not be judged with a meaning nobody asked for."""
    bad = {"chassis_path": CHASSIS, "collision_scope": "Robot"}  # case matters
    with pytest.raises(ValueError, match="collision_scope"):
        resolve_collision_scope(bad)
    with pytest.raises(ValueError, match="collision_scope"):
        NoCollisionOracle().validate_params(bad)  # -> exit 2, before any GPU second
    with pytest.raises(ValueError, match="collision_scope"):
        count_real_collisions([], CHASSIS, [], "Robot")


def test_validate_params_still_requires_the_chassis_path():
    with pytest.raises(ValueError, match="chassis_path"):
        NoCollisionOracle().validate_params({"collision_scope": SCOPE_ROBOT})


def test_the_scope_vocabulary_has_one_definition():
    """The contract's Literal and the reduction's tuple must name the same set —
    two lists would let a value validate and then be refused at evaluation."""
    literal = NoCollisionParams.model_fields["collision_scope"].annotation
    declared = set(literal.__args__[0].__args__)  # Literal[...] | None
    assert declared == set(COLLISION_SCOPES)


# --------------------------------------------------------------------------- #
# (5) The wire: contract -> criteria_view -> resolver, and the identity key.
# --------------------------------------------------------------------------- #
def _job_spec(params: dict) -> dict:
    return {
        "job_id": "job-0001",
        "scenario": {
            "scene": "nova_carter_warehouse",
            "robot": "nova_carter",
            "goal": {"x": -6.0, "y": 5.0, "yaw": 1.5708},
            "seed": 42,
            "timeout_s": 120.0,
        },
        "sut_image_ref": "carter-sut:p2",
        "interface": {"type": "ros2", "adapter_config": {}},
        "acceptance_criteria": [{"oracle": "no_collision", "params": params}],
    }


def test_a_declared_scope_reaches_the_reduction_through_the_real_parse():
    request, _ = main.parse_request(
        _job_spec({"chassis_path": GO2_BASE, "collision_scope": SCOPE_ROBOT})
    )
    view = main.criteria_view(request)
    assert view["collision_scope"] == SCOPE_ROBOT
    assert resolve_collision_scope(view) == SCOPE_ROBOT


def test_an_undeclared_scope_never_reaches_the_view_at_all():
    """``criteria_view`` merges params with ``exclude_none``, so the absent key
    stays absent and the oracle's own default decides (M2 owns the VALUE)."""
    request, _ = main.parse_request(_job_spec({"chassis_path": CHASSIS}))
    view = main.criteria_view(request)
    assert "collision_scope" not in view
    assert resolve_collision_scope(view) == SCOPE_CHASSIS


def test_growing_this_field_moves_no_existing_requests_identity_key():
    """Held to the bar the p7 ``obstacles`` growth was held to: the new key is
    ``null`` on every pre-AR-25 request, the identity projection prunes nulls, so
    every live baseline row keeps matching (a moved key = silent ``no-baseline``).
    The pin is IMPORTED, never retyped."""
    from cv_infra.contract.loader import load_request
    from cv_infra.report.regression import identity_key
    from tests.test_report_regression import CANONICAL_FIXTURE_KEY

    dump = load_request(FIXTURE).request.model_dump(mode="json", by_alias=True)
    params = dump["acceptance_criteria"][1]["params"]
    assert params["collision_scope"] is None, "the AR-25 field must be in the dump"
    pre_growth = copy.deepcopy(dump)
    del pre_growth["acceptance_criteria"][1]["params"]["collision_scope"]  # the pre-AR-25 wire
    assert identity_key(dump) == identity_key(pre_growth) == CANONICAL_FIXTURE_KEY


def test_declaring_the_scope_does_move_the_key_which_is_the_price_of_writing_it():
    """The counter-example that makes the test above a fact about pruning rather
    than about the field being ignored: a scenario that WRITES the key (even the
    default value) is a different request and starts a new baseline."""
    from cv_infra.contract.loader import load_request
    from cv_infra.report.regression import identity_key
    from tests.test_report_regression import CANONICAL_FIXTURE_KEY

    dump = load_request(FIXTURE).request.model_dump(mode="json", by_alias=True)
    declared = copy.deepcopy(dump)
    declared["acceptance_criteria"][1]["params"]["collision_scope"] = SCOPE_CHASSIS
    assert identity_key(declared) != CANONICAL_FIXTURE_KEY


def test_a_typo_is_rejected_at_admit_with_a_fixable_example():
    with pytest.raises(ValidationError) as exc_info:
        NoCollisionParams.model_validate({"chassis_path": CHASSIS, "collision_scope": "subtree"})
    (err,) = from_validation_error(exc_info.value, model=NoCollisionParams)
    assert err.field_path == "collision_scope"
    assert "chassis" in err.expected and "robot" in err.expected
    assert err.example == "collision_scope: robot"


def test_unknown_keys_are_still_forbidden():
    """extra=forbid is unchanged — the near-miss spelling must not slip in."""
    with pytest.raises(ValidationError):
        NoCollisionParams.model_validate({"chassis_path": CHASSIS, "chassis_subtree": True})
