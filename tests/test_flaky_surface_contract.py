"""Flaky 사용자 표면 계약 — ``repeats``/``pass``/``fail``/``flaky`` 셀 내용 고정.

CEO 결정 **E-1/E-2**(2026-08-10 determinism): 비결정적 SUT가 비결정적으로 나오는 것이
올바른 출력이고, flakiness를 다루는 제품 수단은 ``repeats``(반복 검증)다. **M-2**(2026-08-10
mvp-scope): *flaky 여부 + pass/fail 카운트까지가 MVP 표면*이며 그 이상(분포 인지형 회귀
판정)은 post-MVP. ⇒ **이 표면 자체가 제품 가치**인데 지금까지 그것을 지키는 테스트가 0개라
컬럼이 사라져도, 분포가 any-fail 하나로 뭉개져도 CI는 green이었다. 이 파일이 그 구멍이다.

무엇을 지키나 (모두 **셀 내용** 단언 — 헤더 존재만으로는 뭉개짐을 못 잡는다):

* publish 3면(sticky PR comment · Check Run 요약 · step summary)의 matrix 행이 한 요청의
  **반복 분포를 보존**한다 — ``repeats=3 · pass=2 · fail=1 · flaky=yes`` 가 any-fail 판정
  (``verdict=fail``)과 **함께** 보인다. 판정이 분포를 대체하면 red.
* flaky 행과 non-flaky 행이 **구분**된다(둘 다 렌더해 상수 "yes"/"no" 변이를 잡는다).
* CLI ``cv-infra report`` 텍스트 뷰의 ``flakiness``/``jobs``/``pass``/``fail`` 열이 리포트
  행의 값을 읽는다 — 값이 있으면 값, 없으면 ``-``(M4 ``render_text``의 기존 null 관용구).

무장 조건(G-59): 픽스처가 **실제로 flaky 분기를 밟는다**. ``flakiness`` 는 손으로 타이핑한
상수가 아니라 **생산자**(``orchestrator.rollup.roll_up``)가 계산한 값이고, 리포트는 실제
``report.aggregate.build_report`` 출력이다(G-28 앵커: 손으로 만든 dict 금지). CLI HTTP 이음매
(``batch._make_client`` + ``MockTransport``)와 요청/결과 빌더는 ``tests/test_cli_report.py``
/ ``tests/test_report_github_renderer.py`` 의 관용구를 그대로 따른다.

CPU only · 네트워크 0 · GitHub 0.
"""

from __future__ import annotations

from typing import Any

import httpx
import pytest

from cv_infra.cli import batch
from cv_infra.cli.main import EXIT_PASS, main
from cv_infra.contract.schema import Result, VerificationRequest
from cv_infra.orchestrator.models import Job, JobResult, JobState, Verdict
from cv_infra.orchestrator.rollup import roll_up
from cv_infra.orchestrator.store import Store
from cv_infra.report import github
from cv_infra.report.aggregate import RequestReportInput, build_report

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

#: The flaky fixture's repeat verdicts: 3 repeats, 1 disagreeing -> any-fail verdict
#: ``fail`` WITH ``flakiness = 1/3`` (rollup._flakiness), the exact shape M-2 sells.
_FLAKY_VERDICTS = [Verdict.PASS, Verdict.FAIL, Verdict.PASS]
_STABLE_VERDICTS = [Verdict.PASS, Verdict.PASS]


# --------------------------------------------------------------------------- #
# Builders — the rollup comes from the REAL producer (flakiness never hand-typed)
# --------------------------------------------------------------------------- #
def _request(**overrides) -> dict:
    return VerificationRequest.model_validate({**_BASE_REQUEST, **overrides}).model_dump(
        mode="json", by_alias=True
    )


def _result(job_id: str, verdict: str) -> dict:
    return Result.model_validate(
        {"job_id": job_id, "verdict": verdict, "metrics": {}, "artifacts": {}}
    ).model_dump(mode="json")


def _producer_rollup(request_id: str, verdicts: list[Verdict]):
    """``RequestRollup`` built by M3's OWN ``roll_up`` from per-repeat JobResults.

    The point: ``flakiness`` (and therefore the ``flaky`` cell) is DERIVED by the
    producer, so this fixture cannot pass by carrying a hand-set 0.0 while the
    verdicts disagree (the arming condition, G-59)."""
    results = [
        JobResult(job=Job(request_id, index), state=JobState.COMPLETED, verdict=verdict)
        for index, verdict in enumerate(verdicts)
    ]
    return roll_up(request_id, results)


