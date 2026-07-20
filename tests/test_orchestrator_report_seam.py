"""p5c2 report/baseline seam (M3 api ``_persist_terminal`` + ``GET .../report``).

CPU + fake runner, TestClient only. Proves the TERMINAL seam assembles the M4
``VerificationReport`` server-side (``report.aggregate.build_report`` — M4 code
CALLED, never modified), persists it (store v7, restart-surviving), and advances
the request-level regression baseline (``report.baseline.update_baseline``) AFTER
assembly — the 순서 불변식 (a request never regresses against itself; p5c1 읽기/쓰기
분리의 취지). Covers the 5 core edges (task): 순서 불변식, errored-skip, restart
survival, crash marker, C-1 internal-store-only — plus the route status codes.
Stdlib + pytest.
"""

from __future__ import annotations

import copy
import threading
import time
from pathlib import Path

import yaml
from fastapi.testclient import TestClient

from cv_infra.orchestrator.api import create_app
from cv_infra.orchestrator.fake_runner import FakeRunner
from cv_infra.orchestrator.models import Job, JobResult, JobState, Verdict
from cv_infra.orchestrator.store import Store
from cv_infra.report.baseline import update_baseline

# Platform copy of the consumer scenario — admitted by the REAL 6-stage loader.
_FIXTURE = Path(__file__).parent / "fixtures" / "nova_carter_warehouse_goal.yaml"
_CANONICAL_DOC = yaml.safe_load(_FIXTURE.read_text(encoding="utf-8"))


def _doc(*, sut_ref: str | None = None, repeats: int | None = None) -> dict:
    """A valid request doc, optionally with a specific SUT / repeats override.

    The SUT is the ONLY identity axis excluded from ``request_identity_key`` — so
    two docs differing only in ``sut_ref`` map to the SAME baseline key (the whole
    point of the regression contract, REQ-REPORT-002)."""
    doc = copy.deepcopy(_CANONICAL_DOC)
    if sut_ref is not None:
        doc["sut"] = {"image_ref": sut_ref}
    if repeats is not None:
        doc["execution_settings"] = {"repeats": repeats}
    return doc


class _ScriptedRunner:
    """Fake runner keyed by ``<request suffix>[:repeat_index]`` (envelope ids are
    random). Behaviors: ``pass`` (COMPLETED+PASS, default) · ``fail``
    (COMPLETED+FAIL) · ``error`` (FAILED, verdict-less = infra outcome)."""

    def __init__(self, behaviors: dict[str, str] | None = None) -> None:
        self._behaviors = dict(behaviors or {})

    def run(self, job: Job) -> JobResult:
        suffix = job.request_id.rsplit("/", 1)[-1]
        behavior = self._behaviors.get(
            f"{suffix}:{job.repeat_index}", self._behaviors.get(suffix, "pass")
        )
        if behavior == "pass":
            return JobResult(job=job, state=JobState.COMPLETED, verdict=Verdict.PASS)
        if behavior == "fail":
            return JobResult(job=job, state=JobState.COMPLETED, verdict=Verdict.FAIL)
        assert behavior == "error", f"unknown script {behavior!r}"
        return JobResult(job=job, state=JobState.FAILED, verdict=None)


def _submit(client: TestClient, documents: list[dict]) -> str:
    response = client.post("/envelopes", json={"requests": documents})
    assert response.status_code == 202, response.text
    return response.json()["envelope_id"]


def _wait_completed(client: TestClient, envelope_id: str, timeout_s: float = 10.0) -> dict:
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        if client.get(f"/envelopes/{envelope_id}").json()["status"] == "completed":
            return client.get(f"/envelopes/{envelope_id}/report").json()
        time.sleep(0.02)
    raise AssertionError(f"envelope {envelope_id} did not complete within {timeout_s}s")


# --------------------------------------------------------------------------- #
# (1) 200 shape: server-assembled VerificationReport, trigger_source recorded
# --------------------------------------------------------------------------- #


