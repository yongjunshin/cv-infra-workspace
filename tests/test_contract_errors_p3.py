"""M1 P3 friendly-error formatter tests (errors.py — NFR-INTAKE-001, D-L).

The ContractError carries the VERBATIM 8-field annotation shape (M1 §3.4 <->
M8 file/line/col 1:1) and ``from_validation_error`` post-processes pydantic
``ValidationError.errors()`` into it: dotted/indexed field path + expected +
got + fixable example (from the schema's ``Field(examples=[...])``). A raw
traceback never appears in the rendered message.
"""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from cv_infra.contract.errors import (
    ANNOTATION_KEYS,
    ContractError,
    from_validation_error,
    render_loc,
)
from cv_infra.contract.schema import Scenario, VerificationRequest

VERBATIM_KEYS = (
    "field_path",
    "expected",
    "got",
    "example",
    "doc_link",
    "source_path",
    "source_line",
    "source_col",
)


def test_annotation_keys_are_the_verbatim_contract():
    assert ANNOTATION_KEYS == VERBATIM_KEYS


def test_annotation_dict_has_exactly_the_eight_keys():
    err = ContractError(field_path="a.b", expected="x", got="'y'", example="b: 1")
    annotation = err.to_annotation_dict()
    assert tuple(annotation) == VERBATIM_KEYS
    assert annotation["field_path"] == "a.b" and annotation["source_line"] is None


def test_render_loc_dotted_and_indexed():
    assert (
        render_loc(("requests", 0, "acceptance_criteria", "timeout_s"))
        == "requests[0].acceptance_criteria.timeout_s"
    )
    assert render_loc(()) == ""
    assert render_loc((0, "x")) == "[0].x"


def _validation_error(doc: dict) -> ValidationError:
    with pytest.raises(ValidationError) as exc_info:
        Scenario.model_validate(doc)
    return exc_info.value


def test_bad_type_yields_path_expected_got_and_example():
    exc = _validation_error(
        {
            "scene": "s",
            "robot": "r",
            "goal": {"x": 0, "y": 0, "yaw": 0},
            "seed": 42,
            "timeout_s": "banana",
        }
    )
    (err,) = from_validation_error(exc, model=Scenario, source_path="scenario.yaml")
    assert err.field_path == "timeout_s"
    assert "number" in err.expected
    assert err.got == "'banana'"
    assert err.example == "timeout_s: 120"  # from Field(examples=[120]) — fixable shape
    assert err.source_path == "scenario.yaml"


def test_missing_field_renders_missing_not_the_parent_dump():
    exc = _validation_error({"scene": "s", "robot": "r", "seed": 42, "timeout_s": 120})
    errors = {e.field_path: e for e in from_validation_error(exc, model=Scenario)}
    assert errors["goal"].got == "(missing)"


def test_nested_example_lookup_through_list_items():
    doc = {
        "scenario": {
            "scene": "s",
            "robot": "r",
            "goal": {"x": 0, "y": 0, "yaw": 0},
            "seed": 1,
            "timeout_s": 10,
        },
        "sut": {"image_ref": "img"},
        "acceptance_criteria": [{"oracle": "reached_goal", "params": {"position_tolerance_m": -1}}],
    }
    with pytest.raises(ValidationError) as exc_info:
        VerificationRequest.model_validate(doc)
    (err,) = from_validation_error(exc_info.value, model=VerificationRequest)
    assert "position_tolerance_m" in err.field_path
    assert "greater than 0" in err.expected


def test_friendly_message_never_contains_a_traceback():
    exc = _validation_error({"scene": "s"})
    for err in from_validation_error(exc, model=Scenario, source_path="s.yaml"):
        assert "Traceback" not in str(err)
        assert str(err)  # non-empty prose


def test_str_carries_location_when_present():
    err = ContractError(
        field_path="scenario.timeout_s",
        expected="positive number (seconds)",
        got="'-5'",
        example="timeout_s: 120",
        source_path="scenarios/warehouse_goal.yaml",
        source_line=42,
        source_col=18,
    )
    text = str(err)
    assert "scenario.timeout_s" in text
    assert "positive number (seconds)" in text
    assert "timeout_s: 120" in text
    assert "scenarios/warehouse_goal.yaml:42:18" in text


def test_locator_is_remembered_for_locator_less_rerenders():
    # p3c3 ①: a locator passed once is remembered on the SAME exception, so the
    # consumer's list-traversal re-render (no locator argument — the CLI idiom
    # over ``err.__cause__``) still attaches line/col to EVERY violation.
    exc = _validation_error(
        {
            "scene": "s",
            "robot": "r",
            "goal": {"x": 0, "y": 0, "yaw": 0},
            "seed": "banana",
            "timeout_s": "banana",
        }
    )
    first = from_validation_error(exc, model=Scenario, locator=lambda loc: (7, 3))
    assert len(first) == 2
    again = from_validation_error(exc, model=Scenario)  # no locator
    assert [(e.source_line, e.source_col) for e in again] == [(7, 3), (7, 3)]


def test_block_missing_top_level_fields_carry_a_fixable_example():
    # p3c3 ② (DoD-P3-02 footnote): whole-block-missing violations now carry an
    # example from ``Field(examples=[...])`` — dicts render as YAML flow maps.
    with pytest.raises(ValidationError) as exc_info:
        VerificationRequest.model_validate({"apiVersion": "cv-infra/v1"})
    errors = {
        e.field_path: e for e in from_validation_error(exc_info.value, model=VerificationRequest)
    }
    assert errors["scenario"].example.startswith("scenario: {")
    assert "'scene'" in errors["scenario"].example
    assert errors["sut"].example.startswith("sut: {")
    assert "image_ref" in errors["sut"].example
    assert errors["acceptance_criteria"].example.startswith("acceptance_criteria: [")
    assert "reached_goal" in errors["acceptance_criteria"].example


def test_block_missing_nested_goal_carries_a_fixable_example():
    exc = _validation_error({"scene": "s", "robot": "r", "seed": 42, "timeout_s": 120})
    errors = {e.field_path: e for e in from_validation_error(exc, model=Scenario)}
    assert errors["goal"].got == "(missing)"
    assert errors["goal"].example.startswith("goal: {")
    assert "'yaw'" in errors["goal"].example
