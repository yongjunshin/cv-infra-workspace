"""M4 report-level matrix unit tests (DoD-P4-09 preview; REQ-REPORT-001, SR-19).

Feeds REAL producer output — ``roll_up`` over ``JobResult``s (G-17: consume the
landed M3 code, not prose) — into ``build_matrix`` / ``render_text`` and pins:
mixed-verdict correctness, empty input, flakiness surfacing, the LOCKED §7.12
boundary (rollup verdict/flakiness consumed verbatim, NEVER recomputed from
``verdicts`` — asserted with contradictory hand-crafted rollups as positive
controls), and a byte-for-byte render golden. Stdlib + pytest only.
"""

from __future__ import annotations

from cv_infra.orchestrator.models import Job, JobResult, JobState, RequestRollup, Verdict
from cv_infra.orchestrator.rollup import roll_up
from cv_infra.report.matrix import build_matrix, render_text


def _results(request_id: str, verdicts: list[Verdict | None]) -> list[JobResult]:
    return [
        JobResult(job=Job(request_id, i), state=JobState.COMPLETED, verdict=v)
        for i, v in enumerate(verdicts)
    ]


def _mixed_rollups() -> list[RequestRollup]:
    """One pass / one fail / one errored request, deliberately out of id order."""
    mixed: list[Verdict | None] = [Verdict.PASS, Verdict.FAIL, Verdict.PASS]
    return [
        roll_up("carter-goal-b", _results("carter-goal-b", mixed)),
        roll_up("carter-goal-c", _results("carter-goal-c", [None, None])),  # infra: no verdicts
        roll_up("carter-goal-a", _results("carter-goal-a", [Verdict.PASS] * 3)),
    ]


# ---------------------------------------------------------------------------
# (1) mixed rollups → matrix correctness — REQ-REPORT-001
# ---------------------------------------------------------------------------


def test_mixed_rollups_matrix_rows_and_summary():
    matrix = build_matrix(_mixed_rollups())
    assert [r["request_id"] for r in matrix["matrix"]] == [
        "carter-goal-a",
        "carter-goal-b",
        "carter-goal-c",
    ]  # deterministic request_id sort despite unsorted input
    row_a, row_b, row_c = matrix["matrix"]
    assert row_a == {
        "request_id": "carter-goal-a",
        "verdict": "pass",
        "flakiness": 0.0,
        "jobs": 3,
        "counts": {"pass": 3, "fail": 0},
    }
    assert row_b["verdict"] == "fail"  # rollup any-fail verdict, projected verbatim
    assert row_b["jobs"] == 3
    assert row_b["counts"] == {"pass": 2, "fail": 1}
    assert row_c["verdict"] is None  # infra territory stays None in the dict
    assert row_c["flakiness"] is None
    assert row_c["jobs"] == 0
    assert matrix["summary"] == {"total": 3, "passed": 1, "failed": 1, "errored": 1}


# ---------------------------------------------------------------------------
# (2) empty input — no crash, zero rows / zero summary
# ---------------------------------------------------------------------------


def test_empty_input_yields_zero_rows_and_zero_summary():
    matrix = build_matrix([])
    assert matrix["matrix"] == []
    assert matrix["summary"] == {"total": 0, "passed": 0, "failed": 0, "errored": 0}
    rendered = render_text(matrix)  # must not crash on width computation
    assert rendered == (
        "Report matrix (0 requests: 0 passed, 0 failed, 0 errored)\n"
        "request_id  verdict  flakiness  jobs  pass  fail"
    )


# ---------------------------------------------------------------------------
# (3) flakiness surfacing — REQ-ORCH-013 value passes through untouched
# ---------------------------------------------------------------------------


def test_flakiness_from_rollup_surfaces_verbatim_in_row_and_text():
    rollup = roll_up("r", _results("r", [Verdict.PASS, Verdict.FAIL, Verdict.PASS]))
    assert rollup.flakiness is not None and rollup.flakiness > 0.0  # producer premise
    matrix = build_matrix([rollup])
    assert matrix["matrix"][0]["flakiness"] == rollup.flakiness  # exact pass-through
    assert "0.333" in render_text(matrix)


# ---------------------------------------------------------------------------
# (4) boundary negative — LOCKED §7.12: consume, never recompute
# ---------------------------------------------------------------------------


def test_rollup_verdict_wins_over_contradicting_verdicts_list():
    # Hand-crafted contradiction as positive control: verdicts are all FAIL, so
    # ANY recomputation (any-fail or majority) would say "fail" — if this row
    # reads "pass" the matrix provably consumed rollup.verdict verbatim.
    fake = RequestRollup(
        request_id="contradict",
        verdicts=[Verdict.FAIL, Verdict.FAIL, Verdict.FAIL],
        flakiness=0.0,
        verdict=Verdict.PASS,
    )
    assert Verdict.FAIL in fake.verdicts  # the contradiction is real
    matrix = build_matrix([fake])
    assert matrix["matrix"][0]["verdict"] == "pass"  # rollup.verdict wins
    assert matrix["summary"] == {"total": 1, "passed": 1, "failed": 0, "errored": 0}
    # counts/jobs merely display the list contents — they may disagree with the
    # verdict and that is the point (display, not judgement).
    assert matrix["matrix"][0]["counts"] == {"pass": 0, "fail": 3}


def test_rollup_none_verdict_stays_errored_despite_passing_verdicts():
    # Inverse direction: recomputation over [PASS, PASS] would say "pass";
    # verdict=None (infra) must survive as errored.
    fake = RequestRollup(
        request_id="infra", verdicts=[Verdict.PASS, Verdict.PASS], flakiness=0.0, verdict=None
    )
    matrix = build_matrix([fake])
    assert matrix["matrix"][0]["verdict"] is None
    assert matrix["summary"]["errored"] == 1 and matrix["summary"]["passed"] == 0


def test_rollup_flakiness_wins_over_recomputation():
    # Uniform verdicts would recompute to flakiness 0.0 — the artificial 0.5
    # must pass through untouched (flakiness is M3-owned, LOCKED §7.12).
    fake = RequestRollup(
        request_id="flaky-lie",
        verdicts=[Verdict.PASS, Verdict.PASS],
        flakiness=0.5,
        verdict=Verdict.PASS,
    )
    assert build_matrix([fake])["matrix"][0]["flakiness"] == 0.5


# ---------------------------------------------------------------------------
# (5) text render golden — deterministic sort + fixed cell formats
# ---------------------------------------------------------------------------


def test_render_text_golden():
    rendered = render_text(build_matrix(_mixed_rollups()))
    assert rendered == (
        "Report matrix (3 requests: 1 passed, 1 failed, 1 errored)\n"
        "request_id     verdict  flakiness  jobs  pass  fail\n"
        "carter-goal-a  pass     0.000      3     3     0\n"
        "carter-goal-b  fail     0.333      3     2     1\n"
        "carter-goal-c  errored  -          0     0     0"
    )
    # Determinism: same input (fresh objects) → byte-identical output.
    assert render_text(build_matrix(_mixed_rollups())) == rendered
