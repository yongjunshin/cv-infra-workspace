"""REST submit-surface tests (M3 api.py) — REQ-INTAKE-001, M3 §7, D-1 wire.

CPU + fake 러너, TestClient only (resident uvicorn = P5): submit -> background
supervision -> status roundtrip for all three ``report_outcome`` values, the
structured-422 admit path (M1 ContractError fields, zero traceback, zero job
non-propagation), the frozen ``RequestRollup`` wire shape, SQLite job
persistence through the API path, and the wire-v2 ``oracle_plugin_dirs``
per-request stage-5 anchor (p4c3 — section d). The TestClient is always
entered as a context manager — the app's background drive task lives on the
client's portal loop, which only persists inside the ``with`` block.
"""

from __future__ import annotations

import copy
import sys
import threading
import time
from pathlib import Path

import pytest
import yaml
from fastapi.testclient import TestClient

from cv_infra.orchestrator.api import create_app, report_outcome_of
from cv_infra.orchestrator.fake_runner import FakeRunner
from cv_infra.orchestrator.models import Job, JobResult, JobState, Verdict
from cv_infra.orchestrator.store import Store

# Canonical M1-valid request document (the platform copy of the consumer
# scenario — admitted by the real 6-stage loader gate, no test-local schema).
_FIXTURE = Path(__file__).parent / "fixtures" / "nova_carter_warehouse_goal.yaml"
_CANONICAL_DOC = yaml.safe_load(_FIXTURE.read_text(encoding="utf-8"))

_ROLLUP_WIRE_KEYS = {"request_id", "verdicts", "flakiness", "verdict"}  # p4c1 frozen shape


def _request_doc(repeats: int | None = None) -> dict:
    doc = copy.deepcopy(_CANONICAL_DOC)
    if repeats is not None:
        doc["execution_settings"] = {"repeats": repeats}
    return doc


class SuffixScriptedRunner:
    """Fake runner keyed by ``<request suffix>[:repeat_index]`` — envelope ids
    are random, so scripts key on the API's stable ``/rN`` suffix instead.

    Behaviors: "pass" (COMPLETED+PASS, default) · "fail-verdict"
    (COMPLETED+FAIL) · "exit-nonzero" (FAILED, no verdict — infra outcome).
    """

    def __init__(self, behaviors: dict[str, str] | None = None) -> None:
        self._behaviors = dict(behaviors or {})

    def run(self, job: Job) -> JobResult:
        suffix = job.request_id.rsplit("/", 1)[-1]
        behavior = self._behaviors.get(
            f"{suffix}:{job.repeat_index}", self._behaviors.get(suffix, "pass")
        )
        if behavior == "pass":
            return JobResult(job=job, state=JobState.COMPLETED, verdict=Verdict.PASS)
        if behavior == "fail-verdict":
            return JobResult(job=job, state=JobState.COMPLETED, verdict=Verdict.FAIL)
        assert behavior == "exit-nonzero", f"unknown script {behavior!r}"
        return JobResult(job=job, state=JobState.FAILED, verdict=None)


def _submit(client: TestClient, documents: list[dict]) -> str:
    response = client.post("/envelopes", json={"requests": documents})
    assert response.status_code == 202, response.text
    return response.json()["envelope_id"]


def _wait_completed(client: TestClient, envelope_id: str, timeout_s: float = 10.0) -> dict:
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        body = client.get(f"/envelopes/{envelope_id}").json()
        if body["status"] == "completed":
            return body
        time.sleep(0.02)
    raise AssertionError(f"envelope {envelope_id} did not complete within {timeout_s}s")


# --------------------------------------------------------------------------- #
# (a) submit -> fake-runner completion -> status roundtrip (pass path)
# --------------------------------------------------------------------------- #


