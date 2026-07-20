"""GitHub publish-payload renderer (M4 §3.3 D, SR-22) — REQ-REPORT-007, NFR-REPORT-001.

Pure functions ``dict``(report JSON) -> the FOUR publish surfaces, and nothing
else: a Check Run payload, a sticky PR-comment markdown, a step-summary markdown,
and an artifact upload manifest. The actual GitHub API calls / uploads are the M8
Action plane's job (LOCKED §7.14) — this module holds NO GitHub token, opens NO
socket, touches NO network (M4-09 이식성 negative): a developer can produce every
surface from a report JSON standalone. Consequently it imports ONLY the stdlib and
``cv_infra.cli.exit_codes`` (itself a stdlib-only leaf, so the import drags no
orchestrator/fastapi/network graph — the property M4-09 rests on).

The exit -> Check conclusion table lives in ONE place — ``cv_infra.cli.exit_codes``,
the M8 single source (LOCKED §7.9 / D-I) — and is IMPORTED, never re-declared here:
this file contains no conclusion mapping literal (duplicate-literal grep gate). The
conclusion is folded as ``CHECK_CONCLUSION_BY_EXIT[exit_code_for_report_outcome(
report["summary"]["report_outcome"])]`` — ``report_outcome`` (pass|fail|errored) is
the single exit-driving key (Q1: verdict stays pure domain). An infra-incomplete
report (exit 3 -> the not-a-pass conclusion) additionally surfaces
``INFRA_INCOMPLETE_MESSAGE`` so an infra problem is never read as a self-regression
(exit 3 is never folded into a failing conclusion — D-I, DoD-P5-13).

All four renderers are byte-deterministic in their input (only ``report`` is read,
no clock / env / set iteration), so the sticky marker + body reproduce identically
across calls — the renderer-side premise of the P5-14 in-place comment upsert (the
upsert itself is the Action's job). Absent fields (a T3-seam report carries
``metrics: {}`` and ``None`` artifact paths) render as honest "n/a" / "경로 미제공
(제어평면)" notes — crash-0, no fabricated values.
"""

from __future__ import annotations

from typing import Any

from cv_infra.cli.exit_codes import (
    CHECK_CONCLUSION_BY_EXIT,
    EXIT_INFRA,
    INFRA_INCOMPLETE_MESSAGE,
    exit_code_for_report_outcome,
)

#: Hidden HTML comment anchoring the sticky PR comment for in-place upsert (P5-14).
#: MUST appear in every rendered sticky comment; the Action finds+updates the
#: comment carrying this exact marker instead of posting a new one each push.
STICKY_COMMENT_MARKER = "<!-- cv-infra:verification-report -->"

#: Stable Check Run name (the checks-tab title the Action creates/updates).
CHECK_RUN_NAME = "CV-Infra Verification"

#: The three per-repeat artifact kinds a selected entry carries (§3.4). Iterated in
#: this fixed order for deterministic output; also the manifest's kind vocabulary.
_ARTIFACT_KINDS = ("result_json", "rosbag_mcap", "recording_mp4")
_ARTIFACT_LABELS = {
    "result_json": "result.json",
    "rosbag_mcap": "rosbag(mcap)",
    "recording_mp4": "녹화(mp4)",
}

#: Honest note for a selected artifact whose path is ``None`` — the control plane
#: did not carry a filesystem path (T3 seam), which is NOT a size-cap exclusion.
_NO_PATH_NOTE = "경로 미제공(제어평면)"

_NA = "n/a"


# --------------------------------------------------------------------------- #
# Public surfaces (4)
# --------------------------------------------------------------------------- #
def render_check_run(report: dict[str, Any]) -> dict[str, Any]:
    """Render the GitHub Check Run payload the Action posts (SR-22, DoD-P5-13).

    Shape: ``{name, status: "completed", conclusion, output: {title, summary}}``.
    ``conclusion`` is folded from ``report_outcome`` through the IMPORTED single
    source (no local mapping). When the fold lands on infra (exit 3), the summary
    is prefixed with ``INFRA_INCOMPLETE_MESSAGE`` (P5-13: platform/infra problem,
    not a SUT judgement — never collapsed into a failing conclusion).
    """
    summary = report.get("summary") or {}
    exit_code = exit_code_for_report_outcome(summary.get("report_outcome"))
    conclusion = CHECK_CONCLUSION_BY_EXIT[exit_code]
    body = _render_body(report)
    if exit_code == EXIT_INFRA:
        body = f"{INFRA_INCOMPLETE_MESSAGE}\n\n{body}"
    return {
        "name": CHECK_RUN_NAME,
        "status": "completed",
        "conclusion": conclusion,
        "output": {"title": _check_title(summary), "summary": body},
    }


