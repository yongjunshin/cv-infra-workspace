"""CPU unit tests — reached_goal position tolerance DERIVED from the SUT's own
declared budget (CEO D-6, p5c11 decision 2026-08-05 §D-6).

Why this exists: the verdict used to compare the GT closest approach against a
CONSTANT baked into the scenario (``position_tolerance_m: 0.75``), and p5c10/p5c11
measured that this constant sits INSIDE the tail of the GT residual distribution
(k=1: 0.216-0.557, spread 0.341, margin +0.193 < its own spread; k=8: 0.088-0.824,
margin -0.074). D-6 answers that by deriving the tolerance from what the SUT
declares it achieves (``sut_xy_goal_tolerance_m`` + ``localization_budget_m``)
instead of raising the constant -- raising it is explicitly NOT (B) (G-55 (4)).

What these tests hold down:
* the derivation, its LOUD failure modes, and the untouched legacy/default paths;
* the APPLIED number being auditable after the fact (outcome detail -> result.json);
* **regression-detection power**: the p5c8-style crippled SUT (nav2 DWB
  ``max_vel_x`` 0.8 -> 0.02, goal never reached) must still FAIL under the derived
  tolerance. A derived tolerance that passes that SUT would be a failed design.

Contract shape (verbatim pin, task runner-2026-08-10-p5c13-derived-tolerance): the
budget is a NESTED mapping under the reached_goal params. The criteria dicts below
are the flattened ``criteria_view`` shape the oracles actually receive at runtime
(M1's typed params model dumps the nested block as a plain dict).
"""

# ⚠ 이 파일이 인용하는 워크스테이션 증적 경로(`~/cv-infra-p2-out/**`·`~/cv-infra-ci/**`)는
# 2026-08-20 **의도적으로 만료**됐다 — 측정이 일어난 사실은 유효하고, 재현은 불가하다.
# 목록·sha256 매니페스트의 소재와 그 구분은 docs/evidence-anchors.md 를 보라.

from __future__ import annotations

import inspect
import re

import pytest

from cv_infra.oracles.reached_goal import (
    DEFAULT_POS_TOL_M,
    TOL_FROM_BUDGET,
    TOL_FROM_CONSTANT,
    TOL_FROM_DEFAULT,
    ReachedGoalOracle,
    resolve_position_tolerance,
)
from cv_infra.runner import main
from cv_infra.runner.evaluate import VERDICT_PASS, build_result_dict, fold_verdict
from cv_infra.runner.telemetry import PoseSample, TelemetryRecord, time_to_goal_s

# Canonical mission geometry (tests/fixtures/nova_carter_warehouse_goal.yaml):
# AMCL start pose (-6.0, -1.0) -> goal (-6.0, 5.0) = a 6 m straight lane drive.
GOAL_XY = (-6.0, 5.0)
START_XY = (-6.0, -1.0)
MISSION_LEN_M = 6.0
TIMEOUT_S = 120.0

# The task's verbatim contract example: 0.25 + 0.30 -> 0.55 m derived.
# G-28 provenance for the SUT-side term (measured 2026-08-10, read-only container
# on the workstation, no GPU): carter-sut:p5c5-slim declares
# ``general_goal_checker.xy_goal_tolerance: 0.25`` (and DWB's own 0.25) in
# /opt/carter_ws/install/share/carter_navigation/params/carter_navigation_params.yaml
# -- so 0.25 is what this SUT actually declares, not a placeholder. The
# localization term is a CONSUMER declaration and is NOT measured anywhere yet.
SUT_XY_TOL_M = 0.25
LOC_BUDGET_M = 0.30
DERIVED_TOL_M = SUT_XY_TOL_M + LOC_BUDGET_M
FIXTURE_CONSTANT_TOL_M = 0.75  # what the canonical scenario declares today


def _samples(stop_short_m: float, *, speed_mps: float = 0.5, dt: float = 1.0) -> list[PoseSample]:
    """GT samples driving the lane and stopping ``stop_short_m`` short of the goal.

    Monotone approach, so the closest approach IS ``stop_short_m`` -- the single
    continuous quantity the verdict thresholds.
    """
    travelled = MISSION_LEN_M - stop_short_m
    steps = max(1, round(travelled / (speed_mps * dt)))
    step_m = travelled / steps
    return [
        PoseSample(
            sim_time_s=i * dt,
            position=(GOAL_XY[0], START_XY[1] + i * step_m, 0.0),
            orientation_wxyz=(1.0, 0.0, 0.0, 0.0),
        )
        for i in range(steps + 1)
    ]


def _record(stop_short_m: float, **kwargs) -> TelemetryRecord:
    return TelemetryRecord(gt_pose_samples=_samples(stop_short_m, **kwargs), contact_events=[])


def _budget_criteria(sut_xy: object = SUT_XY_TOL_M, loc: object = LOC_BUDGET_M) -> dict:
    return {
        "goal_position": [GOAL_XY[0], GOAL_XY[1], 0.0],
        "timeout_s": TIMEOUT_S,
        "goal_tolerance_budget": {
            "sut_xy_goal_tolerance_m": sut_xy,
            "localization_budget_m": loc,
        },
    }


