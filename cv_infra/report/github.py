"""The report as GitHub sees it: a Check Run payload and two markdown bodies.

Pure functions of the report dict — no token, no socket, no clock, no environment. A
developer can render every published surface from a ``report.json`` on a laptop, and
two calls on the same report produce the same bytes (the premise of the sticky
comment's in-place upsert: an unchanged report must not produce a "new" comment).

Two rules this file exists to keep:

* **The conclusion is not decided here.** ``summary.exit_code`` is the single source
  and ``CHECK_CONCLUSION_BY_EXIT`` is the single table (``cli/exit_codes.py``); this
  module looks the answer up. The one thing it adds is the NON-GATING override: a sweep
  (no oracle) or a ``--report-only`` run reports its findings and must never turn a PR
  red, so its conclusion is forced neutral and the body says so in the first line a
  reader meets.
* **ERRORs are shown, not folded.** A case whose runs died is listed with its message
  under its own heading, because an infrastructure fault that renders as "0 checks
  failed" is how a broken run gets read as a green one.
"""

from __future__ import annotations

from typing import Any

from cv_infra.cli.exit_codes import (
    CHECK_CONCLUSION_BY_EXIT,
    EXIT_INFRA,
    INFRA_INCOMPLETE_MESSAGE,
)
from cv_infra.report.identity_display import identity_cell, was_abbreviated

#: Hidden HTML comment anchoring the sticky PR comment. The workflow finds the comment
#: carrying this exact marker and edits it, instead of posting one per push.
STICKY_COMMENT_MARKER = "<!-- cv-infra:verification-report -->"

#: Stable Check Run name (the checks-tab title the workflow creates/updates).
CHECK_RUN_NAME = "CV-Infra Verification"

#: The banner a non-gating run leads with. Short and unmissable: a neutral conclusion
#: is easy to mistake for a passing one at a glance.
NON_GATING_BANNER = (
    "> **this check does not gate** — no `oracle_script` was declared (sweep), or "
    "`report_only` was set. The cases ran and are reported below; nothing here can fail "
    "the build."
)

_NEUTRAL = "neutral"
_NA = "n/a"


def render_check_run(report: dict[str, Any]) -> dict[str, Any]:
    """The Check Run payload the workflow posts: name, conclusion, title, body."""
    return {
        "name": CHECK_RUN_NAME,
        "status": "completed",
        "conclusion": conclusion_of(report),
        "output": {"title": _check_title(report), "summary": _render_body(report)},
    }


def render_sticky_comment(report: dict[str, Any]) -> str:
    """The PR comment body — the marker first, so the upsert can find it again."""
    return f"{STICKY_COMMENT_MARKER}\n{_render_body(report)}"


def render_step_summary(report: dict[str, Any]) -> str:
    """The ``$GITHUB_STEP_SUMMARY`` body (no marker — nothing upserts a step summary)."""
    return _render_body(report)


def conclusion_of(report: dict[str, Any]) -> str:
    """Check conclusion for this report: the exit-code table, unless the run does not
    gate — see the module doc."""
    if _non_gating(report):
        return _NEUTRAL
    return CHECK_CONCLUSION_BY_EXIT[report["summary"]["exit_code"]]


# --- the shared markdown body ---------------------------------------------------------


def _render_body(report: dict[str, Any]) -> str:
    sections = [
        _header(report),
        _matrix_section(report),
        _error_section(report),
        _baseline_section(report),
    ]
    return "\n\n".join(section for section in sections if section)


def _non_gating(report: dict[str, Any]) -> bool:
    return report["mode"] == "sweep" or bool(report["inputs"]["report_only"])


def _check_title(report: dict[str, Any]) -> str:
    summary = report["summary"]
    return (
        f"cv-infra {report['mode']}: {summary['report_outcome']} · "
        f"{summary['cases_run']}/{summary['cases_planned']} cases, "
        f"{summary['runs_total']} runs · {summary['checks_failed']} check(s) failed · "
        f"{summary['cases_errored']} errored"
    )


def _header(report: dict[str, Any]) -> str:
    summary = report["summary"]
    lines = ["## CV-Infra verification", ""]
    if _non_gating(report):
        lines += [NON_GATING_BANNER, ""]
    if summary["exit_code"] == EXIT_INFRA:
        lines += [f"> **{INFRA_INCOMPLETE_MESSAGE}**", ""]
    lines += [
        f"**{summary['report_outcome']}** (exit {summary['exit_code']}) · "
        f"{summary['cases_run']}/{summary['cases_planned']} cases · "
        f"{summary['runs_total']} runs · {summary['checks_failed']} check(s) failed · "
        f"{summary['cases_errored']} case(s) errored · "
        f"{summary['regressions']} regression(s)",
        "",
        f"`{_md(report['inputs']['sim_script'])}` × "
        f"`{_md(report['inputs']['sim_input_space'])}` (k={summary['coverage']['requested_k']}, "
        f"repeats={report['inputs']['repeats']}) · commit "
        f"`{_md(report['inputs']['checkout_sha'] or _NA)}` · generated "
        f"{report['generated_at']}",
    ]
    truncation = _truncation_line(report)
    if truncation:
        lines += ["", truncation]
    return "\n".join(lines)