def test_submit_roundtrip_pass_with_per_request_repeats(tmp_path):
    with Store(tmp_path / "cv.sqlite3") as store:
        app = create_app(store, FakeRunner(), k=2)
        with TestClient(app) as client:
            envelope_id = _submit(client, [_request_doc(repeats=2), _request_doc(repeats=3)])
            body = _wait_completed(client, envelope_id)

            assert body["envelope_id"] == envelope_id
            assert len(body["jobs"]) == 5  # per-request repeats: 2 + 3 (Σ_i repeats(i))
            assert all(j["state"] == "completed" for j in body["jobs"])
            assert all(j["attempt_count"] == 1 for j in body["jobs"])
            by_request: dict[str, list[int]] = {}
            for job in body["jobs"]:
                by_request.setdefault(job["request_id"], []).append(job["repeat_index"])
            assert sorted(len(v) for v in by_request.values()) == [2, 3]
            for indices in by_request.values():  # repeat_index unique 0..r-1 per request
                assert sorted(indices) == list(range(len(indices)))

            assert len(body["rollups"]) == 2  # one per request, submission order
            assert [len(r["verdicts"]) for r in body["rollups"]] == [2, 3]
            for rollup in body["rollups"]:
                assert set(rollup) == _ROLLUP_WIRE_KEYS  # frozen shape (M4 consume)
                assert rollup["verdict"] == "pass"
                assert rollup["flakiness"] == 0.0
                assert all(v == "pass" for v in rollup["verdicts"])
            assert body["report_outcome"] == "pass"

        # API path persisted every job through the store (REQ-ORCH-011).
        persisted = store.load_jobs()
        assert len(persisted) == 5
        assert all(j.state is JobState.COMPLETED for j in persisted)


def test_status_is_running_with_null_outcome_until_completed(tmp_path):
    class GateRunner:
        """Blocks every job until the test opens the gate (deterministic
        in-flight window — no sleep-timing)."""

        def __init__(self) -> None:
            self.gate = threading.Event()

        def run(self, job: Job) -> JobResult:
            assert self.gate.wait(timeout=10.0), "test never opened the gate"
            return JobResult(job=job, state=JobState.COMPLETED, verdict=Verdict.PASS)

    runner = GateRunner()
    with Store(tmp_path / "cv.sqlite3") as store:
        app = create_app(store, runner, k=1)
        with TestClient(app) as client:
            envelope_id = _submit(client, [_request_doc()])
            in_flight = client.get(f"/envelopes/{envelope_id}").json()
            assert in_flight["status"] == "running"
            assert in_flight["report_outcome"] is None  # 미완료 구분은 status 필드
            (rollup,) = in_flight["rollups"]  # shape present even while running
            assert set(rollup) == _ROLLUP_WIRE_KEYS
            assert rollup["verdicts"] == [] and rollup["verdict"] is None
            runner.gate.set()
            assert _wait_completed(client, envelope_id)["report_outcome"] == "pass"


# --------------------------------------------------------------------------- #
# (b) report_outcome: fail / errored (errored 우선 > fail) + flakiness surface
# --------------------------------------------------------------------------- #


def test_report_outcome_fail_when_any_request_fails(tmp_path):
    with Store(tmp_path / "cv.sqlite3") as store:
        app = create_app(store, SuffixScriptedRunner({"r1": "fail-verdict"}), k=2)
        with TestClient(app) as client:
            envelope_id = _submit(client, [_request_doc(), _request_doc()])
            body = _wait_completed(client, envelope_id)
            assert body["report_outcome"] == "fail"
            verdicts = {r["request_id"].rsplit("/", 1)[-1]: r["verdict"] for r in body["rollups"]}
            assert verdicts == {"r0": "pass", "r1": "fail"}


def test_report_outcome_errored_outranks_fail(tmp_path):
    # r0 errors (exit != 0, no verdict) AND r1 fails: errored>0 must win —
    # infra noise never reads as a domain FAIL (M8 exit-3 우선 매핑의 입력).
    runner = SuffixScriptedRunner({"r0": "exit-nonzero", "r1": "fail-verdict"})
    with Store(tmp_path / "cv.sqlite3") as store:
        app = create_app(store, runner, k=2)
        with TestClient(app) as client:
            envelope_id = _submit(client, [_request_doc(), _request_doc()])
            body = _wait_completed(client, envelope_id)
            assert body["report_outcome"] == "errored"
            by_suffix = {r["request_id"].rsplit("/", 1)[-1]: r for r in body["rollups"]}
            assert by_suffix["r0"]["verdict"] is None  # verdict-less rollup = infra
            assert by_suffix["r1"]["verdict"] == "fail"
            states = {j["request_id"].rsplit("/", 1)[-1]: j["state"] for j in body["jobs"]}
            assert states == {"r0": "failed", "r1": "completed"}


