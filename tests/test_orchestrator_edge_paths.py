"""Control-plane EDGE paths — the loud guards and the graceful degradations (p8c2 T2).

Every branch here is one the happy-path suites never take: a guard that must FIRE
(and say why), a degradation that must NOT crash the process, or a lifecycle hook
that only runs at app startup/shutdown. They are unit-level on purpose — a guard
nobody can reach from a test is a guard nobody knows still works (G-59).

Two host-independence notes, both deliberate:

* the NVML paths are driven by an INJECTED fake ``pynvml`` module, never by this
  host's GPU. ``tests/test_orchestrator_monitor.py::test_real_nvml_snapshot_never_
  raises_on_this_host`` keeps the real-host smoke; it takes a DIFFERENT branch on a
  GPU box than on a CPU box, so it cannot be what pins either one (p8c2 발견 ①).
* no test here binds a socket, pulls an image or touches a docker daemon.
"""

from __future__ import annotations

import asyncio
import contextlib
import sqlite3
import sys
import time
import types
from collections import deque
from pathlib import Path

import pytest
import yaml
from fastapi import FastAPI
from fastapi.testclient import TestClient

from cv_infra.orchestrator import serve
from cv_infra.orchestrator.allocator import DomainIdAllocator
from cv_infra.orchestrator.api import (
    _AppState,
    _drive,
    _EnvelopeRecord,
    create_app,
)
from cv_infra.orchestrator.fake_runner import FakeRunner
from cv_infra.orchestrator.models import Job, JobState
from cv_infra.orchestrator.monitor import (
    NvmlSnapshot,
    ResourceHealthSampler,
    _duration_s,
    attach_sampler,
    nvml_snapshot,
)
from cv_infra.orchestrator.scheduler import PynvmlVramGauge, Scheduler
from cv_infra.orchestrator.store import ENVELOPE_COMPLETED, Store

# --------------------------------------------------------------------------- #
# Fake pynvml (module object, injected into sys.modules) — the ONE lever both
# NVML consumers use. `import pynvml` inside the function bodies picks it up, so
# neither consumer needs a seam and neither test needs a GPU.
# --------------------------------------------------------------------------- #


class _NVMLError(Exception):
    """Stand-in for ``pynvml.NVMLError`` (the scheduler gauge catches it BY NAME)."""


def _fake_pynvml(
    *,
    free_bytes: int = 12 * 1024**3,
    used_bytes: int = 4 * 1024**3,
    total_bytes: int = 16 * 1024**3,
    util_pct: int = 37,
    init_error: Exception | None = None,
) -> types.ModuleType:
    module = types.ModuleType("pynvml")
    calls: list[str] = []
    module.calls = calls  # the shutdown-always assertion reads this
    module.NVMLError = _NVMLError

    def nvml_init() -> None:
        calls.append("init")
        if init_error is not None:
            raise init_error

    module.nvmlInit = nvml_init
    module.nvmlShutdown = lambda: calls.append("shutdown")
    module.nvmlDeviceGetHandleByIndex = lambda index: ("handle", index)
    module.nvmlDeviceGetMemoryInfo = lambda handle: types.SimpleNamespace(
        free=free_bytes, used=used_bytes, total=total_bytes
    )
    module.nvmlDeviceGetUtilizationRates = lambda handle: types.SimpleNamespace(gpu=util_pct)
    return module


# --------------------------------------------------------------------------- #
# (a) scheduler.PynvmlVramGauge — the admission gauge's TWO outcomes (R-NV)
# --------------------------------------------------------------------------- #


def test_vram_gauge_reports_free_mib_and_always_shuts_nvml_down(monkeypatch):
    """The gauge returns NVML free bytes as MiB and closes NVML on the way out.

    MiB (1024**2), not MB — the divisor is the one `compute_k` is fed with
    (scheduler module docstring: every VRAM figure in this system is NVML bytes
    // 1024**2), so an operator's measured MiB and the guard's number are the
    same quantity.
    """
    monkeypatch.setitem(sys.modules, "pynvml", _fake_pynvml(free_bytes=9000 * 1024**2))
    gauge = PynvmlVramGauge()

    assert gauge.available_vram_mb() == 9000.0
    assert sys.modules["pynvml"].calls == ["init", "shutdown"]


