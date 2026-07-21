"""``cv-infra {submit,status,wait}`` batch-CLI tests (M8, DoD-P4-14 / M8-D11).

The REAL M3 FastAPI app (``orchestrator.api.create_app`` + fake runners) is
wired straight into the CLI through httpx ``ASGITransport`` injected at the
``batch._make_client`` seam — no sockets, no uvicorn. The M1 envelope loader
is consumed two ways (G-17 close-out, Wave-2 integration): most tests
monkeypatch the ``batch._load_envelope`` adapter with verbatim-shaped stubs
(``LoadedEnvelope``/``LoadedRequestRef`` — unit isolation kept), while the
E2E section (7) drives the REAL ``contract.envelope.load_envelope`` from an
envelope YAML on disk through wire v2 (``oracle_plugin_dirs`` emitted — the
M3 server acceptance landed with Wave 2) into the real server admit gate.

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
import sys
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


@pytest.fixture(autouse=True)
def _isolate_ci_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """G2/G3 (p5c4): ``cmd_submit`` now reads CV_INFRA_SUT_IMAGE / GITHUB_ACTIONS.

    A real GitHub runner (the platform CI itself runs under
    ``GITHUB_ACTIONS=true``) or a dev shell must not leak them into the wire
    assertions here — without this, every exit-2 submit test would drop an
    ``errors.json`` into the CI checkout CWD and any ambient SUT-image env
    would rewrite the pinned wire bodies."""
    monkeypatch.delenv("CV_INFRA_SUT_IMAGE", raising=False)
    monkeypatch.delenv("GITHUB_ACTIONS", raising=False)


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


def _wire_transport(
    monkeypatch: pytest.MonkeyPatch, app, *, spy: SpyASGITransport | None = None
) -> SpyASGITransport:
    """Wire ONLY the HTTP seam: real app over a spying ASGITransport + fast
    polls. The envelope adapter stays REAL (E2E section 7)."""
    spy = spy if spy is not None else SpyASGITransport(app)
    monkeypatch.setattr(
        batch,
        "_make_client",
        lambda api_base: httpx.AsyncClient(transport=spy, base_url="http://cv-infra.test"),
    )
    monkeypatch.setattr(batch, "_POLL_INTERVAL_S", 0.01)
    return spy


def _wire_cli(
    monkeypatch: pytest.MonkeyPatch,
    app,
    *,
    docs: list[dict] | None = None,
    load_error: ContractError | None = None,
) -> SpyASGITransport:
    """``_wire_transport`` + the STUBBED envelope adapter (unit isolation)."""
    spy = _wire_transport(monkeypatch, app)

    def fake_load(source):
        if load_error is not None:
            raise load_error
        assert docs is not None, "test wiring: docs or load_error required"
        return _stub_envelope(docs)

    monkeypatch.setattr(batch, "_load_envelope", fake_load)
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

        # Wire-v2 pin: raw docs verbatim + equal-length oracle_plugin_dirs
        # (stub anchors — emitted since the Wave-2 server acceptance).
        (post,) = [r for r in spy.requests if r.method == "POST"]
        body = json.loads(post.content)
        assert set(body) == {"requests", "oracle_plugin_dirs"}
        assert body["requests"] == docs
        assert body["oracle_plugin_dirs"] == [
            "/abs/consumer/scenarios0",
            "/abs/consumer/scenarios1",
        ]


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
# (6) infra reachability + pure units (mapping single source, wire gate, --api)
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


def test_wire_body_emits_oracle_plugin_dirs_equal_length(monkeypatch):
    """Wire v2 (Wave-2 flip): the anchor field rides by DEFAULT, equal-length
    with ``requests``; the module gate stays as an explicit escape hatch."""
    envelope = _stub_envelope([_request_doc(), _request_doc()])
    body = batch._wire_body(envelope)
    assert set(body) == {"requests", "oracle_plugin_dirs"}
    assert body["oracle_plugin_dirs"] == ["/abs/consumer/scenarios0", "/abs/consumer/scenarios1"]
    assert len(body["oracle_plugin_dirs"]) == len(body["requests"])  # 등길이 (wire v2)

    monkeypatch.setattr(batch, "_INCLUDE_ORACLE_PLUGIN_DIRS", False)
    assert set(batch._wire_body(envelope)) == {"requests"}  # escape hatch: omission path


def test_api_base_resolution_flag_env_default(monkeypatch):
    monkeypatch.delenv("CV_INFRA_API", raising=False)
    assert batch._resolve_api(None) == "http://127.0.0.1:8000"
    monkeypatch.setenv("CV_INFRA_API", "http://gpu-box:8000")
    assert batch._resolve_api(None) == "http://gpu-box:8000"
    assert batch._resolve_api("http://flag:9") == "http://flag:9"  # flag outranks env


# --------------------------------------------------------------------------- #
# (7) Wave-2 E2E: REAL load_envelope -> wire v2 -> REAL server admit (no stubs)
# --------------------------------------------------------------------------- #


def _write_envelope_tree(
    tmp_path: Path, scenario_texts: dict[str, str], envelope_text: str
) -> Path:
    """tmp layout mirroring the consumer shape: batch.yaml + scenarios/ beside it."""
    scenarios = tmp_path / "scenarios"
    scenarios.mkdir(exist_ok=True)
    for name, text in scenario_texts.items():
        (scenarios / name).write_text(text, encoding="utf-8")
    envelope = tmp_path / "batch.yaml"
    envelope.write_text(envelope_text, encoding="utf-8")
    return envelope


def test_e2e_real_envelope_submit_wait_roundtrip_pass(monkeypatch, tmp_path, capsys):
    """No adapter stub: envelope YAML (file refs + repeats override) -> REAL
    ``contract.envelope.load_envelope`` -> wire v2 (anchors = scenario parent
    dirs) -> REAL server admit -> fan-out -> terminal exit 0 (M8-D11의 실
    라운드트립 관찰 형태)."""
    fixture_text = _FIXTURE.read_text(encoding="utf-8")
    envelope = _write_envelope_tree(
        tmp_path,
        {"goal_a.yaml": fixture_text, "goal_b.yaml": fixture_text},
        "apiVersion: cv-infra/v1\n"
        "requests:\n"
        "  - scenario: scenarios/goal_a.yaml\n"
        "    repeats: 2\n"
        "  - scenario: scenarios/goal_b.yaml\n",
    )
    with Store(tmp_path / "cv.sqlite3") as store:
        app = create_app(store, FakeRunner(), k=2)
        spy = _wire_transport(monkeypatch, app)  # _load_envelope stays REAL

        assert main(["submit", str(envelope), "--wait"]) == EXIT_PASS
        out_lines = capsys.readouterr().out.strip().splitlines()
        envelope_id = out_lines[0]
        assert envelope_id.startswith("env-")
        assert "report_outcome=pass" in out_lines[-1]

        # Real-loader wire: verbatim scenario docs, the envelope ``repeats``
        # override applied to raw_doc, REAL parent-dir anchors (equal length).
        (post,) = [r for r in spy.requests if r.method == "POST"]
        body = json.loads(post.content)
        assert set(body) == {"requests", "oracle_plugin_dirs"}
        assert body["requests"][0]["execution_settings"]["repeats"] == 2
        assert "execution_settings" not in body["requests"][1]  # no override -> doc untouched
        anchor = str((tmp_path / "scenarios").resolve())
        assert body["oracle_plugin_dirs"] == [anchor, anchor]

        # The override drove the REAL fan-out: 2 + 1 jobs.
        assert main(["status", envelope_id]) == EXIT_PASS
        status_body = json.loads(capsys.readouterr().out)
        assert len(status_body["jobs"]) == 3


_E2E_ORACLE_MODULE = "p4c3_cli_e2e_oracle"

#: Consumer-authored custom oracle module (the ``tests/oracle_plugin_fixture``
#: CustomOracle shape) — written NEXT TO the scenario YAML, the D-1(a)
#: scenario-adjacent ``module:Class`` submission-plane form.
_E2E_ORACLE_SRC = """\
from cv_infra.oracles.base import OracleBase


