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
* (6) ``min_pass_ratio`` bounds (shape only this cycle — p6c4 consumes it);
* (7) the p7 obstacle notation — the COUNT axis (``randint``, its own union),
  what an ``Obstacle`` may say, and the two loud rules that keep a stage from
  quietly disagreeing with the document (dimensions are the box's only; the
  legacy single box and the list are mutually exclusive).
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
    BUILTIN_BOX_ASSET,
    MAX_OBSTACLE_COUNT,
    Choice,
    DebugObstacle,
    DerivationMeta,
    ExecutionSettings,
    Goal,
    InitialPose,
    Obstacle,
    Randint,
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
    assert set(Randint.model_fields) == {"randint"}
    assert set(DerivationMeta.model_fields) == {"version", "index"}
    for model in (Uniform, Choice, Randint, DerivationMeta):
        with pytest.raises(ValidationError):  # extra="forbid" at every level
            model.model_validate({**_valid_of(model), "bogus": 1})


def _valid_of(model) -> dict:
    return {
        Uniform: {"uniform": [0.0, 1.0]},
        Choice: {"choice": [0.0]},
        Randint: {"randint": [0, 2]},
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


# --------------------------------------------------------------------------- #
# (7) obstacles — the count axis and the Obstacle block (p7)
# --------------------------------------------------------------------------- #
_BOX = {"asset": BUILTIN_BOX_ASSET, "x": -6.0, "y": 2.0}


def _obstacle_errors(**overrides) -> list[dict]:
    with pytest.raises(ValidationError) as excinfo:
        Obstacle.model_validate({**_BOX, **overrides})
    return excinfo.value.errors(include_url=False)


@pytest.mark.parametrize("bounds", [[0, 0], [2, 2], [0, MAX_OBSTACLE_COUNT], [0, 5]])
def test_randint_accepts_the_closed_interval(bounds):
    """``lo == hi`` is legal for the same reason it is on ``uniform``: pinning one
    group's count while another sweeps must not force a shape rewrite."""
    assert Obstacle.model_validate({**_BOX, "count": {"randint": bounds}}).count.randint == bounds


@pytest.mark.parametrize(
    ("count", "expect_type"),
    [
        ({"randint": [5, 0]}, "value_error"),  # lo > hi = a typo, not a descending range
        ({"randint": [-1, 2]}, "greater_than_equal"),  # there is no negative count
        ({"randint": [0, 99]}, "less_than_equal"),  # MAX_OBSTACLE_COUNT: expansion runaway
        ({"randint": [3]}, "too_short"),
        ({"randint": [1, 2, 3]}, "too_long"),
        (2.5, "int_from_float"),  # a count is an integer, never rounded into one
        (None, "int_type"),  # non-optional on purpose (a pruned null forks identity)
        (-1, "greater_than_equal"),
        (MAX_OBSTACLE_COUNT + 1, "less_than_equal"),
    ],
)
def test_malformed_count_is_rejected_loudly(count, expect_type):
    assert [e["type"] for e in _obstacle_errors(count=count)] == [expect_type]


def test_randint_bounds_error_names_the_bounds_and_the_fix():
    (err,) = _obstacle_errors(count={"randint": [5, 0]})
    assert "[5, 0]" in err["msg"] and "randint: [0, 5]" in err["msg"]


def test_the_count_axis_has_its_own_vocabulary_and_says_so():
    """A count is not a float axis: writing the FLOAT notation there must teach
    the consumer the words that DO belong (NFR-INTAKE-001), not silently coerce."""
    (err,) = _obstacle_errors(count={"uniform": [0, 5]})
    assert err["type"] == "union_tag_invalid"
    assert "uniform" in err["msg"]
    for word in ("static", "randint"):
        assert f"'{word}'" in err["msg"], err["msg"]


@pytest.mark.parametrize("field", ["x", "y", "yaw"])
@pytest.mark.parametrize("notation", [{"uniform": [-1.0, 1.0]}, {"choice": [0.0, 1.5708]}])
def test_placement_fields_accept_a_distribution(field, notation):
    obstacle = Obstacle.model_validate({**_BOX, field: notation})
    assert isinstance(getattr(obstacle, field), (Uniform, Choice))


@pytest.mark.parametrize("field", ["asset", "height", "width", "depth"])
def test_the_obstacles_neighbouring_scalars_reject_a_distribution(field):
    """Dimensions carry the ``DebugObstacle`` reason (None already means "default",
    so a distribution would need a third state); ``asset`` is a name, and "a random
    asset" is a choice the consumer must write as one, not smuggle past the union."""
    assert _obstacle_errors(**{field: {"uniform": [0.1, 0.2]}})


def test_dimensions_belong_to_the_built_in_box_only():
    """A USD asset carries its own extent, so height/width/depth have nowhere to
    land — silently ignoring them is the ``goal_tolerance_m`` defect (G-25).

    G-35 pair: the same asset WITHOUT dimensions is accepted, and the box WITH
    them is accepted — so the rejection is about the combination, not about
    ``chair`` being unusable.
    """
    box = Obstacle.model_validate({**_BOX, "height": 0.5, "width": 1.2, "depth": 0.4})
    assert (box.height, box.width, box.depth) == (0.5, 1.2, 0.4)
    assert Obstacle.model_validate({**_BOX, "asset": "chair"}).height is None

    (err,) = _obstacle_errors(asset="chair", height=0.5)
    assert "'height'" in err["msg"] and "chair" in err["msg"]
    assert f"asset: {BUILTIN_BOX_ASSET}" in err["msg"]  # the fix is spelled out


@pytest.mark.parametrize("asset", ["box", "chair", "warehouse_desk", "/mnt/assets/car.usd"])
def test_asset_is_a_free_name_not_a_literal_enum(asset):
    """The registry of asset NAMES is the runner's (M2), exactly as ``scene`` is:
    duplicating it here as a ``Literal[...]`` would make adding one asset a
    contract change and let the two planes drift (G-25)."""
    assert Obstacle.model_validate({**_BOX, "asset": asset}).asset == asset
    assert _obstacle_errors(asset="")  # but it must NAME something


def test_the_legacy_box_and_the_list_are_mutually_exclusive():
    """Declaring both would stand a box AND the list on the stage while the
    document reads as if the list replaced the field — an unmeasurable extra
    obstacle. G-35 pair: each one ALONE is accepted.
    """
    both = _doc(obstacles=[{"asset": "box", "x": 1.0, "y": 2.0}])
    (err,) = _errors(both)  # _doc() already carries debug_obstacle
    assert err["type"] == "value_error"
    assert "'asset': 'box'" in err["msg"] and "-6.0" in err["msg"]  # migration dict, verbatim

    legacy_only = VerificationRequest.model_validate(_doc())
    assert legacy_only.scenario.debug_obstacle is not None
    list_only = _doc(obstacles=[{"asset": "box", "x": 1.0, "y": 2.0}])
    list_only["scenario"].pop("debug_obstacle")
    assert len(VerificationRequest.model_validate(list_only).scenario.obstacles) == 1


def test_an_empty_obstacle_list_is_not_how_you_say_no_obstacles():
    """``[]`` rides the identity projection verbatim (lists are never pruned), so
    it would fork the key of a request that simply omits the block."""
    empty = _doc(obstacles=[])
    empty["scenario"].pop("debug_obstacle")
    assert [e["type"] for e in _errors(empty)] == ["too_short"]


def test_a_document_without_obstacles_is_unchanged_by_the_new_field():
    """The p7 addition is optional-and-None, like every other growth of this
    contract: absent from the dump, so no identity key moves (D-5)."""
    scenario = Scenario.model_validate(_STATIC_DOC["scenario"])
    assert scenario.obstacles is None
    assert "obstacles" not in scenario.model_dump(exclude_none=True)


def test_a_bad_obstacle_entry_keeps_its_fixable_example_through_the_list_index():
    """The friendly error must survive BOTH the list index and the union branch
    tag (NFR-INTAKE-001). Positive control: the two examples are the schema's own
    (``x: -6.0`` for the placement, ``count: 2`` for the count), not one constant.

    Before the ``errors._unwrap_annotation`` repair that shipped with this field,
    an ``Optional[list[Model]]`` lost its element type at the index step and every
    violation under ``scenario.obstacles[...]`` came back with NO example.
    """
    doc = _doc(obstacles=[{"asset": "box", "x": "later", "y": 2.0}])
    doc["scenario"].pop("debug_obstacle")
    with pytest.raises(ValidationError) as excinfo:
        VerificationRequest.model_validate(doc)
    (err,) = from_validation_error(excinfo.value, model=VerificationRequest)
    assert err.field_path == "scenario.obstacles[0].x.static"
    assert "valid number" in err.expected and err.got == "'later'"
    assert err.example == "x: -6.0"

    doc["scenario"]["obstacles"] = [{"asset": "box", "x": 1.0, "y": 2.0, "count": "many"}]
    with pytest.raises(ValidationError) as excinfo:
        VerificationRequest.model_validate(doc)
    (err,) = from_validation_error(excinfo.value, model=VerificationRequest)
    assert err.field_path == "scenario.obstacles[0].count.static"
    assert err.example == "count: 2"
