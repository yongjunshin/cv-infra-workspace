"""NEG-1 · C-1 미진입 — baseline 탐색·반입은 외부 CI/CD·git 이력에 진입하지 않는다.

DoD §6 NEG-1이 **파일명까지 고정**한 negative 스위트 (REQ-REPORT-006 High;
REQ-REPORT-004/005 · NFR-REPORT-002; 게이트 DoD-P5-05 · DoD-P5-04). 게이트 문면
3항 ↔ 테스트 1:1 매핑:

1. "baseline 매칭 경로에서 git/GitHub API 호출 forbid(monkeypatch) → 호출 시도 0;
   baseline 출처 = 내부 SQLite로 단정"
   -> ``test_forbid_harness_is_armed_on_every_surface``      (하네스 무장 실증)
   -> ``test_baseline_matching_path_attempts_no_external_call``
   -> ``test_matched_baseline_is_the_internal_sqlite_row``   (출처 = 그 파일의 그 행)
   -> ``test_internal_store_outage_has_no_external_fallback``(대체 출처 부재)
   -> ``test_baseline_in_another_store_file_is_invisible``   (출처 = THIS 배포본)
2. "grep -rE ``git (log|clone|fetch)|api\\.github\\.com.*(commits|compare)``
   cv_infra/report/ → 0 매치"
   -> ``test_report_package_has_no_external_history_call_sites``
   -> ``test_neg1_grep_pattern_is_not_vacuous``              (G-21 비공허 실증)
3. "baseline 부재 실행 → exit 0 + 리포트 정상 생성(회귀만 skip)"
   -> ``test_absent_baseline_completes_report_and_skips_only_regression``
   -> ``test_absent_baseline_run_exits_zero``                (프로세스 반환값 0)

**G-35 — 이 파일이 공허해지는 두 갈래를 각각 쌍으로 막는다.** "외부 호출 0"은
(a) 대상 코드가 아예 안 돌아도 참이고 (b) monkeypatch가 *엉뚱한 표면*을 막았어도
참이다. 그래서 ① 하네스 무장 실증 = 각 금지 표면을 실제 git/GitHub 호출 형태로
두드려 기록+예외를 확인하고(그래야 다른 테스트의 ``== []``가 의미를 가진다)
② 착지 단정 = 같은 실행에서 baseline 매칭이 실제로 일어났음(회귀 판정·
``baseline_sut_ref``·내부 행과의 일치)을 함께 단정한다.

구조(import graph) 층 증거는 ``tests/test_report_regression.py``의 allow-list AST
감사가 소유한다 — 여기서 중복하지 않고, 이 파일은 **런타임 호출 시도**를 본다.

Stdlib + pytest (+ ``httpx.MockTransport``를 기존 ``batch._make_client`` seam에 주입).
"""

from __future__ import annotations

import builtins
import http.client
import io
import os
import re
import socket
import sqlite3
import subprocess
import urllib.request
from pathlib import Path

import httpx
import pytest

import cv_infra.report
from cv_infra.cli import batch
from cv_infra.cli.exit_codes import EXIT_PASS, exit_code_for_report_outcome
from cv_infra.cli.main import main
from cv_infra.contract.schema import Result, VerificationRequest
from cv_infra.orchestrator.models import RequestRollup, Verdict
from cv_infra.orchestrator.store import Store
from cv_infra.report.aggregate import RequestReportInput, build_report
from cv_infra.report.baseline import find_baseline, update_baseline
from cv_infra.report.regression import STATUS_NO_BASELINE, STATUS_REGRESSED, identity_key

_AT = "2026-07-16T00:00:00+00:00"
_BASELINE_AT = "2026-07-10T00:00:00+00:00"
_BASELINE_SUT = "carter-sut:a"

# Canonical builders — the REAL M1 models (G-17/G-28: the shape under test is the
# producer's own output), verbatim-anchored to tests/test_report_verification_report.py.
_BASE_SCENARIO = {
    "scene": "nova_carter_warehouse",
    "robot": "nova_carter",
    "goal": {"x": -6.0, "y": 5.0, "yaw": 1.5708},
    "seed": 42,
    "timeout_s": 120,
}


def _request(*, seed: int = 42, sut: str = "carter-sut:b") -> dict:
    return VerificationRequest.model_validate(
        {
            "scenario": {**_BASE_SCENARIO, "seed": seed},
            "sut": {"image_ref": sut},
            "acceptance_criteria": [{"oracle": "reached_goal"}],
        }
    ).model_dump(mode="json", by_alias=True)


