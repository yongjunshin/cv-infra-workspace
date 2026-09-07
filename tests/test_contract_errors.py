"""The rejection object's two surfaces: the sentence a human reads and the dict CI
annotates with. Both are contracts — the sentence is what a developer acts on, and the
eight keys are what ``cli/publish_glue`` rebuilds the sentence from.
"""

from __future__ import annotations

from cv_infra.contract.errors import ANNOTATION_KEYS, ContractError


def test_friendly_line_names_the_field_the_expectation_and_the_fix():
    err = ContractError(
        field_path="--pict-k",
        expected="an integer >= 1",
        got="'two'",
        example="--pict-k 2",
    )
    assert str(err) == "--pict-k: expected an integer >= 1, got 'two' | example: --pict-k 2"


def test_absent_field_and_value_still_render_a_usable_sentence():
    """No field and no value is the worst case; it must still say something true."""
    rendered = str(ContractError(expected="a request"))
    assert rendered == "(document): expected a request, got (missing)"


def test_doc_link_and_location_ride_along_when_present():
    err = ContractError(
        field_path="space.model",
        expected="a declared axis",
        got="'nope'",
        doc_link="https://example.invalid/pict",
        source_path="verify/param_space.pict",
        source_line=4,
        source_col=1,
    )
    assert "see: https://example.invalid/pict" in str(err)
    assert str(err).endswith("at verify/param_space.pict:4:1")


def test_location_degrades_honestly_path_only_line_only_none():
    assert str(ContractError(source_path="m.pict")).endswith("at m.pict")
    # A line without a file still locates something; it says so rather than inventing one.
    assert str(ContractError(source_line=7)).endswith("at <input>:7")
    assert "at " not in str(ContractError(field_path="--repeats", expected="an int"))


def test_annotation_dict_is_exactly_the_eight_keys():
    err = ContractError(field_path="--repeats", expected="an integer >= 1", got="'0'")
    annotation = err.to_annotation_dict()
    assert tuple(annotation) == ANNOTATION_KEYS
    assert annotation["field_path"] == "--repeats"
    assert annotation["source_line"] is None
