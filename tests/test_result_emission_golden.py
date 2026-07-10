"""result.json emission GOLDEN shape tests (D-4' — wire invariance fixed point).

The Phase-2 result.json wire (key tree, nesting, defaults, serialized bytes) must
survive the contract-consumption refactor UNCHANGED. Unlike the old<->new
equivalence guard (tests/test_contract_result_equivalence.py — retires with
contract/models.py), these goldens reference ONLY the producer surface
(``build_result_dict`` + ``write_result``) against EXPLICIT literals, so they
remain the wire's fixed point after models.py is gone. Golden literals were
materialized from the Phase-2 emission at base cc4abb5 (pre-refactor
``VerificationResult.to_dict()``) — do not regenerate them from the code under
test (that would make the guard vacuous).
"""

from __future__ import annotations

import json

from cv_infra.contract.schema import Artifacts
from cv_infra.runner.evaluate import OracleOutcome, build_result_dict
from cv_infra.runner.main import write_result

OUTCOMES = [
    OracleOutcome(name="reached_goal", passed=True, detail="within 0.25m"),
    OracleOutcome(name="no_collision", passed=True),
]

METRICS = {
    "time_to_goal_s": 42.5,
    "min_clearance_m": None,  # None allowed (needs a PhysX scene-query)
    "collision_count": 0,
    "path_len_m": 7.3,
}

# --- golden: full pass emission with artifacts (explicit literal, not derived) --
GOLDEN_PASS = {
    "job_id": "job-0001",
    "verdict": "pass",
    "metrics": {
        "time_to_goal_s": 42.5,
        "min_clearance_m": None,
        "collision_count": 0,
        "path_len_m": 7.3,
    },
    "criteria_results": [
        {"oracle": "reached_goal", "passed": True, "detail": "within 0.25m"},
        {"oracle": "no_collision", "passed": True, "detail": None},
    ],
    "artifacts": {"mcap": "/cv/result/bag/job.mcap", "mp4": "/cv/result/recording.mp4"},
    "request_identity_key": None,
    "origin": None,
    "is_self_test": False,
}

# --- golden: degraded error emission (main.run's except path: no outcomes/metrics)
GOLDEN_ERROR = {
    "job_id": "job-0001",
    "verdict": "error",
    "metrics": {
        "time_to_goal_s": None,
        "min_clearance_m": None,
        "collision_count": 0,
        "path_len_m": None,
    },
    "criteria_results": [],
    "artifacts": {"mcap": None, "mp4": None},
    "request_identity_key": None,
    "origin": None,
    "is_self_test": False,
}

# --- golden: the exact bytes write_result puts on disk (sort_keys + indent=2) ---
GOLDEN_PASS_TEXT = """\
{
  "artifacts": {
    "mcap": "/cv/result/bag/job.mcap",
    "mp4": "/cv/result/recording.mp4"
  },
  "criteria_results": [
    {
      "detail": "within 0.25m",
      "oracle": "reached_goal",
      "passed": true
    },
    {
      "detail": null,
      "oracle": "no_collision",
      "passed": true
    }
  ],
  "is_self_test": false,
  "job_id": "job-0001",
  "metrics": {
    "collision_count": 0,
    "min_clearance_m": null,
    "path_len_m": 7.3,
    "time_to_goal_s": 42.5
  },
  "origin": null,
  "request_identity_key": null,
  "verdict": "pass"
}
"""


def _emit_pass() -> dict:
    return build_result_dict(
        "job-0001",
        "pass",
        OUTCOMES,
        METRICS,
        artifacts=Artifacts(mcap="/cv/result/bag/job.mcap", mp4="/cv/result/recording.mp4"),
    )


def test_pass_emission_equals_golden_dict():
    assert _emit_pass() == GOLDEN_PASS


def test_error_emission_equals_golden_dict():
    assert build_result_dict("job-0001", "error", [], {}) == GOLDEN_ERROR


def test_written_result_json_bytes_equal_golden_text(tmp_path):
    # Wire = the file M3/M8 read back. Byte-identical, not just dict-equal:
    # key order (sorted), indentation, float/None/bool JSON forms all pinned.
    out = write_result(_emit_pass(), tmp_path / "result.json")
    assert out.read_text(encoding="utf-8") == GOLDEN_PASS_TEXT


def test_golden_text_is_the_golden_dict_serialized():
    # Internal consistency of the two literals (catches a hand-edit of one only).
    assert json.loads(GOLDEN_PASS_TEXT) == GOLDEN_PASS


def test_detail_falls_back_to_reason_tag_in_wire():
    outs = [OracleOutcome(name="reached_goal", passed=False, reason="timeout")]
    emitted = build_result_dict("job-0001", "timeout", outs, {})
    assert emitted["verdict"] == "timeout"
    assert emitted["criteria_results"] == [
        {"oracle": "reached_goal", "passed": False, "detail": "timeout"}
    ]


def test_golden_guard_is_not_vacuous():
    # Positive control (G-25 culture): one-field drift on the emission side is
    # caught by the same equality the goldens use.
    tampered = {**_emit_pass(), "job_id": "job-9999"}
    assert tampered != GOLDEN_PASS
