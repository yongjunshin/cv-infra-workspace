"""M4 ``github.py`` publish-payload renderer tests (SR-22) — REQ-REPORT-004/007, M4-09, DoD-P5-13.

Every fixture is the REAL ``aggregate.build_report`` output built from the M1 models
(anchored to the canonical fixture producers in ``test_report_verification_report.py``,
G-17/G-28 — never a hand-built report dict that could drift). Coverage:

* the 4-surface renderers (Check Run payload / sticky comment / step summary /
  artifact manifest);
* the exit->conclusion single source (github.py holds no mapping literal — the
  conclusion tracks the IMPORTED ``cv_infra.cli.exit_codes`` table);
* the 3 outcome renders — pass/fail/errored fold to the imported conclusions, and
  errored surfaces ``INFRA_INCOMPLETE_MESSAGE`` (exit 3 never collapsed, DoD-P5-13);
* determinism/idempotency (byte-identical repeat renders incl. the sticky marker —
  the P5-14 upsert premise);
* honest absence — a T3-seam report (``metrics: {}`` + ``None`` artifact paths)
  renders ``n/a`` / ``경로 미제공(제어평면)`` with zero crashes and no fabrication;
* §3.7 baseline messaging (absent = skip(정상), regressed detail VERBATIM);
* the M4-09 negative (source holds no network/token literal; a standalone import
  drags no server/network graph).

Stdlib + pytest.
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest

from cv_infra.cli.exit_codes import (
    CHECK_CONCLUSION_BY_EXIT,
    EXIT_FAIL,
    EXIT_INFRA,
    EXIT_PASS,
    INFRA_INCOMPLETE_MESSAGE,
    exit_code_for_report_outcome,
)
from cv_infra.contract.schema import Result, VerificationRequest
from cv_infra.orchestrator.models import RequestRollup, Verdict
from cv_infra.orchestrator.store import Store
from cv_infra.report import github
from cv_infra.report.aggregate import RequestReportInput, build_report
from cv_infra.report.baseline import update_baseline
from cv_infra.report.regression import identity_key

_AT = "2026-07-16T00:00:00+00:00"

_BASE_REQUEST = {
    "scenario": {
        "scene": "nova_carter_warehouse",
        "robot": "nova_carter",
        "goal": {"x": -6.0, "y": 5.0, "yaw": 1.5708},
        "seed": 42,
        "timeout_s": 120,
    },
    "sut": {"image_ref": "carter-sut:b"},
    "acceptance_criteria": [{"oracle": "reached_goal"}],
}


# --------------------------------------------------------------------------- #
# Builders (mirror the canonical fixture producers — G-28 anchor)
# --------------------------------------------------------------------------- #
def _request(**overrides) -> dict:
    return VerificationRequest.model_validate({**_BASE_REQUEST, **overrides}).model_dump(
        mode="json", by_alias=True
    )


def _result(job_id: str, verdict: str, *, mcap=None, mp4=None, metrics=None, extra=None) -> dict:
    dump = Result.model_validate(
        {
            "job_id": job_id,
            "verdict": verdict,
            "metrics": metrics or {},
            "artifacts": {"mcap": mcap, "mp4": mp4},
        }
    ).model_dump(mode="json")
    dump.update(extra or {})
    return dump


def _rollup(request_id, verdict, verdicts, flakiness=0.0) -> RequestRollup:
    return RequestRollup(
        request_id=request_id, verdicts=verdicts, flakiness=flakiness, verdict=verdict
    )


def _build(inputs, tmp_path, **kw) -> dict:
    with Store(tmp_path / "cv.sqlite3") as store:
        return build_report(
            inputs, store, envelope_id="env-1", trigger_source="ci-cd", generated_at=_AT, **kw
        )


def _pass_report(tmp_path, **kw) -> dict:
    inputs = [
        RequestReportInput(
            request=_request(),
            rollup=_rollup("req-0", Verdict.PASS, [Verdict.PASS, Verdict.PASS]),
            results=[
                _result(
                    "req-0:0",
                    "pass",
                    mcap="p0.mcap",
                    mp4="p0.mp4",
                    metrics={"time_to_goal_s": 10.0},
                ),
                _result("req-0:1", "pass"),
            ],
        )
    ]
    return _build(inputs, tmp_path, **kw)


def _fail_report(tmp_path, **kw) -> dict:
    inputs = [
        RequestReportInput(
            request=_request(),
            rollup=_rollup("req-0", Verdict.FAIL, [Verdict.FAIL]),
            results=[_result("req-0:0", "fail", mcap="f0.mcap")],
        )
    ]
    return _build(inputs, tmp_path, **kw)


def _errored_report(tmp_path, **kw) -> dict:
    inputs = [
        RequestReportInput(
            request=_request(),
            rollup=_rollup("req-0", Verdict.PASS, [Verdict.PASS]),
            results=[_result("req-0:0", "pass")],
        ),
        RequestReportInput(
            request=_request(sut={"image_ref": "carter-sut:x"}),
            rollup=_rollup("req-1", None, []),  # errored: no domain verdict
            results=[_result("req-1:0", "error")],
        ),
    ]
    return _build(inputs, tmp_path, **kw)


def _seam_result(job_id: str, verdict: str) -> dict:
    """The plain control-plane Result dict ``api._result_wire`` emits VERBATIM (G-28
    anchor): verdict only — ``metrics {}`` + ``artifacts {}`` (no path, no result_json,
    no M1 model default 4-key metrics)."""
    return {"job_id": job_id, "verdict": verdict, "metrics": {}, "artifacts": {}}


def _seam_report(tmp_path, **kw) -> dict:
    """A T3-seam-shaped report: control plane carries verdict only — metrics {} and
    every artifact path None (built from ``_seam_result`` = ``api._result_wire``)."""
    inputs = [
        RequestReportInput(
            request=_request(),
            rollup=_rollup("req-0", Verdict.FAIL, [Verdict.PASS, Verdict.FAIL]),
            results=[_seam_result("req-0:0", "pass"), _seam_result("req-0:1", "fail")],
        )
    ]
    return _build(inputs, tmp_path, **kw)


# --------------------------------------------------------------------------- #
# (1) Check Run — shape + the 3 outcome conclusions (imported single source)
# --------------------------------------------------------------------------- #
def test_check_run_shape_and_pass_conclusion(tmp_path):
    payload = github.render_check_run(_pass_report(tmp_path))
    assert set(payload) == {"name", "status", "conclusion", "output"}
    assert payload["status"] == "completed"
    assert payload["conclusion"] == CHECK_CONCLUSION_BY_EXIT[EXIT_PASS]
    assert set(payload["output"]) == {"title", "summary"}
    assert "CV-Infra verification" in payload["output"]["title"]
    # a pass Check never carries the infra-incomplete note.
    assert INFRA_INCOMPLETE_MESSAGE not in payload["output"]["summary"]


def test_check_run_fail_conclusion(tmp_path):
    payload = github.render_check_run(_fail_report(tmp_path))
    assert payload["conclusion"] == CHECK_CONCLUSION_BY_EXIT[EXIT_FAIL]
    assert INFRA_INCOMPLETE_MESSAGE not in payload["output"]["summary"]


def test_check_run_errored_is_infra_conclusion_with_message(tmp_path):
    payload = github.render_check_run(_errored_report(tmp_path))
    # errored -> exit 3 -> the imported infra conclusion, NOT the fail conclusion
    # (exit 3 is never collapsed into a failing conclusion — D-I / DoD-P5-13).
    assert payload["conclusion"] == CHECK_CONCLUSION_BY_EXIT[EXIT_INFRA]
    assert payload["conclusion"] != CHECK_CONCLUSION_BY_EXIT[EXIT_FAIL]
    assert INFRA_INCOMPLETE_MESSAGE in payload["output"]["summary"]


@pytest.mark.parametrize("outcome", ["pass", "fail", "errored"])
def test_conclusion_tracks_the_imported_single_source(tmp_path, outcome):
    builders = {"pass": _pass_report, "fail": _fail_report, "errored": _errored_report}
    report = builders[outcome](tmp_path)
    payload = github.render_check_run(report)
    expected = CHECK_CONCLUSION_BY_EXIT[
        exit_code_for_report_outcome(report["summary"]["report_outcome"])
    ]
    assert payload["conclusion"] == expected


# --------------------------------------------------------------------------- #
# (2) single source + M4-09: no mapping/network/token literal in the source
# --------------------------------------------------------------------------- #
def _github_source() -> str:
    return Path(github.__file__).read_text(encoding="utf-8")


@pytest.mark.parametrize("literal", ["success", "failure", "neutral"])
def test_no_conclusion_mapping_literal_in_source(literal):
    # The exit->conclusion table is the M8 single source (cv_infra.cli.exit_codes);
    # github.py must not re-declare any conclusion literal (duplicate-literal gate).
    assert literal not in _github_source()


@pytest.mark.parametrize("literal", ["GITHUB_TOKEN", "httpx", "requests", "urllib.request"])
def test_no_network_or_token_literal_in_source(literal):
    # M4-09 negative (G-21 전구문형): the core renderer holds no GitHub token and
    # touches no network — the Action plane owns the real API calls (LOCKED §7.14).
    assert literal not in _github_source()


def test_standalone_import_drags_no_network_or_server_graph():
    # M4-09 이식성: importing the renderer alone must pull no network/server graph —
    # a fresh interpreter proves the property regardless of what the session loaded.
    code = (
        "import sys, cv_infra.report.github\n"
        "heavy = sorted(m for m in sys.modules if m.split('.')[0] in "
        "{'httpx', 'requests', 'urllib3', 'fastapi', 'starlette', 'uvicorn'})\n"
        "assert not heavy, heavy\n"
        "assert 'urllib.request' not in sys.modules\n"
    )
    result = subprocess.run(
        [sys.executable, "-c", code],
        env={**os.environ, "PYTHONPATH": ""},
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stderr


# --------------------------------------------------------------------------- #
# (3) sticky comment / step summary relationship + determinism
# --------------------------------------------------------------------------- #
def test_sticky_comment_leads_with_marker(tmp_path):
    comment = github.render_sticky_comment(_pass_report(tmp_path))
    assert comment.startswith(github.STICKY_COMMENT_MARKER)
    assert github.STICKY_COMMENT_MARKER == "<!-- cv-infra:verification-report -->"
    assert "### Pass/Fail matrix" in comment


def test_step_summary_is_the_body_without_marker(tmp_path):
    report = _pass_report(tmp_path)
    step = github.render_step_summary(report)
    assert github.STICKY_COMMENT_MARKER not in step
    # sticky == marker + newline + step-summary body (the shared body is identical).
    assert github.render_sticky_comment(report) == f"{github.STICKY_COMMENT_MARKER}\n{step}"


@pytest.mark.parametrize(
    "render",
    [
        lambda r: github.render_check_run(r),
        github.render_sticky_comment,
        github.render_step_summary,
        github.render_artifact_manifest,
    ],
)
def test_renders_are_idempotent(tmp_path, render):
    report = _fail_report(tmp_path)
    assert render(report) == render(report)  # byte-identical repeat (P5-14 premise)


# --------------------------------------------------------------------------- #
# (4) honest absence — T3-seam metrics {} / None artifact paths
# --------------------------------------------------------------------------- #
def test_seam_report_renders_metrics_and_paths_honestly(tmp_path):
    report = _seam_report(tmp_path)
    # sanity: this IS the seam shape (empty metrics, None artifact paths).
    row = report["matrix"][0]
    assert row["metrics"] == {}
    assert all(e["rosbag_mcap"] is None for e in row["artifacts"]["selected"])

    body = github.render_step_summary(report)  # must not raise
    assert "n/a" in body  # metrics {} -> honest n/a, never a fabricated 0
    assert github._NO_PATH_NOTE in body  # None path -> 경로 미제공(제어평면)


def test_seam_manifest_routes_none_paths_to_missing(tmp_path):
    manifest = github.render_artifact_manifest(_seam_report(tmp_path))
    assert manifest["uploads"] == []  # no concrete file paths on the control plane
    assert manifest["excluded"] == []  # None != size-cap exclusion
    assert manifest["missing"]  # every selected artifact kind is honestly missing
    assert all(m["note"] == github._NO_PATH_NOTE for m in manifest["missing"])


def test_empty_matrix_report_does_not_crash(tmp_path):
    report = _build([], tmp_path)
    assert github.render_check_run(report)["status"] == "completed"
    assert github.STICKY_COMMENT_MARKER in github.render_sticky_comment(report)
    assert github.render_artifact_manifest(report)["uploads"] == []


# --------------------------------------------------------------------------- #
# (5) §3.7 baseline messaging — absent = skip(정상), regressed detail VERBATIM
# --------------------------------------------------------------------------- #
def test_absent_baseline_reads_as_skip_not_failure(tmp_path):
    body = github.render_step_summary(_pass_report(tmp_path))
    assert "skip(정상" in body  # baseline 부재 = skip(정상), never a failure (REQ-REPORT-004)
    assert "실패 아님" in body  # §3.7 wording preserved so it is not misread as a failure


def test_regression_detail_is_surfaced_verbatim(tmp_path):
    request = _request(sut={"image_ref": "carter-sut:new"})
    inputs = [
        RequestReportInput(
            request=request,
            rollup=_rollup("req-0", Verdict.FAIL, [Verdict.FAIL]),
            results=[_result("req-0:0", "fail")],
        )
    ]
    with Store(tmp_path / "cv.sqlite3") as store:
        update_baseline(
            store,
            request_identity_key=identity_key(request),
            sut_ref="carter-sut:old",
            verdict="pass",
            established_at="2026-07-10T00:00:00+00:00",
        )
        report = build_report(
            inputs, store, envelope_id="env-1", trigger_source="ci-cd", generated_at=_AT
        )
    detail = report["matrix"][0]["regression"]["detail"]
    assert report["matrix"][0]["regression"]["status"] == "regressed"
    # the detail (which request vs which SUT/date) is surfaced VERBATIM, not re-derived.
    assert detail in github.render_sticky_comment(report)
    assert detail in github.render_check_run(report)["output"]["summary"]


# --------------------------------------------------------------------------- #
# (6) artifact manifest — no re-selection, size-cap passthrough
# --------------------------------------------------------------------------- #
def test_manifest_reflects_aggregate_selection_without_reselecting(tmp_path):
    # 3 pass + 2 fail -> aggregate selects the 2 fails + the FIRST pass (rep). The
    # manifest flattens THAT selection; it never re-selects.
    results = [
        _result("req-0:0", "pass", mcap="p0.mcap", mp4="p0.mp4"),
        _result("req-0:1", "fail", mcap="f1.mcap"),
        _result("req-0:2", "pass", mcap="p2.mcap"),
        _result("req-0:3", "fail", mcap="f3.mcap"),
        _result("req-0:4", "pass", mcap="p4.mcap"),
    ]
    inputs = [
        RequestReportInput(
            request=_request(),
            rollup=_rollup(
                "req-0",
                Verdict.FAIL,
                [Verdict.PASS, Verdict.FAIL, Verdict.PASS, Verdict.FAIL, Verdict.PASS],
            ),
            results=results,
        )
    ]
    report = _build(inputs, tmp_path)
    manifest = github.render_artifact_manifest(report)
    # exactly the aggregate-selected repeat indices (0 rep-pass, 1 & 3 failures),
    # never the dropped passes 2 or 4.
    selected_indices = sorted({u["repeat_index"] for u in manifest["uploads"]})
    assert selected_indices == [0, 1, 3]
    rosbags = {
        u["repeat_index"]: u["path"] for u in manifest["uploads"] if u["kind"] == "rosbag_mcap"
    }
    assert rosbags == {0: "p0.mcap", 1: "f1.mcap", 3: "f3.mcap"}
    assert manifest["policy"]  # provenance surfaced (결정 #1/2/3)


def test_manifest_size_cap_exclusion_is_passed_through(tmp_path):
    inputs = [
        RequestReportInput(
            request=_request(),
            rollup=_rollup("req-0", Verdict.FAIL, [Verdict.FAIL]),
            results=[_result("req-0:0", "fail", mcap="big.mcap", extra={"mcap_bytes": 500})],
        )
    ]
    report = _build(inputs, tmp_path, max_mcap_bytes=100)  # cap is a PARAMETER (수치 TBD)
    manifest = github.render_artifact_manifest(report)
    excluded_mcap = [e for e in manifest["excluded"] if e["kind"] == "rosbag_mcap"]
    assert len(excluded_mcap) == 1
    assert excluded_mcap[0]["warnings"] and "상한" in excluded_mcap[0]["warnings"][0]
    # the excluded MCAP is NOT among uploads (excluded, not truncated — 결정 #2).
    assert not any(u["kind"] == "rosbag_mcap" for u in manifest["uploads"])
    # the exclusion + warning also surface in the human body.
    assert "제외(상한 초과)" in github.render_step_summary(report)


def test_matrix_row_echoes_rollup_verdict_verbatim(tmp_path):
    # rollup says pass while both results say fail (LOCKED §7.12 contradiction) — the
    # matrix cell must display the rollup verdict, never a recomputation from results.
    inputs = [
        RequestReportInput(
            request=_request(),
            rollup=_rollup("req-0", Verdict.PASS, [Verdict.FAIL, Verdict.FAIL]),
            results=[_result("req-0:0", "fail"), _result("req-0:1", "fail")],
        )
    ]
    report = _build(inputs, tmp_path)
    body = github.render_step_summary(report)
    # the request row renders verdict `pass` (rollup verbatim) even though 0 pass / 2 fail.
    assert "| req-0 |" in body
    row_line = next(line for line in body.splitlines() if line.startswith("| req-0 |"))
    assert "| pass |" in row_line


# --------------------------------------------------------------------------- #
# (7) request_identity_key on the publish surfaces (p5c20 ⑦ 후속)
# --------------------------------------------------------------------------- #
# REQ-REPORT-002/003 + NFR-REPORT-001 식별성 on the surface people actually read:
# the PR. The key IS the C-1 baseline PK, so "baseline 부재 2건" must say WHICH
# identity was not matched and a regressed row must say which identity was
# matched. Measured before this change: `request_identity_key` appeared ZERO
# times in github.py — the matrix showed 8 columns without it and the regression
# section named only the request/SUT/date. Two display forms, deliberately:
# ABBREVIATED in the table (scannability, identical rule to the CLI table) and
# FULL in the regression section (a PR reader's only other copy of the key is
# inside the artifact zip — unlike the CLI reader, who has ``--json``).

#: The three human surfaces that share ``_render_body`` — asserted SEPARATELY so a
#: surface silently losing the column cannot hide behind the other two.
_HUMAN_SURFACES = [
    pytest.param(github.render_sticky_comment, id="sticky-comment"),
    pytest.param(github.render_step_summary, id="step-summary"),
    pytest.param(
        lambda report: github.render_check_run(report)["output"]["summary"], id="check-run"
    ),
]


def _md_row_cells(body: str, lead: str) -> list[str]:
    """Cells of the markdown matrix row whose first column is ``lead``."""
    line = next((ln for ln in body.splitlines() if ln.startswith(f"| {lead} |")), None)
    assert line is not None, f"no matrix row for {lead!r} in:\n{body}"
    return [cell.strip() for cell in line.strip().strip("|").split("|")]


def _two_identity_report(tmp_path) -> dict:
    """Two requests with DIFFERENT identities (differing scenario seed) — a constant
    or request_id-derived cell cannot pass. Neither has a baseline (both absent)."""
    inputs = [
        RequestReportInput(
            request=_request(),
            rollup=_rollup("req-0", Verdict.PASS, [Verdict.PASS]),
            results=[_result("req-0:0", "pass")],
        ),
        RequestReportInput(
            request=_request(scenario={**_BASE_REQUEST["scenario"], "seed": 7}),
            rollup=_rollup("req-1", Verdict.PASS, [Verdict.PASS]),
            results=[_result("req-1:0", "pass")],
        ),
    ]
    return _build(inputs, tmp_path)


def _regressed_report(tmp_path) -> dict:
    """A row that REGRESSED against a seeded baseline (only the SUT moved)."""
    request = _request(sut={"image_ref": "carter-sut:new"})
    inputs = [
        RequestReportInput(
            request=request,
            rollup=_rollup("req-0", Verdict.FAIL, [Verdict.FAIL]),
            results=[_result("req-0:0", "fail")],
        )
    ]
    with Store(tmp_path / "cv.sqlite3") as store:
        update_baseline(
            store,
            request_identity_key=identity_key(request),
            sut_ref="carter-sut:old",
            verdict="pass",
            established_at="2026-07-10T00:00:00+00:00",
        )
        return build_report(
            inputs, store, envelope_id="env-1", trigger_source="ci-cd", generated_at=_AT
        )


@pytest.mark.parametrize("render", _HUMAN_SURFACES)
def test_every_surface_matrix_carries_the_identity_cell(tmp_path, render):
    report = _two_identity_report(tmp_path)
    keys = {row["request_id"]: row["request_identity_key"] for row in report["matrix"]}
    assert keys["req-0"] != keys["req-1"]  # premise: two REAL, different keys
    body = render(report)
    assert "| flaky | identity | metrics |" in body  # column exists, metrics stays last
    cell_0, cell_1 = _md_row_cells(body, "req-0")[7], _md_row_cells(body, "req-1")[7]
    assert cell_0 != "n/a" and cell_1 != "n/a"  # ...and carries VALUES
    assert cell_0 != cell_1  # ...per row (a constant cell cannot pass)
    # abbreviated, but a literal PREFIX of the real key (only a suffix was dropped)
    assert cell_0.endswith("…") and keys["req-0"].startswith(cell_0.removesuffix("…"))
    assert cell_1.endswith("…") and keys["req-1"].startswith(cell_1.removesuffix("…"))
    assert "축약" in body  # the truncation is DECLARED, not silent


@pytest.mark.parametrize("render", _HUMAN_SURFACES)
def test_every_surface_names_the_identity_the_regression_was_judged_on(tmp_path, render):
    report = _regressed_report(tmp_path)
    row = report["matrix"][0]
    assert row["regression"]["status"] == "regressed"  # premise
    body = render(report)
    # FULL key (not the abbreviation) on the same line as the verbatim detail, so
    # "왜 이게 회귀지" is answerable — and copy-pasteable into a baseline lookup.
    line = next(ln for ln in body.splitlines() if row["regression"]["detail"] in ln)
    assert f"identity `{row['request_identity_key']}`" in line
    assert len(row["request_identity_key"]) == 71  # the whole thing, not a prefix


@pytest.mark.parametrize("render", _HUMAN_SURFACES)
def test_every_surface_names_the_identities_with_no_baseline(tmp_path, render):
    """The exact question the first live E2E raised: *"baseline 부재 2건 — 어느
    키에 대한 부재인가"*. Every skipped row is named with its FULL key, and the
    enumeration cannot disagree with the count above it."""
    report = _two_identity_report(tmp_path)
    assert report["baseline_summary"]["absent"] == 2  # premise: both rows skipped
    body = render(report)
    assert "baseline 부재 2건" in body
    named = [ln for ln in body.splitlines() if ln.startswith("  - ") and "identity `sha256:" in ln]
    assert len(named) == report["baseline_summary"]["absent"]
    for row in report["matrix"]:
        assert f"  - {row['request_id']} — identity `{row['request_identity_key']}`" in body


def test_absent_identity_renders_n_a_on_both_surfaces_and_drops_the_legend(tmp_path):
    """§2-4 honesty, paired with the value case (G-35): a row whose producer carried
    NO key renders the markdown null idiom in the table AND in the regression line,
    while its sibling that HAS a key still renders the value. Nothing was
    abbreviated for the missing row, and a table with NO abbreviation at all must
    not carry the abbreviation note."""
    report = _two_identity_report(tmp_path)
    report["matrix"][1]["request_identity_key"] = None
    body = github.render_step_summary(report)
    assert _md_row_cells(body, "req-1")[7] == "n/a"  # absence is rendered as absence
    assert _md_row_cells(body, "req-0")[7].startswith("sha256:")  # value case survives
    assert "  - req-1 — identity n/a" in body  # ...and in the regression enumeration
    assert f"  - req-0 — identity `{report['matrix'][0]['request_identity_key']}`" in body

    # every key gone -> no cell was abbreviated -> the note must disappear
    report["matrix"][0]["request_identity_key"] = None
    stripped = github.render_step_summary(report)
    assert "축약" not in stripped
    assert _md_row_cells(stripped, "req-0")[7] == "n/a"


def test_table_abbreviation_uses_the_same_rule_as_the_cli_table(tmp_path):
    """One display rule, two surfaces (G-56): the markdown cell is the SHARED
    ``identity_display.identity_cell`` output, not a second truncation that could
    drift from the CLI's."""
    from cv_infra.report.identity_display import identity_cell as shared_cell

    report = _two_identity_report(tmp_path)
    body = github.render_step_summary(report)
    for row in report["matrix"]:
        expected = shared_cell(row["request_identity_key"], absent="n/a")
        assert _md_row_cells(body, row["request_id"])[7] == expected
        assert expected != "n/a"  # the comparison is not vacuous


