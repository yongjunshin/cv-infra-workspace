"""분포 요청의 사람 표면 계약 (p6c5 T1, M4) — REQ-REPORT-001/007, SR-19/SR-22.

p6 는 요청의 입자를 "1잡 = 1결과"에서 **n표본 분포**로 키웠다(§0-4/§0-14). matrix 행 구조는
이미 분포형(``repeats``/``pass``/``fail``/``flaky`` — M-2 표면, ``test_flaky_surface_contract.py``)
이라 이번에 늘어난 것은 **입력의 크기와 판정 정책** 두 가지고, 이 파일은 그 둘을 못박는다:

* **``min_pass_ratio`` 표면 문구**(§0-14) — 요청이 임계를 선언하면 그 사실이 사람 표면에
  **1곳** 드러난다. 이게 없으면 ``verdict=pass`` 옆에 ``fail 19`` 가 앉아 있는 행(=W3 실물)이
  버그처럼 읽힌다. 선언하지 않은 요청의 렌더는 **바이트 동일**(새 문구가 조용히 끼어들지 않음).
* **W3 실물 형태**(repeats 60 · pass 41 · fail 19 · flaky yes · ratio 0.5 충족 → verdict pass,
  `reports/runner-2026-08-27-p6c4-t2-gpu-w3.md` §3) 가 Check/스티키/step summary 3표면에
  기존 계약대로 나온다 — **가드 입력의 성장**(G-59: 계약이 자라면 가드 입력도 자라야 한다).
* **9컬럼 폭·정렬** — repeats 5 와 60 양쪽에서 markdown 9열이 열 수/의미를 유지하고, CLI
  고정폭 표의 열이 어긋나지 않는다(폭은 상수가 아니라 셀에서 유도되므로 자릿수 증가에 견딘다).

무장 조건(G-59): 픽스처가 실제로 ratio 분기를 밟는다 — 같은 60개 verdict 가 ratio 선언 시
``pass``, 미선언 시 ``fail`` 로 갈린다(테스트 (1)). rollup 은 손으로 만든 상수가 아니라 생산자
(``orchestrator.rollup.roll_up``)가, 리포트는 ``report.aggregate.build_report`` 가 만든다(G-28).

CPU only · 네트워크 0 · GitHub 0.
"""

from __future__ import annotations

import copy
from typing import Any

import httpx
import pytest

from cv_infra.cli import batch
from cv_infra.cli.exit_codes import (
    CHECK_CONCLUSION_BY_EXIT,
    EXIT_PASS,
    exit_code_for_report_outcome,
)
from cv_infra.cli.main import main
from cv_infra.contract.schema import Result, VerificationRequest
from cv_infra.orchestrator.api import _min_pass_ratio as _caller_ratio
from cv_infra.orchestrator.models import Job, JobResult, JobState, Verdict
from cv_infra.orchestrator.rollup import roll_up
from cv_infra.orchestrator.store import Store
from cv_infra.report import github
from cv_infra.report.aggregate import (
    RequestReportInput,
    _declared_min_pass_ratio,
    build_report,
)
from cv_infra.report.identity_display import CELL_CHARS

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

#: W3 실물 분포(2026-08-27 GPU 실측): 60표본 · pass 41 · fail 19 · min_pass_ratio 0.5.
#: 순서가 아니라 **크기**만 실물을 따른다(표면 계약은 순서에 무관 — flakiness 는 다수결 비율).
_W3_VERDICTS = [Verdict.PASS] * 41 + [Verdict.FAIL] * 19
_W3_RATIO = 0.5

#: 소비자 E2E 시나리오 크기(정본 §1 p6c5 T3): 5표본 · 1개 실패 · 임계 0.8 = 경계에서 pass.
_SMALL_VERDICTS = [Verdict.PASS, Verdict.PASS, Verdict.FAIL, Verdict.PASS, Verdict.PASS]
_SMALL_RATIO = 0.8

_SURFACES = [
    (github.render_sticky_comment, "sticky-comment"),
    (github.render_step_summary, "step-summary"),
    (lambda report: github.render_check_run(report)["output"]["summary"], "check-run"),
]


