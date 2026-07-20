"""Rollup verdicts list-order canonicalisation — repeat_index invariant (M3 §3.7).

Guards the fix for the QA-confirmed flake
(``test_orchestrator_api.py::test_envelope_status_survives_orchestrator_restart``,
findings 2026-07-20-p5-publish-wiring 플래키 사냥): at k>1 the LIVE status path
folded ``record.results`` (supervisor 완료 순서, non-deterministic) into the rollup
``verdicts`` list, while the persist/restart path sorted by ``repeat_index`` — so
the two surfaces of one envelope diverged on the list-ORDER field (~12% full-suite
``1 failed, 697 passed``). ``verdict``/``flakiness`` fold order-independently, so the
divergence was a pure list-order mismatch, not a domain-result error.

The fix canonicalises INSIDE ``rollup.roll_up`` (the single constructor of
``RequestRollup.verdicts``): every caller becomes repeat_index-canonical for free.
These tests inject an ADVERSARIAL completion order and assert all three surfaces
(live status, persisted report, restart read) produce the SAME repeat_index-ordered
list. Non-vacuous by construction — a non-palindromic verdict sequence means the
pre-fix (input-order) build fails these assertions (G-07). CPU/fake, stdlib only.
"""

from __future__ import annotations

import itertools

from cv_infra.orchestrator.api import _EnvelopeRecord, _report_inputs
from cv_infra.orchestrator.models import Job, JobResult, JobState, Verdict
from cv_infra.orchestrator.rollup import roll_up
from cv_infra.orchestrator.store import Store

# repeat_index -> verdict. Deliberately NON-palindromic so a reversed/shuffled
# completion order yields a DIFFERENT list — the sort is observable (a symmetric
# [PASS, FAIL, PASS] would hide a reversal and make the test vacuous).
_BY_INDEX = {0: Verdict.PASS, 1: Verdict.PASS, 2: Verdict.FAIL}
_CANONICAL = [_BY_INDEX[i] for i in sorted(_BY_INDEX)]  # [PASS, PASS, FAIL]


def _result(request_id: str, repeat_index: int, verdict: Verdict | None) -> JobResult:
    return JobResult(job=Job(request_id, repeat_index), state=JobState.COMPLETED, verdict=verdict)


def test_roll_up_verdicts_are_repeat_index_canonical_under_any_completion_order():
    # EVERY completion-order permutation (adversarial: reversed, shuffled) must
    # yield the repeat_index-canonical verdicts list — the invariant that keeps
    # the live status API and the persisted report byte-identical.
    for order in itertools.permutations(sorted(_BY_INDEX)):
        results = [_result("r", i, _BY_INDEX[i]) for i in order]
        assert roll_up("r", results).verdicts == _CANONICAL, order
    # verdict + flakiness are order-independent (domain result unchanged by the sort).
    reversed_rollup = roll_up("r", [_result("r", i, _BY_INDEX[i]) for i in (2, 1, 0)])
    assert reversed_rollup.verdict is Verdict.FAIL  # any-fail=fail, unchanged
    assert reversed_rollup.flakiness == 1 / 3  # 1-of-3 disagreement, unchanged


def test_roll_up_canonical_order_skips_verdictless_in_place():
    # A verdict-less repeat (errored/timed-out job) is dropped, but the surviving
    # verdicts still read in repeat_index order regardless of completion order.
    by_index = {0: Verdict.PASS, 1: None, 2: Verdict.FAIL}  # canonical -> [PASS, FAIL]
    for order in itertools.permutations(sorted(by_index)):
        results = [_result("r", i, by_index[i]) for i in order]
        assert roll_up("r", results).verdicts == [Verdict.PASS, Verdict.FAIL], order


def test_live_persist_report_surfaces_agree_on_repeat_index_order(tmp_path):
    rid = "env-x/r0"
    # Adversarial completion order: repeat 2 finished first, then 0, then 1 —
    # exactly the k>1 non-determinism the live path fed the rollup unsorted.
    adversarial = [_result(rid, i, _BY_INDEX[i]) for i in (2, 0, 1)]
    assert [r.job.repeat_index for r in adversarial] == [2, 0, 1]  # genuinely out of order
    record = _EnvelopeRecord(
        envelope_id="env-x",
        request_ids=[rid],
        jobs=[r.job for r in adversarial],
        request_dumps={rid: {"scenario": {}, "sut": {}}},  # report seam reads verbatim
        results=adversarial,
        done=True,
    )
    # Surface 1 — LIVE status path: the ``envelope_status`` rollup expression verbatim.
    live = roll_up(rid, [r for r in record.results if r.job.request_id == rid]).verdicts
    # Surface 2 — REPORT path: ``_report_inputs`` assembles the durable per-request rollup.
    report = _report_inputs(record)[0].rollup.verdicts
    # Surface 3 — PERSIST/restart path: round-trip that rollup through the real store.
    with Store(tmp_path / "cv.sqlite3") as store:
        store.upsert_rollup(_report_inputs(record)[0].rollup)
        loaded = store.load_rollup(rid)
        assert loaded is not None
        persisted = loaded.verdicts
    assert live == _CANONICAL
    assert report == _CANONICAL
    assert persisted == _CANONICAL
    assert live == report == persisted  # the flake's `assert after == before`, now canonical