def test_report_200_is_the_assembled_verification_report(tmp_path):
    with Store(tmp_path / "cv.sqlite3") as store:
        app = create_app(store, FakeRunner(), k=2)
        with TestClient(app) as client:
            envelope_id = _submit(client, [_doc(repeats=2)])
            report = _wait_completed(client, envelope_id)

            assert report["apiVersion"] == "cv-infra/v1"
            assert report["kind"] == "VerificationReport"
            assert report["envelope_id"] == envelope_id
            assert report["trigger_source"] == "ci-cd"  # recorded default (재도출 아님)
            assert report["summary"]["report_outcome"] == "pass"
            (row,) = report["matrix"]
            assert row["sut_ref"] == "carter-sut:p2"  # from the request wire dump
            assert row["rollup"]["repeats"] == 2 and row["rollup"]["verdict"] == "pass"
            assert row["regression"]["status"] == "no-baseline"  # skip = normal
            # control plane carries no domain metrics/artifacts -> empty, shape intact.
            assert row["metrics"] == {}
            assert row["artifacts"]["selected"][0]["role"] == "representative-pass"


# --------------------------------------------------------------------------- #
# (2) 순서 불변식: report reads PRE-advance baseline; advance happens AFTER
# --------------------------------------------------------------------------- #


def test_order_invariant_first_pass_seeds_then_second_fail_regresses(tmp_path):
    # Two runs against the SAME cv-infra deployment (shared store) differing ONLY
    # in SUT — the C-1 "same instance, repeated runs" baseline scenario.
    db = tmp_path / "cv.sqlite3"
    with Store(db) as store:
        # run 1: PASS. The report must read 'no-baseline' (nothing seeded yet) —
        # if advance ran BEFORE assembly, the request would compare against itself.
        app1 = create_app(store, FakeRunner(), k=1)
        with TestClient(app1) as client:
            report1 = _wait_completed(client, _submit(client, [_doc(sut_ref="carter-sut:old")]))
        row1 = report1["matrix"][0]
        assert row1["regression"]["status"] == "no-baseline"
        ikey = row1["request_identity_key"]
        # ...but the advance DID happen after assembly: the store now holds a
        # passing baseline for this identity (established from run 1).
        seeded = store.load_baseline(ikey)
        assert seeded is not None and seeded.verdict == "pass"
        assert seeded.sut_ref == "carter-sut:old"

        # run 2: FAIL, same identity (only SUT changed). Now the report regresses
        # against run 1's seeded baseline...
        app2 = create_app(store, _ScriptedRunner({"r0": "fail"}), k=1)
        with TestClient(app2) as client:
            report2 = _wait_completed(client, _submit(client, [_doc(sut_ref="carter-sut:new")]))
        reg = report2["matrix"][0]["regression"]
        assert reg["status"] == "regressed"
        assert reg["baseline_sut_ref"] == "carter-sut:old"  # NFR-REPORT-001 식별성
        assert report2["baseline_summary"]["regressed"] == 1

        # ...and the FAIL did NOT overwrite the good baseline (fail-no-overwrite).
        after = store.load_baseline(ikey)
        assert after.verdict == "pass" and after.sut_ref == "carter-sut:old"
        assert report2["matrix"][0]["request_identity_key"] == ikey  # same key across runs


# --------------------------------------------------------------------------- #
# (3) errored envelope: report_outcome=errored + that request never baselined
# --------------------------------------------------------------------------- #


def test_errored_request_is_reported_errored_and_not_baselined(tmp_path):
    with Store(tmp_path / "cv.sqlite3") as store:
        app = create_app(store, _ScriptedRunner({"r0": "error"}), k=1)
        with TestClient(app) as client:
            report = _wait_completed(client, _submit(client, [_doc(sut_ref="carter-sut:e")]))
        assert report["summary"]["report_outcome"] == "errored"  # exit-3 priority
        ikey = report["matrix"][0]["request_identity_key"]
        # update_baseline's verdict=None path is a no-op: an infra outcome is never
        # a reference (best-effort, REQ-REPORT-005) — no baseline row appears.
        assert store.load_baseline(ikey) is None


