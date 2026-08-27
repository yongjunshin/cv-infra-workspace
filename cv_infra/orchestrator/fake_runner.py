"""Fake runner interface + canned stub (M3, Phase 0 skeleton).

The control plane (fanout / queue / scheduler / state-machine / rollup) is developed and
unit-tested on CPU against a *fake* runner so that scheduling/lifecycle logic is decoupled from
the GPU data plane (Isaac Sim). This is the GPU-decoupled foundation for the "제어 평면 CPU 골격"
walking skeleton (DoD-P1-06, M3 §5).

Phase 0 = interface + stub only. The `Runner` Protocol pins the seam the supervisor drives per
job; `FakeRunner` returns a canned outcome. Real container-spawning supervision (Docker SDK,
M3 §3.5) is Phase 2/4; the CPU unit-test bodies that exercise this seam land in Phase 1. Stdlib
only — no third-party runtime dependency.
"""

from __future__ import annotations

from typing import Protocol

from cv_infra.orchestrator.models import Job, JobResult, JobState, Verdict


class Runner(Protocol):
    """Seam the supervisor drives per job (표본 슬롯당 결과 1개; REQ-ORCH-007).

    Phase 0 pins a synchronous `run(job) -> JobResult` placeholder signature. The real supervisor
    offloads blocking Docker SDK calls off the asyncio event loop (loop.run_in_executor / polling
    wait(timeout=), M3 §3.5); the async shape is a Phase 2/4 decision, not pinned here.

    OPTIONAL CAPABILITY (p6 §0-10, deliberately NOT part of this Protocol): a seam may also
    offer `run_batch(jobs) -> list[JobResult]` (n samples of ONE request through ONE container
    pair) and `batch_job_timeout_s(jobs) -> float`. The supervisor DUCK-TYPES both
    (`getattr(runner, "run_batch", None)`) rather than requiring them here, so a seam that only
    knows how to run one job — every fake in this module's CPU tests, and the pre-p6
    `RunJobRunner` itself — stays a complete, valid Runner and keeps the byte-identical
    single-job path. `run_batch` still honors 1잡=1결과 (REQ-ORCH-007) at the SAMPLE level: it
    returns exactly one JobResult per job handed to it; what it shares is the container, not
    the result.
    """

    def run(self, job: Job) -> JobResult: ...


class FakeRunner:
    """Canned runner stub for CPU unit tests (no container, no GPU).

    Returns a fixed terminal outcome so Phase 1 can exercise fan-out / queue / state-machine /
    retry / rollup without a real runner. Default = COMPLETED + PASS (UC-01 happy path); callers
    override `state`/`verdict` to simulate FAILED / TIMEOUT or mixed verdicts for flakiness
    (REQ-ORCH-013).
    """

    def __init__(
        self,
        state: JobState = JobState.COMPLETED,
        verdict: Verdict | None = Verdict.PASS,
    ) -> None:
        self._state = state
        self._verdict = verdict

    def run(self, job: Job) -> JobResult:
        return JobResult(job=job, state=self._state, verdict=self._verdict)


class FakeBatchRunner(FakeRunner):
    """`FakeRunner` + the duck-typed batch CAPABILITY (p6 §0-10) — CPU tests only.

    Exists so the control plane's carrier path (group admission, one slot / one domain id per
    carrier, group-head-only RUNNING, per-sample fold, per-vehicle outer watchdog) is testable
    with no docker at all. `FakeRunner` is deliberately left UNCHANGED so every pre-p6 test
    keeps proving the single-job path.

    Scripting surface:

    * `slot_outcomes` — `{repeat_index: (state, verdict)}` overrides for individual samples
      (everything else gets the constructor default), i.e. the "some samples completed, the
      rest are infra" fold is expressible.
    * `batch_error` — an exception `run_batch` raises instead of returning (the supervisor's
      crash boundary).
    * `batch_timeout_s` — the value `batch_job_timeout_s` reports (the outer-cap scaling
      input). `job_timeout_s` is set as a PUBLIC attribute only when given, because its mere
      PRESENCE is what the supervisor's coherence gate and outer scaling read.

    Observation surface: `batch_calls` (the job lists handed to `run_batch`, in order) and
    `single_calls` (the jobs that took the single-job path) — so "which path did admission
    take" is asserted off a record instead of inferred.
    """

    def __init__(
        self,
        state: JobState = JobState.COMPLETED,
        verdict: Verdict | None = Verdict.PASS,
        *,
        slot_outcomes: dict[int, tuple[JobState, Verdict | None]] | None = None,
        batch_error: Exception | None = None,
        batch_timeout_s: float = 0.0,
        job_timeout_s: float | None = None,
    ) -> None:
        super().__init__(state, verdict)
        self._slot_outcomes = dict(slot_outcomes or {})
        self._batch_error = batch_error
        self._batch_timeout_s = batch_timeout_s
        if job_timeout_s is not None:
            self.job_timeout_s = job_timeout_s  # duck-typed inner-watchdog declaration
        self.batch_calls: list[list[Job]] = []
        self.single_calls: list[Job] = []

    def run(self, job: Job) -> JobResult:
        self.single_calls.append(job)
        return super().run(job)

    def batch_job_timeout_s(self, jobs: list[Job]) -> float:
        return self._batch_timeout_s

    def run_batch(self, jobs: list[Job]) -> list[JobResult]:
        self.batch_calls.append(list(jobs))
        if self._batch_error is not None:
            raise self._batch_error
        results = []
        for job in jobs:
            state, verdict = self._slot_outcomes.get(job.repeat_index, (self._state, self._verdict))
            results.append(JobResult(job=job, state=state, verdict=verdict))
        return results