def test_vram_gauge_failure_is_loud_and_names_the_missing_capability(monkeypatch):
    """R-NV: an NVML failure RAISES — silently disabling the 2nd guard would neuter
    over-launch protection (NFR-ORCH-003) with nobody noticing. The message must
    point at the deployment fix (NVIDIA_DRIVER_CAPABILITIES=utility, M5 계약)."""
    fake = _fake_pynvml(init_error=_NVMLError("driver not loaded"))
    monkeypatch.setitem(sys.modules, "pynvml", fake)

    with pytest.raises(RuntimeError) as exc:
        PynvmlVramGauge().available_vram_mb()

    message = str(exc.value)
    assert "NVML query failed" in message
    assert "NVIDIA_DRIVER_CAPABILITIES=utility" in message
    assert isinstance(exc.value.__cause__, _NVMLError)  # the NVML error is not swallowed
    assert fake.calls == ["init"]  # init raised before the handle existed -> no shutdown


# --------------------------------------------------------------------------- #
# (b) scheduler.Scheduler — construction guard + the over-launch OBSERVER
# --------------------------------------------------------------------------- #


def test_max_attempts_below_one_is_rejected():
    """`max_attempts` is "total run attempts allowed", so 0 would mean a job that
    can never run — refused at construction rather than hanging the queue."""
    with pytest.raises(ValueError) as exc:
        Scheduler(runner=FakeRunner(), k=1, max_attempts=0)
    assert "max_attempts must be >= 1" in str(exc.value)


def test_admission_over_fill_is_counted_not_assumed():
    """Positive control for the NFR-ORCH-003 observer (`_admit`'s over-launch count).

    The admission loop cannot over-fill (`while ... len(running) < k`), so this
    hands `_admit` an already-over-filled slot table — the exact shape an admission
    regression would produce — and asserts the counter NOTICES. Without this the
    "over_launch_count == 0" evidence every DoD-P4 run reports would be vacuous:
    a counter that can never move reads 0 for free.
    """
    scheduler = Scheduler(runner=FakeRunner(), k=2)
    already_running = [Job(request_id="r", repeat_index=i) for i in range(5)]

    scheduler._admit(deque(), already_running)

    assert scheduler.over_launch_count == 3  # 5 running against a cap of 2
    assert scheduler.max_concurrent_observed == 5


# --------------------------------------------------------------------------- #
# (c) store.record_envelope — the anchor-list length contract
# --------------------------------------------------------------------------- #


def test_record_envelope_rejects_a_mismatched_anchor_list(tmp_path):
    """`oracle_plugin_dirs` is POSITIONAL (anchor i belongs to request i), so a
    short/long list would silently re-map anchors onto the wrong requests. Refused
    at the write, before any row exists."""
    with Store(tmp_path / "cv.sqlite3") as store:
        with pytest.raises(ValueError) as exc:
            store.record_envelope("env-1", ["env-1/r0", "env-1/r1"], ["/scenario"])
        assert "oracle_plugin_dirs must have 2 items, got 1" in str(exc.value)
        assert store.load_envelope("env-1") is None  # nothing was written


# --------------------------------------------------------------------------- #
# (d) api — supervision crash / persistence failure / loop shutdown
# --------------------------------------------------------------------------- #

_DOC_FIXTURE = Path(__file__).parent / "fixtures" / "nova_carter_warehouse_goal.yaml"


def _canonical_doc() -> dict:
    return yaml.safe_load(_DOC_FIXTURE.read_text(encoding="utf-8"))


def _submit(client: TestClient) -> str:
    response = client.post("/envelopes", json={"requests": [_canonical_doc()]})
    assert response.status_code == 202, response.text
    return response.json()["envelope_id"]


