"""Resource-aware scheduling: k computation + slot accounting (M3 §3.4).

Three pieces live here:

* ``compute_k`` — the LOCKED §7.4 cap rule
  ``k = min(max_concurrent, floor(availVRAM / per_instance), render_cap)``:
  operator budget is the AUTHORITATIVE cap, NVML VRAM is the 2nd guard,
  ``render_cap`` an independent cap term. Every input is injected — no k
  constant is hardcoded (NFR-ORCH-001 규율).
* ``VramGauge`` / ``PynvmlVramGauge`` — the injectable NVML seam (CPU tests
  mock it; the real gauge lazy-imports ``pynvml`` so GPU-free hosts import this
  module harmlessly, D-A/R-NV).
* ``SlotAccountant`` — slot-token accounting (REQ-ORCH-006, NFR-ORCH-003):
  admission deducts a token, runner exit returns it, and the counters make
  over-launch == 0 / reclaim-leak == 0 unit-assertable.

``Scheduler`` below is the Phase-1 CPU walking skeleton (DoD-P1-06) kept intact
for the frozen unit tests — synchronous, wave-structured. The Phase-4 k-parallel
path is ``supervisor.ParallelSupervisor`` driving ``JobQueue`` +
``SlotAccountant``; the per-slot accounting is the same, only the timing
differs. The job state machine moved to ``queue.py`` (M3 §3.3 home) and is
re-exported here so the P1 import path stays frozen.
"""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass
from typing import Protocol

from cv_infra.orchestrator.fake_runner import Runner
from cv_infra.orchestrator.models import Job, JobResult, JobState

# Redundant-alias imports = explicit re-exports: the state machine moved to
# queue.py (M3 §3.3 home); the P1 scheduler import path stays frozen.
from cv_infra.orchestrator.queue import (
    IllegalTransitionError as IllegalTransitionError,
)
from cv_infra.orchestrator.queue import (
    transition as transition,
)


class VramGauge(Protocol):
    """Injectable available-VRAM source for the 2nd guard (REQ-ORCH-005).

    CPU tests inject a fake; the production implementation is
    ``PynvmlVramGauge``. Returns available VRAM in MiB.
    """

    def available_vram_mb(self) -> float: ...


class PynvmlVramGauge:
    """NVML-backed available-VRAM gauge (``nvidia-ml-py``), lazily imported.

    ``pynvml`` is imported inside the call so importing this module is harmless
    on GPU-free hosts (the runner image installs the wheel --no-deps and never
    calls this). An NVML failure raises LOUD: silently disabling the guard is
    the R-NV hazard — it would neuter the over-launch protection (NFR-ORCH-003)
    without anyone noticing. The control-plane container therefore needs NVML
    visibility (``NVIDIA_DRIVER_CAPABILITIES=utility``, M5 compose 계약, D-A).
    """

    def __init__(self, device_index: int = 0) -> None:
        self._device_index = device_index

    def available_vram_mb(self) -> float:
        import pynvml  # noqa: PLC0415 — lazy: GPU-free hosts must import this module fine

        try:
            pynvml.nvmlInit()
            try:
                handle = pynvml.nvmlDeviceGetHandleByIndex(self._device_index)
                free_bytes = pynvml.nvmlDeviceGetMemoryInfo(handle).free
            finally:
                pynvml.nvmlShutdown()
        except pynvml.NVMLError as exc:
            raise RuntimeError(
                f"NVML query failed ({exc}) — the VRAM 2nd guard cannot run;"
                " the control plane needs NVML GPU visibility"
                " (NVIDIA_DRIVER_CAPABILITIES=utility, M5 compose contract; R-NV)"
            ) from exc
        return free_bytes / (1024 * 1024)


