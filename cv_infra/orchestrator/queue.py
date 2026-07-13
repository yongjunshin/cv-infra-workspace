"""Job queue + state machine + retry policy (M3 §3.3) — REQ-ORCH-003/010/011.

State machine (transitions validated here; the definitions moved verbatim from
scheduler.py — scheduler re-exports them so the P1 import path stays frozen):

    queued -> running -> completed | failed | timeout
    failed | timeout -> queued          (re-queue while attempts remain)

Every transition is persisted when a ``Store`` is attached (REQ-ORCH-011), so a
restart rebuilds the queue via ``JobQueue.restore``. Retry policy
(REQ-ORCH-010): a FAILED (and, by config, TIMEOUT) outcome increments
``attempt_count`` and re-queues while ``attempt_count < max_attempts`` — the
numeric policy (max attempts / backoff) is caller-injected, no constant lives
here (requirements Notes deferral). Stdlib only.
"""

from __future__ import annotations

from collections import deque
from collections.abc import Iterable

from cv_infra.orchestrator.models import Job, JobState
from cv_infra.orchestrator.store import Store

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


class JobQueue:
    """FIFO job queue enforcing the state machine + retry policy, persisted via Store.

    ``store=None`` keeps the queue purely in-memory (unit tests / DoD-P1 CPU
    skeleton); with a store, every enqueue / mark_running / record_outcome
    persists the job's current state so a restart restores it (REQ-ORCH-011).

    Args:
        jobs: initial QUEUED jobs (the fan-out output).
        store: optional SQLite store for transition persistence.
        max_attempts: total run attempts allowed per job (>= 1) before a
            retryable failure becomes terminal.
        retry_on_timeout: whether a TIMEOUT outcome is retried like FAILED
            (M3 §3.3 treats wall-clock timeout as infra-ish, retryable).
    """

    def __init__(
        self,
        jobs: Iterable[Job] = (),
        *,
        store: Store | None = None,
        max_attempts: int = 1,
        retry_on_timeout: bool = True,
    ) -> None:
        if max_attempts < 1:
            raise ValueError(f"max_attempts must be >= 1, got {max_attempts}")
        self._queue: deque[Job] = deque()
        self._store = store
        self._max_attempts = max_attempts
        self._retry_on_timeout = retry_on_timeout
        self.enqueue(jobs)

    @classmethod
    def restore(
        cls,
        store: Store,
        *,
        max_attempts: int = 1,
        retry_on_timeout: bool = True,
    ) -> JobQueue:
        """Rebuild the queue from SQLite after an orchestrator restart.

        QUEUED jobs re-enter the queue; terminal jobs stay terminal (still
        visible via ``store.load_jobs()``). Jobs persisted as RUNNING are
        in-flight orphans awaiting docker-label reconciliation (M3 §3.9, R14 —
        a later cycle); they are NOT silently re-queued here because
        RUNNING -> QUEUED is not a legal transition.
        """
        queued = [job for job in store.load_jobs() if job.state is JobState.QUEUED]
        return cls(
            queued,
            store=store,
            max_attempts=max_attempts,
            retry_on_timeout=retry_on_timeout,
        )

    def enqueue(self, jobs: Iterable[Job]) -> None:
        """Add QUEUED jobs (persisting them). Non-QUEUED input is a caller bug."""
        for job in jobs:
            if job.state is not JobState.QUEUED:
                raise ValueError(f"can only enqueue QUEUED jobs, got {job.state} for {job}")
            self._persist(job)
            self._queue.append(job)

    def pending(self) -> int:
        """Number of jobs waiting for admission."""
        return len(self._queue)

    def pop_next(self) -> Job | None:
        """Remove and return the next waiting job (None when empty). The caller
        (admission gate) transitions it via ``mark_running`` once a slot is held."""
        return self._queue.popleft() if self._queue else None

    def mark_running(self, job: Job) -> None:
        """QUEUED -> RUNNING (admission), persisted."""
        job.state = transition(job.state, JobState.RUNNING)
        self._persist(job)

    def record_outcome(self, job: Job, outcome_state: JobState) -> bool:
        """Apply a terminal attempt outcome + the retry policy; persist the result.

        Increments ``attempt_count`` (this attempt ran), transitions
        RUNNING -> ``outcome_state``, then — if the outcome is retryable and
        attempts remain — re-queues (state back to QUEUED) and returns True.
        Returns False when the job is terminal.
        """
        job.attempt_count += 1
        job.state = transition(job.state, outcome_state)
        if self._should_retry(outcome_state, job.attempt_count):
            job.state = transition(job.state, JobState.QUEUED)
            self._persist(job)
            self._queue.append(job)
            return True
        self._persist(job)
        return False

    def _should_retry(self, outcome_state: JobState, attempt_count: int) -> bool:
        if attempt_count >= self._max_attempts:
            return False
        if outcome_state == JobState.TIMEOUT:
            return self._retry_on_timeout
        return outcome_state == JobState.FAILED

    def _persist(self, job: Job) -> None:
        if self._store is not None:
            self._store.upsert_job(job)
