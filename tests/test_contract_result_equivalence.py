"""Old<->new Result WIRE-EQUIVALENCE guard (M1 P3 cycle-1 — G-25/G-17).

The new pydantic ``cv_infra.contract.schema.Result`` must pass the result.json
dict the REAL Phase-2 emission produces UNMODIFIED. The material is never
hand-copied shape (G-25): every dict here comes from the actual producer —
``cv_infra.runner.evaluate.build_result_dict`` (== the old
``VerificationResult.to_dict()``, pinned by tests/test_result_roundtrip.py) —
or from the old model's own default materialization (the crosscut roundtrip
gate's minimal ``{"job_id", "verdict"}`` shims).

Guard law:  emitted -> Result.model_validate -> model_dump == emitted
            (key set AND values identical, all nesting levels)

POSITIVE CONTROLS prove the guard is non-vacuous (G-25 (2)(3)): corrupting one
field of the emission makes the guard actually fail.
"""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from cv_infra.contract.models import VERDICTS, VerificationResult
from cv_infra.contract.schema import Result
from cv_infra.runner.evaluate import OracleOutcome, build_result_dict

METRICS = {
    "time_to_goal_s": 42.5,
    "min_clearance_m": None,
    "collision_count": 0,
    "path_len_m": 7.3,
}

OUTCOMES = [
    OracleOutcome(name="reached_goal", passed=True, detail="within 0.25m"),
    OracleOutcome(name="no_collision", passed=True),
]

# The real M2 emission paths (same materials as tests/test_result_roundtrip.py).
EMISSIONS = {
    "pass-full": build_result_dict("job-0001", "pass", OUTCOMES, METRICS),
    "error-degraded": build_result_dict("job-0001", "error", [], {}),
    "timeout-reason-fallback": build_result_dict(
        "job-0001",
        "timeout",
        [OracleOutcome(name="reached_goal", passed=False, reason="timeout")],
        {},
    ),
}


@pytest.mark.parametrize("name", sorted(EMISSIONS))
def test_new_result_passes_real_emission_unmodified(name):
    emitted = EMISSIONS[name]
    assert Result.model_validate(emitted).model_dump() == emitted


@pytest.mark.parametrize("verdict", ["pass", "fail"])
def test_minimal_shim_materializes_same_defaults_as_old_model(verdict):
    # The crosscut roundtrip gate feeds minimal {"job_id","verdict"} shims to the
    # old model; the new model must materialize IDENTICAL defaults from them.
    shim = {"job_id": "rt-0", "verdict": verdict}
    assert Result.model_validate(shim).model_dump() == VerificationResult.from_dict(shim).to_dict()


def test_verdict_domain_tracks_old_model_verbatim():
    for verdict in VERDICTS:  # the old model's tuple is the material, not a retyped list
        assert Result.model_validate({"job_id": "j", "verdict": verdict}).verdict == verdict
    with pytest.raises(ValidationError):
        Result.model_validate({"job_id": "j", "verdict": "not_a_verdict"})


# --------------------------------------------------------------------------- #
# POSITIVE CONTROLS — the guard must actually fail on one-field drift (G-25).
# --------------------------------------------------------------------------- #
def test_positive_control_renamed_top_level_key_fails_loud():
    # The historical G-17 drift: criteria_results emitted as "criteria".
    emitted = dict(EMISSIONS["pass-full"])
    emitted["criteria"] = emitted.pop("criteria_results")
    with pytest.raises(ValidationError):
        Result.model_validate(emitted)


def test_positive_control_unknown_nested_key_fails_loud():
    emitted = dict(EMISSIONS["pass-full"])
    emitted["metrics"] = {**emitted["metrics"], "goal_tolerance_m": 0.5}
    with pytest.raises(ValidationError):
        Result.model_validate(emitted)


def test_positive_control_corrupted_verdict_fails_loud():
    emitted = {**EMISSIONS["pass-full"], "verdict": "ok"}
    with pytest.raises(ValidationError):
        Result.model_validate(emitted)


def test_positive_control_equality_discriminates_value_drift():
    # The dump-equality comparison itself is not vacuous: a one-value change on
    # the emission side is detected by the same == the guard law uses.
    emitted = EMISSIONS["pass-full"]
    tampered = {**emitted, "job_id": "job-9999"}
    assert Result.model_validate(emitted).model_dump() != tampered