def _constant_criteria(tol: float = FIXTURE_CONSTANT_TOL_M) -> dict:
    return {
        "goal_position": [GOAL_XY[0], GOAL_XY[1], 0.0],
        "timeout_s": TIMEOUT_S,
        "position_tolerance_m": tol,
    }


# --------------------------------------------------------------------------- #
# The derivation itself (one home: cv_infra/oracles/reached_goal.py).
# --------------------------------------------------------------------------- #
def test_budget_derives_the_sum_and_names_its_source():
    decision = resolve_position_tolerance(_budget_criteria())
    assert decision.value_m == pytest.approx(DERIVED_TOL_M)
    assert decision.source == TOL_FROM_BUDGET
    # Auditable: the audit line carries the applied number AND both declared terms.
    assert "0.550" in decision.audit
    assert "sut_xy_goal_tolerance_m 0.250" in decision.audit
    assert "localization_budget_m 0.300" in decision.audit


def test_constant_path_unchanged_when_no_budget_declared():
    """Backward compatibility (undeclaring SUTs): behaviour change 0."""
    decision = resolve_position_tolerance(_constant_criteria())
    assert decision.value_m == pytest.approx(FIXTURE_CONSTANT_TOL_M)
    assert decision.source == TOL_FROM_CONSTANT


def test_neither_declared_falls_back_to_the_oracle_default():
    decision = resolve_position_tolerance({"goal_position": [0.0, 0.0, 0.0]})
    assert decision.value_m == pytest.approx(DEFAULT_POS_TOL_M)
    assert decision.source == TOL_FROM_DEFAULT


def test_declaring_both_is_loud_and_names_both_keys():
    """One home per field -- never a silent precedence rule (G-25 class)."""
    criteria = _budget_criteria() | {"position_tolerance_m": FIXTURE_CONSTANT_TOL_M}
    with pytest.raises(ValueError) as exc_info:
        resolve_position_tolerance(criteria)
    message = str(exc_info.value)
    assert "goal_tolerance_budget" in message and "position_tolerance_m" in message


@pytest.mark.parametrize("missing", ["sut_xy_goal_tolerance_m", "localization_budget_m"])
def test_half_a_budget_is_loud_not_treated_as_zero(missing):
    criteria = _budget_criteria()
    del criteria["goal_tolerance_budget"][missing]
    with pytest.raises(ValueError) as exc_info:
        resolve_position_tolerance(criteria)
    assert missing in str(exc_info.value)


@pytest.mark.parametrize("bad", [0.0, -0.1])
def test_non_positive_budget_term_is_loud(bad):
    with pytest.raises(ValueError):
        resolve_position_tolerance(_budget_criteria(sut_xy=bad))


def test_non_numeric_budget_term_is_loud():
    with pytest.raises(ValueError):
        resolve_position_tolerance(_budget_criteria(loc="thirty centimetres"))


def test_validate_params_rejects_pre_boot_and_maps_to_exit_2():
    """The contract violation lands on the usage path (exit 2), pre-sim, not as a
    mid-mission crash (exit 3): ``validate_oracle_params`` is the runner's pre-boot
    gate (D-1 2026-07-13)."""
    criteria = _budget_criteria() | {"position_tolerance_m": FIXTURE_CONSTANT_TOL_M}
    with pytest.raises(ValueError):
        ReachedGoalOracle().validate_params(criteria)
    with pytest.raises(main.BadJobSpec):
        main.validate_oracle_params([ReachedGoalOracle()], criteria)


# --------------------------------------------------------------------------- #
# The verdict taken with the derived number.
# --------------------------------------------------------------------------- #
def test_budget_path_passes_a_run_inside_the_derived_tolerance():
    out = ReachedGoalOracle().evaluate(_record(0.40), _budget_criteria())
    assert out.passed is True
    assert "0.550" in out.detail  # the number that judged it, in result.json


def test_budget_path_fails_a_run_the_old_constant_would_have_passed():
    """0.65 m residual: inside the scenario constant (0.75) -- OUTSIDE the derived
    0.55. The derived tolerance is TIGHTER here, which is the direction D-6 asks
    for (it forbids raising the threshold)."""
    record = _record(0.65)
    assert ReachedGoalOracle().evaluate(record, _constant_criteria()).passed is True
    out = ReachedGoalOracle().evaluate(record, _budget_criteria())
    assert out.passed is False
    assert out.reason in {"not_reached", "timeout"}
    assert "0.650" in out.detail and "0.550" in out.detail  # observed vs applied


