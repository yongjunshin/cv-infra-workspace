"""``cv-infra {submit,status,wait}`` batch-CLI tests (M8, DoD-P4-14 / M8-D11).

The REAL M3 FastAPI app (``orchestrator.api.create_app`` + fake runners) is
wired straight into the CLI through httpx ``ASGITransport`` injected at the
``batch._make_client`` seam — no sockets, no uvicorn. The M1 envelope loader
is NOT consumed here (G-17: T1 lands ``contract/envelope.py`` in parallel):
the ``batch._load_envelope`` adapter is monkeypatched with verbatim-shaped
stubs (``LoadedEnvelope``/``LoadedRequestRef``, task data contract); the
real-loader round-trip is the PM merge gate's measurement. Submissions ride
the wire-v2 OMISSION path (no ``oracle_plugin_dirs`` — server acceptance is
Wave 2); the prepared field is unit-pinned separately.

Loop-lifetime note (why the timeout case uses a purpose-built app): the app's
envelope drive task lives on the event loop of the CLI invocation that
submitted it (each command = one ``asyncio.run``), so a real ``create_app``
envelope cannot deterministically stay "running" across CLI invocations.
Terminal-state assertions therefore complete the envelope INSIDE one
``submit --wait`` invocation (later ``wait``/``status`` reads see the stored
terminal record); the client-side ``--timeout`` deadline is exercised against
a minimal always-running FastAPI app over the same ASGITransport seam.
"""

from __future__ import annotations

import copy
import json
from pathlib import Path

import httpx
import pytest
import yaml
from fastapi import FastAPI

from cv_infra.cli import batch
from cv_infra.cli.main import EXIT_CONTRACT, EXIT_FAIL, EXIT_INFRA, EXIT_PASS, main
from cv_infra.contract.errors import ContractError
from cv_infra.orchestrator import api
from cv_infra.orchestrator.api import create_app
from cv_infra.orchestrator.fake_runner import FakeRunner
from cv_infra.orchestrator.models import JobState, Verdict
from cv_infra.orchestrator.store import Store
from tests.test_orchestrator_api import SuffixScriptedRunner

# Canonical M1-valid request document — same platform fixture the api tests
# admit through the real 6-stage gate (drift guard: test_fixture_canonical_guard).
_FIXTURE = Path(__file__).parent / "fixtures" / "nova_carter_warehouse_goal.yaml"
_CANONICAL_DOC = yaml.safe_load(_FIXTURE.read_text(encoding="utf-8"))


def _request_doc() -> dict:
    return copy.deepcopy(_CANONICAL_DOC)


# --- verbatim T1 stub shapes (task data contract — LoadedEnvelope/RequestRef) --


class StubLoadedRequestRef:
    """Field-exact stand-in for the pinned ``LoadedRequestRef`` (verbatim)."""

    def __init__(self, admitted, raw_doc: dict, scenario_path: str, oracle_plugin_dir: str):
        self.admitted = admitted
        self.raw_doc = raw_doc
        self.scenario_path = scenario_path
        self.oracle_plugin_dir = oracle_plugin_dir


class StubLoadedEnvelope:
    """Field-exact stand-in for the pinned ``LoadedEnvelope`` (verbatim)."""

    def __init__(self, api_version: str, requests: tuple[StubLoadedRequestRef, ...]):
        self.api_version = api_version
        self.requests = requests


def _stub_envelope(docs: list[dict]) -> StubLoadedEnvelope:
    refs = tuple(
        StubLoadedRequestRef(
            admitted=object(),  # opaque — the CLI must not re-validate (loader owns admission)
            raw_doc=doc,
            scenario_path=f"scenarios/s{i}.yaml",
            oracle_plugin_dir=f"/abs/consumer/scenarios{i}",
        )
        for i, doc in enumerate(docs)
    )
    return StubLoadedEnvelope(api_version="cv-infra/v1", requests=refs)


class SpyASGITransport(httpx.ASGITransport):
    """ASGITransport over the real app that records every request — the
    "server was (not) called" assertions read ``self.requests``."""

    def __init__(self, app) -> None:
        super().__init__(app=app)
        self.requests: list[httpx.Request] = []

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        return await super().handle_async_request(request)


