"""M6 operational monitoring — read model(projection) + sampler + web surface.

The operational/infra-layer view ON TOP of the M3 store (DoD-P4-12/13): "무엇이
몇 건 pass/fail, 어디서 깨졌나" for the OPERATOR, structurally separated from the
DEVELOPER's domain result review (M4). The boundary (``REQ-MONITOR-002/004``,
``NFR-MONITOR-002``) is enforced at the DATA SOURCE, not the display layer —

* the projection reads ``store.load_operational_jobs`` whose SELECT list omits
  the domain-carrying columns (``job_spec`` = scenario/criteria/sut명세,
  ``oracle_plugin_dir``) ENTIRELY, so no domain detail can leak (누수 불가 =
  출처 분리); pass/fail come from the ``RequestRollup`` COUNT summary (SR-10,
  재집계 금지 — the verdicts list body itself is never surfaced), never from the
  domain matrix M4 owns (SR-19, LOCKED §7.12);
* the ``OperationalRecord`` pydantic model declares EXACTLY the pinned operational
  fields — a new field on the left is a review gate (NEG-3 schema-equality test),
  not a silent leak.

Two surfaces share the one projection (M6 §3.6): ``/monitor.json``
(``OperationalRecord`` serialized) and ``/monitor`` (a stdlib-rendered one-glance
HTML page — NO Jinja2/SPA/chart lib, 신규 의존 금지). One dedicated async
``ResourceHealthSampler`` (M6 §3.4) is the SOLE periodic NVML poller: it upserts a
single latest resource/health snapshot; NVML absence is a LOUD-once graceful
degrade (``gpu_reachable=false`` / vram·util null — the default path on a
GPU-free/CPU host), NOT a crash. The scheduler's admission-time on-demand gauge
(``scheduler.PynvmlVramGauge``) is not a periodic poller, so it is unchanged;
full store-shared coherence between the two is a P5 follow-up (PM 룰링). Stdlib +
the already-pinned FastAPI/pydantic/pynvml — no new dependency.
"""

from __future__ import annotations

import asyncio
import html
import sys
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime

from fastapi import FastAPI
from fastapi.responses import HTMLResponse
from pydantic import BaseModel

from cv_infra.orchestrator.models import JobState, Verdict
from cv_infra.orchestrator.store import (
    OperationalJobRow,
    ResourceSample,
    Store,
    StoredEnvelope,
)

#: Recent-envelope window for the operational view (P4 scale — pagination is
#: post-MVP; a constant, not an NFR quantity).
RECENT_ENVELOPE_LIMIT = 50

#: Default resource/health sampling period (operational placeholder — not an NFR
#: threshold, M6 self-owns no quantitative pin).
DEFAULT_SAMPLE_INTERVAL_S = 5.0

#: Terminal job states that are a verdict-less INFRA outcome (the error_count /
#: error_category population — a COMPLETED job always carries a rollup verdict).
_ERROR_STATES = frozenset({JobState.FAILED.value, JobState.TIMEOUT.value})

#: The supervisor crash-boundary reason prefix (``supervisor._run_one`` — "runner
#: seam crashed: ..."). Soft coupling for the runner-crash category; a drift only
#: downgrades the category to the safe "infra" default, never a crash/leak.
_CRASH_REASON_PREFIX = "runner seam crashed"


def _now_iso() -> str:
    """Timezone-aware UTC ISO8601 stamp for ``generated_at`` (M6-owned)."""
    return datetime.now(UTC).isoformat()


# --------------------------------------------------------------------------- #
# Operational read model (pydantic) — the pinned projection shape (NEG-3).
# The field NAME sets are the frozen wire contract a later CLI consumes; a
# schema-equality test fails on any add/rename (G-17 review gate).
# --------------------------------------------------------------------------- #


