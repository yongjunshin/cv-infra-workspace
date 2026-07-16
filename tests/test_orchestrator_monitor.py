"""M6 operational monitoring tests (monitor.py) — DoD-P4-12/13, NEG-3.

CPU + fake runner, TestClient only (resident uvicorn/sampler = P5). Four required
gates:

1. **NEG-3 schema equality** — every projection (sub)model's field-name set is the
   pinned operational contract EXACTLY (a new field reddens the test = the review
   gate against domain leak, G-17); a live ``/monitor.json`` carries only pin keys
   and NO domain string (``job_spec`` / scenario / oracle / sut image), with a
   positive control proving the domain detail really IS in the store (비공허, G-35).
2. **P4-12 batch-8 REST surface** — a batch of 8 fanned-out jobs submitted over
   REST surfaces running jobs / queue_depth / running_k WHILE executing, then
   pass/fail/error counts + timestamps + report_outcome once done; ``/monitor``
   HTML 200 with the counts visible (NFR-MONITOR-001).
3. **sampler degrade** — NVML absent -> zero crash, gpu_reachable=false, null
   vram/util, ONE loud log (not per-tick); the real NVML read never raises.
4. **projection read-only** — the projection + both routes write NOTHING to the
   store (the boundary is 출처 분리, not a display filter — M6 §3.2).
"""

from __future__ import annotations

import asyncio
import copy
import json
import threading
import time
from pathlib import Path

import pytest
import yaml
from fastapi.testclient import TestClient

from cv_infra.orchestrator.api import create_app
from cv_infra.orchestrator.models import Job, JobResult, JobState, Verdict
from cv_infra.orchestrator.monitor import (
    MonitorHealth,
    MonitorJob,
    MonitorRequest,
    MonitorResources,
    OperationalRecord,
    ResourceHealthSampler,
    build_operational_record,
    nvml_snapshot,
)
from cv_infra.orchestrator.store import OperationalJobRow, Store

_FIXTURE = Path(__file__).parent / "fixtures" / "nova_carter_warehouse_goal.yaml"
_CANONICAL_DOC = yaml.safe_load(_FIXTURE.read_text(encoding="utf-8"))

# The pinned operational contract (task 핀 계약, verbatim). The tests assert
# EQUALITY, so any add/rename in monitor.py fails here = the NEG-3 review gate.
_PIN_TOP = {"generated_at", "health", "resources", "requests"}
_PIN_HEALTH = {"orchestrator_up", "gpu_reachable", "last_sample_at"}
_PIN_RESOURCES = {
    "queue_depth",
    "running_k",
    "over_launch_count",
    "vram_used_mib",
    "vram_total_mib",
    "gpu_util_pct",
}
_PIN_REQUEST = {
    "envelope_id",
    "request_id",
    "submitted_at",
    "envelope_status",
    "report_outcome",
    "pass_count",
    "fail_count",
    "error_count",
    "flakiness",
    "jobs",
}
_PIN_JOB = {
    "job_id",
    "repeat_index",
    "state",
    "attempt_count",
    "started_at",
    "ended_at",
    "duration_s",
    "error_category",
    "runner_exit_code",
    "infra_error",
}

# Distinctive DOMAIN strings the store's job_spec carries (positive control for the
# no-leak guard) — must never appear on the operational surface (DoD-P4-13).
_SUT_IMAGE = _CANONICAL_DOC["sut"]["image_ref"]  # "carter-sut:p2"
_DOMAIN_STRINGS = (_SUT_IMAGE, "reached_goal", "position_tolerance_m", "job_spec", "verdicts")


def _request_doc(repeats: int | None = None) -> dict:
    doc = copy.deepcopy(_CANONICAL_DOC)
    if repeats is not None:
        doc["execution_settings"] = {"repeats": repeats}
    return doc


