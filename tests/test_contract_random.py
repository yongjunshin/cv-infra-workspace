"""M1 random NOTATION tests (p6c3 T1) — the ``RandomizableFloat`` union.

What a consumer may WRITE, and what happens when they write it wrong. The
derivation itself (what the platform draws from the notation) is
tests/test_contract_derive.py; this file is the schema surface only:

* (1) vocabulary — ``uniform``/``choice`` accepted, degenerate forms legal,
  every malformed form rejected LOUDLY with the accepted words listed;
* (2) reach — exactly the 8 fields the design names are randomizable, and the
  neighbouring scalars are NOT (a distribution there must not be swallowed);
* (3) static-document invariance — the union must not move a single byte of a
  pre-p6 document (the property the identity keys and the JOB_SPEC twins rest
  on);
* (4) the friendly error keeps its fixable EXAMPLE across the union branch tag
  (NFR-INTAKE-001 — with the positive control that the example is really the
  schema's, not a constant);
* (5) the platform stamp ``scenario.derivation`` is rejected at ADMIT when a
  consumer submits it (paired with the same document minus the stamp being
  admitted — G-35: a rejection test alone is true even if nothing is admitted);
* (6) ``min_pass_ratio`` bounds (shape only this cycle — p6c4 consumes it).
"""

from __future__ import annotations

import io
import json

import pytest
import yaml
from pydantic import ValidationError

from cv_infra.contract.errors import ContractError, from_validation_error
from cv_infra.contract.loader import load_request
from cv_infra.contract.schema import (
    Choice,
    DebugObstacle,
    DerivationMeta,
    ExecutionSettings,
    Goal,
    InitialPose,
    Scenario,
    Uniform,
    VerificationRequest,
)

_STATIC_DOC = {
    "apiVersion": "cv-infra/v1",
    "scenario": {
        "scene": "nova_carter_warehouse",
        "robot": "nova_carter",
        "goal": {"x": -6.0, "y": 5.0, "yaw": 1.5708},
        "seed": 42,
        "timeout_s": 120,
        "initial_pose": {"x": -6.0, "y": -1.0, "yaw": 3.1416},
        "debug_obstacle": {"x": -6.0, "y": 2.0},
    },
    "sut": {"image_ref": "carter-sut:a"},
    "acceptance_criteria": [{"oracle": "reached_goal"}],
}


def _doc(**scenario_overrides) -> dict:
    doc = json.loads(json.dumps(_STATIC_DOC))
    doc["scenario"].update(scenario_overrides)
    return doc


def _errors(doc: dict) -> list[dict]:
    with pytest.raises(ValidationError) as excinfo:
        VerificationRequest.model_validate(doc)
    return excinfo.value.errors(include_url=False)


# --------------------------------------------------------------------------- #
# (1) vocabulary
# --------------------------------------------------------------------------- #
def test_uniform_and_choice_are_accepted_and_typed():
    goal = Goal.model_validate({"x": {"uniform": [-6.5, -5.5]}, "y": {"choice": [1.0]}, "yaw": 0.0})
    assert isinstance(goal.x, Uniform) and goal.x.uniform == [-6.5, -5.5]
    assert isinstance(goal.y, Choice) and goal.y.choice == [1.0]
    assert goal.yaw == 0.0  # the static branch stays a plain float, not a wrapper


def test_degenerate_forms_are_legal():
    """``lo == hi`` and a 1-element ``choice`` pin ONE axis while another sweeps —
    rejecting them would force a consumer to rewrite the field's SHAPE to do it."""
    assert Goal.model_validate({"x": {"uniform": [2.0, 2.0]}, "y": 0.0, "yaw": 0.0}).x.uniform == [
        2.0,
        2.0,
    ]
    assert Goal.model_validate({"x": {"choice": [2.0]}, "y": 0.0, "yaw": 0.0}).x.choice == [2.0]


@pytest.mark.parametrize(
    ("value", "expect_type"),
    [
        ({"uniform": [2.0, 1.0]}, "value_error"),  # lo > hi = a typo, not a range
        ({"uniform": [1.0]}, "too_short"),
        ({"uniform": [1.0, 2.0, 3.0]}, "too_long"),
        ({"choice": []}, "too_short"),
        ({"uniform": [float("nan"), 1.0]}, "finite_number"),
        ({"uniform": [1.0, float("inf")]}, "finite_number"),
        ({"choice": [float("-inf")]}, "finite_number"),
        ({"gaussian": [0.0, 1.0]}, "union_tag_invalid"),  # unknown vocabulary word
        ({}, "union_tag_invalid"),  # no key at all
        ({"uniform": [1.0, 2.0], "choice": [3.0]}, "union_tag_invalid"),  # two at once
        ({"uniform": [1.0, 2.0], "clamp": True}, "union_tag_invalid"),  # unknown extra key
        ("later", "float_parsing"),
        (None, "float_type"),
    ],
)
def test_malformed_notation_is_rejected_loudly(value, expect_type):
    errors = _errors(_doc(goal={"x": value, "y": 5.0, "yaw": 0.0}))
    assert [e["type"] for e in errors] == [expect_type], errors


