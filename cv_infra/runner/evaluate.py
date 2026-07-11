"""Acceptance-criteria evaluation engine + verdict (M2, REQ-EXEC-010/012).

Turns a ``TelemetryRecord`` into a verdict and metrics by running the bound
oracles (``reached_goal`` / ``no_collision``) and folding their outcomes. The
verdict math and the fold (pass / fail / timeout / error) are Isaac-independent
and are unit-tested on CPU.

M1-contract seam (SEAM-1, FU-11 / D-4' 2026-07-10): the final ``Metrics`` /
``CriterionResult`` / ``Result`` objects are the M1 pydantic canon
(``contract.schema``, run on the BUNDLED pydantic in the runner image) and are
imported for real — the payload is built only at the serialization boundary
(``build_result_dict``) as ``Result.model_dump()``, so the wire shape has a
single definition (G-17: no hand-built dict, key drift impossible by
construction). The emitted dict is WIRE-IDENTICAL to the Phase-2 emission —
pinned by the golden shape test (tests/test_result_emission_golden.py) and the
old<->new equivalence guard. The oracle layer returns the M2-internal
``OracleOutcome`` (below).
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field

from cv_infra.contract.schema import (
    Artifacts,
    CriterionResult,
    Metrics,
    Result,
)
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
    the only dependency edge and avoid an import cycle. ``main`` composes the
    engine from the request's criteria through the M1 loader (``build_oracles``,
    D-1 (4) uniform path — MVP entry points and custom ``module:Class`` alike).
    """

    def __init__(self, oracles: list[object]) -> None:
        self.oracles = list(oracles)

    def evaluate(self, telemetry: TelemetryRecord, criteria: object) -> tuple[str, list]:
        outcomes: list[OracleOutcome] = []
        for oracle in self.oracles:
            outcomes.append(oracle.evaluate(telemetry, criteria))
        return fold_verdict(outcomes), outcomes


def build_result_dict(
    job_id: str,
    verdict: str,
    outcomes: list[OracleOutcome],
    metrics: dict[str, float | None],
    artifacts: Artifacts | None = None,
) -> dict:
    """Assemble ``result.json`` as the M1 canonical ``Result.model_dump()``.

    SEAM-1 (FU-11 / G-17): the authoritative serialization IS the M1 model — no
    hand-built dict remains, so producer/consumer key drift cannot reappear.
    ``OracleOutcome`` (M2-internal) maps onto the M1 ``CriterionResult``:
    ``name`` -> ``oracle``; ``detail`` falls back to the ``reason`` tag (so e.g. a
    bare "timeout" is not lost — ``reason`` itself only steers the verdict fold and
    is not a canonical field). ``artifacts`` defaults to the canonical None fields
    until the recorders produce files. The emitted key tree/values are frozen by
    the golden shape test (D-4' wire invariance — supersedes nothing on the wire).
    """
    result = Result(
        job_id=job_id,
        verdict=verdict,
        metrics=Metrics.model_validate(metrics or {}),
        criteria_results=[
            CriterionResult(oracle=o.name, passed=o.passed, detail=(o.detail or o.reason) or None)
            for o in outcomes
        ],
        artifacts=artifacts if artifacts is not None else Artifacts(),
    )
    return result.model_dump()