def test_p5c11_observed_k1_max_residual_is_outside_the_derived_tolerance():
    """Honesty tripwire, not a celebration: p5c11 arm A (k=1, N=20) observed a GT
    closest approach up to 0.557 m, which is 7 mm ABOVE the 0.550 derived from the
    contract's example budget. So this change does NOT put the observed residual
    distribution safely inside the threshold -- it relocates the threshold (tighter,
    not looser), and the live verdict may flip (Wave B measures it).

    Counterfactual re-judgment of p5c11's raw residuals (~/cv-infra-p2-out/p5c11/
    rows-k{1,8}.json, N=40) at 0.550 vs the 0.750 constant: k=1 1/20 vs 0/20 fail,
    k=8 9/20 vs 1/20 fail. DoD-P2-06 is NOT closed by this change.

    If a later edit inflates the derivation until this run passes, that edit is the
    forbidden threshold raise (G-55 (4)) and this test goes red on purpose."""
    out = ReachedGoalOracle().evaluate(_record(0.557), _budget_criteria())
    assert out.passed is False


# --------------------------------------------------------------------------- #
# Regression-detection power -- the pass/fail criterion of this whole change.
# --------------------------------------------------------------------------- #
def test_crippled_sut_still_fails_under_the_derived_tolerance():
    """p5c8's intentional regression SUT: nav2 DWB ``max_vel_x`` 0.8 -> 0.02 m/s, so
    the robot crawls ~2.4 m of the 6 m lane inside the 120 s sim budget and never
    arrives. It MUST still fail once the tolerance is derived -- a derived tolerance
    that swallows a 3.6 m miss would have destroyed the product's regression
    detection (cycle plan p5c13 §2, honesty constraint).

    G-28 provenance: the 0.02 m/s is the REAL crippled artifact's value --
    ``carter-sut:p5c8-fail`` carries ``max_vel_x: 0.02`` (measured 2026-08-10,
    read-only container on the workstation, no GPU)."""
    crawl = _record(MISSION_LEN_M - 0.02 * TIMEOUT_S, speed_mps=0.02)
    out = ReachedGoalOracle().evaluate(crawl, _budget_criteria())
    assert out.passed is False
    assert out.reason in {"not_reached", "timeout"}
    assert fold_verdict([out]) != VERDICT_PASS
    assert "3.600" in out.detail  # the miss is reported, not hidden


def test_crippled_sut_fails_on_every_tolerance_path():
    """Same crawl, all three tolerance sources -- the detection power does not
    depend on WHICH path decided the number (input diversity, G-59)."""
    crawl = _record(MISSION_LEN_M - 0.02 * TIMEOUT_S, speed_mps=0.02)
    for criteria in (
        _budget_criteria(),
        _constant_criteria(),
        {"goal_position": [GOAL_XY[0], GOAL_XY[1], 0.0], "timeout_s": TIMEOUT_S},
    ):
        assert ReachedGoalOracle().evaluate(crawl, criteria).passed is False


def test_applied_tolerance_reaches_result_json_for_pass_and_fail():
    """Post-hoc audit (task requirement 2): result.json must show WHICH number
    judged the run, on the failing path above all."""
    oracle = ReachedGoalOracle()
    for record in (_record(0.40), _record(0.65)):
        outcome = oracle.evaluate(record, _budget_criteria())
        result = build_result_dict("job-0001", fold_verdict([outcome]), [outcome], {})
        (criterion,) = result["criteria_results"]
        assert "0.550" in criterion["detail"]
        assert TOL_FROM_BUDGET in criterion["detail"]


# --------------------------------------------------------------------------- #
# The two decision surfaces cannot drift (G-25): oracle verdict vs the
# ``time_to_goal_s`` metric in runner.main.
# --------------------------------------------------------------------------- #
def test_main_has_no_second_tolerance_read_site():
    """``runner.main`` used to re-read ``position_tolerance_m`` AND re-type the 0.25
    default for its metric -- a silent second decision surface. It must take the
    number from the single home instead."""
    source = inspect.getsource(main)
    assert "resolve_position_tolerance" in source
    assert not re.search(r"""read_field\(\s*criteria\s*,\s*["']position_tolerance_m""", source)


@pytest.mark.parametrize(
    ("criteria_factory", "stop_short_m", "reaches"),
    [
        (_budget_criteria, 0.40, True),  # inside derived 0.55
        (_budget_criteria, 0.65, False),  # outside derived 0.55...
        (_constant_criteria, 0.65, True),  # ...but inside the constant 0.75
        (_constant_criteria, 0.90, False),
    ],
)
def test_metric_and_verdict_agree_on_goal_reach(criteria_factory, stop_short_m, reaches):
    """``time_to_goal_s`` (main's metric) and the oracle verdict must agree about
    "reached", on BOTH tolerance branches -- otherwise result.json reports a
    time-to-goal for a run the verdict calls a miss (or vice versa)."""
    criteria = criteria_factory()
    record = _record(stop_short_m)
    tolerance = resolve_position_tolerance(criteria)  # exactly what main.py does
    goal_xyz = (GOAL_XY[0], GOAL_XY[1], 0.0)
    metric = time_to_goal_s(record.gt_pose_samples, goal_xyz, tolerance.value_m)
    assert (metric is not None) is reaches
    assert ReachedGoalOracle().evaluate(record, criteria).passed is reaches