def _result(job_id: str, verdict: str) -> dict:
    return Result.model_validate(
        {
            "job_id": job_id,
            "verdict": verdict,
            "metrics": {"time_to_goal_s": 12.5},
            "artifacts": {"mcap": f"/cv/out/{job_id}.mcap", "mp4": None},
        }
    ).model_dump(mode="json")


def _rollup(request_id: str, verdict: Verdict) -> RequestRollup:
    return RequestRollup(request_id=request_id, verdicts=[verdict], flakiness=0.0, verdict=verdict)


def _input(request_id: str, request: dict, verdict: Verdict) -> RequestReportInput:
    return RequestReportInput(
        request=request,
        rollup=_rollup(request_id, verdict),
        results=[_result(f"{request_id}:0", verdict.value)],
    )


def _report(inputs: list[RequestReportInput], store: Store) -> dict:
    return build_report(
        inputs, store, envelope_id="env-1", trigger_source="ci-cd", generated_at=_AT
    )


# --------------------------------------------------------------------------- #
# forbid harness — the surfaces a git/GitHub reach-out MUST pass through
# --------------------------------------------------------------------------- #


class ExternalCallForbidden(RuntimeError):
    """Raised the instant baseline code touches a process-spawn or network surface."""


# (owner, attribute) pairs. Each is a REAL entry point, not a proxy:
#   * subprocess.Popen        — the single CPython funnel every subprocess.run /
#                               call / check_call / check_output / os.popen builds
#                               on: patching it catches `git`/`gh` spawns even when
#                               a module did `from subprocess import run` at import.
#   * subprocess.run/.check_output — patched too so the RECORD names the real call
#                               site (Popen alone would attribute every spawn to Popen).
#   * os.system / os.posix_spawn — the two spawn funnels that bypass subprocess.
#   * socket.socket.connect / socket.create_connection / socket.getaddrinfo —
#                               every HTTP client (urllib, httpx, requests) ends at
#                               these; getaddrinfo fires even before the dial, so a
#                               reach-out is recorded even if the TCP connect fails.
#   * urllib.request.urlopen / http.client.HTTPConnection.request — the higher-level
#                               surfaces an api.github.com call would use directly
#                               (HTTPSConnection inherits .request from HTTPConnection).
_FORBIDDEN_SURFACES = (
    (subprocess, "Popen"),
    (subprocess, "run"),
    (subprocess, "check_output"),
    (os, "system"),
    (os, "posix_spawn"),
    (socket.socket, "connect"),
    (socket, "create_connection"),
    (socket, "getaddrinfo"),
    (urllib.request, "urlopen"),
    (http.client.HTTPConnection, "request"),
)


def _label(owner: object, attribute: str) -> str:
    module = getattr(owner, "__name__", type(owner).__name__)
    if isinstance(owner, type):
        module = f"{owner.__module__}.{owner.__qualname__}"
    return f"{module}.{attribute}"


@pytest.fixture
def forbidden_attempts(monkeypatch) -> list[str]:
    """Forbid every git/GitHub/network surface; yield the list of ATTEMPTS made.

    Attempts are RECORDED before the exception is raised, so a reach-out stays
    visible even if the caller swallows the failure (a silent try/except around a
    `git log` would still show up here). ``[]`` == 호출 시도 0.
    """
    attempts: list[str] = []

    def _blocked(label: str):
        def _refuse(*args, **kwargs):
            attempts.append(f"{label}(args={args!r})")
            raise ExternalCallForbidden(f"{label} is forbidden on the baseline path (C-1)")

        return _refuse

    for owner, attribute in _FORBIDDEN_SURFACES:
        monkeypatch.setattr(owner, attribute, _blocked(_label(owner, attribute)))
    return attempts


