"""M1 — ``goal_tolerance_budget`` additive contract (CEO D-6, determinism (B)).

The reached_goal threshold may now be DERIVED from what the SUT declares
(``sut_xy_goal_tolerance_m``) plus the consumer's ``localization_budget_m``,
instead of the constant a scenario types in. These tests own the SHAPE only —
applying ``derived = sut_xy_goal_tolerance_m + localization_budget_m`` is the
oracle's (M2), landing in parallel.

What is pinned here:

* budget declared alone -> admitted, both numbers survive the wire round-trip;
* ``position_tolerance_m`` alone -> byte-identical behaviour to pre-p5c13
  (backwards-compatibility regression guard: every existing scenario);
* BOTH -> loud reject naming both keys and which one to delete (a silent
  precedence rule is the ``goal_tolerance_m`` silent-ignore pattern, G-25);
* half a budget / non-positive / unknown nested key -> reject.

The friendly-reject path is exercised through the REAL loader gate (stages
1-6), not just ``model_validate``, so the DoD-P3-02 surface (field path +
prose + YAML line/col, no traceback) is measured rather than assumed.
"""

from __future__ import annotations

import io

import pytest
from pydantic import ValidationError

from cv_infra.contract.errors import ContractError
from cv_infra.contract.loader import load_request
from cv_infra.contract.schema import GoalToleranceBudget, ReachedGoalParams, VerificationRequest

BUDGET = {"sut_xy_goal_tolerance_m": 0.25, "localization_budget_m": 0.30}

# Scenario/goal/criteria differ from the canonical fixture on purpose (DoD-P3-01
# "N different YAMLs, zero runner modification"); the canonical fixture stays
# untouched (tests/test_fixture_canonical_guard.py watches it).
_DOC_TEMPLATE = """\
apiVersion: cv-infra/v1
scenario:
  scene: small_office
  robot: nova_carter
  goal: {{x: 2.5, y: -1.0, yaw: 0.0}}
  seed: 7
  timeout_s: 90
sut:
  image_ref: carter-sut:p2
acceptance_criteria:
  - oracle: reached_goal
    params:
{params}
"""

_BUDGET_BLOCK = """\
      goal_tolerance_budget:
        sut_xy_goal_tolerance_m: 0.25
        localization_budget_m: 0.30
"""


def _yaml(params: str) -> io.StringIO:
    return io.StringIO(_DOC_TEMPLATE.format(params=params))


def _params(**kwargs) -> ReachedGoalParams:
    return ReachedGoalParams.model_validate(kwargs)


# --------------------------------------------------------------------------- #
# 1. budget declared alone -> accepted, both values round-trip
# --------------------------------------------------------------------------- #
def test_budget_alone_parses_and_round_trips_both_values():
    params = _params(goal_tolerance_budget=BUDGET)
    assert params.goal_tolerance_budget.sut_xy_goal_tolerance_m == 0.25
    assert params.goal_tolerance_budget.localization_budget_m == 0.30
    assert params.position_tolerance_m is None  # the constant path stays unused
    # wire round-trip: the block survives the dump the CLI JOB_SPEC transform
    # uses (exclude_none) and re-validates to the same model.
    dumped = params.model_dump(exclude_none=True)
    assert dumped == {"goal_tolerance_budget": BUDGET}
    assert ReachedGoalParams.model_validate(dumped) == params


def test_budget_document_admits_through_the_full_loader_gate():
    admitted = load_request(_yaml(_BUDGET_BLOCK), source_path="budget.yaml")
    assert admitted.admitted is True
    assert admitted.oracles == ("reached_goal",)  # stage-5 binding proof
    budget = admitted.request.acceptance_criteria[0].params.goal_tolerance_budget
    assert (budget.sut_xy_goal_tolerance_m, budget.localization_budget_m) == (0.25, 0.30)


# --------------------------------------------------------------------------- #
# 2. constant path untouched (backwards compatibility)
# --------------------------------------------------------------------------- #
def test_position_tolerance_only_is_unchanged():
    params = _params(position_tolerance_m=0.75)
    assert params.position_tolerance_m == 0.75
    assert params.goal_tolerance_budget is None
    # the additive field must not appear on the wire for a scenario that never
    # declared it (identity-key/JOB_SPEC surface stays byte-identical).
    assert params.model_dump(exclude_none=True) == {"position_tolerance_m": 0.75}


def test_constant_document_still_admits():
    admitted = load_request(_yaml("      position_tolerance_m: 0.75\n"), source_path="const.yaml")
    assert admitted.request.acceptance_criteria[0].params.position_tolerance_m == 0.75


