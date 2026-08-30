"""M4 report EDGE paths — the branches the happy-path suites never take (p8c2 T5).

Six unreached statements/branches in ``cv_infra/report/*``, each closed by the
CONTRACT that makes it exist rather than by re-asserting today's bytes:

* ``identity_display`` — a key that FITS the display cell renders verbatim and must
  not be marked (nor legended) as abbreviated. That honesty invariant is the whole
  reason ``was_abbreviated`` is a separate predicate;
* ``aggregate._sut_ref`` — an ``image_id``-pinned SUT renders ``ref@id`` while the
  ``request_identity_key`` stays PUT (the SUT axis is excluded, REQ-REPORT-002), so
  pinning the exact image can never orphan a baseline;
* ``aggregate._representative_index`` / ``_metrics`` — a request that produced NO
  result at all (the real ``roll_up(rid, [])`` shape: no domain verdict = errored)
  reports ``metrics {}`` + no artifacts instead of crashing;
* ``aggregate._apply_mcap_cap`` — the NON-exclusion arc under a live cap: an
  under-cap bag is kept, and a selected entry carrying no bag path is never
  "excluded" over a size it has no file for (결정 #2 pairs with the over-cap
  positive in ``test_report_verification_report.py``);
* ``github._regression_section`` — the ``improved`` count line (baseline fail ->
  current pass), the one baseline outcome no suite rendered.

Fixtures are the REAL producers (M1 models + M3 ``roll_up`` + ``build_report``),
never hand-built report dicts (G-17/G-28). Stdlib + pytest.
"""

from __future__ import annotations

import pytest

from cv_infra.contract.schema import Result, VerificationRequest
from cv_infra.orchestrator.models import RequestRollup, Verdict
from cv_infra.orchestrator.rollup import roll_up
from cv_infra.orchestrator.store import Store
from cv_infra.report import github
from cv_infra.report.aggregate import RequestReportInput, build_report
from cv_infra.report.baseline import update_baseline
from cv_infra.report.identity_display import (
    CELL_CHARS,
    TRUNCATION_MARK,
    identity_cell,
    was_abbreviated,
)
from cv_infra.report.matrix import _IDENTITY_LEGEND, build_matrix, render_text
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

#: A full ``sut.image_id`` pin (M1 requires a complete ``sha256:`` id). Shape sample
#: only — never a fetchable image (schema.py, docs/evidence-anchors.md).
_IMAGE_ID = "sha256:" + "4a" * 32


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


def _one_pass_report(tmp_path, **kw) -> dict:
    inputs = [
        RequestReportInput(
            request=_request(),
            rollup=_rollup("req-0", Verdict.PASS, [Verdict.PASS]),
            results=[_result("req-0:0", "pass")],
        )
    ]
    return _build(inputs, tmp_path, **kw)


# --------------------------------------------------------------------------- #
# (1) identity_display — a key that FITS is rendered verbatim, and no surface
#     claims an abbreviation that did not happen (identity_display.py:52)
# --------------------------------------------------------------------------- #
def test_identity_cell_keeps_a_fitting_key_verbatim_and_claims_no_abbreviation():
    """``identity_cell`` marks the cut ONLY when characters were actually dropped.

    The cut-off is ``<=``, not ``<``: at EXACTLY ``CELL_CHARS`` nothing is dropped,
    so appending ``TRUNCATION_MARK`` there would advertise a truncation that never
    happened (and, via ``was_abbreviated``, a legend to go with it)."""
    full = identity_key(_BASE_REQUEST)
    assert len(full) > CELL_CHARS  # premise: a real key does not fit

    # positive control — the mark IS applied when a suffix is genuinely dropped.
    abbreviated = identity_cell(full, absent="-")
    assert was_abbreviated(abbreviated)
    assert abbreviated == full[:CELL_CHARS] + TRUNCATION_MARK

    # the boundary + shorter: rendered verbatim, and NOT claimed as abbreviated.
    for fits in (full[:CELL_CHARS], full[: CELL_CHARS - 1], "sha256:0"):
        cell = identity_cell(fits, absent="-")
        assert cell == fits  # nothing dropped -> nothing rewritten
        assert not was_abbreviated(cell)  # ... and nothing claimed
    # the two renders differ by exactly the mark a '<' bound would have added.
    assert abbreviated.removesuffix(TRUNCATION_MARK) == full[:CELL_CHARS]