@pytest.fixture
def opened_paths(monkeypatch) -> list[str]:
    """Record (and allow) every Python-level file open; yield the paths opened.

    The **fs half** of the C-1 audit. Reading a consumer checkout's git history
    directly (``open(".git/HEAD")``) needs NO import and NO subprocess, so it slips
    past all three of the other layers: the import allow-list AST (nothing is
    imported), the NEG-1 grep (no ``git log``/``api.github.com`` literal), and the
    call-forbid above (neither a spawn nor a dial). cv-infra's own SQLite is opened
    by the sqlite3 C extension, which does NOT go through these surfaces —
    measured: the baseline path records ZERO Python-level opens — so anything
    recorded here means something OTHER than the internal store was read.
    Recorded-and-delegated (not blocked): this is an audit, not a forbid.
    """
    paths: list[str] = []
    originals = {(builtins, "open"): builtins.open, (io, "open"): io.open, (os, "open"): os.open}

    def _recorder(label: str, original):
        def _record(path, *args, **kwargs):
            paths.append(f"{label}:{path}")
            return original(path, *args, **kwargs)

        return _record

    for (owner, attribute), original in originals.items():
        label = f"{getattr(owner, '__name__', owner)}.{attribute}"
        monkeypatch.setattr(owner, attribute, _recorder(label, original))
    return paths


def _dial_socket() -> None:
    with socket.socket() as sock:
        sock.connect(("api.github.com", 443))


# Realistic reach-out shapes — how a "fetch the consumer's history" implementation
# would actually be written. Each must be intercepted by the surface it names.
_SIMULATED_EXTERNAL_CALLS = (
    ("subprocess.run", lambda: subprocess.run(["git", "log", "-1"], check=False)),
    ("subprocess.Popen", lambda: subprocess.Popen(["git", "clone", "https://x/y"])),
    ("subprocess.check_output", lambda: subprocess.check_output(["gh", "api", "repos/o/r"])),
    ("os.system", lambda: os.system("git fetch --all")),
    ("os.posix_spawn", lambda: os.posix_spawn("/usr/bin/git", ["git", "log"], {})),
    ("socket.getaddrinfo", lambda: socket.getaddrinfo("api.github.com", 443)),
    ("socket.create_connection", lambda: socket.create_connection(("api.github.com", 443))),
    ("socket.socket.connect", _dial_socket),
    ("urllib.request.urlopen", lambda: urllib.request.urlopen("https://api.github.com/x")),
    (
        "http.client.HTTPConnection.request",
        lambda: http.client.HTTPSConnection("api.github.com").request("GET", "/repos/o/r/commits"),
    ),
)


# --------------------------------------------------------------------------- #
# (1) NEG-1 1항 — forbid → 호출 시도 0 · 출처 = 내부 SQLite
# --------------------------------------------------------------------------- #


def test_forbid_harness_is_armed_on_every_surface(forbidden_attempts):
    """G-35 positive control: the guard itself must be load-bearing.

    Without this, ``forbidden_attempts == []`` in every other test would also hold
    for a harness that patched nothing (or patched a surface no caller uses) —
    i.e. the whole file could be green while forbidding nothing. Each simulated
    git/GitHub call must be RECORDED (attributed to the named surface) and RAISE.
    """
    for index, (label, call) in enumerate(_SIMULATED_EXTERNAL_CALLS, start=1):
        with pytest.raises(ExternalCallForbidden):
            call()
        assert len(forbidden_attempts) == index, f"{label} was not intercepted"
        assert forbidden_attempts[-1].startswith(label), forbidden_attempts[-1]
    assert len(forbidden_attempts) == len(_SIMULATED_EXTERNAL_CALLS)


def test_baseline_matching_path_attempts_no_external_call(tmp_path, forbidden_attempts):
    """The whole baseline lifecycle (establish -> match -> judge) under forbid.

    Everything below runs with the harness armed: opening the store, writing a
    baseline, looking it up, and assembling the report that matches + judges it.
    """
    request = _request()
    key = identity_key(request)
    with Store(tmp_path / "cv.sqlite3") as store:
        assert update_baseline(
            store,
            request_identity_key=key,
            sut_ref=_BASELINE_SUT,
            verdict="pass",
            established_at=_BASELINE_AT,
        )
        matched = find_baseline(store, key)
        report = _report([_input("req-0", request, Verdict.FAIL)], store)

    assert forbidden_attempts == []  # 게이트 1항: 호출 시도 0

    # G-35 착지 단정 — the matching path really RAN in that same window (a report
    # that never consulted a baseline would also make 0 external calls).
    assert matched is not None and matched.sut_ref == _BASELINE_SUT
    (row,) = report["matrix"]
    assert row["request_identity_key"] == key
    assert row["regression"]["status"] == STATUS_REGRESSED
    assert row["regression"]["baseline_sut_ref"] == _BASELINE_SUT
    assert row["regression"]["baseline_established_at"] == _BASELINE_AT
    assert report["baseline_summary"]["matched"] == 1