# --------------------------------------------------------------------------- #
# 3. both declared -> LOUD reject (one home per threshold)
# --------------------------------------------------------------------------- #
def test_declaring_both_raises_naming_both_keys():
    with pytest.raises(ValidationError) as exc_info:
        _params(position_tolerance_m=0.75, goal_tolerance_budget=BUDGET)
    message = str(exc_info.value)
    assert "position_tolerance_m" in message and "goal_tolerance_budget" in message
    assert "0.75" in message  # the value to delete, quoted back at the author
    assert "delete" in message  # actionable, not a bare "invalid"


def test_declaring_both_rejects_through_the_loader_with_a_friendly_error():
    with pytest.raises(ContractError) as exc_info:
        load_request(
            _yaml("      position_tolerance_m: 0.75\n" + _BUDGET_BLOCK), source_path="both.yaml"
        )
    err = exc_info.value
    assert "params" in err.field_path  # located at the offending criterion's params
    assert "position_tolerance_m" in err.expected and "goal_tolerance_budget" in err.expected
    assert err.source_line is not None and err.source_col is not None
    assert "Traceback" not in str(err)  # NFR-INTAKE-001: never a raw traceback


def test_explicit_null_budget_still_means_unspecified():
    # Module contract convention: ``null`` == absent (M4 identity pruning erases
    # any other reading). So constant + ``goal_tolerance_budget: null`` is legal.
    params = _params(position_tolerance_m=0.75, goal_tolerance_budget=None)
    assert params.position_tolerance_m == 0.75


# --------------------------------------------------------------------------- #
# 4-6. malformed budgets
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize(
    "bad",
    [
        {"sut_xy_goal_tolerance_m": 0.25},  # half a budget is not a budget
        {"localization_budget_m": 0.30},
        {},
        {"sut_xy_goal_tolerance_m": 0, "localization_budget_m": 0.30},  # gt=0
        {"sut_xy_goal_tolerance_m": 0.25, "localization_budget_m": 0},
        {"sut_xy_goal_tolerance_m": -0.25, "localization_budget_m": 0.30},
        {"sut_xy_goal_tolerance_m": 0.25, "localization_budget_m": -0.30},
        {**BUDGET, "typo_budget_m": 0.1},  # unknown nested key: never swallowed (G-25)
        {"xy_goal_tolerance": 0.25, "localization_budget_m": 0.30},  # near-miss rename
    ],
)
def test_malformed_budget_rejects(bad):
    with pytest.raises(ValidationError):
        _params(goal_tolerance_budget=bad)


def test_unknown_nested_key_reports_the_exact_path_through_the_loader():
    bad_block = _BUDGET_BLOCK + "        typo_budget_m: 0.1\n"
    with pytest.raises(ContractError) as exc_info:
        load_request(_yaml(bad_block), source_path="typo.yaml")
    assert exc_info.value.field_path.endswith("goal_tolerance_budget.typo_budget_m")
    assert "not permitted" in exc_info.value.expected


# --------------------------------------------------------------------------- #
# 7. neither declared -> the oracle default path (values stay M2-owned)
# --------------------------------------------------------------------------- #
def test_neither_declared_is_valid_and_puts_nothing_on_the_wire():
    params = _params()
    assert params.position_tolerance_m is None and params.goal_tolerance_budget is None
    assert params.model_dump(exclude_none=True) == {}


# --------------------------------------------------------------------------- #
# shape pins (explicit literals — an added/renamed key is a conscious contract
# change and must touch this line too, G-25/G-17)
# --------------------------------------------------------------------------- #
def test_field_set_pins():
    assert set(GoalToleranceBudget.model_fields) == {
        "sut_xy_goal_tolerance_m",
        "localization_budget_m",
    }
    assert set(ReachedGoalParams.model_fields) == {
        "position_tolerance_m",
        "yaw_tolerance_rad",
        "goal_orientation_wxyz",
        "goal_tolerance_budget",
    }


def test_budget_docstring_carries_the_derivation_and_its_decision_trace():
    doc = GoalToleranceBudget.__doc__ or ""
    assert "derived_tolerance_m = sut_xy_goal_tolerance_m + localization_budget_m" in doc
    assert "D-6" in doc and "REQ-EXEC-005" in doc  # why the scenario is the only path


def test_full_request_with_budget_round_trips():
    doc = {
        "apiVersion": "cv-infra/v1",
        "scenario": {
            "scene": "small_office",
            "robot": "nova_carter",
            "goal": {"x": 2.5, "y": -1.0, "yaw": 0.0},
            "seed": 7,
            "timeout_s": 90,
        },
        "sut": {"image_ref": "carter-sut:p2"},
        "acceptance_criteria": [
            {"oracle": "reached_goal", "params": {"goal_tolerance_budget": BUDGET}}
        ],
    }
    request = VerificationRequest.model_validate(doc)
    dumped = request.model_dump(by_alias=True, exclude_none=True)
    assert dumped["acceptance_criteria"][0]["params"] == {"goal_tolerance_budget": BUDGET}
    assert VerificationRequest.model_validate(dumped) == request
