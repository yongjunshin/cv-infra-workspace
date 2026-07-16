"""M4 VerificationReport assembly tests (SR-19 + §3.4) — REQ-REPORT-001/003/007.

End-to-end over ``aggregate.build_report``: the report JSON shape (§3.4), the
regression integration + G-35 positive control (baseline must be SEEDED in the
internal store for a regression to appear — proves the store is the only source,
C-1), the LOCKED §7.12 재계산-금지 positive control (a contradictory rollup wins
over the results the row displays), the report_outcome tri-state (errored>0 ->
errored, exit-3 priority), and artifact selection (결정 #1 all-failures+1-rep-pass,
결정 #2 size-cap exclusion+warning — policy only). Result/Request dumps come from
the REAL M1 models (G-17). Stdlib + pytest.
"""

from __future__ import annotations

from cv_infra.contract.schema import Result, VerificationRequest
from cv_infra.orchestrator.models import RequestRollup, Verdict
from cv_infra.orchestrator.store import Store
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
    dump.update(extra or {})  # optional M8-plane ride-alongs (result_json, mcap_bytes)
    return dump


def _rollup(request_id, verdict, verdicts, flakiness=0.0) -> RequestRollup:
    return RequestRollup(
        request_id=request_id, verdicts=verdicts, flakiness=flakiness, verdict=verdict
    )


def _report(inputs, store, **kw):
    return build_report(
        inputs, store, envelope_id="env-1", trigger_source="ci-cd", generated_at=_AT, **kw
    )


# --------------------------------------------------------------------------- #
# (1) report JSON shape (§3.4)
# --------------------------------------------------------------------------- #


def test_report_json_top_level_and_row_shape(tmp_path):
    with Store(tmp_path / "cv.sqlite3") as store:
        request = _request()
        inputs = [
            RequestReportInput(
                request=request,
                rollup=_rollup("req-0", Verdict.PASS, [Verdict.PASS, Verdict.PASS]),
                results=[_result("req-0:0", "pass"), _result("req-0:1", "pass")],
            )
        ]
        report = _report(inputs, store)

    assert report["apiVersion"] == "cv-infra/v1"
    assert report["kind"] == "VerificationReport"
    assert report["envelope_id"] == "env-1"
    assert report["trigger_source"] == "ci-cd"
    assert report["generated_at"] == _AT
    assert report["summary"] == {
        "total": 1,
        "passed": 1,
        "failed": 0,
        "errored": 0,
        "verdict": "pass",
        "report_outcome": "pass",
    }
    (row,) = report["matrix"]
    assert row["request_id"] == "req-0"
    assert row["request_identity_key"] == identity_key(request)
    assert row["sut_ref"] == "carter-sut:b"
    assert row["scenario"] == "nova_carter_warehouse"
    assert row["rollup"] == {
        "repeats": 2,
        "verdicts": ["pass", "pass"],
        "flaky": False,
        "verdict": "pass",
    }
    assert row["regression"]["status"] == "no-baseline"  # skip = normal
    assert report["baseline_summary"]["absent"] == 1
    assert report["baseline_summary"]["regressed"] == 0


# --------------------------------------------------------------------------- #
# (2) regression integration + G-35 positive control (seed makes it appear)
# --------------------------------------------------------------------------- #


def test_regression_only_appears_after_baseline_is_seeded(tmp_path):
    request = _request(sut={"image_ref": "carter-sut:new"})
    inputs = [
        RequestReportInput(
            request=request,
            rollup=_rollup("req-0", Verdict.FAIL, [Verdict.FAIL]),
            results=[_result("req-0:0", "fail")],
        )
    ]
    with Store(tmp_path / "cv.sqlite3") as store:
        # (a) empty store -> no baseline -> no regression (skip). If the baseline
        # came from anywhere but the store, this would already regress.
        before = _report(inputs, store)
        assert before["matrix"][0]["regression"]["status"] == "no-baseline"
        assert before["baseline_summary"]["regressed"] == 0
        assert before["summary"]["report_outcome"] == "fail"  # domain fail regardless

        # (b) SEED a passing baseline for the SAME identity (only SUT differed) ->
        # now the fail is a regression, identified against the seeded SUT.
        update_baseline(
            store,
            request_identity_key=identity_key(request),
            sut_ref="carter-sut:old",
            verdict="pass",
            established_at="2026-07-10T00:00:00+00:00",
        )
        after = _report(inputs, store)
        reg = after["matrix"][0]["regression"]
        assert reg["status"] == "regressed"
        assert reg["baseline_sut_ref"] == "carter-sut:old"
        assert reg["baseline_established_at"] == "2026-07-10T00:00:00+00:00"
        assert "pass→fail" in reg["detail"] and "req-0" in reg["detail"]
        assert after["baseline_summary"] == {
            "matched": 1,
            "absent": 0,
            "regressed": 1,
            "improved": 0,
            "unchanged": 0,
            "note": after["baseline_summary"]["note"],
        }


# --------------------------------------------------------------------------- #
# (3) LOCKED §7.12 재계산-금지 positive control
# --------------------------------------------------------------------------- #