class GatedScriptedRunner:
    """Fake runner keyed by request suffix, held behind a gate (deterministic
    in-flight window — no sleep-timing). "pass"/"fail"/"error" scripts."""

    def __init__(self, behaviors: dict[str, str] | None = None, *, gated: bool = False) -> None:
        self._behaviors = dict(behaviors or {})
        self.gate = threading.Event()
        if not gated:
            self.gate.set()

    def run(self, job: Job) -> JobResult:
        assert self.gate.wait(timeout=10.0), "test never opened the gate"
        suffix = job.request_id.rsplit("/", 1)[-1]
        behavior = self._behaviors.get(suffix, "pass")
        if behavior == "pass":
            return JobResult(job=job, state=JobState.COMPLETED, verdict=Verdict.PASS)
        if behavior == "fail":
            return JobResult(job=job, state=JobState.COMPLETED, verdict=Verdict.FAIL)
        assert behavior == "error", f"unknown script {behavior!r}"
        return JobResult(
            job=job,
            state=JobState.FAILED,
            verdict=None,
            runner_exit_code=1,
            infra_error="runner exited nonzero (fake)",
        )


def _submit(client: TestClient, documents: list[dict]) -> str:
    response = client.post("/envelopes", json={"requests": documents})
    assert response.status_code == 202, response.text
    return response.json()["envelope_id"]


def _wait_status(
    client: TestClient, envelope_id: str, status: str, timeout_s: float = 10.0
) -> None:
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        if client.get(f"/envelopes/{envelope_id}").json()["status"] == status:
            return
        time.sleep(0.02)
    raise AssertionError(f"envelope {envelope_id} never reached {status!r}")


# --------------------------------------------------------------------------- #
# (1) NEG-3: schema equality + live-surface key subset + no domain leak
# --------------------------------------------------------------------------- #


def test_projection_models_match_the_pin_exactly():
    """Field-name SET equality (not a denylist) — the review gate against a new
    field silently widening the operational surface (G-17)."""
    assert set(OperationalRecord.model_fields) == _PIN_TOP
    assert set(MonitorHealth.model_fields) == _PIN_HEALTH
    assert set(MonitorResources.model_fields) == _PIN_RESOURCES
    assert set(MonitorRequest.model_fields) == _PIN_REQUEST
    assert set(MonitorJob.model_fields) == _PIN_JOB


def test_monitor_json_keys_are_pin_only_and_no_domain_detail_leaks(tmp_path):
    with Store(tmp_path / "cv.sqlite3") as store:
        app = create_app(store, GatedScriptedRunner(), k=2)
        with TestClient(app) as client:
            envelope_id = _submit(client, [_request_doc(), _request_doc()])
            _wait_status(client, envelope_id, "completed")
            raw = client.get("/monitor.json").text
        body = json.loads(raw)

        # Response carries ONLY pin keys at every level.
        assert set(body) <= _PIN_TOP
        assert set(body["health"]) == _PIN_HEALTH
        assert set(body["resources"]) == _PIN_RESOURCES
        assert body["requests"], "operational view surfaced no requests (vacuous)"
        for req in body["requests"]:
            assert set(req) <= _PIN_REQUEST
            for jb in req["jobs"]:
                assert set(jb) <= _PIN_JOB

        # Positive control (비공허, G-35): the domain detail REALLY is in the store
        # (job_spec carries the sut image + oracle) — so its absence below is a
        # structural guard, not luck.
        specs = [j.job_spec for j in store.load_jobs()]
        assert specs and all(s is not None for s in specs)
        assert _SUT_IMAGE in json.dumps(specs)
        # NEG-3: not one domain string rides the operational surface.
        for needle in _DOMAIN_STRINGS:
            assert needle not in raw, f"domain leak: {needle!r} on the operational view"


def test_operational_job_row_omits_domain_columns_structurally():
    """The read-model row TYPE (store SELECT target) has no job_spec/oracle field —
    the leak is impossible at the source, per M6 §3.2 (구조적 강제)."""
    fields = set(OperationalJobRow.__dataclass_fields__)
    assert "job_spec" not in fields
    assert "oracle_plugin_dir" not in fields
    assert fields == {
        "request_id",
        "repeat_index",
        "state",
        "attempt_count",
        "started_at",
        "ended_at",
        "runner_exit_code",
        "infra_error",
    }