def _wire_cli(
    monkeypatch: pytest.MonkeyPatch,
    app,
    *,
    docs: list[dict] | None = None,
    load_error: ContractError | None = None,
) -> SpyASGITransport:
    """Wire the CLI seams: stub envelope adapter + ASGI transport + fast polls."""
    spy = SpyASGITransport(app)

    def fake_load(source):
        if load_error is not None:
            raise load_error
        assert docs is not None, "test wiring: docs or load_error required"
        return _stub_envelope(docs)

    monkeypatch.setattr(batch, "_load_envelope", fake_load)
    monkeypatch.setattr(
        batch,
        "_make_client",
        lambda api_base: httpx.AsyncClient(transport=spy, base_url="http://cv-infra.test"),
    )
    monkeypatch.setattr(batch, "_POLL_INTERVAL_S", 0.01)
    return spy


@pytest.fixture()
def envelope_file(tmp_path: Path) -> Path:
    """A RequestEnvelope YAML per decision p4c3 D-2 (content is consumed only
    by the stubbed adapter — its admission semantics are T1's)."""
    path = tmp_path / "envelope.yaml"
    path.write_text(
        "apiVersion: cv-infra/v1\n"
        "requests:\n"
        "  - scenario: scenarios/s0.yaml\n"
        "  - scenario: scenarios/s1.yaml\n",
        encoding="utf-8",
    )
    return path


# --------------------------------------------------------------------------- #
# (1) submit --wait roundtrip: terminal aggregated verdict -> exit 0/1/3
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    ("behaviors", "expected_exit", "expected_outcome"),
    [
        ({}, EXIT_PASS, "pass"),
        ({"r1": "fail-verdict"}, EXIT_FAIL, "fail"),
        # errored 우선: r0 has NO verdict (infra) AND r1 FAILs -> exit 3, not 1
        # (infra noise never reads as a self-regression — D-I).
        ({"r0": "exit-nonzero", "r1": "fail-verdict"}, EXIT_INFRA, "errored"),
    ],
)
def test_submit_wait_roundtrip_maps_terminal_outcome_to_exit(
    monkeypatch, tmp_path, capsys, envelope_file, behaviors, expected_exit, expected_outcome
):
    docs = [_request_doc(), _request_doc()]
    with Store(tmp_path / "cv.sqlite3") as store:
        app = create_app(store, SuffixScriptedRunner(behaviors), k=2)
        spy = _wire_cli(monkeypatch, app, docs=docs)

        rc = main(["submit", str(envelope_file), "--wait"])

        assert rc == expected_exit
        out_lines = capsys.readouterr().out.strip().splitlines()
        assert out_lines[0].startswith("env-")  # bare envelope_id first (scriptable)
        assert f"report_outcome={expected_outcome}" in out_lines[-1]

        # Wire-v2 pin, OMISSION path: raw docs verbatim, and NO
        # oracle_plugin_dirs key until the M3 server acceptance lands (Wave 2).
        (post,) = [r for r in spy.requests if r.method == "POST"]
        body = json.loads(post.content)
        assert set(body) == {"requests"}
        assert body["requests"] == docs


def test_standalone_wait_returns_the_terminal_verdict_exit(
    monkeypatch, tmp_path, capsys, envelope_file
):
    """A later ``cv-infra wait <id>`` re-derives the SAME terminal exit from
    the stored envelope record (M8-D11: wait == submit --wait semantics)."""
    with Store(tmp_path / "cv.sqlite3") as store:
        app = create_app(store, SuffixScriptedRunner({"r0": "fail-verdict"}), k=2)
        _wire_cli(monkeypatch, app, docs=[_request_doc()])

        assert main(["submit", str(envelope_file), "--wait"]) == EXIT_FAIL
        envelope_id = capsys.readouterr().out.strip().splitlines()[0]

        assert main(["wait", envelope_id]) == EXIT_FAIL
        assert "report_outcome=fail" in capsys.readouterr().out


# --------------------------------------------------------------------------- #
# (2) invalid envelope: client-side pre-validation -> exit 2, server NOT called
# --------------------------------------------------------------------------- #


def test_invalid_envelope_exits_2_and_never_calls_server(
    monkeypatch, tmp_path, capsys, envelope_file
):
    rejection = ContractError(
        field_path="requests[0].scenario",
        expected="an existing scenario file (path relative to the envelope)",
        got="'missing.yaml'",
        example="scenario: scenarios/nova_carter_warehouse_goal.yaml",
        doc_link="decisions/2026-07-13-p4c3-envelope-file-refs.md",
        source_path=str(envelope_file),
        source_line=3,
        source_col=15,
    )
    with Store(tmp_path / "cv.sqlite3") as store:
        app = create_app(store, FakeRunner(), k=1)
        spy = _wire_cli(monkeypatch, app, load_error=rejection)

        rc = main(["submit", str(envelope_file), "--wait"])

        captured = capsys.readouterr()
        assert rc == EXIT_CONTRACT
        assert spy.requests == []  # SPY: rejected envelope never reaches the server
        assert captured.out == ""  # no envelope_id — nothing was submitted
        assert captured.err.startswith("cv-infra submit: requests[0].scenario: expected ")
        assert f"at {envelope_file}:3:15" in captured.err  # M1 line/col rides verbatim
        assert "Traceback" not in captured.err  # raw traceback 0 (NFR-INTAKE-001)