def _mixed_report(tmp_path, *, extra: list[RequestReportInput] | None = None) -> dict:
    """A report with BOTH a flaky request (1-of-3 fails) and a stable one."""
    inputs = [
        RequestReportInput(
            request=_request(sut={"image_ref": "carter-sut:flaky"}),
            rollup=_producer_rollup("req-flaky", _FLAKY_VERDICTS),
            results=[_result(f"req-flaky:{i}", v.value) for i, v in enumerate(_FLAKY_VERDICTS)],
        ),
        RequestReportInput(
            request=_request(sut={"image_ref": "carter-sut:stable"}),
            rollup=_producer_rollup("req-stable", _STABLE_VERDICTS),
            results=[_result(f"req-stable:{i}", v.value) for i, v in enumerate(_STABLE_VERDICTS)],
        ),
        *(extra or []),
    ]
    with Store(tmp_path / "cv.sqlite3") as store:
        return build_report(
            inputs, store, envelope_id="env-1", trigger_source="ci-cd", generated_at=_AT
        )


def _errored_input() -> RequestReportInput:
    """A request whose repeats produced NO verdict -> ``flakiness is None`` (the
    honest-absence case the CLI must render as ``-``)."""
    return RequestReportInput(
        request=_request(sut={"image_ref": "carter-sut:err"}),
        rollup=_producer_rollup("req-zerr", []),
        results=[_result("req-zerr:0", "fail")],
    )


# --------------------------------------------------------------------------- #
# Cell readers (markdown table / fixed-width text table)
# --------------------------------------------------------------------------- #
def _md_cells(body: str, lead: str) -> list[str]:
    """Cells of the markdown matrix row whose first column is ``lead``."""
    line = next(
        (ln for ln in body.splitlines() if ln.startswith(f"| {lead} |")),
        None,
    )
    assert line is not None, f"no matrix row for {lead!r} in:\n{body}"
    return [cell.strip() for cell in line.strip().strip("|").split("|")]


def _text_cells(out: str, lead: str) -> list[str]:
    """Cells of the CLI fixed-width matrix row whose first column is ``lead``."""
    line = next((ln for ln in out.splitlines() if ln.startswith(lead)), None)
    assert line is not None, f"no CLI matrix row for {lead!r} in:\n{out}"
    return line.split()


# --------------------------------------------------------------------------- #
# (1) publish surfaces — the repeat distribution survives into the CELLS
# --------------------------------------------------------------------------- #
def test_fixture_actually_walks_the_flaky_branch(tmp_path):
    """Arming check (G-59): the flaky row's producer-derived flakiness is > 0 and
    the report marks it flaky — otherwise every assertion below would be vacuous."""
    report = _mixed_report(tmp_path)
    flaky_row, stable_row = report["matrix"]
    assert flaky_row["request_id"] == "req-flaky"
    assert flaky_row["flakiness"] == 1 / 3  # rollup._flakiness((3-2)/3), not hand-set
    assert flaky_row["rollup"]["flaky"] is True
    assert stable_row["flakiness"] == 0.0 and stable_row["rollup"]["flaky"] is False


def test_matrix_columns_and_flaky_row_cells(tmp_path):
    """The distribution columns exist AND carry the right values (M-2 표면)."""
    body = github.render_step_summary(_mixed_report(tmp_path))
    assert _md_cells(body, "request") == [
        "request",
        "sut",
        "verdict",
        "repeats",
        "pass",
        "fail",
        "flaky",
        "identity",  # p5c20 ⑦ 후속 — ADDED after flaky, ahead of the free-text cell
        "metrics",
    ]
    # cells[:7] = the M-2 surface (the trailing identity/metrics cells are M4's own
    # contract, covered by tests/test_report_github_renderer.py — not re-pinned here).
    assert _md_cells(body, "req-flaky")[:7] == [
        "req-flaky",
        "carter-sut:flaky",
        "fail",  # any-fail 판정
        "3",  # ...alongside the FULL distribution
        "2",
        "1",
        "yes",
    ]


