"""``cv-infra report <envelope-id>`` — the informational review surface (M8,
DoD-P5-10 / M8 §3.2 D-O + §3.7). CPU-only; no GitHub, no sockets.

The report JSON served to the CLI is built by the REAL M4 ``aggregate.build_report``
from the SAME request/result/rollup builders as the canonical fixture
``tests/test_report_verification_report.py`` (G-28 anchor: the shape under test is
the producer's real output, never a hand-authored dict). The orchestrator report
endpoint ``GET /envelopes/{id}/report`` is faked with an ``httpx.MockTransport``
injected at the ``batch._make_client`` seam (the same seam the batch tests use);
the endpoint contract is the T3-shared pin (200 = VerificationReport verbatim,
404 = unknown id, 409 = not-terminal / supervision-error).

Contract under test (D-O informational):
* a fetched report ALWAYS exits 0 — even a *fail* report (a query never gates CI);
* only an infra/lookup problem (unreachable / unknown id / not-terminal / crash)
  exits 3; exit 1 and 2 are structurally unreachable on the fetch path.
"""

from __future__ import annotations

import json

import httpx
import pytest

from cv_infra.cli import batch
from cv_infra.cli.main import EXIT_CONTRACT, EXIT_FAIL, EXIT_INFRA, EXIT_PASS, main
from cv_infra.contract.schema import Result, VerificationRequest
from cv_infra.orchestrator.models import RequestRollup, Verdict
from cv_infra.orchestrator.store import Store
from cv_infra.report.aggregate import RequestReportInput, build_report
from cv_infra.report.baseline import update_baseline
from cv_infra.report.regression import identity_key

# --- canonical report builders (verbatim-anchored to test_report_verification_report) --

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


def _result(job_id: str, verdict: str, *, mcap=None, mp4=None) -> dict:
    return Result.model_validate(
        {
            "job_id": job_id,
            "verdict": verdict,
            "metrics": {},
            "artifacts": {"mcap": mcap, "mp4": mp4},
        }
    ).model_dump(mode="json")


def _rollup(request_id, verdict, verdicts) -> RequestRollup:
    return RequestRollup(request_id=request_id, verdicts=verdicts, flakiness=0.0, verdict=verdict)


def _make_report(tmp_path, inputs, *, seed_baseline=None) -> dict:
    """Assemble a real VerificationReport JSON (the shape the CLI consumes)."""
    with Store(tmp_path / "cv.sqlite3") as store:
        if seed_baseline is not None:
            update_baseline(store, **seed_baseline)
        return build_report(
            inputs, store, envelope_id="env-1", trigger_source="ci-cd", generated_at=_AT
        )


def _pass_report(tmp_path) -> dict:
    inputs = [
        RequestReportInput(
            request=_request(),
            rollup=_rollup("req-0", Verdict.PASS, [Verdict.PASS, Verdict.PASS]),
            results=[_result("req-0:0", "pass", mcap="p0.mcap"), _result("req-0:1", "pass")],
        )
    ]
    return _make_report(tmp_path, inputs)


def _fail_report(tmp_path) -> dict:
    inputs = [
        RequestReportInput(
            request=_request(),
            rollup=_rollup("req-0", Verdict.FAIL, [Verdict.FAIL]),
            results=[_result("req-0:0", "fail", mcap="f0.mcap")],
        )
    ]
    return _make_report(tmp_path, inputs)


def _errored_report(tmp_path) -> dict:
    inputs = [
        RequestReportInput(
            request=_request(),
            rollup=_rollup("req-0", Verdict.PASS, [Verdict.PASS]),
            results=[_result("req-0:0", "pass")],
        ),
        RequestReportInput(
            request=_request(sut={"image_ref": "carter-sut:x"}),
            rollup=_rollup("req-1", None, []),  # no domain verdict -> errored
            results=[_result("req-1:0", "error")],
        ),
    ]
    return _make_report(tmp_path, inputs)