def test_flaky_repeats_surface_flakiness_separately(tmp_path):
    # 1-of-3 repeats fails: any-fail verdict + flakiness 1/3 through the API.
    runner = SuffixScriptedRunner({"r0:1": "fail-verdict"})
    with Store(tmp_path / "cv.sqlite3") as store:
        app = create_app(store, runner, k=2)
        with TestClient(app) as client:
            envelope_id = _submit(client, [_request_doc(repeats=3)])
            body = _wait_completed(client, envelope_id)
            (rollup,) = body["rollups"]
            assert sorted(rollup["verdicts"]) == ["fail", "pass", "pass"]
            assert rollup["verdict"] == "fail"
            assert rollup["flakiness"] == 1 / 3
            assert body["report_outcome"] == "fail"


def test_report_outcome_of_priority_unit():
    def result(verdict: Verdict | None) -> JobResult:
        state = JobState.COMPLETED if verdict is not None else JobState.FAILED
        return JobResult(job=Job("r", 0), state=state, verdict=verdict)

    assert report_outcome_of([result(Verdict.PASS)]) == "pass"
    assert report_outcome_of([result(Verdict.PASS), result(Verdict.FAIL)]) == "fail"
    assert report_outcome_of([result(Verdict.FAIL), result(None)]) == "errored"


# --------------------------------------------------------------------------- #
# (c) admit rejection: structured 422, traceback 0, zero-job non-propagation
# --------------------------------------------------------------------------- #


def test_invalid_request_is_structured_422_and_creates_zero_jobs(tmp_path):
    bad = _request_doc()
    del bad["sut"]  # violates the REQ-INTAKE-006 triad -> M1 schema reject
    with Store(tmp_path / "cv.sqlite3") as store:
        app = create_app(store, FakeRunner(), k=2)
        with TestClient(app) as client:
            response = client.post("/envelopes", json={"requests": [_request_doc(), bad]})
            assert response.status_code == 422  # never a 500
            (error,) = response.json()["detail"]["errors"]
            # M1 ContractError annotation shape (8 verbatim keys) with the
            # failing request indexed in source_path and the field named.
            assert set(error) == {
                "field_path",
                "expected",
                "got",
                "example",
                "doc_link",
                "source_path",
                "source_line",
                "source_col",
            }
            assert error["field_path"] == "sut"
            assert error["source_path"] == "requests[1]"
            assert "Traceback" not in response.text
        # All-or-nothing (비전파): the VALID sibling request also produced no jobs.
        assert store.load_jobs() == []


def test_multiple_bad_requests_each_get_an_error_entry(tmp_path):
    bad_criteria = _request_doc()
    bad_criteria["acceptance_criteria"] = []
    bad_repeats = _request_doc(repeats=0)  # ge=1 violation at the M1 schema
    with Store(tmp_path / "cv.sqlite3") as store:
        app = create_app(store, FakeRunner(), k=2)
        with TestClient(app) as client:
            response = client.post("/envelopes", json={"requests": [bad_criteria, bad_repeats]})
            assert response.status_code == 422
            errors = response.json()["detail"]["errors"]
            assert [e["source_path"] for e in errors] == ["requests[0]", "requests[1]"]
            assert errors[0]["field_path"] == "acceptance_criteria"
            assert errors[1]["field_path"] == "execution_settings.repeats"
            assert "Traceback" not in response.text
        assert store.load_jobs() == []


def test_wire_wrapper_violations_are_structured_422(tmp_path):
    with Store(tmp_path / "cv.sqlite3") as store:
        app = create_app(store, FakeRunner(), k=2)
        with TestClient(app) as client:
            for payload in ({}, {"requests": []}, {"requests": ["not-an-object"]}, [1, 2]):
                response = client.post("/envelopes", json=payload)
                assert response.status_code == 422, payload
                (error,) = response.json()["detail"]["errors"]
                assert error["expected"]  # structured, self-describing
                assert "Traceback" not in response.text
            malformed = client.post(
                "/envelopes", content=b"{not json", headers={"content-type": "application/json"}
            )
            assert malformed.status_code == 422
            assert "Traceback" not in malformed.text
        assert store.load_jobs() == []


def test_unknown_envelope_is_404(tmp_path):
    with Store(tmp_path / "cv.sqlite3") as store:
        app = create_app(store, FakeRunner(), k=1)
        with TestClient(app) as client:
            assert client.get("/envelopes/env-nope").status_code == 404


