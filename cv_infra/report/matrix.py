"""Report-level pass/fail matrix (M4 §3.3 A, SR-19 preview) — REQ-REPORT-001, DoD-P4-09.

Consumes the ``RequestRollup`` list M3 emits (SR-10 handoff, M4 §4) into the
report-level matrix M4 owns: one row per request + report-level summary counts.
LOCKED §7.12 splits the two aggregations — the rollup's ``verdict`` and
``flakiness`` are consumed VERBATIM here, never recomputed from ``verdicts``
(the row's job / per-verdict counts merely display the list contents; the
verdict field never reads it). ``verdict=None`` is surfaced as *errored*: the
rollup pins ``None`` as an infra outcome, not a domain judgement (rollup.py).

Preview scope (P4): structured dict + deterministic text render only. GitHub
publishing (SR-22), regression/baseline (SR-20/21) and the report-level
``summary.verdict`` / ``report_outcome`` fields (M4 §3.4 — errored>0 semantics
need the M8 exit-mapping agreement, LOCKED §7.9) land in Phase 5. Row/summary
key names co-align with the §3.4 report JSON proposal so P5 can embed the
matrix without a rename. Stdlib only.
"""

from __future__ import annotations

from collections import Counter
from typing import Any

from cv_infra.orchestrator.models import RequestRollup, Verdict

#: Text-render column order (matches the row dict shape below).
_HEADERS = ("request_id", "verdict", "flakiness", "jobs", "pass", "fail")


def build_matrix(rollups: list[RequestRollup]) -> dict[str, Any]:
    """Build the report-level matrix dict from M3 ``RequestRollup``s (SR-19).

    Shape (§3.4 co-aligned)::

        {
          "matrix": [  # one row per request, sorted by request_id (deterministic)
            {"request_id": str,
             "verdict": "pass" | "fail" | None,   # rollup.verdict VERBATIM (LOCKED §7.12)
             "flakiness": float | None,            # rollup.flakiness VERBATIM
             "jobs": int,                          # verdict-bearing repeats (see note)
             "counts": {"pass": int, "fail": int}},
            ...
          ],
          "summary": {"total": int, "passed": int, "failed": int,
                      "errored": int},             # errored = requests with verdict None
        }

    Note: ``RequestRollup.verdicts`` only carries verdict-bearing repeats
    (``roll_up`` skips verdict-less results), so ``jobs`` counts those — the
    rollup shape has no total-repeat field (field additions allowed per M3
    models.py; surface via questions/ if P5 needs it).
    """
    rows: list[dict[str, Any]] = []
    for rollup in sorted(rollups, key=lambda r: r.request_id):
        counts = Counter(rollup.verdicts)
        rows.append(
            {
                "request_id": rollup.request_id,
                # LOCKED §7.12: single truth = the rollup's own verdict — never
                # derived from ``verdicts`` (no any-fail recomputation here).
                "verdict": rollup.verdict.value if rollup.verdict is not None else None,
                "flakiness": rollup.flakiness,
                "jobs": len(rollup.verdicts),
                "counts": {v.value: counts.get(v, 0) for v in Verdict},
            }
        )
    return {
        "matrix": rows,
        "summary": {
            "total": len(rows),
            "passed": sum(1 for r in rows if r["verdict"] == Verdict.PASS.value),
            "failed": sum(1 for r in rows if r["verdict"] == Verdict.FAIL.value),
            "errored": sum(1 for r in rows if r["verdict"] is None),
        },
    }


def render_text(matrix: dict[str, Any]) -> str:
    """Deterministic human-readable table for a ``build_matrix`` result.

    Row order is already pinned by ``build_matrix`` (request_id sort), cell
    formats are fixed (flakiness ``.3f``, ``None`` verdict → ``errored``,
    ``None`` flakiness → ``-``) — golden-testable byte-for-byte.
    """
    summary = matrix["summary"]
    head = (
        f"Report matrix ({summary['total']} requests: {summary['passed']} passed, "
        f"{summary['failed']} failed, {summary['errored']} errored)"
    )
    rows = [
        (
            row["request_id"],
            row["verdict"] if row["verdict"] is not None else "errored",
            "-" if row["flakiness"] is None else f"{row['flakiness']:.3f}",
            str(row["jobs"]),
            str(row["counts"]["pass"]),
            str(row["counts"]["fail"]),
        )
        for row in matrix["matrix"]
    ]
    widths = [
        max([len(header), *(len(row[i]) for row in rows)]) for i, header in enumerate(_HEADERS)
    ]
    lines = [head, "  ".join(h.ljust(w) for h, w in zip(_HEADERS, widths)).rstrip()]
    lines += ["  ".join(c.ljust(w) for c, w in zip(row, widths)).rstrip() for row in rows]
    return "\n".join(lines)
