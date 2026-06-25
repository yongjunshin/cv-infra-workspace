"""Request-level rollup + flakiness (M3 §3.7) — REQ-ORCH-012/013, SR-10.

Aggregates the per-repeat Job verdicts of ONE Verification Request into a
``RequestRollup`` handed off to M4. LOCKED §7.12 splits this request-level
rollup (owned here) from M4's report-level pass/fail matrix (SR-19) — the two
aggregation responsibilities do not overlap. Stdlib only.
"""

from __future__ import annotations

from collections import Counter

from cv_infra.orchestrator.models import JobResult, RequestRollup, Verdict


def roll_up(request_id: str, results: list[JobResult]) -> RequestRollup:
    """Roll up repeated-job results for ``request_id`` into a ``RequestRollup``.

    Collects each result's verdict (skipping results that carry no verdict, e.g.
    an errored job) and attaches a flakiness metric (see ``_flakiness``).
    """
    verdicts = [r.verdict for r in results if r.verdict is not None]
    return RequestRollup(
        request_id=request_id,
        verdicts=verdicts,
        flakiness=_flakiness(verdicts),
    )


def _flakiness(verdicts: list[Verdict]) -> float | None:
    """PLACEHOLDER flakiness metric (REQ-ORCH-013) — formalized in Phase 4.

    Defined here as the fraction of repeats whose verdict disagrees with the
    modal (majority) verdict: ``(n - max_count) / n``. So uniform verdicts -> 0.0
    (not flaky) and any disagreement -> > 0.0 (flaky). Returns None when there is
    no verdict to roll up. The verdict-rollup policy (all-pass / majority /
    any-fail) and the flakiness formalization are deferred to Phase 4 (M3 §3.7,
    requirements Notes deferral) — this is one explicit, documented choice so the
    rollup seam is exercised on CPU.
    """
    if not verdicts:
        return None
    counts = Counter(verdicts)
    majority = counts.most_common(1)[0][1]
    return (len(verdicts) - majority) / len(verdicts)