def test_submit_timeout_without_wait_is_usage_error_2(monkeypatch, tmp_path, capsys, envelope_file):
    with Store(tmp_path / "cv.sqlite3") as store:
        app = create_app(store, FakeRunner(), k=1)
        spy = _wire_cli(monkeypatch, app, docs=[_request_doc()])
        assert main(["submit", str(envelope_file), "--timeout", "5"]) == EXIT_CONTRACT
        assert "--timeout requires --wait" in capsys.readouterr().err
        assert spy.requests == []


# --------------------------------------------------------------------------- #
# (3) status is informational (D-O): fail batch -> exit 0; 404 -> exit 2
# --------------------------------------------------------------------------- #


def test_status_is_informational_even_for_a_failed_batch(
    monkeypatch, tmp_path, capsys, envelope_file
):
    with Store(tmp_path / "cv.sqlite3") as store:
        app = create_app(store, FakeRunner(state=JobState.COMPLETED, verdict=Verdict.FAIL), k=1)
        _wire_cli(monkeypatch, app, docs=[_request_doc()])

        assert main(["submit", str(envelope_file), "--wait"]) == EXIT_FAIL
        envelope_id = capsys.readouterr().out.strip().splitlines()[0]

        assert main(["status", envelope_id]) == EXIT_PASS  # query never turns CI red
        body = json.loads(capsys.readouterr().out)  # response JSON verbatim on stdout
        assert body["envelope_id"] == envelope_id
        assert body["status"] == "completed"
        assert body["report_outcome"] == "fail"


@pytest.mark.parametrize("command", ["status", "wait"])
def test_unknown_envelope_id_is_friendly_exit_2(monkeypatch, tmp_path, capsys, command):
    with Store(tmp_path / "cv.sqlite3") as store:
        app = create_app(store, FakeRunner(), k=1)
        _wire_cli(monkeypatch, app, docs=[])
        assert main([command, "env-nope"]) == EXIT_CONTRACT
        err = capsys.readouterr().err
        assert "unknown envelope id 'env-nope'" in err
        assert "Traceback" not in err


# --------------------------------------------------------------------------- #
# (4) wait --timeout exceeded -> exit 3 (client-side deadline, infra semantics)
# --------------------------------------------------------------------------- #


def _always_running_app() -> FastAPI:
    """Minimal real FastAPI app whose envelope never terminates (see the module
    docstring for why ``create_app`` cannot deterministically provide this
    across CLI invocations — the drive task is loop-scoped)."""
    app = FastAPI()

    @app.get("/envelopes/{envelope_id}")
    async def status(envelope_id: str) -> dict:
        return {
            "envelope_id": envelope_id,
            "status": "running",
            "jobs": [],
            "rollups": [],
            "report_outcome": None,
        }

    @app.post("/envelopes", status_code=202)
    async def submit() -> dict:
        return {"envelope_id": "env-neverdone"}

    return app


def test_wait_timeout_exceeded_exits_3(monkeypatch, capsys):
    spy = _wire_cli(monkeypatch, _always_running_app(), docs=[])
    assert main(["wait", "env-neverdone", "--timeout", "0.05"]) == EXIT_INFRA
    err = capsys.readouterr().err
    assert "not terminal within --timeout 0.05s" in err
    assert "not a SUT verdict" in err
    assert len(spy.requests) >= 1  # at least one poll always happens


def test_submit_wait_timeout_exceeded_exits_3(monkeypatch, capsys, envelope_file):
    _wire_cli(monkeypatch, _always_running_app(), docs=[_request_doc()])
    assert main(["submit", str(envelope_file), "--wait", "--timeout", "0.05"]) == EXIT_INFRA
    captured = capsys.readouterr()
    assert captured.out.strip().splitlines()[0] == "env-neverdone"  # id printed before waiting
    assert "not terminal within --timeout 0.05s" in captured.err