# --------------------------------------------------------------------------- #
# (d) wire v2: oracle_plugin_dirs — per-request stage-5 anchor (p4c3)
# --------------------------------------------------------------------------- #

# The EXISTING runner-side plugin fixture dir (tests/fixtures/) stands in for
# a consumer scenario dir; its module is only importable WITH the anchor.
_PLUGIN_DIR = str(Path(__file__).parent / "fixtures")
_PLUGIN_MODULE = "custom_oracle_plugin"
_CUSTOM_ORACLE = f"{_PLUGIN_MODULE}:ParamVerdictOracle"


def _custom_oracle_doc() -> dict:
    doc = _request_doc()
    doc["acceptance_criteria"] = [
        {"oracle": _CUSTOM_ORACLE, "params": {"custom_should_pass": True}}
    ]
    return doc


@pytest.fixture()
def plugin_import_state():
    """No cached-import bleed-through: an earlier admit must not make the
    anchor-less negative pass vacuously (test_runner_oracle_plugins idiom)."""
    sys.modules.pop(_PLUGIN_MODULE, None)
    yield
    sys.modules.pop(_PLUGIN_MODULE, None)


def test_custom_oracle_admits_with_plugin_dir_anchor(tmp_path, plugin_import_state):
    # Mixed envelope: r0 anchor-less (entry-point oracles, null item leaves it
    # unchanged) + r1 anchored to the fixture dir -> both admit, jobs fan out.
    with Store(tmp_path / "cv.sqlite3") as store:
        app = create_app(store, FakeRunner(), k=2)
        with TestClient(app) as client:
            response = client.post(
                "/envelopes",
                json={
                    "requests": [_request_doc(), _custom_oracle_doc()],
                    "oracle_plugin_dirs": [None, _PLUGIN_DIR],
                },
            )
            assert response.status_code == 202, response.text
            body = _wait_completed(client, response.json()["envelope_id"])
            assert len(body["jobs"]) == 2  # one job per request (default repeats)
            assert body["report_outcome"] == "pass"
        assert len(store.load_jobs()) == 2


def test_custom_oracle_without_anchor_is_structured_422(tmp_path, plugin_import_state):
    # Positive control (p4c2 실측 재확인): the SAME document admits above WITH
    # the anchor and rejects without it — the anchor is load-bearing.
    with Store(tmp_path / "cv.sqlite3") as store:
        app = create_app(store, FakeRunner(), k=2)
        with TestClient(app) as client:
            response = client.post("/envelopes", json={"requests": [_custom_oracle_doc()]})
            assert response.status_code == 422
            (error,) = response.json()["detail"]["errors"]
            assert error["field_path"] == "acceptance_criteria[0].oracle"
            assert error["source_path"] == "requests[0]"
            assert "Traceback" not in response.text
        assert store.load_jobs() == []


def test_oracle_plugin_dirs_wrapper_violations_are_structured_422(tmp_path):
    # 등길이 위반(짧게/길게) · 리스트 아님 · 항목이 str/절대경로 아님 — 전부
    # 구조화 422, 잡 0 (기존 wrapper-위반 관용구와 동일 shape).
    with Store(tmp_path / "cv.sqlite3") as store:
        app = create_app(store, FakeRunner(), k=2)
        with TestClient(app) as client:
            for dirs in ([], [None, None], "not-a-list", [123], ["relative/path"]):
                response = client.post(
                    "/envelopes", json={"requests": [_request_doc()], "oracle_plugin_dirs": dirs}
                )
                assert response.status_code == 422, dirs
                (error,) = response.json()["detail"]["errors"]
                assert error["field_path"].startswith("oracle_plugin_dirs")
                assert error["expected"]  # structured, self-describing
                assert "Traceback" not in response.text
        assert store.load_jobs() == []  # 비전파: no job from any rejected envelope