# --------------------------------------------------------------------------- #
# Builders — real producers only (rollup by roll_up, report by build_report)
# --------------------------------------------------------------------------- #
def _request(ratio: float | None, repeats: int) -> dict:
    settings: dict[str, Any] = {"repeats": repeats}
    if ratio is not None:
        settings["min_pass_ratio"] = ratio
    return VerificationRequest.model_validate(
        {**_BASE_REQUEST, "execution_settings": settings}
    ).model_dump(mode="json", by_alias=True)


def _result(job_id: str, verdict: str) -> dict:
    return Result.model_validate(
        {"job_id": job_id, "verdict": verdict, "metrics": {}, "artifacts": {}}
    ).model_dump(mode="json")


def _input(request_id: str, verdicts: list[Verdict], ratio: float | None) -> RequestReportInput:
    """One request's report input — the rollup comes from M3's OWN ``roll_up`` with the
    SAME ratio the request declares (``api._min_pass_ratio`` -> ``roll_up``), so the row
    can only display a verdict the declared policy actually produced."""
    results = [
        JobResult(job=Job(request_id, index), state=JobState.COMPLETED, verdict=verdict)
        for index, verdict in enumerate(verdicts)
    ]
    return RequestReportInput(
        request=_request(ratio, len(verdicts)),
        rollup=roll_up(request_id, results, min_pass_ratio=ratio),
        results=[_result(f"{request_id}:{i}", v.value) for i, v in enumerate(verdicts)],
    )


def _report(tmp_path, inputs: list[RequestReportInput]) -> dict:
    with Store(tmp_path / "cv.sqlite3") as store:
        return build_report(
            inputs, store, envelope_id="env-1", trigger_source="ci-cd", generated_at=_AT
        )


def _w3_report(tmp_path, *, ratio: float | None = _W3_RATIO) -> dict:
    return _report(tmp_path, [_input("req-w3", _W3_VERDICTS, ratio)])


def _md_cells(body: str, lead: str) -> list[str]:
    line = next((ln for ln in body.splitlines() if ln.startswith(f"| {lead} |")), None)
    assert line is not None, f"no matrix row for {lead!r} in:\n{body}"
    return [cell.strip() for cell in line.strip().strip("|").split("|")]


# --------------------------------------------------------------------------- #
# (1) arming (G-59) — the declared-ratio branch is REALLY walked
# --------------------------------------------------------------------------- #
def test_the_ratio_fixture_actually_walks_the_declared_branch(tmp_path):
    """Same 60 verdicts, two policies, two verdicts. If this ever stops splitting,
    every ratio assertion below is vacuous (the note would ride a row that the
    frozen any-fail rule judged anyway)."""
    declared = _w3_report(tmp_path)["matrix"][0]
    frozen = _w3_report(tmp_path, ratio=None)["matrix"][0]
    assert declared["rollup"]["verdict"] == "pass"  # 41/60 = 0.6833 >= 0.5
    assert frozen["rollup"]["verdict"] == "fail"  # any-fail
    assert declared["min_pass_ratio"] == _W3_RATIO and frozen["min_pass_ratio"] is None
    # ...and the distribution/flakiness are producer-derived, not hand-typed.
    assert declared["rollup"]["verdicts"].count("fail") == 19
    assert declared["flakiness"] == pytest.approx(19 / 60)


# --------------------------------------------------------------------------- #
# (2) the row's ratio == the ratio the ROLLUP CALLER applied (G-25 복제본 가드)
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize(
    ("case", "dump"),
    [
        ("declared", {"execution_settings": {"min_pass_ratio": 0.8}}),
        ("null", {"execution_settings": {"min_pass_ratio": None}}),
        ("absent-key", {"execution_settings": {"repeats": 3}}),
        ("no-settings", {"scenario": {}}),
        ("not-a-mapping", {"execution_settings": 0.8}),
        ("int-is-a-ratio", {"execution_settings": {"min_pass_ratio": 1}}),
        ("bool-is-not-a-ratio", {"execution_settings": {"min_pass_ratio": True}}),
    ],
)
def test_row_ratio_agrees_with_the_rollup_caller(case, dump):
    """``aggregate._declared_min_pass_ratio`` duplicates ``api._min_pass_ratio``'s
    normalization (it cannot import it — circular + fastapi in the renderer graph),
    so this holds the copy to its source: if they ever disagree, the surface would
    announce a threshold that was never applied to the verdict shown next to it."""
    assert _declared_min_pass_ratio(dump) == _caller_ratio(dump), case