# --------------------------------------------------------------------------- #
# (5) server-side 422 re-rejection renders M1 friendly prose (Traceback 0)
# --------------------------------------------------------------------------- #


def test_server_422_rerejection_renders_friendly_prose(
    monkeypatch, tmp_path, capsys, envelope_file
):
    bad = _request_doc()
    del bad["sut"]  # violates the REQ-INTAKE-006 triad -> server admit gate rejects
    with Store(tmp_path / "cv.sqlite3") as store:
        app = create_app(store, FakeRunner(), k=1)
        # The stubbed client-side loader ACCEPTS — the server re-runs the real
        # M1 gate and stays authoritative (all-or-nothing 422).
        spy = _wire_cli(monkeypatch, app, docs=[_request_doc(), bad])

        rc = main(["submit", str(envelope_file), "--wait"])

        captured = capsys.readouterr()
        assert rc == EXIT_CONTRACT
        assert len(spy.requests) == 1  # exactly the POST; no polling followed
        assert captured.out == ""  # no envelope_id — nothing was accepted
        assert "cv-infra submit: sut: expected " in captured.err
        assert "requests[1]" in captured.err  # server names the failing request
        assert "Traceback" not in captured.err
        assert store.load_jobs() == []  # 비전파: zero jobs on our side either


# --------------------------------------------------------------------------- #
# (6) infra reachability + pure units (mapping single source, wire prep, --api)
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize("argv_tail", [["submit"], ["status", "env-x"], ["wait", "env-x"]])
def test_orchestrator_unreachable_exits_3(monkeypatch, envelope_file, capsys, argv_tail):
    def refuse(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("connection refused")

    monkeypatch.setattr(batch, "_load_envelope", lambda source: _stub_envelope([_request_doc()]))
    monkeypatch.setattr(
        batch,
        "_make_client",
        lambda api_base: httpx.AsyncClient(
            transport=httpx.MockTransport(refuse), base_url="http://cv-infra.test"
        ),
    )
    argv = argv_tail + ([str(envelope_file)] if argv_tail == ["submit"] else [])
    assert main(argv) == EXIT_INFRA
    err = capsys.readouterr().err
    assert "orchestrator unreachable" in err
    assert "not a SUT verdict" in err


def test_report_outcome_exit_mapping_is_the_single_source():
    """The M8 mapping table keys ARE the api literals (import 정합, 재정의 금지)
    and the fold follows LOCKED §9 / D-I; unknown/absent -> INFRA, never FAIL."""
    assert batch.REPORT_OUTCOME_EXIT == {
        api.REPORT_OUTCOME_PASS: EXIT_PASS,
        api.REPORT_OUTCOME_FAIL: EXIT_FAIL,
        api.REPORT_OUTCOME_ERRORED: EXIT_INFRA,
    }
    assert batch.exit_code_for_report_outcome(api.REPORT_OUTCOME_PASS) == 0
    assert batch.exit_code_for_report_outcome(api.REPORT_OUTCOME_FAIL) == 1
    assert batch.exit_code_for_report_outcome(api.REPORT_OUTCOME_ERRORED) == 3
    assert batch.exit_code_for_report_outcome("wat") == EXIT_INFRA
    assert batch.exit_code_for_report_outcome(None) == EXIT_INFRA


def test_wire_body_prepares_oracle_plugin_dirs_equal_length(monkeypatch):
    """Wave-2 prepared path (code-only this cycle): flipping the module gate
    adds ``oracle_plugin_dirs`` equal-length with ``requests``; the default
    stays the omission path (server acceptance = M3 T3)."""
    envelope = _stub_envelope([_request_doc(), _request_doc()])
    assert set(batch._wire_body(envelope)) == {"requests"}  # default: omitted

    monkeypatch.setattr(batch, "_INCLUDE_ORACLE_PLUGIN_DIRS", True)
    body = batch._wire_body(envelope)
    assert body["oracle_plugin_dirs"] == ["/abs/consumer/scenarios0", "/abs/consumer/scenarios1"]
    assert len(body["oracle_plugin_dirs"]) == len(body["requests"])  # 등길이 (wire v2)


def test_api_base_resolution_flag_env_default(monkeypatch):
    monkeypatch.delenv("CV_INFRA_API", raising=False)
    assert batch._resolve_api(None) == "http://127.0.0.1:8000"
    monkeypatch.setenv("CV_INFRA_API", "http://gpu-box:8000")
    assert batch._resolve_api(None) == "http://gpu-box:8000"
    assert batch._resolve_api("http://flag:9") == "http://flag:9"  # flag outranks env