def render_sticky_comment(report: dict[str, Any]) -> str:
    """Render the sticky PR-comment markdown (SR-22): marker + shared body.

    The ``STICKY_COMMENT_MARKER`` leads so the Action can upsert in place (P5-14).
    """
    return f"{STICKY_COMMENT_MARKER}\n{_render_body(report)}"


def render_step_summary(report: dict[str, Any]) -> str:
    """Render the ``$GITHUB_STEP_SUMMARY`` markdown (SR-22): the shared body only
    (no sticky marker — the step summary is not upserted)."""
    return _render_body(report)


def render_artifact_manifest(report: dict[str, Any]) -> dict[str, Any]:
    """Flatten ``matrix[].artifacts{policy, selected[]}`` into an upload plan (SR-22).

    The selection (all failing jobs + one representative pass, 결정 #1) is ALREADY
    made by ``aggregate._select_artifacts`` — this NEVER re-selects; it only splits
    each already-selected entry's three artifact kinds into ``uploads`` (path
    present), ``missing`` (path ``None`` — honest control-plane absence), and
    ``excluded`` (size-cap dropped, 결정 #2, warnings passed through). Actual file
    upload/sizing stays the M8 plane's job (no path is fetched or measured here).
    """
    rows = report.get("matrix") or []
    uploads: list[dict[str, Any]] = []
    missing: list[dict[str, Any]] = []
    excluded: list[dict[str, Any]] = []
    for row in rows:
        request_id = row.get("request_id")
        artifacts = row.get("artifacts") or {}
        for entry in artifacts.get("selected") or []:
            _classify_entry(request_id, entry, uploads, missing, excluded)
    return {
        "policy": _first_policy(rows),
        "uploads": uploads,
        "missing": missing,
        "excluded": excluded,
    }


# --------------------------------------------------------------------------- #
# Shared markdown body (sticky comment == step summary, modulo the marker)
# --------------------------------------------------------------------------- #
def _render_body(report: dict[str, Any]) -> str:
    """The markdown body shared by the sticky comment and the step summary."""
    sections = [
        _header(report),
        _matrix_section(report),
        _regression_section(report),
        _artifact_section(report),
    ]
    return "\n\n".join(section for section in sections if section)


def _check_title(summary: dict[str, Any]) -> str:
    """One-line Check Run title (counts + pure-domain verdict)."""
    return (
        f"CV-Infra verification: verdict={summary.get('verdict', _NA)} · "
        f"{summary.get('passed', 0)}/{summary.get('total', 0)} passed · "
        f"{summary.get('failed', 0)} failed · {summary.get('errored', 0)} errored"
    )


def _header(report: dict[str, Any]) -> str:
    summary = report.get("summary") or {}
    return "\n".join(
        [
            "## CV-Infra Verification Report",
            "",
            f"**Verdict:** `{summary.get('verdict', _NA)}` · "
            f"**Outcome:** `{summary.get('report_outcome', _NA)}` · "
            f"{summary.get('total', 0)} request(s): {summary.get('passed', 0)} passed, "
            f"{summary.get('failed', 0)} failed, {summary.get('errored', 0)} errored",
            "",
            f"envelope `{report.get('envelope_id', _NA)}` · "
            f"trigger `{report.get('trigger_source', _NA)}` · "
            f"generated {report.get('generated_at', _NA)}",
        ]
    )


def _matrix_section(report: dict[str, Any]) -> str:
    rows = report.get("matrix") or []
    header = "### Pass/Fail matrix"
    if not rows:
        return f"{header}\n\n_(집계된 요청 없음)_"
    table = [
        "| request | sut | verdict | repeats | pass | fail | flaky | metrics |",
        "|---|---|---|---|---|---|---|---|",
        *(_matrix_row(row) for row in rows),
    ]
    return f"{header}\n\n" + "\n".join(table)


def _matrix_row(row: dict[str, Any]) -> str:
    rollup = row.get("rollup") or {}
    verdicts = rollup.get("verdicts") or []
    verdict = rollup.get("verdict")
    cells = (
        row.get("request_id", _NA),
        row.get("sut_ref") or _NA,
        verdict if verdict is not None else "errored",
        rollup.get("repeats", len(verdicts)),
        verdicts.count("pass"),
        verdicts.count("fail"),
        "yes" if rollup.get("flaky") else "no",
        _metrics_cell(row.get("metrics")),
    )
    return "| " + " | ".join(_md_cell(cell) for cell in cells) + " |"