def test_unknown_vocabulary_error_lists_the_accepted_words():
    """The consumer must learn the vocabulary FROM the rejection (NFR-INTAKE-001):
    what they wrote, and what they could have written instead."""
    (err,) = _errors(_doc(goal={"x": {"gaussian": [0.0, 1.0]}, "y": 5.0, "yaw": 0.0}))
    assert "gaussian" in err["msg"]
    for word in ("static", "uniform", "choice"):
        assert f"'{word}'" in err["msg"], err["msg"]


def test_bounds_error_names_the_bounds_and_the_fix():
    (err,) = _errors(_doc(goal={"x": {"uniform": [2.0, 1.0]}, "y": 5.0, "yaw": 0.0}))
    assert "[2.0, 1.0]" in err["msg"] and "uniform: [1.0, 2.0]" in err["msg"]


# --------------------------------------------------------------------------- #
# (2) reach — which fields are randomizable, and which deliberately are not
# --------------------------------------------------------------------------- #
_RANDOMIZABLE = (
    ("initial_pose", "x"),
    ("initial_pose", "y"),
    ("initial_pose", "yaw"),
    ("goal", "x"),
    ("goal", "y"),
    ("goal", "yaw"),
    ("debug_obstacle", "x"),
    ("debug_obstacle", "y"),
)


@pytest.mark.parametrize(("block", "field"), _RANDOMIZABLE)
def test_every_designed_field_accepts_a_distribution(block, field):
    doc = _doc()
    doc["scenario"][block][field] = {"uniform": [-1.0, 1.0]}
    request = VerificationRequest.model_validate(doc)
    assert isinstance(getattr(getattr(request.scenario, block), field), Uniform)


@pytest.mark.parametrize(
    ("block", "field"),
    [
        ("goal", "frame"),  # names a coordinate system — "a random frame" is meaningless
        ("debug_obstacle", "height"),  # optional dimension: None already means "default"
        ("debug_obstacle", "width"),
        ("debug_obstacle", "depth"),
    ],
)
def test_neighbouring_scalars_reject_a_distribution(block, field):
    """The scalars NEXT TO the randomizable ones must not swallow the notation —
    a silently-ignored ``{uniform: ...}`` is the ``goal_tolerance_m`` defect (G-25)."""
    doc = _doc()
    doc["scenario"][block][field] = {"uniform": [0.1, 0.2]}
    assert _errors(doc), f"scenario.{block}.{field} accepted a distribution"


@pytest.mark.parametrize("field", ["seed", "timeout_s"])
def test_determinism_inputs_reject_a_distribution(field):
    """seed/timeout_s are the determinism + mission budget inputs, shared by every
    sample of a request (design §0-4) — randomizing them is not in the contract."""
    doc = _doc(**{field: {"uniform": [1.0, 2.0]}})
    assert _errors(doc)


def test_model_field_sets_pin_the_notation_shape():
    assert set(Uniform.model_fields) == {"uniform"}
    assert set(Choice.model_fields) == {"choice"}
    assert set(DerivationMeta.model_fields) == {"version", "index"}
    for model in (Uniform, Choice, DerivationMeta):
        with pytest.raises(ValidationError):  # extra="forbid" at every level
            model.model_validate({**_valid_of(model), "bogus": 1})


def _valid_of(model) -> dict:
    return {
        Uniform: {"uniform": [0.0, 1.0]},
        Choice: {"choice": [0.0]},
        DerivationMeta: {"version": "cv-derive/1", "index": 0},
    }[model]


# --------------------------------------------------------------------------- #
# (3) static-document invariance — the property everything downstream rests on
# --------------------------------------------------------------------------- #
def test_a_static_document_dumps_exactly_as_before():
    """No wrapper, no tag, no shape change: a static scalar is a JSON number.

    This is what keeps ``CANONICAL_FIXTURE_KEY`` and the frozen JOB_SPEC wire
    where they are — the union adds branches, it does not re-encode the one
    every existing document already takes.
    """
    dump = VerificationRequest.model_validate(_STATIC_DOC).model_dump(mode="json", by_alias=True)
    assert dump["scenario"]["goal"] == {"x": -6.0, "y": 5.0, "yaw": 1.5708, "frame": "map"}
    assert dump["scenario"]["initial_pose"] == {"x": -6.0, "y": -1.0, "yaw": 3.1416}
    assert dump["scenario"]["debug_obstacle"]["x"] == -6.0
    assert json.dumps(dump)  # plain JSON: no NaN/Infinity, nothing unserializable


