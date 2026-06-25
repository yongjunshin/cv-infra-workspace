"""k-capped, resource-aware scheduler — CPU skeleton (M3 §3.3/§3.4).

Phase 1 proves the *control-plane* invariants on CPU against a fake Runner, with
NO asyncio / Docker SDK / NVML (those land in Phase 2/4). The scheduler:

* admits queued Jobs into runner slots while holding the concurrency cap ``k``
  (REQ-ORCH-004; over-launch == 0, NFR-ORCH-003);
* drives each admitted Job through the injected ``Runner`` (1잡=1러너=1결과,
  REQ-ORCH-007);
* applies the job state machine, allowing only legal transitions (M3 §3.3,
  REQ-ORCH-003);
* retries FAILED (and, by config, TIMEOUT) jobs up to ``max_attempts``,
  incrementing ``attempt_count`` and re-queueing (REQ-ORCH-010);
* exposes observable counters (``over_launch_count``, ``max_concurrent_observed``)
  so the invariants are unit-test assertable.

Assumptions surfaced (placeholder — formalized in Phase 4):

* ``k`` is an injected fixed cap here. Phase 4 computes
  ``k = min(max_concurrent, floor(VRAM / per_instance), CPU/render_cap)`` from the
  Resource Budget + NVML (LOCKED §7.4); no constant is hardcoded into the
  scheduler.
* Execution is synchronous and wave-structured (the fake Runner returns
  immediately): a wave fills free slots up to ``k``, runs them, reclaims the
  slots, then admits the next wave. The real Phase 2/4 supervisor reclaims each
  slot the instant its runner exits (REQ-ORCH-006) — the per-slot accounting is
  the same, only the timing differs.
* ``retry_on_timeout`` defaults to True because M3 §3.3 treats a wall-clock
  timeout as an infra-ish, retryable outcome; the formal retry policy (max
  attempts / backoff / whether timeout retries) is a Phase 4 deferral.
"""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass

from cv_infra.orchestrator.fake_runner import Runner
from cv_infra.orchestrator.models import Job, JobResult, JobState

# Legal job-state transitions (M3 §3.3 state machine). COMPLETED is strictly
# terminal; FAILED/TIMEOUT are terminal-by-policy but may transition back to
# QUEUED for a retry (re-queue) while attempts remain.
_LEGAL_TRANSITIONS: dict[JobState, frozenset[JobState]] = {
    JobState.QUEUED: frozenset({JobState.RUNNING}),
    JobState.RUNNING: frozenset({JobState.COMPLETED, JobState.FAILED, JobState.TIMEOUT}),
    JobState.FAILED: frozenset({JobState.QUEUED}),
    JobState.TIMEOUT: frozenset({JobState.QUEUED}),
    JobState.COMPLETED: frozenset(),
}


class IllegalTransitionError(ValueError):
    """Raised when a job-state transition is not permitted by M3 §3.3."""


def transition(current: JobState, target: JobState) -> JobState:
    """Validate and return ``target`` iff ``current -> target`` is legal.

    Raises ``IllegalTransitionError`` otherwise (e.g. any transition out of the
    strictly-terminal COMPLETED state, or QUEUED -> COMPLETED without passing
    through RUNNING).
    """
    if target not in _LEGAL_TRANSITIONS[current]:
        raise IllegalTransitionError(f"illegal transition {current} -> {target}")
    return target


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