def test_stable_row_is_not_reported_flaky(tmp_path):
    """A uniform-verdict request must read ``flaky=no`` with its own counts — so a
    constant ``yes``/``no`` cell (or a dropped flaky column) cannot pass."""
    body = github.render_step_summary(_mixed_report(tmp_path))
    assert _md_cells(body, "req-stable")[:7] == [
        "req-stable",
        "carter-sut:stable",
        "pass",
        "2",
        "2",
        "0",
        "no",
    ]


def test_any_fail_verdict_never_collapses_the_distribution(tmp_path):
    """E-1/E-2: the gate decision (any-fail) and the observed distribution are
    SEPARATE. A ``fail`` request that passed 2 of 3 repeats must still show those
    2 passes — folding pass/fail into the verdict (e.g. 0/3 or 3/0) is red."""
    cells = _md_cells(github.render_step_summary(_mixed_report(tmp_path)), "req-flaky")
    verdict, repeats, passed, failed, flaky = cells[2:7]
    assert verdict == "fail"
    assert int(passed) == 2 and int(failed) == 1  # the disagreement is visible
    assert int(repeats) == int(passed) + int(failed)
    assert flaky == "yes"


@pytest.mark.parametrize(
    "render",
    [
        github.render_sticky_comment,
        github.render_step_summary,
        lambda report: github.render_check_run(report)["output"]["summary"],
    ],
    ids=["sticky-comment", "step-summary", "check-run"],
)
def test_every_publish_surface_carries_the_same_flaky_row(tmp_path, render):
    """All three human surfaces share the body — assert the flaky cells on EACH so
    a surface-specific regression cannot hide behind the other two."""
    cells = _md_cells(render(_mixed_report(tmp_path)), "req-flaky")
    assert cells[3:7] == ["3", "2", "1", "yes"]


# --------------------------------------------------------------------------- #
# (2) CLI ``cv-infra report`` — the same distribution on the text surface
# --------------------------------------------------------------------------- #
def _wire(monkeypatch, report: dict[str, Any]) -> None:
    """Serve ``report`` at the report endpoint over the ``batch._make_client`` seam
    (idiom copied from ``tests/test_cli_report.py``)."""

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/envelopes/env-1/report"
        return httpx.Response(200, json=report)

    monkeypatch.setattr(
        batch,
        "_make_client",
        lambda api_base: httpx.AsyncClient(
            transport=httpx.MockTransport(handler), base_url="http://cv-infra.test"
        ),
    )


def test_cli_report_renders_the_flakiness_value_and_counts(monkeypatch, tmp_path, capsys):
    """``flakiness`` is a real number on the row — the CLI column must show it (it
    used to be hardcoded ``None`` -> ``-`` while the value existed), next to the
    per-repeat pass/fail counts."""
    _wire(monkeypatch, _mixed_report(tmp_path))
    assert main(["report", "env-1"]) == EXIT_PASS
    out = capsys.readouterr().out
    # request_id  verdict  flakiness  jobs  pass  fail  [identity_key]
    # (M4 render_text columns; the 7th ``identity_key`` column was appended at
    # p5c20 ⑦ and is pinned in tests/test_cli_report.py — sliced off here so this
    # file keeps asserting exactly the M-2 flaky surface it owns.)
    assert _text_cells(out, "req-flaky")[:6] == ["req-flaky", "fail", "0.333", "3", "2", "1"]
    assert _text_cells(out, "req-stable")[:6] == ["req-stable", "pass", "0.000", "2", "2", "0"]


def test_cli_report_renders_dash_when_there_is_no_flakiness(monkeypatch, tmp_path, capsys):
    """Honest absence: a request with no verdict-bearing repeat has ``flakiness
    is None`` — the column renders ``-``, never a fabricated 0.000."""
    _wire(monkeypatch, _mixed_report(tmp_path, extra=[_errored_input()]))
    assert main(["report", "env-1"]) == EXIT_PASS
    out = capsys.readouterr().out
    assert _text_cells(out, "req-zerr")[:6] == ["req-zerr", "errored", "-", "0", "0", "0"]
    assert _text_cells(out, "req-flaky")[2] == "0.333"  # the value case still renders