def test_integers_still_coerce_to_floats_on_the_static_branch():
    # A consumer writing `y: 5` (YAML int) got 5.0 before the union; it still does.
    goal = Goal.model_validate({"x": -6, "y": 5, "yaw": 0})
    assert (goal.x, goal.y, goal.yaw) == (-6.0, 5.0, 0.0)
    assert all(isinstance(v, float) for v in (goal.x, goal.y, goal.yaw))


def test_the_new_optional_fields_default_to_none():
    """Both p6 additions are optional-and-None, which is what makes them
    baseline-safe (they prune out of the identity projection — D-5)."""
    assert Scenario.model_validate(_STATIC_DOC["scenario"]).derivation is None
    assert ExecutionSettings().min_pass_ratio is None


# --------------------------------------------------------------------------- #
# (4) friendly error keeps its example across the union branch tag
# --------------------------------------------------------------------------- #
def test_bad_randomizable_scalar_keeps_field_path_expected_and_example():
    """A discriminated union appends its branch tag to the loc (``goal.x.static``).
    The example must survive that segment — it is the self-correcting half of the
    friendly error (NFR-INTAKE-001).

    Positive control: the rendered example is the SCHEMA's ``examples=[...]``
    (``x: -6.0`` for the goal, ``y: 2.0`` for the obstacle), not a constant.
    """
    doc = _doc(goal={"x": "later", "y": 5.0, "yaw": 0.0})
    with pytest.raises(ValidationError) as excinfo:
        VerificationRequest.model_validate(doc)
    (err,) = from_validation_error(excinfo.value, model=VerificationRequest)
    assert err.field_path == "scenario.goal.x.static"
    assert "valid number" in err.expected and err.got == "'later'"
    assert err.example == "x: -6.0"

    obstacle = _doc()
    obstacle["scenario"]["debug_obstacle"]["y"] = {"gaussian": [0.0]}
    with pytest.raises(ValidationError) as excinfo:
        VerificationRequest.model_validate(obstacle)
    (err,) = from_validation_error(excinfo.value, model=VerificationRequest)
    assert err.field_path == "scenario.debug_obstacle.y"
    assert err.example == "y: 2.0"  # the obstacle's own example, not the goal's


def test_an_unknown_key_still_has_no_example():
    """The complement of the repair above: a segment that names NO field of a
    resolvable model still yields "" — only union branch tags are walked past."""
    doc = _doc()
    doc["scenario"]["bogus"] = 1
    with pytest.raises(ValidationError) as excinfo:
        VerificationRequest.model_validate(doc)
    (err,) = from_validation_error(excinfo.value, model=VerificationRequest)
    assert err.field_path == "scenario.bogus" and err.example == ""


# --------------------------------------------------------------------------- #
# (5) the platform stamp is not a consumer field (loader stage 4)
# --------------------------------------------------------------------------- #
def _admit(doc: dict):
    return load_request(io.StringIO(yaml.safe_dump(doc)), source_path="test-doc")


def test_submitted_derivation_is_rejected_at_admit_with_a_friendly_error():
    doc = _doc()
    doc["scenario"]["derivation"] = {"version": "cv-derive/1", "index": 0}
    with pytest.raises(ContractError) as excinfo:
        _admit(doc)
    err = excinfo.value
    assert err.field_path == "scenario.derivation"
    assert "platform stamps" in err.expected
    assert "delete the 'derivation:' block" in err.example
    assert "Traceback" not in str(err)  # friendly reject, never a raw traceback


def test_the_same_document_without_the_stamp_is_admitted():
    """G-35 pair: without this, the rejection above is also true of a gate that
    admits nothing at all."""
    admitted = _admit(_doc())
    assert admitted.admitted and admitted.request.scenario.derivation is None


def test_the_stamp_offers_no_example_to_type():
    """``errors._example_for`` renders ``examples=[...]`` VERBATIM as "type this
    next" — a field a consumer must never send must not carry one."""
    assert Scenario.model_fields["derivation"].examples is None


# --------------------------------------------------------------------------- #
# (6) min_pass_ratio — shape only (p6c4 consumes it)
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("value", [0.5, 1.0, 0.001])
def test_min_pass_ratio_accepts_the_open_unit_interval(value):
    assert ExecutionSettings(min_pass_ratio=value).min_pass_ratio == value


@pytest.mark.parametrize("value", [0, -0.1, 1.01, 2])
def test_min_pass_ratio_rejects_out_of_range(value):
    with pytest.raises(ValidationError):
        ExecutionSettings(min_pass_ratio=value)


def test_initial_pose_and_debug_obstacle_still_require_their_core_keys():
    """The union widened the TYPE of x/y/yaw, not their requiredness."""
    with pytest.raises(ValidationError):
        InitialPose.model_validate({"x": 1.0, "y": 2.0})
    with pytest.raises(ValidationError):
        DebugObstacle.model_validate({"x": 1.0})