def test_oracle_plugin_dirs_ride_jobs_to_the_runner_seam(tmp_path, plugin_import_state):
    # D-1 wiring #3 잔여 반쪽 (p4c4): the per-request anchor rides each fanned-out
    # Job so the runner seam receives it (production glue then hands it to
    # run_job(oracle_plugin_dir=...)) — runner-limited by construction, and
    # persisted with the job (a restored/retried job keeps its anchor).
    class AnchorRecordingRunner:
        def __init__(self) -> None:
            self.seen: dict[str, str | None] = {}

        def run(self, job: Job) -> JobResult:
            self.seen[job.request_id.rsplit("/", 1)[-1]] = job.oracle_plugin_dir
            return JobResult(job=job, state=JobState.COMPLETED, verdict=Verdict.PASS)

    runner = AnchorRecordingRunner()
    with Store(tmp_path / "cv.sqlite3") as store:
        app = create_app(store, runner, k=2)
        with TestClient(app) as client:
            response = client.post(
                "/envelopes",
                json={
                    "requests": [_request_doc(), _custom_oracle_doc()],
                    "oracle_plugin_dirs": [None, _PLUGIN_DIR],
                },
            )
            assert response.status_code == 202, response.text
            _wait_completed(client, response.json()["envelope_id"])
        assert runner.seen == {"r0": None, "r1": _PLUGIN_DIR}
        anchors = {j.request_id.rsplit("/", 1)[-1]: j.oracle_plugin_dir for j in store.load_jobs()}
        assert anchors == {"r0": None, "r1": _PLUGIN_DIR}  # persisted, restart-safe


# --------------------------------------------------------------------------- #
# (e) p4c4 영속: registry/rollups survive a restart; restart reads stay honest
# --------------------------------------------------------------------------- #


def test_envelope_status_survives_orchestrator_restart(tmp_path):
    # Full restart simulation: a NEW Store object (new connection) + a NEW app
    # process that never saw the envelope must serve the SAME status body from
    # the persisted registry/jobs/rollups (never recomputed from results).
    db = tmp_path / "cv.sqlite3"
    with Store(db) as store:
        app = create_app(store, SuffixScriptedRunner({"r1:1": "fail-verdict"}), k=2)
        with TestClient(app) as client:
            envelope_id = _submit(client, [_request_doc(), _request_doc(repeats=3)])
            before = _wait_completed(client, envelope_id)
            assert before["report_outcome"] == "fail"
    with Store(db) as reopened:  # 재기동
        restarted_app = create_app(reopened, FakeRunner(), k=2)
        with TestClient(restarted_app) as client:
            response = client.get(f"/envelopes/{envelope_id}")
            assert response.status_code == 200
            after = response.json()
            assert after == before  # jobs order, rollups (incl. flakiness), outcome — all equal
            assert client.get("/envelopes/env-nope").status_code == 404  # unknown stays 404


def test_restart_read_of_inflight_envelope_is_running_until_reconciled(tmp_path):
    # An envelope the crash left 'running' reads honestly as running (jobs +
    # empty rollup shapes, outcome null) until reconcile_at_restart marks it.
    db = tmp_path / "cv.sqlite3"
    with Store(db) as store:
        store.record_envelope("env-live", ["env-live/r0"], [None])
        store.upsert_job(Job(request_id="env-live/r0", repeat_index=0, state=JobState.RUNNING))
    with Store(db) as reopened:
        app = create_app(reopened, FakeRunner(), k=1)
        with TestClient(app) as client:
            body = client.get("/envelopes/env-live").json()
            assert body["status"] == "running"
            assert body["report_outcome"] is None
            (job,) = body["jobs"]
            assert (job["request_id"], job["state"]) == ("env-live/r0", "running")
            (rollup,) = body["rollups"]  # empty shape, frozen keys — same as live path
            assert set(rollup) == _ROLLUP_WIRE_KEYS
            assert rollup["verdicts"] == [] and rollup["verdict"] is None


def test_restart_marked_envelope_reads_loud_500(tmp_path):
    # After reconcile_at_restart the marker surfaces exactly like an in-memory
    # supervision crash: a loud 500, never a silent forever-'running'.
    db = tmp_path / "cv.sqlite3"
    with Store(db) as store:
        store.record_envelope("env-crashed", ["env-crashed/r0"], [None])
        assert store.fail_running_envelopes("orchestrator restarted mid-envelope") == 1
    with Store(db) as reopened:
        app = create_app(reopened, FakeRunner(), k=1)
        with TestClient(app) as client:
            response = client.get("/envelopes/env-crashed")
            assert response.status_code == 500
            assert "restarted" in response.json()["detail"]