def _regressed_report(tmp_path) -> dict:
    request = _request(sut={"image_ref": "carter-sut:new"})
    inputs = [
        RequestReportInput(
            request=request,
            rollup=_rollup("req-0", Verdict.FAIL, [Verdict.FAIL]),
            results=[_result("req-0:0", "fail")],
        )
    ]
    return _make_report(
        tmp_path,
        inputs,
        seed_baseline={
            "request_identity_key": identity_key(request),
            "sut_ref": "carter-sut:old",
            "verdict": "pass",
            "established_at": "2026-07-10T00:00:00+00:00",
        },
    )


# --- HTTP seam wiring (MockTransport over the batch._make_client seam) --------


def _wire(monkeypatch, handler) -> None:
    monkeypatch.setattr(
        batch,
        "_make_client",
        lambda api_base: httpx.AsyncClient(
            transport=httpx.MockTransport(handler), base_url="http://cv-infra.test"
        ),
    )


def _serve(report: dict, status_code: int = 200):
    def handler(request: httpx.Request) -> httpx.Response:
        # Endpoint contract pin (T3-shared): report lives at /envelopes/{id}/report.
        assert request.url.path == "/envelopes/env-1/report"
        return httpx.Response(status_code, json=report)

    return handler


# --------------------------------------------------------------------------- #
# (1) 200 human render: matrix + outcome + baseline messaging, exit 0
# --------------------------------------------------------------------------- #


def test_pass_report_renders_and_exits_0(monkeypatch, tmp_path, capsys):
    _wire(monkeypatch, _serve(_pass_report(tmp_path)))
    rc = main(["report", "env-1"])
    out = capsys.readouterr().out
    assert rc == EXIT_PASS
    assert "Verification report — envelope env-1" in out
    assert (
        "Report matrix (1 requests: 1 passed, 0 failed, 0 errored)" in out
    )  # M4 render_text reuse
    assert "req-0" in out
    assert "outcome: verdict=pass report_outcome=pass" in out
    # baseline absent = skip(정상), never a failure (§3.7).
    assert "regression check skipped" in out


def test_fail_report_still_exits_0_but_shows_fail(monkeypatch, tmp_path, capsys):
    """D-O: a *failing* report is informational — the query exits 0, never 1
    (a report read must not turn CI red by itself)."""
    _wire(monkeypatch, _serve(_fail_report(tmp_path)))
    rc = main(["report", "env-1"])
    out = capsys.readouterr().out
    assert rc == EXIT_PASS  # NOT EXIT_FAIL — the whole point of D-O
    assert "outcome: verdict=fail report_outcome=fail" in out


def test_errored_report_exits_0_and_surfaces_errored(monkeypatch, tmp_path, capsys):
    _wire(monkeypatch, _serve(_errored_report(tmp_path)))
    rc = main(["report", "env-1"])
    out = capsys.readouterr().out
    assert rc == EXIT_PASS  # informational read never gates, even on infra outcome
    assert "report_outcome=errored" in out


def test_regressed_report_identifies_request_sut_and_date(monkeypatch, tmp_path, capsys):
    """§3.7: baseline exists -> which request got worse against which SUT/date.
    The baseline lives ONLY in cv-infra's internal store (seeded here) — consumer
    git/CI history is never consulted (REQ-REPORT-006)."""
    _wire(monkeypatch, _serve(_regressed_report(tmp_path)))
    rc = main(["report", "env-1"])
    out = capsys.readouterr().out
    assert rc == EXIT_PASS
    assert "regressed vs baseline" in out
    assert "carter-sut:old" in out  # baseline SUT identified
    assert "2026-07-10" in out  # baseline established date
    assert "pass→fail" in out  # the domain transition (from the report detail)


# --------------------------------------------------------------------------- #
# (2) --json: raw report JSON verbatim on stdout, exit 0
# --------------------------------------------------------------------------- #


def test_json_flag_prints_raw_report_verbatim(monkeypatch, tmp_path, capsys):
    report = _pass_report(tmp_path)
    _wire(monkeypatch, _serve(report))
    rc = main(["report", "env-1", "--json"])
    captured = capsys.readouterr()
    assert rc == EXIT_PASS
    # stdout is the report JSON verbatim (round-trips) — not the human render.
    assert json.loads(captured.out) == report
    assert "Verification report —" not in captured.out


# --------------------------------------------------------------------------- #
# (3) error paths: 404 / 409 / unreachable all -> exit 3, friendly, traceback 0
# --------------------------------------------------------------------------- #