# --------------------------------------------------------------------------- #
# (8) status vocabulary comes from its definition, not local literals (G-56)
# --------------------------------------------------------------------------- #
# ``regression.py`` DEFINES the status values; this renderer used to compare
# ``"regressed"`` / ``"no-baseline"`` as literals of its own. A literal cannot
# drift loudly: if the definition ever changed a value, the producer would emit
# the new one and this file would silently match NOTHING — the count line would
# say 회귀 1건 with no row beneath it. The tests below rename the vocabulary and
# assert the renderer FOLLOWS (they fail on the pre-fix literal implementation).


def test_no_baseline_status_literal_in_github_source():
    # ``"no-baseline"`` can only ever be a status value, so its absence from the
    # source is a real gate. (``"regressed"`` is NOT gated this way: the §3.4
    # ``baseline_summary`` carries a FIELD of the same spelling — a different
    # namespace that legitimately stays a literal. The drift test below covers it.)
    assert '"no-baseline"' not in _github_source()


def test_regression_enumeration_follows_the_status_constant(tmp_path, monkeypatch):
    report = _regressed_report(tmp_path)
    row = report["matrix"][0]
    assert row["regression"]["status"] == "regressed"  # premise: the real value today
    renamed = "regressed-v2"
    row["regression"]["status"] = renamed  # the definition renamed -> producer emits it
    monkeypatch.setattr(github, "STATUS_REGRESSED", renamed)  # the consumer's copy follows
    body = github.render_step_summary(report)
    # the regressed row is still enumerated WITH its identity (a literal would
    # have matched nothing here — 회귀 1건 with an empty list)
    line = next(ln for ln in body.splitlines() if row["regression"]["detail"] in ln)
    assert f"identity `{row['request_identity_key']}`" in line


def test_absent_enumeration_follows_the_status_constant(tmp_path, monkeypatch):
    report = _two_identity_report(tmp_path)
    assert report["baseline_summary"]["absent"] == 2  # premise
    renamed = "skipped-v2"
    for row in report["matrix"]:
        assert row["regression"]["status"] == "no-baseline"
        row["regression"]["status"] = renamed
    monkeypatch.setattr(github, "STATUS_NO_BASELINE", renamed)
    body = github.render_step_summary(report)
    named = [ln for ln in body.splitlines() if ln.startswith("  - ") and "identity `sha256:" in ln]
    # the enumeration still matches the count above it (the invariant a literal breaks)
    assert len(named) == report["baseline_summary"]["absent"]