# --------------------------------------------------------------------------- #
# (4) restart survival: the persisted report is served store-only after a restart
# --------------------------------------------------------------------------- #


def test_report_survives_orchestrator_restart(tmp_path):
    db = tmp_path / "cv.sqlite3"
    with Store(db) as store:
        app = create_app(store, _ScriptedRunner({"r0": "fail"}), k=1)
        with TestClient(app) as client:
            envelope_id = _submit(client, [_doc(repeats=2)])
            before = _wait_completed(client, envelope_id)
    # Full restart: a NEW Store object + a NEW app process that never saw the
    # envelope serves the SAME report verbatim from store v7 (never re-assembled).
    with Store(db) as reopened:
        restarted = create_app(reopened, FakeRunner(), k=1)
        with TestClient(restarted) as client:
            response = client.get(f"/envelopes/{envelope_id}/report")
            assert response.status_code == 200
            assert response.json() == before  # byte-identical durable twin
            assert client.get("/envelopes/env-nope/report").status_code == 404


# --------------------------------------------------------------------------- #
# (5) crash marker: no report -> 409 supervision-error, baseline table untouched
# --------------------------------------------------------------------------- #


def test_crashed_envelope_report_is_409_and_writes_no_baseline(tmp_path):
    # The seeded state is EXACTLY what a live supervision crash / restart-reconcile
    # produces (complete_envelope(error=...)) — a crashed envelope assembles no
    # report and writes no baseline (the _persist_terminal error branch returns
    # early), so a pre-existing baseline stays untouched.
    db = tmp_path / "cv.sqlite3"
    with Store(db) as store:
        store.record_envelope("env-crash", ["env-crash/r0"], [None])
        store.complete_envelope("env-crash", error="orchestrator restarted mid-envelope")
        # a known baseline that must remain byte-identical after the route call.
        update_baseline(
            store,
            request_identity_key="sha256:seeded",
            sut_ref="carter-sut:seed",
            verdict="pass",
            established_at="2026-07-10T00:00:00+00:00",
        )
    with Store(db) as reopened:
        app = create_app(reopened, FakeRunner(), k=1)
        with TestClient(app) as client:
            response = client.get("/envelopes/env-crash/report")
            assert response.status_code == 409
            assert response.json()["detail"] == {
                "reason": "supervision-error",
                "error": "orchestrator restarted mid-envelope",
            }
        assert reopened.load_report("env-crash") is None
        untouched = reopened.load_baseline("sha256:seeded")
        assert (untouched.verdict, untouched.sut_ref) == ("pass", "carter-sut:seed")


# --------------------------------------------------------------------------- #
# (6) route status codes: 404 unknown, 409 not-terminal while running
# --------------------------------------------------------------------------- #


def test_report_404_unknown_and_409_not_terminal_while_running(tmp_path):
    class _GateRunner:
        """Blocks every job until the test opens the gate (deterministic in-flight
        window — no sleep-timing)."""

        def __init__(self) -> None:
            self.gate = threading.Event()

        def run(self, job: Job) -> JobResult:
            assert self.gate.wait(timeout=10.0), "test never opened the gate"
            return JobResult(job=job, state=JobState.COMPLETED, verdict=Verdict.PASS)

    runner = _GateRunner()
    with Store(tmp_path / "cv.sqlite3") as store:
        app = create_app(store, runner, k=1)
        with TestClient(app) as client:
            assert client.get("/envelopes/env-nope/report").status_code == 404
            envelope_id = _submit(client, [_doc()])
            in_flight = client.get(f"/envelopes/{envelope_id}/report")
            assert in_flight.status_code == 409
            assert in_flight.json()["detail"] == {"reason": "not-terminal", "status": "running"}
            runner.gate.set()
            report = _wait_completed(client, envelope_id)
            assert report["summary"]["report_outcome"] == "pass"  # 200 once terminal