def _truncation_line(report: dict[str, Any]) -> str:
    """The one line a budget-cut run owes its reader: what ran, and what that covers."""
    summary = report["summary"]
    coverage = summary["coverage"]
    cut = coverage["truncated_after_case"]
    if cut is None:
        return ""
    # -1 = the budget was gone before the first case started: there is no "after case N"
    # to name, but a 0-case run is exactly the one that MUST say why it ran nothing.
    where = (
        f"(truncated after case {cut})"
        if cut >= 0
        else "(the budget was spent before the first case)"
    )
    return (
        f"⚠ budget reached: ran {summary['cases_run']}/{summary['cases_planned']} cases "
        f"{where}, {coverage['achieved']:.1%} of the {coverage['requested_k']}-wise coverage."
    )


def _matrix_section(report: dict[str, Any]) -> str:
    rows = report["matrix"]
    header = "### Cases"
    if not rows:
        return f"{header}\n\n_(no case ran)_"
    table = [
        "| case | axes | result | repeats | checks | metrics | vs baseline |",
        "|---|---|---|---|---|---|---|",
        *(_matrix_row(row) for row in rows),
    ]
    if any(was_abbreviated(_case_cell(row)) for row in rows):
        table += [
            "",
            "_case = the first digits of `case_id` (a prefix of the stored key); the full "
            "ids are in `report.json`._",
        ]
    return f"{header}\n\n" + "\n".join(table)


def _matrix_row(row: dict[str, Any]) -> str:
    cells = (
        _case_cell(row),
        _axes_cell(row["axes"]),
        row["result"],
        f"{row['repeats_run']}/{row['repeats_planned']}",
        _checks_cell(row["checks"]),
        _metrics_cell(row["metrics"]),
        _baseline_cell(row["regression"]),
    )
    return "| " + " | ".join(_md(cell) for cell in cells) + " |"


def _case_cell(row: dict[str, Any]) -> str:
    return identity_cell(row.get("case_id"), absent=_NA)


def _axes_cell(axes: dict[str, Any]) -> str:
    return ", ".join(f"{name}={value}" for name, value in axes.items()) or _NA


def _checks_cell(checks: dict[str, Any]) -> str:
    """``name ratio (n=k)`` per check. The ratio is printed, not a tick: 2/3 passing is
    neither, and it is the number the baseline compares."""
    if not checks:
        return _NA
    return ", ".join(
        f"{name} {entry['pass_ratio']:.2f} (n={entry['n']})" for name, entry in checks.items()
    )


def _metrics_cell(metrics: dict[str, Any]) -> str:
    if not metrics:
        return _NA
    return ", ".join(f"{name}={entry['mean']:g}" for name, entry in metrics.items())


def _baseline_cell(regression: dict[str, Any]) -> str:
    """The case's baseline status, plus the numbers behind a regression (a status word
    alone cannot be acted on)."""
    status = regression["status"]
    moved = [
        f"{detail['name']} {_num(detail['baseline'])}→{_num(detail['current'])}"
        for detail in regression["details"]
        if detail["status"] == "regressed"
    ]
    return f"{status}: {', '.join(moved)}" if moved else status


def _error_section(report: dict[str, Any]) -> str:
    """Every ERRORed run, with its message. Absent entirely when there were none — an
    empty "Errors" heading trains readers to skip the section that matters."""
    lines = [
        f"- `{_md(_case_cell(row))}` repeat {run['repeat']}: {_md(run['error'])}"
        for row in report["matrix"]
        for run in row["runs"]
        if run["error"]
    ]
    if not lines:
        return ""
    return "### Errors (not robot verdicts)\n\n" + "\n".join(lines)


def _baseline_section(report: dict[str, Any]) -> str:
    baseline = report["baseline"]
    header = "### Baseline"
    if not baseline["available"]:
        # Best effort by design: a missing or locked baseline never fails a run.
        return (
            f"{header}\n\n- unavailable (`{_md(baseline['db'])}`) — every case skipped; "
            "this run reports without the regression signal."
        )
    lines = [
        f"- compared {baseline['compared']} key(s) · {baseline['absent']} without a baseline"
        f" (skipped, normal on a first run) · {baseline['regressed']} regressed ·"
        f" {baseline['improved']} improved · {baseline['metric_changes']} metric change(s)"
    ]
    if baseline["updated"]:
        lines.append("- baseline UPDATED from this run (`--update-baseline`)")
    if _has_single_sample(report):
        lines.append(
            "- some ratios come from a single run (`repeats: 1`) — thin evidence, still gating"
        )
    return f"{header}\n\n" + "\n".join(lines)


def _has_single_sample(report: dict[str, Any]) -> bool:
    """Did any compared ratio come from a single run? The reader is told, and the gate
    still fires — saying the evidence is thin is not the same as ignoring it."""
    return any(
        detail.get("single_sample")
        for row in report["matrix"]
        for detail in row["regression"]["details"]
    )


def _num(value: Any) -> str:
    return _NA if value is None else f"{value:g}"


def _md(value: Any) -> str:
    """Stringify a cell, escaping ``|`` so a value cannot break the table row."""
    return str(value).replace("|", "\\|")