# --------------------------------------------------------------------------- #
# (3) the declared ratio surfaces ONCE, on every human surface
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize(("render", "surface"), _SURFACES, ids=[s for _, s in _SURFACES])
def test_declared_ratio_shows_up_once_on_each_human_surface(tmp_path, render, surface):
    body = render(_w3_report(tmp_path))
    assert body.count("min_pass_ratio") == 1, surface  # 한 곳 — 도배 아님
    line = next(ln for ln in body.splitlines() if "min_pass_ratio" in ln)
    assert "req-w3" in line and "0.5" in line  # 어느 요청이 어떤 임계인지
    # 표면 문구는 행을 대체하지 않는다 — 분포 셀은 그대로 옆에 있다.
    assert _md_cells(body, "req-w3")[2:7] == ["pass", "60", "41", "19", "yes"]


def test_two_requests_each_get_their_own_threshold_named(tmp_path):
    """A constant 문구(하드코딩 0.8 등)로는 통과할 수 없다: 임계가 다른 두 요청이 각자의
    request_id 와 값으로 나온다. ratio 미선언 요청은 그 줄에 등장하지 않는다."""
    report = _report(
        tmp_path,
        [
            _input("req-a", _SMALL_VERDICTS, _SMALL_RATIO),
            _input("req-b", _W3_VERDICTS, _W3_RATIO),
            _input("req-c", _SMALL_VERDICTS, None),
        ],
    )
    line = next(
        ln for ln in github.render_step_summary(report).splitlines() if "min_pass_ratio" in ln
    )
    assert "req-a: pass ratio ≥ 0.8 declared" in line
    assert "req-b: pass ratio ≥ 0.5 declared" in line
    assert "req-c" not in line


# --------------------------------------------------------------------------- #
# (4) 미선언 = 바이트 동일 (새 필드/문구가 조용히 끼어들지 않는다)
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize(("render", "surface"), _SURFACES, ids=[s for _, s in _SURFACES])
def test_absent_ratio_renders_byte_identically_to_a_pre_field_report(tmp_path, render, surface):
    """A report whose rows carry ``min_pass_ratio: None`` renders byte-for-byte like
    one whose rows have no such key at all (= every report produced before p6c5 T1).
    Both must be identical to the frozen surface — no note, no vocabulary, nothing."""
    report = _w3_report(tmp_path, ratio=None)
    pre_field = copy.deepcopy(report)
    for row in pre_field["matrix"]:
        row.pop("min_pass_ratio")
    assert render(report) == render(pre_field), surface
    body = render(report)
    assert "min_pass_ratio" not in body and "pass ratio" not in body
    assert _md_cells(body, "req-w3")[2:7] == ["fail", "60", "41", "19", "yes"]  # any-fail 그대로


# --------------------------------------------------------------------------- #
# (5) W3 실물 형태가 3표면에 기존 계약대로 (가드 입력의 성장 — G-59)
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize(("render", "surface"), _SURFACES, ids=[s for _, s in _SURFACES])
def test_w3_shape_distribution_survives_on_every_surface(tmp_path, render, surface):
    """60표본 · 41/19 · flaky yes 가 판정(pass)과 **함께** 보인다 — 판정이 분포를 뭉개면 red."""
    cells = _md_cells(render(_w3_report(tmp_path)), "req-w3")
    assert cells[:3] == ["req-w3", "carter-sut:b", "pass"], surface
    repeats, passed, failed, flaky = cells[3:7]
    assert (int(repeats), int(passed), int(failed), flaky) == (60, 41, 19, "yes"), surface
    assert int(repeats) == int(passed) + int(failed)