class MonitorJob(BaseModel):
    """One fanned-out job's OPERATIONAL row (no domain detail — DoD-P4-13)."""

    job_id: str
    repeat_index: int
    state: str
    attempt_count: int
    started_at: str | None
    ended_at: str | None
    duration_s: float | None
    error_category: str | None  # "contract" | "infra" | "runner-crash" | "timeout" | None
    runner_exit_code: int | None
    infra_error: str | None


class MonitorRequest(BaseModel):
    """One Verification Request's operational rollup COUNT summary + its jobs."""

    envelope_id: str
    request_id: str
    submitted_at: str | None
    envelope_status: str
    report_outcome: str | None
    pass_count: int
    fail_count: int
    error_count: int
    flakiness: float | None
    jobs: list[MonitorJob]


class MonitorHealth(BaseModel):
    """Service health header (orchestrator alive · GPU reachability · last sample)."""

    orchestrator_up: bool
    gpu_reachable: bool
    last_sample_at: str | None


class MonitorResources(BaseModel):
    """Queue/slot + NVML resource header (values null when never sampled/degraded)."""

    queue_depth: int
    running_k: int
    over_launch_count: int
    vram_used_mib: int | None
    vram_total_mib: int | None
    gpu_util_pct: int | None


class OperationalRecord(BaseModel):
    """The whole operational view — one projection, two surfaces (M6 §3.6)."""

    generated_at: str
    health: MonitorHealth
    resources: MonitorResources
    requests: list[MonitorRequest]


# --------------------------------------------------------------------------- #
# Projection assembly — READ-ONLY over the store (M6 §3.2/§3.3).
# --------------------------------------------------------------------------- #


def _duration_s(started_at: str | None, ended_at: str | None) -> float | None:
    """Wall-clock seconds between start and end (None when either is missing)."""
    if started_at is None or ended_at is None:
        return None
    try:
        return (
            datetime.fromisoformat(ended_at) - datetime.fromisoformat(started_at)
        ).total_seconds()
    except ValueError:
        return None


def _error_category(row: OperationalJobRow) -> str | None:
    """Map a terminal job to an operational error category (exit-code 계약 §7 #9).

    None for queued/running/completed (a completed job carries a rollup verdict,
    not an error). A verdict-less FAILED job is sub-classified: exit 2 = contract
    error, exit 3 = infra error, a signal-kill (>=128 → 137 OOM-kill / 139
    segfault) or the supervisor crash boundary = runner-crash, everything else
    (plain non-zero, unknowable) = the safe infra default. TIMEOUT = timeout.
    """
    if row.state == JobState.TIMEOUT.value:
        return "timeout"
    if row.state != JobState.FAILED.value:
        return None
    exit_code = row.runner_exit_code
    if exit_code == 2:
        return "contract"
    if exit_code == 3:
        return "infra"
    if exit_code is not None and exit_code >= 128:
        return "runner-crash"
    if row.infra_error is not None and row.infra_error.startswith(_CRASH_REASON_PREFIX):
        return "runner-crash"
    return "infra"


def _monitor_job(row: OperationalJobRow) -> MonitorJob:
    return MonitorJob(
        job_id=f"{row.request_id}:{row.repeat_index}",  # store.job_key identity
        repeat_index=row.repeat_index,
        state=row.state,
        attempt_count=row.attempt_count,
        started_at=row.started_at,
        ended_at=row.ended_at,
        duration_s=_duration_s(row.started_at, row.ended_at),
        error_category=_error_category(row),
        runner_exit_code=row.runner_exit_code,
        infra_error=row.infra_error,
    )


def _monitor_request(
    envelope: StoredEnvelope,
    request_id: str,
    rows: list[OperationalJobRow],
    flakiness: float | None,
    verdicts: list[Verdict],
) -> MonitorRequest:
    """Assemble one request's operational summary from the rollup COUNT + job states.

    pass/fail come from the ``RequestRollup`` verdicts COUNT (SR-10, 파생만 — the
    list itself never surfaces); error_count is the operational count of
    verdict-less terminal jobs (FAILED/TIMEOUT states), which the rollup drops.
    """
    return MonitorRequest(
        envelope_id=envelope.envelope_id,
        request_id=request_id,
        submitted_at=envelope.submitted_at,
        envelope_status=envelope.status,
        report_outcome=envelope.report_outcome,
        pass_count=sum(1 for v in verdicts if v is Verdict.PASS),
        fail_count=sum(1 for v in verdicts if v is Verdict.FAIL),
        error_count=sum(1 for r in rows if r.state in _ERROR_STATES),
        flakiness=flakiness,
        jobs=[_monitor_job(r) for r in sorted(rows, key=lambda r: r.repeat_index)],
    )