def compute_k(
    max_concurrent: int,
    *,
    vram_gauge: VramGauge | None = None,
    vram_per_instance_mb: float | None = None,
    render_cap: int | None = None,
) -> int:
    """Compute the concurrency cap k (LOCKED §7.4; REQ-ORCH-004/005, REQ-DEPLOY-012).

    ``k = min(max_concurrent, floor(availVRAM / vram_per_instance), render_cap)``

    * ``max_concurrent`` (Resource Budget) is the operator's AUTHORITATIVE cap
      and always applies.
    * The NVML VRAM term is a 2nd guard, applied only when BOTH ``vram_gauge``
      and the MEASURED ``vram_per_instance_mb`` (Phase 2/4 실측 — never a
      constant here) are given. Giving one without the other is a configuration
      error and raises: a silently skipped guard is the R-NV hazard.
    * ``render_cap`` is an independent cap TERM, not an arithmetic division
      (M3 §3.4 D-O) — optional until the Phase-4 throughput curve fixes it.

    k < 1 (the VRAM guard leaves no capacity — the only term that can floor to
    zero) is a LOUD config error, never a silent 0: SlotAccountant/Scheduler
    require k >= 1, so a returned 0 would either crash later without context or
    park admission forever with no operator signal (p4c1 follow-up ①, PM 룰링
    cycle-plan 2026-07-13 — 대기-무한 침묵 금지).
    """
    if max_concurrent < 1:
        raise ValueError(f"max_concurrent must be >= 1, got {max_concurrent}")
    if (vram_gauge is None) != (vram_per_instance_mb is None):
        raise ValueError(
            "vram_gauge and vram_per_instance_mb must be given together"
            " (a half-configured VRAM guard would be silently skipped — R-NV)"
        )
    if vram_per_instance_mb is not None and vram_per_instance_mb <= 0:
        raise ValueError(f"vram_per_instance_mb must be > 0, got {vram_per_instance_mb}")
    if render_cap is not None and render_cap < 1:
        raise ValueError(f"render_cap must be >= 1, got {render_cap}")

    k = max_concurrent
    if vram_gauge is not None and vram_per_instance_mb is not None:
        available_mb = vram_gauge.available_vram_mb()
        k = min(k, int(available_mb // vram_per_instance_mb))
        if k < 1:
            raise ValueError(
                f"computed k = 0: available VRAM {available_mb:.0f} MiB cannot fit one"
                f" instance of {vram_per_instance_mb:.0f} MiB — operator misconfiguration"
                " (free GPU memory or correct vram_per_instance to the Phase-2/4 measured"
                " value); refusing to park admission silently"
            )
    if render_cap is not None:
        k = min(k, render_cap)
    return k


@dataclass
class SlotAccountant:
    """Slot-token accounting: admit deducts, exit returns (REQ-ORCH-006).

    ``try_acquire()`` is the admission gate — when no slot is free it returns
    False and the launch simply never happens, so budget-exceeding launch
    attempts == 0 by construction (NFR-ORCH-003); ``over_launch_count`` observes
    that invariant so an admission regression is assertable, not assumed.
    ``release()`` without a matching acquire raises: a reclaim-accounting bug
    must be loud, never a silent slot leak (회수 누락 0). Counters
    ``acquired_total`` / ``released_total`` make the balance assertable.

    Single-threaded by design: acquire/release happen on the asyncio event-loop
    thread (the executor threads never touch the accountant).
    """

    k: int
    in_use: int = 0
    acquired_total: int = 0
    released_total: int = 0
    over_launch_count: int = 0
    max_concurrent_observed: int = 0

    def __post_init__(self) -> None:
        if self.k < 1:
            raise ValueError(f"k must be >= 1, got {self.k}")

    def try_acquire(self) -> bool:
        """Take a slot token if one is free; False = gate closed (no launch)."""
        if self.in_use >= self.k:
            return False
        self.in_use += 1
        self.acquired_total += 1
        if self.in_use > self.k:  # unreachable with the gate above — observed, not assumed
            self.over_launch_count += 1
        self.max_concurrent_observed = max(self.max_concurrent_observed, self.in_use)
        return True

    def release(self) -> None:
        """Return a slot token (runner exited — REQ-EXEC-015 수신)."""
        if self.in_use <= 0:
            raise RuntimeError("slot release without a matching acquire (reclaim accounting bug)")
        self.in_use -= 1
        self.released_total += 1


@dataclass
class Scheduler:
    """k-capped scheduler driving Jobs through a Runner (CPU skeleton).

    Args:
        runner: the (fake, in Phase 1) Runner driven 1:1 per job.
        k: concurrency cap — max jobs RUNNING at once (injected; see module
            docstring on the Phase 4 k computation).
        max_attempts: total run attempts allowed per job before a retryable
            failure becomes terminal (>= 1).
        retry_on_timeout: whether a TIMEOUT outcome is retried like FAILED.
    """

    runner: Runner
    k: int
    max_attempts: int = 1
    retry_on_timeout: bool = True
    over_launch_count: int = 0
    max_concurrent_observed: int = 0

    def __post_init__(self) -> None:
        if self.k < 1:
            raise ValueError(f"k must be >= 1, got {self.k}")
        if self.max_attempts < 1:
            raise ValueError(f"max_attempts must be >= 1, got {self.max_attempts}")

    def run(self, jobs: list[Job]) -> list[JobResult]:
        """Drive all jobs to a terminal state, honoring the k cap.

        Returns one ``JobResult`` per input job (its terminal outcome). Mutates
        each Job's ``state`` / ``attempt_count`` in place — the same per-job state
        SQLite persists in Phase 4 (REQ-ORCH-011).
        """
        queue: deque[Job] = deque(jobs)
        running: list[Job] = []
        results: list[JobResult] = []
        while queue or running:
            self._admit(queue, running)
            self._drive(queue, running, results)
        return results

    def _admit(self, queue: deque[Job], running: list[Job]) -> None:
        """Fill free slots up to ``k`` (admission gate; slot accounting REQ-ORCH-006)."""
        while queue and len(running) < self.k:
            job = queue.popleft()
            job.state = transition(job.state, JobState.RUNNING)
            running.append(job)
        # NFR-ORCH-003 invariant: the gate must never over-fill slots. With a
        # correct admission loop this stays 0; counting it makes the
        # over-launch == 0 guarantee an explicit, assertable observation rather
        # than an implicit one (a regression in admission would surface here).
        if len(running) > self.k:
            self.over_launch_count += len(running) - self.k
        self.max_concurrent_observed = max(self.max_concurrent_observed, len(running))

    def _drive(self, queue: deque[Job], running: list[Job], results: list[JobResult]) -> None:
        """Run each admitted job once, then apply the state machine + retry policy."""
        wave = list(running)
        running.clear()  # reclaim every slot before the next admission wave
        for job in wave:
            outcome = self.runner.run(job)
            job.attempt_count += 1
            if self._should_retry(outcome.state, job.attempt_count):
                job.state = transition(job.state, outcome.state)
                job.state = transition(job.state, JobState.QUEUED)
                queue.append(job)
            else:
                job.state = transition(job.state, outcome.state)
                results.append(outcome)

    def _should_retry(self, outcome_state: JobState, attempt_count: int) -> bool:
        if attempt_count >= self.max_attempts:
            return False
        if outcome_state == JobState.TIMEOUT:
            return self.retry_on_timeout
        return outcome_state == JobState.FAILED