def test_matched_baseline_is_the_internal_sqlite_row(tmp_path, forbidden_attempts):
    """출처 단정: the matched baseline IS the row in cv-infra's own SQLite file."""
    db = tmp_path / "cv.sqlite3"
    request = _request()
    key = identity_key(request)
    with Store(db) as store:
        update_baseline(
            store,
            request_identity_key=key,
            sut_ref=_BASELINE_SUT,
            verdict="pass",
            established_at=_BASELINE_AT,
        )
        matched = find_baseline(store, key)
    assert forbidden_attempts == []

    # An INDEPENDENT reader over the same file (not the Store API) — what M4 handed
    # back has to be byte-for-byte that row, and there is exactly one of them.
    connection = sqlite3.connect(str(db))
    try:
        rows = connection.execute(
            "SELECT request_identity_key, sut_ref, verdict, established_at FROM request_baselines"
        ).fetchall()
    finally:
        connection.close()
    assert rows == [(key, _BASELINE_SUT, "pass", _BASELINE_AT)]
    assert (
        matched.request_identity_key,
        matched.sut_ref,
        matched.verdict,
        matched.established_at,
    ) == rows[0]


def test_internal_store_outage_has_no_external_fallback(tmp_path, forbidden_attempts, monkeypatch):
    """대체 출처 부재: kill the internal read and NOTHING else answers.

    ``load_baseline`` raises the realistic outage error (``sqlite3.OperationalError``)
    — exactly what an implementation with a git/API fallback would catch to go
    fetch the history elsewhere. The error must propagate and no surface may be
    touched: the internal store is the only source, not the preferred one.
    """

    def _store_down(self, request_identity_key):
        raise sqlite3.OperationalError("database is locked")

    monkeypatch.setattr(Store, "load_baseline", _store_down)
    with Store(tmp_path / "cv.sqlite3") as store:
        with pytest.raises(sqlite3.OperationalError):
            _report([_input("req-0", _request(), Verdict.PASS)], store)
    assert forbidden_attempts == []


def test_baseline_path_reads_no_file_outside_the_store(tmp_path, forbidden_attempts, opened_paths):
    """fs 층 단정: the baseline path reads nothing outside cv-infra's own store dir.

    Pairs with the call-forbid: together they cover both ways OUT of the process
    (spawn/dial and file read). ``.git`` under the process CWD is the realistic
    target — this asserts it was never touched.
    """
    request = _request()
    key = identity_key(request)
    with Store(tmp_path / "cv.sqlite3") as store:
        update_baseline(store, request_identity_key=key, sut_ref=_BASELINE_SUT, verdict="pass")
        find_baseline(store, key)
        report = _report([_input("req-0", request, Verdict.PASS)], store)

    assert forbidden_attempts == []
    outside = [path for path in opened_paths if str(tmp_path) not in path]
    assert outside == [], f"baseline path read outside its own store: {outside}"
    assert report["baseline_summary"]["matched"] == 1  # 착지: 매칭은 실제로 일어났다


def test_baseline_in_another_store_file_is_invisible(tmp_path, forbidden_attempts):
    """출처 = THIS deployment's file: a baseline in another store never matches."""
    request = _request()
    key = identity_key(request)
    with Store(tmp_path / "other.sqlite3") as other:
        update_baseline(
            store=other, request_identity_key=key, sut_ref=_BASELINE_SUT, verdict="pass"
        )
        assert find_baseline(other, key) is not None  # it really is a valid baseline

    with Store(tmp_path / "cv.sqlite3") as store:
        assert find_baseline(store, key) is None
        report = _report([_input("req-0", request, Verdict.FAIL)], store)
    assert forbidden_attempts == []

    (row,) = report["matrix"]
    assert row["regression"]["status"] == STATUS_NO_BASELINE
    assert row["regression"]["baseline_sut_ref"] is None


# --------------------------------------------------------------------------- #
# (2) NEG-1 2항 — cv_infra/report/ 에 외부 이력 호출 사이트 0
# --------------------------------------------------------------------------- #

#: DoD §6 NEG-1 2항의 grep 패턴 **verbatim** (게이트가 고정 — 강화·완화 금지).
#: 알려진 사각: list-form argv(``["git", "log"]``)는 이 패턴에 안 걸린다. 그 갈래는
#: 위 (1)의 런타임 forbid(subprocess.Popen 차단)가 덮는다 — 두 항은 상보적이다.
_NEG1_GREP = r"git (log|clone|fetch)|api\.github\.com.*(commits|compare)"

