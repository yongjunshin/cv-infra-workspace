"""Request-level rollup + flakiness (M3 §3.7) — REQ-ORCH-012/013, SR-10.

Aggregates the per-repeat Job verdicts of ONE Verification Request into a
``RequestRollup`` handed off to M4. LOCKED §7.12 splits this request-level
rollup (owned here) from M4's report-level pass/fail matrix (SR-19) — the two
aggregation responsibilities do not overlap. Stdlib only.

Rollup verdict policy (구현 확정, task 2026-07-13 / M3 §3.7 deferral resolved):
**any-fail = fail** — a single FAIL among the repeats fails the request
(conservative CI gate; a flaky failure must block, not average out), all-PASS
passes. Flakiness (REQ-ORCH-013) is surfaced SEPARATELY in the same rollup,
never folded into the verdict.

p6 §0-14 adds ONE opt-in relaxation of that policy: ``min_pass_ratio`` (M1
``ExecutionSettings``, 0 < r <= 1) — the fraction of the request's samples that
must pass. ``None`` (the default, and every pre-p6 request) is the any-fail rule
BYTE-IDENTICAL. It changes the verdict COMPUTATION only: ``RequestRollup``'s
shape, the verdict-less (infra) exclusion and ``flakiness`` are untouched, so an
errored/timed-out sample is still no judgement at all rather than a cheap "fail"
the ratio could average away (P5-13 stays exit-3 territory).
"""

from __future__ import annotations

from collections import Counter

from cv_infra.orchestrator.models import JobResult, RequestRollup, Verdict


def roll_up(
    request_id: str, results: list[JobResult], *, min_pass_ratio: float | None = None
) -> RequestRollup:
    """Roll up repeated-job results for ``request_id`` into a ``RequestRollup``.

    Orders the results by ``repeat_index`` FIRST (canonical order invariant),
    then collects each result's verdict (skipping results that carry no verdict,
    e.g. an errored/timed-out job) and attaches:

    * ``verdicts`` — the surviving per-repeat verdicts in ``repeat_index`` order.
      This is the SINGLE constructor of ``RequestRollup.verdicts``, so every
      surface that goes through ``roll_up`` (live status API, persisted report,
      restart read) sees the SAME list regardless of the completion order the
      caller passes in (``record.results`` is supervisor完료 순서, non-deterministic
      at k>1). ``verdict``/``flakiness`` are order-independent — this sort is a
      pure canonicalisation of the list-order field, no domain-result change.
    * ``verdict`` — any-fail=fail (module docstring), or the ``min_pass_ratio``
      threshold when the request declared one. All repeats verdict-less
      leaves ``verdict=None``: an infra outcome, not a domain judgement —
      exit-code 3 territory (M8 매핑), never mapped to pass/fail here.
    * ``flakiness`` — repeat-disagreement metric (see ``_flakiness``), surfaced
      separately from the verdict.

    ``min_pass_ratio`` (p6 §0-14) is the request's own judgement policy, read by
    the CALLER off the request wire dump (``api._min_pass_ratio``) — this module
    never reaches for a document. None = the frozen any-fail rule.
    """
    ordered = sorted(results, key=lambda r: r.job.repeat_index)
    verdicts = [r.verdict for r in ordered if r.verdict is not None]
    return RequestRollup(
        request_id=request_id,
        verdicts=verdicts,
        flakiness=_flakiness(verdicts),
        verdict=_verdict(verdicts, min_pass_ratio),
    )


def _verdict(verdicts: list[Verdict], min_pass_ratio: float | None = None) -> Verdict | None:
    """Rollup verdict; None when no repeat produced a verdict.

    Default (``min_pass_ratio is None``) = any-fail=fail, byte-identical to the
    frozen policy. With a ratio: PASS iff ``pass_count / len(verdicts) >= r``.
    The DENOMINATOR is the surviving verdicts, not ``repeats`` — verdict-less
    (errored/timed-out) samples were already excluded above, and counting them as
    failures would let infra noise masquerade as a SUT judgement (P5-13); an
    envelope carrying such samples still folds to errored/exit-3 upstream,
    independently of this ratio.
    """
    if not verdicts:
        return None
    if min_pass_ratio is None:
        return Verdict.FAIL if Verdict.FAIL in verdicts else Verdict.PASS
    passed = sum(1 for verdict in verdicts if verdict is Verdict.PASS)
    return Verdict.PASS if passed / len(verdicts) >= min_pass_ratio else Verdict.FAIL


def _flakiness(verdicts: list[Verdict]) -> float | None:
    """Flakiness metric (REQ-ORCH-013): repeat disagreement with the modal verdict.

    ``(n - modal_count) / n`` — uniform verdicts -> 0.0 (not flaky), any
    disagreement -> > 0.0 (flaky), None when there is no verdict to roll up.
    This is the pinned M3 metric (M3 §3.7 정량 정의 확정); it is deliberately
    independent of the any-fail verdict so a 1-of-3 FAIL reads as
    ``verdict=FAIL, flakiness=1/3`` — the reviewer sees both the gate decision
    and the instability signal.
    """
    if not verdicts:
        return None
    counts = Counter(verdicts)
    majority = counts.most_common(1)[0][1]
    return (len(verdicts) - majority) / len(verdicts)