def test_unknown_envelope_404_exits_3_not_2(monkeypatch, capsys):
    """A missing envelope on an informational read is infra/lookup (3), NOT a
    contract error (2) — report never returns 2 (unlike status/wait's 404->2)."""
    _wire(monkeypatch, _serve({"detail": "no such envelope"}, status_code=404))
    rc = main(["report", "env-1"])
    err = capsys.readouterr().err
    assert rc == EXIT_INFRA
    assert "unknown envelope id 'env-1'" in err
    assert "not a SUT verdict" in err
    assert "Traceback" not in err


def test_not_terminal_409_points_at_wait_and_exits_3(monkeypatch, capsys):
    _wire(
        monkeypatch,
        _serve({"detail": {"reason": "not-terminal", "status": "running"}}, status_code=409),
    )
    rc = main(["report", "env-1"])
    err = capsys.readouterr().err
    assert rc == EXIT_INFRA
    assert "not terminal" in err
    assert "cv-infra wait env-1" in err  # points the user at the blocking command
    assert "status=running" in err


def test_supervision_error_409_reports_crash_and_exits_3(monkeypatch, capsys):
    _wire(
        monkeypatch,
        _serve(
            {"detail": {"reason": "supervision-error", "error": "boom-marker-42"}}, status_code=409
        ),
    )
    rc = main(["report", "env-1"])
    err = capsys.readouterr().err
    assert rc == EXIT_INFRA
    assert "supervision crashed" in err
    assert "boom-marker-42" in err


def test_orchestrator_unreachable_exits_3(monkeypatch, capsys):
    def refuse(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("connection refused")

    _wire(monkeypatch, refuse)
    rc = main(["report", "env-1"])
    err = capsys.readouterr().err
    assert rc == EXIT_INFRA
    assert "orchestrator unreachable" in err
    assert "not a SUT verdict" in err


# --------------------------------------------------------------------------- #
# (4) invariant: the report fetch path NEVER returns exit 1 or 2 (D-O)
# --------------------------------------------------------------------------- #


def test_report_fetch_never_returns_1_or_2(monkeypatch, tmp_path):
    """Every served report -> 0; every infra/lookup error -> 3. exit 1 and 2 are
    structurally absent from the fetch path (a query is neither a SUT verdict nor
    a contract error)."""
    served = [
        (_serve(_pass_report(tmp_path)), EXIT_PASS),
        (_serve(_fail_report(tmp_path)), EXIT_PASS),
        (_serve(_errored_report(tmp_path)), EXIT_PASS),
        (_serve({"detail": "nope"}, status_code=404), EXIT_INFRA),
        (_serve({"detail": {"reason": "not-terminal", "status": "queued"}}, 409), EXIT_INFRA),
        (_serve({"detail": {"reason": "supervision-error", "error": "x"}}, 409), EXIT_INFRA),
    ]
    for handler, expected in served:
        _wire(monkeypatch, handler)
        rc = main(["report", "env-1"])
        assert rc == expected
        assert rc not in (EXIT_FAIL, EXIT_CONTRACT)


# --------------------------------------------------------------------------- #
# (5) usage: extra positional tokens are a usage error (argparse layer, exit 2)
# --------------------------------------------------------------------------- #


def test_extra_tokens_are_a_usage_error(monkeypatch, tmp_path, capsys):
    """The exit-1/2-free guarantee covers the FETCH path; a malformed invocation
    (stray tokens) is still a usage error (2) at the argparse/main layer, uniform
    with the other commands — cmd_report itself is never reached."""
    _wire(monkeypatch, _serve(_pass_report(tmp_path)))
    assert main(["report", "env-1", "stray-token"]) == EXIT_CONTRACT
    assert "unrecognized argument(s): stray-token" in capsys.readouterr().err


@pytest.mark.parametrize("argv", [["report"], ["report", "--json"]])
def test_missing_envelope_id_is_argparse_usage_error(argv):
    """Missing required ``envelope_id`` exits 2 via argparse (SystemExit) — the
    universal usage-error convention, not a report-semantics path."""
    with pytest.raises(SystemExit) as excinfo:
        main(argv)
    assert excinfo.value.code == 2