class CliE2EOracle(OracleBase):
    name = "cli_e2e_fixture"
    version = "0.0.1"

    def validate_params(self, criteria):
        return None

    def evaluate(self, telemetry, criteria):
        return {"passed": True}
"""


class ProcessBoundaryTransport(SpyASGITransport):
    """SpyASGITransport that drops the custom-oracle module from
    ``sys.modules`` before every server-side request.

    In production the orchestrator is a separate process with its OWN import
    cache; in-process ASGI wiring would otherwise let the server's stage-5
    bind silently reuse the module the CLIENT-side ``load_envelope`` already
    imported — the "server re-admits via the wire anchor" claim would be
    vacuous (G-28: model the external boundary as it really is).
    """

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        sys.modules.pop(_E2E_ORACLE_MODULE, None)
        return await super().handle_async_request(request)


def test_e2e_custom_oracle_anchor_rides_wire_and_readmits(monkeypatch, tmp_path, capsys):
    """Scenario-adjacent ``module:Class`` oracle: REAL ``load_envelope``
    anchors the scenario's parent dir (client-side stage-5 bind), the anchor
    rides ``oracle_plugin_dirs``, and the REAL server re-admits through it
    (fresh import per request — ProcessBoundaryTransport) -> terminal exit 0.
    A broken anchor plumbing would surface as a server 422 -> exit 2 here."""
    doc = _request_doc()
    doc["acceptance_criteria"].append(
        {"oracle": f"{_E2E_ORACLE_MODULE}:CliE2EOracle", "params": {"anything": "goes"}}
    )
    envelope = _write_envelope_tree(
        tmp_path,
        {"custom.yaml": yaml.safe_dump(doc)},
        "apiVersion: cv-infra/v1\nrequests:\n  - scenario: scenarios/custom.yaml\n",
    )
    (tmp_path / "scenarios" / f"{_E2E_ORACLE_MODULE}.py").write_text(
        _E2E_ORACLE_SRC, encoding="utf-8"
    )
    with Store(tmp_path / "cv.sqlite3") as store:
        app = create_app(store, FakeRunner(), k=1)
        spy = _wire_transport(monkeypatch, app, spy=ProcessBoundaryTransport(app))
        try:
            rc = main(["submit", str(envelope), "--wait"])
        finally:
            sys.modules.pop(_E2E_ORACLE_MODULE, None)  # no import residue (G-29 정신)

        out_lines = capsys.readouterr().out.strip().splitlines()
        assert rc == EXIT_PASS
        assert "report_outcome=pass" in out_lines[-1]
        (post,) = [r for r in spy.requests if r.method == "POST"]
        body = json.loads(post.content)
        anchor = str((tmp_path / "scenarios").resolve())
        assert body["oracle_plugin_dirs"] == [anchor]  # the anchor the server bound with