def build_operational_record(store: Store) -> OperationalRecord:
    """Project the store into the ``OperationalRecord`` — READ-ONLY (DoD-P4-13).

    Only read methods are touched: ``load_recent_envelopes`` /
    ``load_operational_jobs`` (domain columns absent from its SELECT) /
    ``load_rollup`` (COUNT taken, list dropped) / ``job_state_counts`` /
    ``load_resource_sample``. queue_depth / running_k are LIVE store counts (the
    resource-sample copies lag by a poll interval); vram/util/over_launch come
    from the latest sampler snapshot (defaults when never sampled).
    """
    jobs_by_request: dict[str, list[OperationalJobRow]] = {}
    for row in store.load_operational_jobs():
        jobs_by_request.setdefault(row.request_id, []).append(row)

    requests: list[MonitorRequest] = []
    for envelope in store.load_recent_envelopes(RECENT_ENVELOPE_LIMIT):
        for request_id in envelope.request_ids:
            rollup = store.load_rollup(request_id)
            requests.append(
                _monitor_request(
                    envelope,
                    request_id,
                    jobs_by_request.get(request_id, []),
                    rollup.flakiness if rollup is not None else None,
                    rollup.verdicts if rollup is not None else [],
                )
            )

    counts = store.job_state_counts()
    sample = store.load_resource_sample()
    return OperationalRecord(
        generated_at=_now_iso(),
        health=MonitorHealth(
            orchestrator_up=True,  # serving this projection IS the liveness proof
            gpu_reachable=sample.gpu_reachable if sample is not None else False,
            last_sample_at=sample.sampled_at if sample is not None else None,
        ),
        resources=MonitorResources(
            queue_depth=counts.get(JobState.QUEUED.value, 0),  # live store count
            running_k=counts.get(JobState.RUNNING.value, 0),  # live store count
            over_launch_count=sample.over_launch_count if sample is not None else 0,
            vram_used_mib=sample.vram_used_mib if sample is not None else None,
            vram_total_mib=sample.vram_total_mib if sample is not None else None,
            gpu_util_pct=sample.gpu_util_pct if sample is not None else None,
        ),
        requests=requests,
    )


# --------------------------------------------------------------------------- #
# One-glance HTML surface (NFR-MONITOR-001) — stdlib render, html.escape, NO
# Jinja2/SPA/chart lib (신규 의존 금지).
# --------------------------------------------------------------------------- #


def _cell(value: object) -> str:
    return html.escape("-" if value is None else str(value))


