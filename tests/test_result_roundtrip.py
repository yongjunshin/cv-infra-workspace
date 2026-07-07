"""SEAM-1 round-trip tests (FU-11 / G-17): M2 emit -> M1 ``from_dict`` -> ``to_dict``.

Cycle-1 shipped a hand-built result payload whose keys drifted from the M1 canonical
shape (``criteria`` vs ``criteria_results``, ``name`` vs ``oracle``, ``job_id`` /
``artifacts`` not emitted). ``build_result_dict`` now IS ``VerificationResult.to_dict()``
— these tests pin the seam by round-tripping the emitted dict through the real M1
models and asserting exact equality, so key drift cannot silently reappear.
CPU-only, stdlib + pytest.
"""

from __future__ import annotations

from cv_infra.contract.models import VerificationResult
from cv_infra.runner.evaluate import OracleOutcome, build_result_dict

METRICS = {
    "time_to_goal_s": 42.5,
    "min_clearance_m": None,  # None allowed in Phase 2 (needs PhysX scene-query)
    "collision_count": 0,
    "path_len_m": 7.3,
}

OUTCOMES = [
    OracleOutcome(name="reached_goal", passed=True, detail="within 0.25m"),
    OracleOutcome(name="no_collision", passed=True),
]


def _emitted_pass() -> dict:
    return build_result_dict("job-0001", "pass", OUTCOMES, METRICS)


def test_roundtrip_equality_pass_path():
    emitted = _emitted_pass()
    assert VerificationResult.from_dict(emitted).to_dict() == emitted


def test_roundtrip_equality_error_path():
    # Degraded path in main.run(): no outcomes, no metrics — still canonical.
    emitted = build_result_dict("job-0001", "error", [], {})
    assert VerificationResult.from_dict(emitted).to_dict() == emitted


def test_emitted_top_level_keys_are_canonical():
    emitted = _emitted_pass()
    assert set(emitted) == {
        "job_id",
        "verdict",
        "metrics",
        "criteria_results",
        "artifacts",
        "request_identity_key",
        "origin",
        "is_self_test",
    }
    assert emitted["job_id"] == "job-0001"  # propagated from JOB_SPEC (REQ-EXEC-013)


def test_emitted_criterion_keys_use_oracle_not_name():
    emitted = _emitted_pass()
    assert [set(c) for c in emitted["criteria_results"]] == [{"oracle", "passed", "detail"}] * 2
    assert [c["oracle"] for c in emitted["criteria_results"]] == ["reached_goal", "no_collision"]
    assert emitted["criteria_results"][1]["detail"] is None  # empty detail -> canonical None


def test_artifacts_fields_always_present_none_until_produced():
    assert _emitted_pass()["artifacts"] == {"mcap": None, "mp4": None}


def test_detail_falls_back_to_reason_tag():
    # ``reason`` steers the verdict fold and is not a canonical field; when an oracle
    # gives no prose detail, the reason tag is kept as the detail so it is not lost.
    outs = [OracleOutcome(name="reached_goal", passed=False, reason="timeout")]
    emitted = build_result_dict("job-0001", "timeout", outs, {})
    assert emitted["criteria_results"][0]["detail"] == "timeout"
    assert VerificationResult.from_dict(emitted).to_dict() == emitted


def test_typed_view_of_emitted_result():
    res = VerificationResult.from_dict(_emitted_pass())
    assert res.job_id == "job-0001"
    assert res.metrics.collision_count == 0
    assert res.metrics.min_clearance_m is None
    assert [c.oracle for c in res.criteria_results] == ["reached_goal", "no_collision"]
    assert res.artifacts.mcap is None and res.artifacts.mp4 is None
    assert res.is_self_test is False
