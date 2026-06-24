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
    """Seam the supervisor drives per job (1잡=1러너=1결과; REQ-ORCH-007).

    Phase 0 pins a synchronous `run(job) -> JobResult` placeholder signature. The real supervisor
    offloads blocking Docker SDK calls off the asyncio event loop (loop.run_in_executor / polling
    wait(timeout=), M3 §3.5); the async shape is a Phase 2/4 decision, not pinned here.
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