def test_row_verdict_is_rollup_not_recomputed_from_results(tmp_path):
    # Contradiction: every RESULT says fail, but the rollup verdict is pass. Any
    # recomputation from results would read 'fail'; the row must read 'pass'
    # (rollup verbatim). The failing results still drive artifact selection —
    # display, not judgement.
    request = _request()
    inputs = [
        RequestReportInput(
            request=request,
            rollup=_rollup("req-0", Verdict.PASS, [Verdict.FAIL, Verdict.FAIL], flakiness=0.0),
            results=[_result("req-0:0", "fail"), _result("req-0:1", "fail")],
        )
    ]
    with Store(tmp_path / "cv.sqlite3") as store:
        report = _report(inputs, store)
    row = report["matrix"][0]
    assert row["rollup"]["verdict"] == "pass"  # rollup wins over contradicting results
    assert report["summary"]["verdict"] == "pass"
    assert report["summary"]["failed"] == 0
    # the two failing jobs are still selected for artifact upload (display).
    assert [e["role"] for e in row["artifacts"]["selected"]] == ["failure", "failure"]


# --------------------------------------------------------------------------- #
# (4) report_outcome tri-state — errored>0 -> errored (exit-3 priority, §3.3 D)
# --------------------------------------------------------------------------- #


def test_errored_request_makes_report_outcome_errored(tmp_path):
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
    with Store(tmp_path / "cv.sqlite3") as store:
        report = _report(inputs, store)
    summary = report["summary"]
    assert summary["errored"] == 1 and summary["failed"] == 0
    # verdict is pure domain (no domain fail -> pass); report_outcome carries the
    # errored tri-state and is what drives exit 3 (§3.3 D "verdict와 별개로").
    assert summary["verdict"] == "pass"
    assert summary["report_outcome"] == "errored"


def test_failed_without_errored_reports_fail(tmp_path):
    inputs = [
        RequestReportInput(
            request=_request(),
            rollup=_rollup("req-0", Verdict.FAIL, [Verdict.FAIL]),
            results=[_result("req-0:0", "fail")],
        )
    ]
    with Store(tmp_path / "cv.sqlite3") as store:
        report = _report(inputs, store)
    assert report["summary"]["verdict"] == "fail"
    assert report["summary"]["report_outcome"] == "fail"


# --------------------------------------------------------------------------- #
# (5) artifact selection (결정 #1) + metrics representative
# --------------------------------------------------------------------------- #


def test_artifact_selection_all_failures_plus_one_representative_pass(tmp_path):
    # 3 passes + 2 fails -> both fails + the FIRST pass (rep, lowest index); the
    # other two passes are dropped (결정 #1 용량 절제).
    results = [
        _result("req-0:0", "pass", mcap="p0.mcap", mp4="p0.mp4", metrics={"time_to_goal_s": 10.0}),
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
    with Store(tmp_path / "cv.sqlite3") as store:
        report = _report(inputs, store)
    selected = report["matrix"][0]["artifacts"]["selected"]
    assert [(e["repeat_index"], e["role"]) for e in selected] == [
        (0, "representative-pass"),
        (1, "failure"),
        (3, "failure"),
    ]
    # representative-pass is deterministic: index 0 (lowest), never 2 or 4.
    rep = next(e for e in selected if e["role"] == "representative-pass")
    assert rep["rosbag_mcap"] == "p0.mcap" and rep["recording_mp4"] == "p0.mp4"


def test_metrics_come_from_representative_result(tmp_path):
    # fail-request -> metrics from the first non-pass result (the diagnostic one).
    inputs = [
        RequestReportInput(
            request=_request(),
            rollup=_rollup("req-0", Verdict.FAIL, [Verdict.PASS, Verdict.FAIL]),
            results=[
                _result("req-0:0", "pass", metrics={"time_to_goal_s": 10.0}),
                _result("req-0:1", "fail", metrics={"time_to_goal_s": 99.0, "collision_count": 3}),
            ],
        )
    ]
    with Store(tmp_path / "cv.sqlite3") as store:
        report = _report(inputs, store)
    assert report["matrix"][0]["metrics"]["collision_count"] == 3  # the failing run's metrics


# --------------------------------------------------------------------------- #
# (6) size-cap policy (결정 #2) — exclude + warn, no truncation
# --------------------------------------------------------------------------- #


def test_mcap_over_cap_is_excluded_with_warning(tmp_path):
    inputs = [
        RequestReportInput(
            request=_request(),
            rollup=_rollup("req-0", Verdict.FAIL, [Verdict.FAIL]),
            results=[
                _result("req-0:0", "fail", mcap="big.mcap", extra={"mcap_bytes": 500}),
            ],
        )
    ]
    with Store(tmp_path / "cv.sqlite3") as store:
        # cap is a PARAMETER (production value TBD 실측 후) — no hardcoded number.
        report = _report(inputs, store, max_mcap_bytes=100)
    entry = report["matrix"][0]["artifacts"]["selected"][0]
    assert entry["rosbag_mcap"] is None  # excluded, NOT truncated
    assert entry["excluded"] == ["rosbag_mcap"]
    assert entry["warnings"] and "상한" in entry["warnings"][0]


def test_no_cap_keeps_mcap(tmp_path):
    # G-35 pairing: the exclusion machinery must be OFF when no cap is set, so the
    # positive (exclusion above) proves the cap actually drives it.
    inputs = [
        RequestReportInput(
            request=_request(),
            rollup=_rollup("req-0", Verdict.FAIL, [Verdict.FAIL]),
            results=[_result("req-0:0", "fail", mcap="big.mcap", extra={"mcap_bytes": 500})],
        )
    ]
    with Store(tmp_path / "cv.sqlite3") as store:
        report = _report(inputs, store, max_mcap_bytes=None)
    entry = report["matrix"][0]["artifacts"]["selected"][0]
    assert entry["rosbag_mcap"] == "big.mcap" and entry["excluded"] == []