def _await_500(client: TestClient, envelope_id: str, timeout_s: float = 10.0) -> dict:
    """Poll status until the crash marker surfaces (background drive task)."""
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        response = client.get(f"/envelopes/{envelope_id}")
        if response.status_code == 500:
            return response.json()
        time.sleep(0.02)
    raise AssertionError(f"envelope {envelope_id} never surfaced a supervision error")


def test_supervision_crash_is_loud_on_status_and_persisted_as_the_envelope_error(
    tmp_path, monkeypatch
):
    """A crash INSIDE supervision (here: the SQLite write behind `mark_running`
    fails) must never look like a verdict.

    Contract (api module docstring / `_persist_terminal`): the reason is kept on
    the record and surfaces as 500 on every status read, NO report is assembled
    and NO baseline is advanced, and the envelope is closed in the store with the
    error marker so `/report` answers 409 supervision-error after a restart too.
    """
    with Store(tmp_path / "cv.sqlite3") as store:
        original_upsert = store.upsert_job

        def fail_when_a_job_goes_running(job: Job) -> None:
            if job.state is JobState.RUNNING:
                raise sqlite3.OperationalError("disk I/O error")
            original_upsert(job)

        monkeypatch.setattr(store, "upsert_job", fail_when_a_job_goes_running)
        app = create_app(store, FakeRunner(), k=1)
        with TestClient(app) as client:
            envelope_id = _submit(client)
            detail = _await_500(client, envelope_id)["detail"]
            assert detail.startswith("envelope supervision crashed: ")
            assert "disk I/O error" in detail  # the REASON survives, not a bare 'failed'

            report = client.get(f"/envelopes/{envelope_id}/report")
            assert report.status_code == 409
            assert report.json()["detail"]["reason"] == "supervision-error"

        stored = store.load_envelope(envelope_id)
        assert stored.status == ENVELOPE_COMPLETED  # terminal, but...
        assert "disk I/O error" in stored.error  # ...marked as a crash, not an outcome
        assert stored.report_outcome is None  # no fabricated verdict
        assert store.load_report(envelope_id) is None  # ...and no report was assembled


def test_a_persistence_failure_after_a_clean_run_is_surfaced_not_masked(tmp_path, monkeypatch):
    """Supervision succeeded but the write-through failed: `_drive`'s SECOND
    boundary. The status read must say so (500, prefixed `persist failed:`) —
    returning the in-memory results as if they were durable would be the lie the
    boundary exists to prevent."""
    with Store(tmp_path / "cv.sqlite3") as store:

        def boom(*args, **kwargs):
            raise sqlite3.OperationalError("database is locked")

        monkeypatch.setattr(store, "save_report", boom)
        app = create_app(store, FakeRunner(), k=1)
        with TestClient(app) as client:
            envelope_id = _submit(client)
            detail = _await_500(client, envelope_id)["detail"]
            assert detail.startswith("envelope supervision crashed: persist failed: ")
            assert "database is locked" in detail


def test_loop_shutdown_mid_envelope_leaves_the_envelope_running(tmp_path):
    """Cancellation is NOT a supervision failure: `_drive` re-raises it, writes
    nothing and leaves `done` False, so the envelope stays 'running' in the store
    and `reconcile_at_restart` (R14) is what decides its fate on the next boot.
    Persisting a fabricated outcome out of partial results here would be a lie."""

    class _CancelledSupervisor:
        async def run(self):
            raise asyncio.CancelledError

    with Store(tmp_path / "cv.sqlite3") as store:
        store.record_envelope("env-cancel", ["env-cancel/r0"])
        state = _AppState(
            store=store,
            runner=FakeRunner(),
            k=1,
            max_attempts=1,
            retry_on_timeout=True,
            job_timeout_s=None,
            allocator=DomainIdAllocator(store),
        )
        record = _EnvelopeRecord(envelope_id="env-cancel", request_ids=["env-cancel/r0"], jobs=[])

        async def drive() -> None:
            await _drive(state, record, _CancelledSupervisor())

        with pytest.raises(asyncio.CancelledError):
            asyncio.run(drive())

        assert record.done is False  # the `finally` never ran — nothing was concluded
        assert record.error is None
        assert store.load_envelope("env-cancel").status == "running"