def render_dashboard_html(record: OperationalRecord) -> str:
    """Render the operational view as ONE static HTML page (NFR-MONITOR-001).

    Health/resource header + a per-request table (pass/fail/error counts + the
    failed jobs with their error category) — the operator sees "무엇이 몇 건
    pass/fail, 어디서 깨졌나" at a glance. Every dynamic string is ``html.escape``d.
    """
    h, r = record.health, record.resources
    header = (
        f"<p class='health'>orchestrator_up={_cell(h.orchestrator_up)} · "
        f"gpu_reachable={_cell(h.gpu_reachable)} · last_sample_at={_cell(h.last_sample_at)}</p>"
        f"<p class='resources'>queue_depth={r.queue_depth} · running_k={r.running_k} · "
        f"over_launch_count={r.over_launch_count} · "
        f"vram={_cell(r.vram_used_mib)}/{_cell(r.vram_total_mib)} MiB · "
        f"gpu_util={_cell(r.gpu_util_pct)}%</p>"
    )
    rows: list[str] = []
    for req in record.requests:
        broken = [
            f"{html.escape(j.job_id)}[{html.escape(j.error_category or '')}]"
            for j in req.jobs
            if j.error_category is not None
        ]
        rows.append(
            "<tr>"
            f"<td>{_cell(req.envelope_id)}</td>"
            f"<td>{_cell(req.request_id)}</td>"
            f"<td>{_cell(req.envelope_status)}</td>"
            f"<td>{_cell(req.report_outcome)}</td>"
            f"<td class='pass'>{req.pass_count}</td>"
            f"<td class='fail'>{req.fail_count}</td>"
            f"<td class='error'>{req.error_count}</td>"
            f"<td>{_cell(req.flakiness)}</td>"
            f"<td>{', '.join(broken) if broken else '-'}</td>"
            "</tr>"
        )
    body = "".join(rows) if rows else "<tr><td colspan='9'>no requests yet</td></tr>"
    return (
        "<!doctype html><html lang='en'><head><meta charset='utf-8'>"
        "<title>cv-infra operational monitor</title></head><body>"
        "<h1>cv-infra operational monitor</h1>"
        f"<p>generated_at={_cell(record.generated_at)}</p>"
        f"{header}"
        "<table border='1'><thead><tr>"
        "<th>envelope</th><th>request</th><th>status</th><th>report_outcome</th>"
        "<th>pass</th><th>fail</th><th>error</th><th>flakiness</th><th>broken jobs</th>"
        "</tr></thead><tbody>"
        f"{body}"
        "</tbody></table></body></html>"
    )


# --------------------------------------------------------------------------- #
# Resource/health sampler — the SOLE periodic NVML poller (M6 §3.4).
# --------------------------------------------------------------------------- #


@dataclass
class NvmlSnapshot:
    """One NVML read (VRAM used/total + GPU util) — the sampler's GPU telemetry."""

    vram_used_mib: int
    vram_total_mib: int
    gpu_util_pct: int