#: G-21: the pattern must actually FIRE on what it targets — otherwise "0 매치"는
#: 겨냥한 게 없다는 뜻일 뿐이다.
_GREP_POSITIVE_SAMPLES = (
    'subprocess.run("git log -1 --format=%H", shell=True)',
    'subprocess.run("git clone " + repo_url, shell=True)',
    'os.system(f"git fetch origin {sha}")',
    'urlopen("https://api.github.com/repos/o/r/commits/" + sha)',
    'client.get("https://api.github.com/repos/o/r/compare/base...head")',
)


def test_report_package_has_no_external_history_call_sites():
    pattern = re.compile(_NEG1_GREP)
    package_dir = Path(cv_infra.report.__file__).parent
    sources = sorted(package_dir.rglob("*.py"))
    assert sources, "report package has no modules to audit"
    hits = [
        f"{source.relative_to(package_dir)}:{number}: {line.strip()}"
        for source in sources
        for number, line in enumerate(source.read_text().splitlines(), 1)
        if pattern.search(line)
    ]
    assert hits == []


def test_neg1_grep_pattern_is_not_vacuous():
    pattern = re.compile(_NEG1_GREP)
    for sample in _GREP_POSITIVE_SAMPLES:
        assert pattern.search(sample), f"NEG-1 grep pattern missed a real call site: {sample}"


# --------------------------------------------------------------------------- #
# (3) NEG-1 3항 — baseline 부재 = 정상 (회귀만 skip, exit 0)
# --------------------------------------------------------------------------- #


def test_absent_baseline_completes_report_and_skips_only_regression(tmp_path, forbidden_attempts):
    """부재는 정상 상태다 — 그리고 부재가 외부 조회를 유발하지 않는다.

    The miss is the C-1 moment of truth: a system that reaches out would do it
    exactly when the internal lookup finds nothing. Everything but the regression
    block must survive intact (NFR-REPORT-002, REQ-REPORT-004).
    """
    inputs = [
        _input("req-0", _request(seed=42), Verdict.PASS),
        _input("req-1", _request(seed=43), Verdict.PASS),
    ]
    with Store(tmp_path / "cv.sqlite3") as store:  # never written to — no baseline
        report = _report(inputs, store)

    assert forbidden_attempts == []  # baseline 부재 → 외부로 나가지 않는다

    outcome = report["summary"]["report_outcome"]
    assert outcome == "pass"
    assert exit_code_for_report_outcome(outcome) == EXIT_PASS == 0

    assert len(report["matrix"]) == 2
    for row in report["matrix"]:
        assert row["regression"]["status"] == STATUS_NO_BASELINE  # 회귀만 skip
        assert "skip" in row["regression"]["detail"]
        assert row["rollup"]["verdict"] == "pass"  # 도메인 결과 무손상
        assert row["metrics"]["time_to_goal_s"] == 12.5  # 메트릭 정상
        assert row["artifacts"]["selected"]  # 아티팩트 선별 정상
    assert report["baseline_summary"]["matched"] == 0
    assert report["baseline_summary"]["absent"] == 2
    assert report["baseline_summary"]["regressed"] == 0


def test_absent_baseline_run_exits_zero(tmp_path, monkeypatch, forbidden_attempts):
    """프로세스 반환값 0: the same all-pass/no-baseline report folded at the CLI.

    ``cv-infra wait`` is where ``report_outcome`` becomes the process exit code
    (batch.py ``_poll_until_terminal`` -> ``exit_codes.exit_code_for_report_outcome``,
    the M8 single source). The outcome served here is taken VERBATIM from a report
    the real M4 built against an EMPTY store, so the 0 is the baseline-absent run's
    own outcome — not a hand-written literal. The orchestrator is faked at the
    established ``batch._make_client`` seam (no socket is opened; the forbid
    harness is armed throughout).
    """
    with Store(tmp_path / "cv.sqlite3") as store:
        report = _report([_input("req-0", _request(), Verdict.PASS)], store)
    outcome = report["summary"]["report_outcome"]

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/envelopes/env-1"
        return httpx.Response(
            200, json={"envelope_id": "env-1", "status": "completed", "report_outcome": outcome}
        )

    monkeypatch.setattr(
        batch,
        "_make_client",
        lambda api_base: httpx.AsyncClient(
            transport=httpx.MockTransport(handler), base_url="http://cv-infra.test"
        ),
    )
    assert main(["wait", "env-1"]) == EXIT_PASS == 0
    assert forbidden_attempts == []