# --------------------------------------------------------------------------- #
# (2) P4-12: batch-8 REST surface — running mid-flight, counts/timestamps done
# --------------------------------------------------------------------------- #


def test_batch8_surfaces_running_then_terminal_counts(tmp_path):
    k = 3
    behaviors = {
        "r0": "pass",
        "r1": "pass",
        "r2": "pass",
        "r3": "fail",
        "r4": "fail",
        "r5": "error",
        "r6": "pass",
        "r7": "pass",
    }
    runner = GatedScriptedRunner(behaviors, gated=True)  # held so we can observe RUNNING
    with Store(tmp_path / "cv.sqlite3") as store:
        app = create_app(store, runner, k=k)
        with TestClient(app) as client:
            envelope_id = _submit(client, [_request_doc() for _ in range(8)])

            # WHILE executing: k running, the rest queued, running jobs visible.
            deadline = time.monotonic() + 10.0
            body = client.get("/monitor.json").json()
            while body["resources"]["running_k"] != k:
                assert time.monotonic() < deadline, f"never saw running_k={k}: {body['resources']}"
                time.sleep(0.01)
                body = client.get("/monitor.json").json()
            assert body["resources"]["queue_depth"] == 8 - k
            running_jobs = [
                j for req in body["requests"] for j in req["jobs"] if j["state"] == "running"
            ]
            assert len(running_jobs) == k
            assert all(j["started_at"] is not None for j in running_jobs)

            runner.gate.set()  # let every wave drain
            _wait_status(client, envelope_id, "completed")
            done = client.get("/monitor.json").json()

            assert done["resources"]["running_k"] == 0
            assert done["resources"]["queue_depth"] == 0
            by_suffix = {r["request_id"].rsplit("/", 1)[-1]: r for r in done["requests"]}
            assert len(by_suffix) == 8
            # pass/fail/error counts surface per request (1 job each).
            assert by_suffix["r0"]["pass_count"] == 1
            assert by_suffix["r3"]["fail_count"] == 1
            assert by_suffix["r5"]["error_count"] == 1 and by_suffix["r5"]["fail_count"] == 0
            assert by_suffix["r5"]["report_outcome"] == "errored"  # errored 우선 (envelope-level)
            # timestamps + duration surface on terminal jobs.
            for req in done["requests"]:
                for jb in req["jobs"]:
                    assert jb["ended_at"] is not None
                    assert jb["started_at"] is not None
                    assert jb["duration_s"] is not None and jb["duration_s"] >= 0.0
            # the errored job's operational breadcrumb rides (infra category).
            (err_job,) = by_suffix["r5"]["jobs"]
            assert err_job["error_category"] == "infra"
            assert err_job["runner_exit_code"] == 1

            # HTML one-glance view: 200 + counts visible (NFR-MONITOR-001).
            html_resp = client.get("/monitor")
            assert html_resp.status_code == 200
            assert "text/html" in html_resp.headers["content-type"]
            page = html_resp.text
            assert "operational monitor" in page
            assert "queue_depth=" in page
            assert "running_k=" in page
            assert envelope_id in page  # the request rows rendered


# --------------------------------------------------------------------------- #
# (3) sampler degrade: NVML absent -> no crash, false/null, loud once
# --------------------------------------------------------------------------- #


def test_sampler_degrades_gracefully_without_nvml(tmp_path, capsys):
    with Store(tmp_path / "cv.sqlite3") as store:
        # inject NVML-absent (this GPU-free host's default path).
        sampler = ResourceHealthSampler(store, nvml_snapshot_fn=lambda _idx: None)
        sample = sampler.sample_once()  # must NOT crash
        assert sample.gpu_reachable is False
        assert (sample.vram_used_mib, sample.vram_total_mib, sample.gpu_util_pct) == (
            None,
            None,
            None,
        )

        sampler.sample_once()  # a second degraded tick
        err = capsys.readouterr().err
        assert err.count("NVML unavailable") == 1  # LOUD, but exactly ONCE (not per-tick)

        # the projection reflects the degrade (a sample DID land — last_sample_at set).
        record = build_operational_record(store)
        assert record.health.gpu_reachable is False
        assert record.health.last_sample_at is not None
        assert record.resources.vram_used_mib is None
        assert record.resources.gpu_util_pct is None


