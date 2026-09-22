"""Verdict typing tests — contract/verdict.py.

The one that matters most is ``bool`` before number: in Python ``bool`` is a subclass
of ``int``, so the natural isinstance order files every check as a metric and the gate
passes everything forever, silently. The rest pin the lanes (rc -> ERROR), the
last-JSON-line guard against a chatty stdout, null-as-unjudgeable (never false), and
the emptiness check that stops a gate from asserting nothing.
"""

from __future__ import annotations

from cv_infra.contract.verdict import (
    LANE_ERROR,
    LANE_OK,
    CaseRunResult,
    classify,
    has_boolean_check,
)


def judged(stdout: str) -> CaseRunResult:
    return classify(0, 0, stdout, True)


# --- (1) the type table ---------------------------------------------------------------


def test_a_bool_is_a_check_not_a_metric():
    result = judged('{"fell": false, "reached": true}')

    assert result.lane == LANE_OK
    assert result.checks == {"fell": False, "reached": True}
    assert result.metrics == {}  # bool is a subclass of int — the order is load bearing


def test_numbers_are_metrics_nulls_are_unjudgeable_strings_are_notes():
    result = judged('{"z_final": 0.12, "steps": 3, "visibility": null, "note": "dim"}')

    assert result.metrics == {"z_final": 0.12, "steps": 3.0}
    assert result.nulls == ["visibility"]  # excluded from ratios, NOT a false check
    assert result.checks == {}
    assert result.notes == {"note": "dim"}
    assert result.verdict == {"z_final": 0.12, "steps": 3, "visibility": None, "note": "dim"}


def test_a_nested_value_is_this_cases_error_naming_the_key():
    result = judged('{"fell": false, "pose": {"x": 1}}')

    assert result.lane == LANE_ERROR
    assert "pose" in result.error and "FLAT" in result.error
    assert "dict" in result.error


def test_a_list_value_is_rejected_the_same_way():
    assert classify(0, 0, '{"xs": [1, 2]}', True).lane == LANE_ERROR


# --- (2) the last-JSON-line guard -----------------------------------------------------


def test_the_last_json_object_line_wins_over_kit_chatter():
    stdout = '[INFO] booting\n{"fell": true}\n[Warning] shutting down\n'

    assert judged(stdout).checks == {"fell": True}


def test_a_trailing_non_object_json_line_is_skipped_not_taken():
    """A bare number or list on the final line is not a verdict — keep walking back."""
    assert judged('{"fell": true}\n[1, 2]\n42\n\n').checks == {"fell": True}


def test_stdout_without_any_json_object_is_an_error_quoting_the_tail():
    result = classify(0, 0, "Traceback (most recent call last):\n  boom\n", True)

    assert result.lane == LANE_ERROR
    assert "boom" in result.error


def test_empty_stdout_is_an_error_that_says_so():
    result = classify(0, 0, None, True)

    assert result.lane == LANE_ERROR and "(empty)" in result.error


# --- (3) the three lanes --------------------------------------------------------------


def test_a_bad_sim_rc_is_an_error_and_never_a_verdict():
    result = classify(1, 0, '{"fell": false}', True)

    assert result.lane == LANE_ERROR and "sim exited rc=1" in result.error
    assert result.checks == {}  # the sim's exit status cannot carry pass/fail


def test_a_sim_that_never_exited_names_the_timeout():
    assert "never exited" in classify(None, None, None, True).error


def test_a_bad_oracle_rc_is_an_error():
    result = classify(0, 3, '{"fell": false}', True)

    assert result.lane == LANE_ERROR and "oracle exited rc=3" in result.error


def test_an_oracle_that_never_exited_is_an_error():
    assert "never exited" in classify(0, None, "", True).error


def test_sweep_mode_judges_nothing_but_still_succeeds():
    result = classify(0, None, None, False)

    assert result.lane == LANE_OK and result.checks == {} and result.verdict is None


# --- (4) emptiness --------------------------------------------------------------------


def test_has_boolean_check_sees_a_single_check_anywhere():
    results = [judged('{"z": 1.0}'), judged('{"fell": false}')]

    assert has_boolean_check(results) is True


def test_a_gate_whose_verdicts_hold_no_check_asserts_nothing():
    assert has_boolean_check([judged('{"z": 1.0, "note": "x"}')]) is False
    assert has_boolean_check([]) is False