def _metrics_cell(metrics: Any) -> str:
    """Compact metrics cell (honest, deterministic). Only measured values are shown:
    keys with a ``None`` value (the M1 ``Metrics`` model defaults unset fields to
    ``None``) are omitted, and an empty map (a true T3-seam row carries ``{}``) or an
    all-``None`` map renders ``n/a`` — never a fabricated 0/None. Keys are sorted for
    determinism; a ``0`` value (e.g. ``collision_count=0``) IS a measurement and stays."""
    measured = {key: value for key, value in (metrics or {}).items() if value is not None}
    if not measured:
        return _NA
    return ", ".join(f"{key}={measured[key]}" for key in sorted(measured))


def _regression_section(report: dict[str, Any]) -> str:
    """Regression / baseline messaging (§3.7, LOCKED §7.13 C-1).

    Absent baseline = skip(정상), never read as a failing result (REQ-REPORT-004); a
    regressed row surfaces the report's ``regression.detail`` VERBATIM (재도출 금지,
    NFR-REPORT-001 식별성 — which request vs which SUT/date)."""
    bsum = report.get("baseline_summary") or {}
    absent = bsum.get("absent", 0)
    regressed = bsum.get("regressed", 0)
    improved = bsum.get("improved", 0)
    header = "### Regression vs baseline (C-1: cv-infra 내부 SQLite 한정)"
    lines: list[str] = []
    if absent:
        lines.append(
            f"- baseline 부재 {absent}건 — 회귀 판정 skip(정상: 최초 실행/신규 SUT, 실패 아님)"
        )
    if regressed:
        lines.append(f"- 회귀 {regressed}건:")
        for row in report.get("matrix") or []:
            reg = row.get("regression") or {}
            if reg.get("status") == "regressed":
                lines.append(f"  - {reg.get('detail')}")
    if improved:
        lines.append(f"- 개선 {improved}건 (baseline 대비 fail→pass)")
    if not lines:
        lines.append("- baseline 대비 회귀 없음")
    return f"{header}\n\n" + "\n".join(lines)


def _artifact_section(report: dict[str, Any]) -> str:
    rows = report.get("matrix") or []
    header = "### Artifacts"
    blocks = [
        _artifact_line(row.get("request_id", _NA), entry)
        for row in rows
        for entry in (row.get("artifacts") or {}).get("selected") or []
    ]
    if not blocks:
        return f"{header}\n\n_(선별된 아티팩트 없음)_"
    policy = _first_policy(rows)
    lead = f"{header}\n\n_선별 정책: {policy}_" if policy else header
    return lead + "\n\n" + "\n".join(blocks)


def _artifact_line(request_id: str, entry: dict[str, Any]) -> str:
    entry_excluded = entry.get("excluded") or []
    parts: list[str] = []
    for kind in _ARTIFACT_KINDS:
        label = _ARTIFACT_LABELS[kind]
        if kind in entry_excluded:
            parts.append(f"{label}: 제외(상한 초과)")
        elif entry.get(kind):
            parts.append(f"{label}: `{entry[kind]}`")
        else:
            parts.append(f"{label}: {_NO_PATH_NOTE}")
    warnings = entry.get("warnings") or []
    warn = f" — 경고: {'; '.join(warnings)}" if warnings else ""
    return (
        f"- {request_id} (repeat {entry.get('repeat_index')}, {entry.get('role')}, "
        f"verdict={entry.get('verdict')}): {' · '.join(parts)}{warn}"
    )


# --------------------------------------------------------------------------- #
# Small pure helpers
# --------------------------------------------------------------------------- #
def _classify_entry(
    request_id: Any,
    entry: dict[str, Any],
    uploads: list[dict[str, Any]],
    missing: list[dict[str, Any]],
    excluded: list[dict[str, Any]],
) -> None:
    """Split one already-selected entry's three artifact kinds (no re-selection)."""
    entry_excluded = entry.get("excluded") or []
    warnings = list(entry.get("warnings") or [])
    context = {
        "request_id": request_id,
        "repeat_index": entry.get("repeat_index"),
        "role": entry.get("role"),
        "verdict": entry.get("verdict"),
    }
    for kind in _ARTIFACT_KINDS:
        if kind in entry_excluded:
            excluded.append({**context, "kind": kind, "warnings": warnings})
        elif entry.get(kind):
            uploads.append({**context, "kind": kind, "path": entry[kind]})
        else:
            missing.append({**context, "kind": kind, "note": _NO_PATH_NOTE})


def _first_policy(rows: list[dict[str, Any]]) -> str | None:
    """The artifact-selection policy provenance (identical across rows by
    construction — ``aggregate._ARTIFACT_POLICY``); ``None`` if no row carries one."""
    for row in rows:
        policy = (row.get("artifacts") or {}).get("policy")
        if policy:
            return policy
    return None


def _md_cell(value: Any) -> str:
    """Stringify a table cell, escaping ``|`` so a value never breaks the row."""
    return str(value).replace("|", "\\|")