def nvml_snapshot(device_index: int = 0) -> NvmlSnapshot | None:
    """Read VRAM used/total + GPU util via NVML, or None on absence/failure.

    Reuses the scheduler's lazy-pynvml idiom (GPU-free hosts import this module
    fine) but with the OPPOSITE failure policy: the scheduler's admission gauge
    raises LOUD (a silently-skipped guard neuters over-launch protection), while
    this observational sampler DEGRADES GRACEFULLY — a monitor that crashed the
    process on a missing GPU would be worse than one reporting gpu_reachable=false.
    The loud-once log is the sampler's (``ResourceHealthSampler``), so absence is
    never silent (G-26) but also never a crash.
    """
    try:
        import pynvml  # noqa: PLC0415 — lazy: GPU-free hosts must import this module fine
    except Exception:  # pynvml wheel absent entirely
        return None
    try:
        pynvml.nvmlInit()
        try:
            handle = pynvml.nvmlDeviceGetHandleByIndex(device_index)
            mem = pynvml.nvmlDeviceGetMemoryInfo(handle)
            util = pynvml.nvmlDeviceGetUtilizationRates(handle)
            return NvmlSnapshot(
                vram_used_mib=int(mem.used // (1024 * 1024)),
                vram_total_mib=int(mem.total // (1024 * 1024)),
                gpu_util_pct=int(util.gpu),
            )
        finally:
            pynvml.nvmlShutdown()
    except Exception:  # NVML present but init/query failed (G-36 device-cgroup loss等)
        return None


class ResourceHealthSampler:
    """Dedicated async sampler (M6 §3.4) — the SOLE periodic NVML poller.

    Each tick upserts ONE latest resource/health snapshot: the NVML read (VRAM /
    util / reachability, degrading to null/false + a loud-once log when NVML is
    absent — the default path on this CPU/GPU-free host) plus the queue depth /
    running_k (store job-state counts, the M3 §3.4 shared source) and
    over_launch_count (injected provider; the P4 architecture's SlotAccountants
    are per-envelope ephemeral, so the by-construction invariant 0 is the honest
    default until a persistent-accountant wiring lands in P5 — PM 룰링). Time-series
    retention is post-MVP (one latest row).
    """

    def __init__(
        self,
        store: Store,
        *,
        interval_s: float = DEFAULT_SAMPLE_INTERVAL_S,
        device_index: int = 0,
        over_launch_provider: Callable[[], int] | None = None,
        nvml_snapshot_fn: Callable[[int], NvmlSnapshot | None] = nvml_snapshot,
    ) -> None:
        self._store = store
        self._interval_s = interval_s
        self._device_index = device_index
        self._over_launch_provider = over_launch_provider
        self._nvml_snapshot_fn = nvml_snapshot_fn
        self._degrade_logged = False

    def sample_once(self) -> ResourceSample:
        """Take ONE resource/health sample and upsert it (the periodic unit)."""
        snap = self._nvml_snapshot_fn(self._device_index)
        if snap is None:
            self._log_degrade_once()
        counts = self._store.job_state_counts()
        sample = ResourceSample(
            sampled_at=_now_iso(),
            gpu_reachable=snap is not None,
            vram_used_mib=snap.vram_used_mib if snap is not None else None,
            vram_total_mib=snap.vram_total_mib if snap is not None else None,
            gpu_util_pct=snap.gpu_util_pct if snap is not None else None,
            queue_depth=counts.get(JobState.QUEUED.value, 0),
            running_k=counts.get(JobState.RUNNING.value, 0),
            over_launch_count=self._over_launch_provider() if self._over_launch_provider else 0,
        )
        self._store.record_resource_sample(sample)
        return sample

    def _log_degrade_once(self) -> None:
        if self._degrade_logged:
            return
        self._degrade_logged = True
        print(
            "[cv-monitor] NVML unavailable — GPU telemetry degraded"
            " (gpu_reachable=false, vram/util=null); expected on a GPU-free/CPU host,"
            " the operational view stays up (M6 §3.4 graceful degrade)",
            file=sys.stderr,
            flush=True,
        )

    async def run(self) -> None:
        """Sample forever at ``interval_s`` (cancelled at app shutdown).

        A sampling failure is surfaced on stderr but never stops the loop — the
        operational view degrading is always better than the sampler dying silent.
        """
        while True:
            try:
                self.sample_once()
            except Exception as exc:  # never let one bad tick kill the sampler
                print(f"[cv-monitor] resource sample failed: {exc!r}", file=sys.stderr, flush=True)
            await asyncio.sleep(self._interval_s)


# --------------------------------------------------------------------------- #
# Web surface registration (called by api.create_app / serve.build_app —
# 최소 호출: routes only; the periodic sampler is attached separately in prod).
# --------------------------------------------------------------------------- #


def register(app: FastAPI, store: Store) -> None:
    """Register the two operational routes on the M3 app (routes only, no writes)."""

    @app.get("/monitor.json")
    async def monitor_json() -> dict:
        return build_operational_record(store).model_dump()

    @app.get("/monitor", response_class=HTMLResponse)
    async def monitor_dashboard() -> str:
        return render_dashboard_html(build_operational_record(store))


def attach_sampler(app: FastAPI, sampler: ResourceHealthSampler) -> None:
    """Run ``sampler`` as a background task for the app's lifetime (production only).

    Started on app startup, cancelled on shutdown. Kept off the CPU-test path
    (``serve.build_app(start_sampler=False)`` default) so TestClient suites do not
    spawn a background poller.
    """
    holder: dict[str, asyncio.Task[None]] = {}

    @app.on_event("startup")
    async def _start_sampler() -> None:
        holder["task"] = asyncio.get_running_loop().create_task(sampler.run())

    @app.on_event("shutdown")
    async def _stop_sampler() -> None:
        task = holder.pop("task", None)
        if task is not None:
            task.cancel()