def test_w3_shape_folds_to_the_imported_conclusion_and_the_selected_artifacts(tmp_path):
    """The 60-sample row still drives the frozen downstream contracts: the Check
    conclusion comes from the IMPORTED exit table (no local mapping), and artifact
    selection stays 결정 #1 (all 19 failures + exactly one representative pass = 20,
    the count W3 measured) — a distribution 12배 큰 입력이 정책을 바꾸지 않는다."""
    report = _w3_report(tmp_path)
    check = github.render_check_run(report)
    assert check["conclusion"] == CHECK_CONCLUSION_BY_EXIT[EXIT_PASS]
    assert exit_code_for_report_outcome(report["summary"]["report_outcome"]) == EXIT_PASS
    selected = report["matrix"][0]["artifacts"]["selected"]
    assert len(selected) == 20
    assert [e["role"] for e in selected].count("representative-pass") == 1
    manifest = github.render_artifact_manifest(report)
    assert len({e["repeat_index"] for e in manifest["missing"]}) == 20  # 20 entries × 3 kinds
    assert manifest["uploads"] == [] and manifest["excluded"] == []


# --------------------------------------------------------------------------- #
# (6) 9컬럼 폭·정렬 — repeats 5 와 60 양쪽
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize(
    ("case", "verdicts", "ratio"),
    [("repeats-5", _SMALL_VERDICTS, _SMALL_RATIO), ("repeats-60", _W3_VERDICTS, _W3_RATIO)],
)
def test_nine_columns_keep_their_shape_at_both_repeat_scales(tmp_path, case, verdicts, ratio):
    """9열 표가 자릿수 증가(5 -> 60/41/19)에도 열 수·의미·폭 규칙을 유지한다: 모든 행이
    정확히 9셀, 빈 셀 0, identity 셀은 공유 축약 폭 그대로(자유길이 셀은 맨 오른쪽 1개뿐)."""
    body = github.render_step_summary(_report(tmp_path, [_input("req-0", verdicts, ratio)]))
    table = [ln for ln in body.splitlines() if ln.startswith("|")]
    header, delimiter, *rows = table
    assert [c.strip() for c in header.strip().strip("|").split("|")] == [
        "request",
        "sut",
        "verdict",
        "repeats",
        "pass",
        "fail",
        "flaky",
        "identity",
        "metrics",
    ], case
    assert delimiter.count("---") == 9, case
    for row in rows:
        cells = [c.strip() for c in row.strip().strip("|").split("|")]
        assert len(cells) == 9, f"{case}: {row}"
        assert all(cells), f"{case}: empty cell in {row}"
        assert len(cells[7]) == CELL_CHARS + 1, case  # sha256:12hex + '…' — 폭 불변


def _wire(monkeypatch, report: dict[str, Any]) -> None:
    """Serve ``report`` at the report endpoint (idiom: tests/test_cli_report.py)."""

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


def test_cli_text_table_stays_aligned_when_the_counts_grow(monkeypatch, tmp_path, capsys):
    """CLI 고정폭 표의 열 시작 위치가 헤더와 모든 행에서 일치한다 — 폭이 상수가 아니라 셀에서
    유도되므로 5(1자리)와 60/41/19(2자리)가 한 표에 섞여도 어긋나지 않는다(폭 상수 조정 불요)."""
    report = _report(
        tmp_path,
        [_input("req-0", _SMALL_VERDICTS, _SMALL_RATIO), _input("req-w3", _W3_VERDICTS, _W3_RATIO)],
    )
    _wire(monkeypatch, report)
    assert main(["report", "env-1"]) == EXIT_PASS
    lines = capsys.readouterr().out.splitlines()
    header = next(ln for ln in lines if ln.startswith("request_id"))
    offsets = [header.index(col) for col in header.split()]
    for lead in ("req-0", "req-w3"):
        row = next(ln for ln in lines if ln.startswith(lead))
        cells = row.split()
        assert len(cells) == len(offsets)
        assert [row.index(cell, offset) for cell, offset in zip(cells, offsets)] == offsets, row
    # 자릿수가 자란 열이 실제로 자란 값을 담고 있다(공허한 정렬 단언 방지).
    assert next(ln for ln in lines if ln.startswith("req-w3")).split()[3:6] == ["60", "41", "19"]