# --------------------------------------------------------------------------- #
# (e) monitor — projection helper, NVML degrade policy, sampler lifecycle
# --------------------------------------------------------------------------- #


def test_duration_of_an_unparsable_timestamp_is_absent_not_a_crash():
    """The operational view is a projection over whatever the store holds; a row
    whose timestamps are not ISO8601 (legacy/hand-edited) must read as "unknown
    duration", never take the dashboard down."""
    assert _duration_s("2026-08-30T00:00:00", "2026-08-30T00:00:03") == 3.0
    assert _duration_s("not-a-timestamp", "2026-08-30T00:00:03") is None
    assert _duration_s(None, "2026-08-30T00:00:03") is None


def test_nvml_snapshot_reads_used_total_and_util_in_mib(monkeypatch):
    """Host-independent pin of the NVML success shape (bytes -> MiB, util as int %)."""
    fake = _fake_pynvml(used_bytes=4096 * 1024**2, total_bytes=16384 * 1024**2, util_pct=37)
    monkeypatch.setitem(sys.modules, "pynvml", fake)

    snap = nvml_snapshot()

    assert snap == NvmlSnapshot(vram_used_mib=4096, vram_total_mib=16384, gpu_util_pct=37)
    assert fake.calls == ["init", "shutdown"]  # shutdown runs even on the success path


def test_nvml_snapshot_is_none_when_the_wheel_is_absent(monkeypatch):
    """No pynvml at all (the GPU-free host default): None, and NOT the loud raise
    the scheduler's admission gauge does — M6 §3.4 opposite failure policy."""
    monkeypatch.setitem(sys.modules, "pynvml", None)  # makes `import pynvml` raise
    assert nvml_snapshot() is None


def test_nvml_snapshot_is_none_when_the_query_fails(monkeypatch):
    """pynvml present but NVML unusable (G-36 device-cgroup loss, driver mismatch):
    still None — an observational sampler must never crash the process."""
    monkeypatch.setitem(sys.modules, "pynvml", _fake_pynvml(init_error=RuntimeError("no device")))
    assert nvml_snapshot() is None


def test_sample_once_with_a_reachable_gpu_does_not_log_the_degrade_line(tmp_path, capsys):
    """The degrade log is for ABSENCE only; a reachable GPU must stay silent and
    carry the NVML numbers onto the sample."""
    with Store(tmp_path / "cv.sqlite3") as store:
        sampler = ResourceHealthSampler(
            store,
            nvml_snapshot_fn=lambda _idx: NvmlSnapshot(
                vram_used_mib=1000, vram_total_mib=16000, gpu_util_pct=12
            ),
        )
        sample = sampler.sample_once()

    assert sample.gpu_reachable is True
    assert (sample.vram_used_mib, sample.vram_total_mib, sample.gpu_util_pct) == (1000, 16000, 12)
    assert "NVML unavailable" not in capsys.readouterr().err


def test_a_failing_tick_is_reported_but_never_stops_the_sampler(tmp_path, capsys):
    """One bad tick must not kill the sole periodic poller (M6 §3.4): the failure
    goes to stderr and the loop keeps going — a sampler that dies silently would
    freeze the operational view at its last good sample forever."""
    with Store(tmp_path / "cv.sqlite3") as store:
        sampler = ResourceHealthSampler(store, interval_s=0)
        ticks: list[int] = []

        def failing_tick():
            ticks.append(len(ticks))
            raise RuntimeError("sample exploded")

        sampler.sample_once = failing_tick

        async def run_a_few_ticks() -> None:
            task = asyncio.get_running_loop().create_task(sampler.run())
            for _ in range(6):
                await asyncio.sleep(0)
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task

        asyncio.run(run_a_few_ticks())

    assert len(ticks) >= 2, "the loop stopped after the first failure"
    assert "[cv-monitor] resource sample failed: RuntimeError('sample exploded')" in (
        capsys.readouterr().err
    )