@pytest.mark.parametrize("surface", ["cli-text-table", "github-markdown"])
def test_a_fitting_key_drops_the_abbreviation_legend_on_both_human_surfaces(tmp_path, surface):
    """The shared rule is what BOTH human surfaces gate their legend on, so neither
    can announce an abbreviation on a table that has none. ``TRUNCATION_MARK`` is the
    single observable: it appears in an abbreviated cell AND in either surface's
    legend, and nowhere else in a body (the regression section prints keys FULL)."""

    def rendered(key: str) -> str:
        if surface == "cli-text-table":
            matrix = build_matrix([_rollup("req-0", Verdict.PASS, [Verdict.PASS])])
            matrix["matrix"][0]["request_identity_key"] = key
            return render_text(matrix)
        report = _one_pass_report(tmp_path)
        report["matrix"][0]["request_identity_key"] = key
        return github.render_step_summary(report)

    full = identity_key(_BASE_REQUEST)
    long_render = rendered(full)
    assert TRUNCATION_MARK in long_render  # positive control: cell + legend

    short_render = rendered(full[:CELL_CHARS])
    assert TRUNCATION_MARK not in short_render  # no mark anywhere -> no legend either
    assert _IDENTITY_LEGEND not in short_render  # (the CLI legend by name)
    assert full[:CELL_CHARS] in short_render  # the value is still SHOWN, just whole


# --------------------------------------------------------------------------- #
# (2) aggregate._sut_ref — an image_id pin is DISPLAY, not identity (aggregate.py:205)
# --------------------------------------------------------------------------- #
def test_image_id_pin_shows_in_sut_ref_without_moving_the_identity_key(tmp_path):
    """``sut.image_id`` (FU-10 image-as-artifact pin) renders as ``ref@id`` in the
    row, but the ``request_identity_key`` is unchanged: ``sut`` is THE excluded axis
    (REQ-REPORT-002). If the pin leaked into the key, pinning the digest of the very
    image under test would orphan its own baseline on every rebuild."""
    unpinned = _request()
    pinned = _request(sut={"image_ref": "carter-sut:b", "image_id": _IMAGE_ID})
    assert pinned["sut"]["image_id"] == _IMAGE_ID  # premise: the pin is on the wire

    report = _build(
        [
            RequestReportInput(
                request=pinned,
                rollup=_rollup("req-0", Verdict.PASS, [Verdict.PASS]),
                results=[_result("req-0:0", "pass")],
            )
        ],
        tmp_path,
    )
    row = report["matrix"][0]
    assert row["sut_ref"] == f"carter-sut:b@{_IMAGE_ID}"  # ref@id, both parts kept
    assert row["request_identity_key"] == identity_key(unpinned)  # SUT axis excluded


# --------------------------------------------------------------------------- #
# (3) aggregate._representative_index / _metrics — a request with NO result
#     (aggregate.py:255, 264)
# --------------------------------------------------------------------------- #
def test_request_with_no_results_reports_empty_metrics_and_no_artifacts(tmp_path):
    """A request whose repeats never produced a terminal result — the shape
    ``api._report_inputs`` builds when ``record.results`` holds nothing for it. The
    REAL M3 producer is used so the row cannot be a shape the control plane never
    emits: ``roll_up(rid, [])`` = no verdicts, ``verdict=None``, ``flakiness=None``.

    There is no representative repeat to speak for, so metrics are honestly ``{}``
    (never a fabricated 0) and nothing is selected for upload; the request surfaces
    as errored -> ``report_outcome=errored`` (exit-3 territory, §3.3 D), and the
    regression is skipped for the errored reason, not the absent-baseline one."""
    empty = roll_up("req-0", [])
    assert (empty.verdicts, empty.verdict, empty.flakiness) == ([], None, None)  # premise

    report = _build([RequestReportInput(request=_request(), rollup=empty, results=[])], tmp_path)
    row = report["matrix"][0]
    assert row["metrics"] == {}
    assert row["artifacts"]["selected"] == []
    assert row["rollup"] == {"repeats": 0, "verdicts": [], "flaky": False, "verdict": None}
    assert report["summary"]["errored"] == 1
    assert report["summary"]["report_outcome"] == "errored"
    assert "errored" in row["regression"]["detail"]  # skipped as errored, not as no-baseline

    # ... and every publish surface still renders it (crash-0 on a live shape).
    body = github.render_step_summary(report)
    assert "| req-0 | carter-sut:b | errored |" in body
    assert github.render_artifact_manifest(report) == {
        "policy": row["artifacts"]["policy"],
        "uploads": [],
        "missing": [],
        "excluded": [],
    }


# --------------------------------------------------------------------------- #
# (4) aggregate._apply_mcap_cap — the NON-exclusion arc under a live cap
#     (aggregate.py:308->exit)
# --------------------------------------------------------------------------- #
def test_live_cap_excludes_nothing_it_should_not(tmp_path):
    """G-35 pairing for the over-cap exclusion (test_report_verification_report.py):
    with the cap ARMED and the size KNOWN, two entries must still come through
    untouched — an under-cap bag (결정 #2 excludes 상한 초과, not everything measured)
    and an entry with no bag path at all (there is no file to drop, so a warning
    there would be a fabricated exclusion, §2-4)."""
    inputs = [
        RequestReportInput(
            request=_request(),
            rollup=_rollup("req-0", Verdict.FAIL, [Verdict.FAIL, Verdict.FAIL]),
            results=[
                _result("req-0:0", "fail", mcap="small.mcap", extra={"mcap_bytes": 100}),
                # no mcap path, yet the plane reported a size WAY over the cap
                _result("req-0:1", "fail", extra={"mcap_bytes": 10_000}),
            ],
        )
    ]
    report = _build(inputs, tmp_path, max_mcap_bytes=100)
    under_cap, no_path = report["matrix"][0]["artifacts"]["selected"]

    # exactly AT the cap is not 초과 -> kept whole, no warning.
    assert under_cap["rosbag_mcap"] == "small.mcap"
    assert (under_cap["excluded"], under_cap["warnings"]) == ([], [])
    # no path -> nothing to exclude, however big the reported size is.
    assert no_path["rosbag_mcap"] is None
    assert (no_path["excluded"], no_path["warnings"]) == ([], [])
    # and the manifest agrees: an absent path is 'missing', never 'excluded'.
    manifest = github.render_artifact_manifest(report)
    assert manifest["excluded"] == []
    assert {m["kind"] for m in manifest["missing"]} >= {"rosbag_mcap"}


