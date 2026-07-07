"""Acceptance-criteria evaluation engine + verdict (M2, REQ-EXEC-010/012).

Turns a ``TelemetryRecord`` into a verdict and metrics by running the bound
oracles (``reached_goal`` / ``no_collision``) and folding their outcomes. The
verdict math and the fold (pass / fail / timeout / error) are Isaac-independent
and are unit-tested on CPU.

M1-contract seam: the final ``Metrics`` / ``CriterionResult`` / ``VerificationResult``
objects are M1-owned. They are constructed only at the serialization boundary
(``build_result_dict``), which imports the M1 models lazily so that this module
imports cleanly while M1's classes are authored in parallel and resolves after the
M1 -> M2 merge. The oracle layer returns the M2-internal ``OracleOutcome`` (below).
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field

from cv_infra.runner.telemetry import TelemetryRecord


def read_field(criteria: object, name: str, default: object = None) -> object:
    """Read ``name`` from criteria as either a Mapping (dict) or an attr object.

    Lets the oracles work today against a plain criteria dict and, after the M1
    merge, against the ``AcceptanceCriteria`` / ``Goal`` models unchanged.
    """
    if isinstance(criteria, Mapping):
        return criteria.get(name, default)
    return getattr(criteria, name, default)


# Verdict domain (task data contract): pass / fail / timeout / error.
VERDICT_PASS = "pass"
VERDICT_FAIL = "fail"
VERDICT_TIMEOUT = "timeout"
VERDICT_ERROR = "error"


@dataclass(frozen=True)
class OracleOutcome:
    """One oracle's evaluation result (M2-internal — mapped to M1 CriterionResult).

    ``passed`` is the boolean verdict of this criterion; ``reason`` is a short tag
    (e.g. ``"timeout"`` when reached_goal failed because the goal was not reached
    within the sim-time budget) that lets the engine promote a fail to a timeout
    verdict; ``metrics`` are this oracle's numeric contributions (REQ-EXEC-012).
    """

    name: str
    passed: bool
    reason: str = ""
    detail: str = ""
    metrics: dict[str, float | None] = field(default_factory=dict)


def fold_verdict(outcomes: list[OracleOutcome]) -> str:
    """Fold per-criterion outcomes into the overall verdict.

    - all passed                       -> pass
    - a reached_goal timeout failure   -> timeout   (mission clock ran out, D-F)
    - any other failure                -> fail
    (``error`` is set by ``main`` on an unhandled runner exception, not here.)
    """
    if all(o.passed for o in outcomes):
        return VERDICT_PASS
    if any((not o.passed) and o.reason == "timeout" for o in outcomes):
        return VERDICT_TIMEOUT
    return VERDICT_FAIL


class EvaluationEngine:
    """Binds oracles and evaluates a telemetry record into (verdict, outcomes).

    Oracles are injected (not imported here) to keep ``oracles`` -> ``evaluate``
    the only dependency edge and avoid an import cycle. ``main`` builds the engine
    with the concrete ``reached_goal`` / ``no_collision`` oracles.
    """

    def __init__(self, oracles: list[object]) -> None:
        self.oracles = list(oracles)

    def evaluate(self, telemetry: TelemetryRecord, criteria: object) -> tuple[str, list]:
        outcomes: list[OracleOutcome] = []
        for oracle in self.oracles:
            outcomes.append(oracle.evaluate(telemetry, criteria))
        return fold_verdict(outcomes), outcomes


def build_result_dict(
    verdict: str,
    outcomes: list[OracleOutcome],
    metrics: dict[str, float | None],
    request: object | None = None,
) -> dict:
    """Assemble the ``result.json`` payload (VerificationResult.to_dict shape).

    M1-contract seam (VERBATIM data contract): the authoritative serialization is
    ``VerificationResult.to_dict()`` once the M1 models are merged. Until then this
    builds the same shape from a dict so the runner is exercisable end-to-end and
    ``main`` writes exactly one result.json. Cycle 3 swaps this for the real model;
    meanwhile the field names here stay aligned with the M1 contract (VERBATIM).
    """
    return {
        "verdict": verdict,
        "criteria": [
            {
                "name": o.name,
                "passed": o.passed,
                "reason": o.reason,
                "detail": o.detail,
            }
            for o in outcomes
        ],
        "metrics": {
            "time_to_goal_s": metrics.get("time_to_goal_s"),
            "min_clearance_m": metrics.get("min_clearance_m"),  # None until measured
            "collision_count": metrics.get("collision_count"),
            "path_len_m": metrics.get("path_len_m"),
        },
    }