class _SleepingSampler:
    """Duck-typed sampler: records the task it runs in, then parks forever."""

    def __init__(self) -> None:
        self.task: asyncio.Task | None = None

    async def run(self) -> None:
        self.task = asyncio.current_task()
        await asyncio.sleep(3600)


def test_attach_sampler_runs_the_poller_for_the_app_lifetime_only():
    """Production wiring (`serve.build_app(start_sampler=True)`): the sampler is a
    background task started at app startup and CANCELLED at shutdown — it must not
    outlive the app (a leaked NVML poller would keep sampling a dead store)."""
    app = FastAPI()
    sampler = _SleepingSampler()
    attach_sampler(app, sampler)

    with TestClient(app) as client:
        client.get("/does-not-exist")  # cycle the loop so the task actually starts
        assert sampler.task is not None, "startup did not create the sampler task"

    assert sampler.task.cancelling() >= 1 or sampler.task.cancelled()


def test_shutdown_without_startup_is_a_no_op():
    """The shutdown hook must tolerate "no task": an app that failed before startup
    (or a test harness that only fires shutdown) must not turn a boot failure into a
    second, unrelated KeyError."""
    app = FastAPI()
    attach_sampler(app, _SleepingSampler())
    (stop_handler,) = app.router.on_shutdown

    asyncio.run(stop_handler())  # must not raise


# --------------------------------------------------------------------------- #
# (f) serve — the production composition switches
# --------------------------------------------------------------------------- #


def _serve_env(tmp_path: Path) -> dict[str, str]:
    return {
        "CV_STORE_PATH": str(tmp_path / "cv.sqlite3"),
        "CV_OUT_DIR": str(tmp_path / "out"),
        "CV_RUNNER_IMAGE": "runner:test",
        "CV_MAX_CONCURRENT": "2",
    }


def test_start_sampler_is_what_wires_the_resident_poller(tmp_path):
    """`start_sampler` is the ONE switch between the CPU-test app (poller-free, so
    TestClient suites never spawn a background NVML poll) and the resident
    deployment (`main` passes True)."""
    config = serve.config_from_env(_serve_env(tmp_path))

    quiet = serve.build_app(config, start_sampler=False)  # the default CPU-test path
    resident = serve.build_app(config, start_sampler=True)

    assert quiet.router.on_startup == []
    assert len(resident.router.on_startup) == 1


def test_main_composes_the_real_client_and_serves_the_configured_bind(tmp_path, monkeypatch):
    """`main` is the ONE production command: env -> config -> real docker client ->
    app (with the sampler on) -> uvicorn on the configured host/port. Both heavy
    imports are lazy INSIDE it, which is exactly what lets this test replace them.
    """
    swept = {"containers": 0, "networks": 0}

    class _Listing:
        def __init__(self, kind: str) -> None:
            self._kind = kind

        def list(self, all: bool = False, filters: dict | None = None):  # noqa: A002 — SDK name
            swept[self._kind] += 1
            return []

    client = types.SimpleNamespace(containers=_Listing("containers"), networks=_Listing("networks"))
    docker_module = types.ModuleType("docker")
    docker_module.from_env = lambda: client

    served: dict = {}
    uvicorn_module = types.ModuleType("uvicorn")
    uvicorn_module.run = lambda app, host, port: served.update(app=app, host=host, port=port)

    monkeypatch.setitem(sys.modules, "docker", docker_module)
    monkeypatch.setitem(sys.modules, "uvicorn", uvicorn_module)
    for name, value in {**_serve_env(tmp_path), "CV_BIND_PORT": "9123"}.items():
        monkeypatch.setenv(name, value)

    assert serve.main() == 0
    assert (served["host"], served["port"]) == ("127.0.0.1", 9123)
    assert swept == {"containers": 1, "networks": 1}  # the boot sweep saw the real client
    assert len(served["app"].router.on_startup) == 1  # production runs the sampler