# --------------------------------------------------------------------------- #
# (5) github._regression_section — the improved line (github.py:321)
# --------------------------------------------------------------------------- #
def test_improved_rows_are_reported_as_improvement_not_as_no_regression(tmp_path):
    """baseline fail -> current pass = ``improved``. The section must SAY so: the
    fallback line ('회귀 없음') is technically true here but drops the one fact this
    run carries, and the count has to be the report's own ``baseline_summary``
    counter so the sentence cannot drift from the rows it describes."""
    request = _request(sut={"image_ref": "carter-sut:fixed"})
    inputs = [
        RequestReportInput(
            request=request,
            rollup=_rollup("req-0", Verdict.PASS, [Verdict.PASS]),
            results=[_result("req-0:0", "pass")],
        )
    ]
    with Store(tmp_path / "cv.sqlite3") as store:
        update_baseline(
            store,
            request_identity_key=identity_key(request),
            sut_ref="carter-sut:broken",
            verdict="fail",  # the baseline a first-run fail established (baseline.py)
            established_at="2026-07-10T00:00:00+00:00",
        )
        report = build_report(
            inputs, store, envelope_id="env-1", trigger_source="ci-cd", generated_at=_AT
        )
    assert report["matrix"][0]["regression"]["status"] == "improved"  # premise
    assert report["baseline_summary"] == {
        **report["baseline_summary"],
        "improved": 1,
        "regressed": 0,
        "absent": 0,
        "matched": 1,
    }

    body = github.render_step_summary(report)
    improved = report["baseline_summary"]["improved"]
    assert f"- 개선 {improved}건" in body  # the count comes from the report's counter
    assert "회귀 없음" not in body  # the no-baseline-news fallback must not win here
    # a pass against a fail baseline is not a regression, and is not silently a skip.
    assert "회귀 1건" not in body
    assert "skip(정상" not in body


# --------------------------------------------------------------------------- #
# (6) github._first_policy — a row carrying no policy does not end the search
#     (github.py:413->411)
# --------------------------------------------------------------------------- #
# This arc IS reached today, but only from an M8 test driving the CLI
# (tests/test_cli_error_paths.py::test_artifact_block_omits_the_policy_line_when_
# the_report_carries_none -> cli/batch._render_artifacts -> this renderer). A
# branch of ours pinned only by another module's suite reopens silently the day
# that suite changes, so the contract is stated here too.
def test_a_row_without_a_selection_policy_neither_stops_the_search_nor_invents_one(tmp_path):
    """``_first_policy`` scans for the provenance line and takes the FIRST row that
    carries one — rows are not required to be homogeneous (a report JSON can reach
    the renderer from an older/partial producer, which is exactly how the CLI meets
    one). A row without it is skipped, not treated as "no policy anywhere"; and when
    NO row carries one the surfaces drop the provenance line instead of printing a
    fabricated (or ``None``) policy (§2-4)."""
    inputs = [
        RequestReportInput(
            request=_request(),
            rollup=_rollup("req-0", Verdict.FAIL, [Verdict.FAIL]),
            results=[_result("req-0:0", "fail", mcap="f0.mcap")],
        ),
        RequestReportInput(
            request=_request(sut={"image_ref": "carter-sut:x"}),
            rollup=_rollup("req-1", Verdict.FAIL, [Verdict.FAIL]),
            results=[_result("req-1:0", "fail", mcap="f1.mcap")],
        ),
    ]
    report = _build(inputs, tmp_path)
    policy = report["matrix"][0]["artifacts"]["policy"]
    assert policy and report["matrix"][1]["artifacts"]["policy"] == policy  # premise

    del report["matrix"][0]["artifacts"]["policy"]  # heterogeneous rows
    assert github.render_artifact_manifest(report)["policy"] == policy  # kept looking
    assert policy in github.render_step_summary(report)

    for row in report["matrix"]:  # nobody carries one
        row["artifacts"].pop("policy", None)
    assert github.render_artifact_manifest(report)["policy"] is None
    body = github.render_step_summary(report)
    assert "선별 정책" not in body  # dropped, not fabricated / stringified None
    assert "f0.mcap" in body and "f1.mcap" in body  # the artifact rows still render