def test_real_nvml_snapshot_never_raises_on_this_host():
    """크래시 0 for the real reader: a GPU-free host returns None, a GPU host a
    snapshot — either way no exception escapes (graceful degrade)."""
    result = nvml_snapshot()
    assert result is None or (result.vram_total_mib >= 0 and result.gpu_util_pct >= 0)


def test_sampler_run_loop_ticks_then_cancels_cleanly(tmp_path):
    with Store(tmp_path / "cv.sqlite3") as store:
        sampler = ResourceHealthSampler(store, interval_s=0.001, nvml_snapshot_fn=lambda _idx: None)

        async def _drive() -> None:
            task = asyncio.create_task(sampler.run())
            await asyncio.sleep(0.05)  # let several ticks land
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task

        asyncio.run(_drive())
        assert store.load_resource_sample() is not None  # at least one tick persisted


# --------------------------------------------------------------------------- #
# (4) projection read-only: neither build nor the routes write to the store
# --------------------------------------------------------------------------- #

_STORE_WRITE_METHODS = (
    "upsert_job",
    "record_envelope",
    "complete_envelope",
    "upsert_rollup",
    "record_domain_id",
    "release_domain_id",
    "release_all_domain_ids",
    "fail_running_envelopes",
    "record_resource_sample",
)


def test_projection_and_routes_write_nothing_to_the_store(tmp_path, monkeypatch):
    with Store(tmp_path / "cv.sqlite3") as store:
        app = create_app(store, GatedScriptedRunner(), k=2)
        with TestClient(app) as client:
            envelope_id = _submit(client, [_request_doc(repeats=2)])
            _wait_status(client, envelope_id, "completed")

            # Only NOW arm the write tripwire — populating the store above is fine;
            # the READ path afterward must not touch a single writer.
            writes: list[str] = []
            for name in _STORE_WRITE_METHODS:
                original = getattr(store, name)

                def spy(*args, _name=name, _orig=original, **kwargs):
                    writes.append(_name)
                    return _orig(*args, **kwargs)

                monkeypatch.setattr(store, name, spy)

            build_operational_record(store)  # direct projection path
            assert client.get("/monitor.json").status_code == 200  # route path
            assert client.get("/monitor").status_code == 200  # HTML route path
    assert writes == []  # zero writes on the operational read path (M6 §3.2)


# --------------------------------------------------------------------------- #
# error-category mapping (exit-code 계약 §7 #9) — the operational classification
# --------------------------------------------------------------------------- #


def _row(
    state: str, exit_code: int | None = None, infra_error: str | None = None
) -> OperationalJobRow:
    return OperationalJobRow(
        request_id="req",
        repeat_index=0,
        state=state,
        attempt_count=1,
        started_at="2026-07-16T00:00:00+00:00",
        ended_at="2026-07-16T00:00:01+00:00",
        runner_exit_code=exit_code,
        infra_error=infra_error,
    )


@pytest.mark.parametrize(
    ("row", "expected"),
    [
        (_row("queued"), None),
        (_row("running"), None),
        (_row("completed"), None),  # a completed job carries a verdict, not an error
        (_row("timeout"), "timeout"),
        (_row("failed", exit_code=2), "contract"),  # exit 2 = contract error (§7 #9)
        (_row("failed", exit_code=3), "infra"),  # exit 3 = infra error
        (_row("failed", exit_code=137), "runner-crash"),  # OOM-kill (128+9)
        (_row("failed", exit_code=139), "runner-crash"),  # segfault (128+11)
        (_row("failed", exit_code=1), "infra"),  # plain non-zero -> safe infra default
        (_row("failed", infra_error="runner seam crashed: RuntimeError: boom"), "runner-crash"),
    ],
)
def test_error_category_maps_the_exit_code_contract(row, expected):
    from cv_infra.orchestrator.monitor import _error_category

    assert _error_category(row) == expected
