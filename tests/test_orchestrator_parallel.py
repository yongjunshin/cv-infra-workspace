"""ParallelSupervisor k-parallel asyncio integration (M3 §3.5) — CPU + fake 러너.

DoD-P4-01/02/03/04/06/07/08 CPU 선행: real (thread-level) k parallelism under
the cap, slot return -> waiting-job re-assignment, wall-clock timeout isolation,
crash -> failed -> retry linkage, SQLite persistence + restart restore through
the full parallel path, allocator reclaim 누락 0, and rollup flakiness end to
end. The Runner seam is synchronous — the supervisor offloads it via
loop.run_in_executor (R-DS), which these tests exercise for real (executor
threads, not mocks of asyncio).
"""

from __future__ import annotations

import asyncio
import threading
import time

from cv_infra.orchestrator.allocator import DomainIdAllocator
from cv_infra.orchestrator.fanout import fan_out
from cv_infra.orchestrator.models import Job, JobResult, JobState, Verdict
from cv_infra.orchestrator.queue import JobQueue
from cv_infra.orchestrator.rollup import roll_up
from cv_infra.orchestrator.scheduler import SlotAccountant
from cv_infra.orchestrator.store import Store, job_key
from cv_infra.orchestrator.supervisor import ParallelSupervisor

# --------------------------------------------------------------------------- #
# fakes (thread-run via the supervisor's run_in_executor offload)
# --------------------------------------------------------------------------- #


class ScriptedRunner:
    """Per-job scripted fake: behavior keyed by ``job_key`` (list = per attempt).

    Behaviors: "pass" (COMPLETED+PASS) · "fail-verdict" (COMPLETED+FAIL) ·
    "exit-nonzero" (FAILED — termination contract exit != 0) · "crash" (raises)
    · "sleep:<seconds>" (blocks, then passes — timeout fodder).
    """

    def __init__(self, behaviors: dict[str, str | list[str]] | None = None) -> None:
        self._behaviors = dict(behaviors or {})

    def run(self, job: Job) -> JobResult:
        script = self._behaviors.get(job_key(job), "pass")
        behavior = script.pop(0) if isinstance(script, list) else script
        if behavior == "crash":
            raise RuntimeError(f"scripted crash for {job_key(job)}")
        if behavior.startswith("sleep:"):
            time.sleep(float(behavior.split(":", 1)[1]))
            behavior = "pass"
        if behavior == "pass":
            return JobResult(job=job, state=JobState.COMPLETED, verdict=Verdict.PASS)
        if behavior == "fail-verdict":
            return JobResult(job=job, state=JobState.COMPLETED, verdict=Verdict.FAIL)
        assert behavior == "exit-nonzero", f"unknown script {behavior!r}"
        return JobResult(job=job, state=JobState.FAILED, verdict=None)


class ConcurrencyProbeRunner:
    """Measures TRUE executor-thread concurrency (belt to SlotAccountant's
    suspenders): ``sync_keys`` jobs rendezvous on a barrier, so the k-overlap is
    deterministic — a broken barrier (no overlap within the timeout) crashes the
    job and fails the test loudly."""

    def __init__(self, sync_keys: frozenset[str] = frozenset(), parties: int = 2) -> None:
        self._lock = threading.Lock()
        self._barrier = threading.Barrier(parties)
        self._sync_keys = sync_keys
        self.active = 0
        self.max_active = 0

    def run(self, job: Job) -> JobResult:
        with self._lock:
            self.active += 1
            self.max_active = max(self.max_active, self.active)
        if job_key(job) in self._sync_keys:
            self._barrier.wait(timeout=5.0)  # both must be in flight simultaneously
        with self._lock:
            self.active -= 1
        return JobResult(job=job, state=JobState.COMPLETED, verdict=Verdict.PASS)


def run_supervisor(jobs, runner, k, **kwargs):
    queue = JobQueue(
        jobs,
        store=kwargs.pop("store", None),
        max_attempts=kwargs.pop("max_attempts", 1),
        retry_on_timeout=kwargs.pop("retry_on_timeout", True),
    )
    slots = SlotAccountant(k=k)
    supervisor = ParallelSupervisor(queue, slots, runner, **kwargs)
    results = asyncio.run(supervisor.run())
    return results, supervisor, queue, slots


# --------------------------------------------------------------------------- #
# (a) M > k: k truly parallel, remainder queued, over-launch 0
#     (REQ-ORCH-003/004, NFR-ORCH-003 — DoD-P4-02/06 CPU 선행)
# --------------------------------------------------------------------------- #


def test_m_gt_k_runs_exactly_k_parallel_and_completes_all():
    jobs = fan_out(["req-a", "req-b", "req-c"], repeats=2)  # 6 jobs, FIFO order
    # The first two admissions (req-a:0, req-a:1) must overlap for real.
    probe = ConcurrencyProbeRunner(sync_keys=frozenset({"req-a:0", "req-a:1"}))
    results, _, queue, slots = run_supervisor(jobs, probe, k=2)
    assert len(results) == 6
    assert all(r.state is JobState.COMPLETED for r in results)
    assert probe.max_active == 2  # true thread-level parallelism == k, never above
    assert slots.max_concurrent_observed == 2
    assert slots.over_launch_count == 0  # NFR-ORCH-003
    assert slots.in_use == 0
    assert slots.acquired_total == slots.released_total == 6  # 회수 누락 0
    assert queue.pending() == 0


# --------------------------------------------------------------------------- #
# (b) slot return -> waiting job re-assigned (REQ-ORCH-006 — DoD-P4-03 CPU 선행)
# --------------------------------------------------------------------------- #


def test_freed_slot_is_reassigned_to_waiting_job():
    jobs = fan_out(["req"], repeats=4)
    results, supervisor, _, _ = run_supervisor(jobs, ScriptedRunner(), k=2)
    assert len(results) == 4
    starts = [i for i, (kind, _) in enumerate(supervisor.events) if kind == "start"]
    ends = [i for i, (kind, _) in enumerate(supervisor.events) if kind == "end"]
    assert len(starts) == len(ends) == 4
    # exactly k admissions before any slot returns; the 3rd start requires an end
    assert starts[1] < ends[0] < starts[2]
    # in-flight depth per the event log never exceeds k
    depth = 0
    for kind, _ in supervisor.events:
        depth += 1 if kind == "start" else -1
        assert 0 <= depth <= 2


# --------------------------------------------------------------------------- #
# (c) timeout: kill-classification for THAT job only (REQ-ORCH-009 — DoD-P4-05
#     의 상태 절반; 컨테이너 kill 실증은 GPU 사이클)
# --------------------------------------------------------------------------- #


def test_timeout_marks_only_the_runaway_job_and_others_complete():
    jobs = fan_out(["req"], repeats=3)
    runner = ScriptedRunner({"req:0": "sleep:0.5"})
    results, _, _, slots = run_supervisor(
        jobs, runner, k=2, job_timeout_s=0.1, retry_on_timeout=False
    )
    by_key = {job_key(r.job): r for r in results}
    assert by_key["req:0"].state is JobState.TIMEOUT
    assert by_key["req:1"].state is JobState.COMPLETED  # 타 잡 무영향
    assert by_key["req:2"].state is JobState.COMPLETED
    assert slots.in_use == 0
    assert slots.acquired_total == slots.released_total  # timeout still reclaims


def test_timeout_retry_reclaims_slot_and_domain_then_recovers(tmp_path):
    # QA probe 2 promoted (p4c1 follow-up ③, scratchpad/qa-p4c1/qa_probe_gaps.py):
    # attempt 1 blocks past the watchdog (TIMEOUT) -> slot AND domain id are
    # reclaimed -> attempt 2 recovers. Pins the retry-after-timeout path the
    # shipped tests only covered via crash, plus the attempt-unit domain-id
    # semantics (allocator docstring): between/after attempts nothing is held.
    class TimeoutThenPassRunner:
        """Attempt 1 blocks past the watchdog; attempt 2 returns immediately."""

        def __init__(self) -> None:
            self.calls = 0

        def run(self, job: Job) -> JobResult:
            self.calls += 1
            if self.calls == 1:
                time.sleep(0.5)
            return JobResult(job=job, state=JobState.COMPLETED, verdict=Verdict.PASS)

    db = tmp_path / "cv.sqlite3"
    (job,) = fan_out(["req"], repeats=1)
    with Store(db) as store:
        allocator = DomainIdAllocator(store)
        (result,), sup, _, slots = run_supervisor(
            [job],
            TimeoutThenPassRunner(),
            k=1,
            store=store,
            max_attempts=2,
            allocator=allocator,
            job_timeout_s=0.1,
        )
        assert result.state is JobState.COMPLETED, result
        assert job.attempt_count == 2
        assert slots.acquired_total == slots.released_total == 2  # one slot per attempt
        assert slots.over_launch_count == 0
        assert allocator.in_use() == {}  # domain id reclaimed after each attempt
        key = job_key(job)
        assert sup.events == [("start", key), ("end", key), ("start", key), ("end", key)]
    with Store(db) as reopened:  # recovery is persisted too
        (persisted,) = reopened.load_jobs()
        assert persisted.state is JobState.COMPLETED
        assert persisted.attempt_count == 2


# --------------------------------------------------------------------------- #
# (d) crash -> failed -> retry linkage (REQ-ORCH-010, NFR-EXEC-004 받침 —
#     DoD-P4-07/11 CPU 선행)
# --------------------------------------------------------------------------- #


def test_crash_marks_failed_then_retry_recovers_with_attempt_count():
    (job,) = fan_out(["req"], repeats=1)
    runner = ScriptedRunner({"req:0": ["crash", "pass"]})
    results, _, _, slots = run_supervisor([job], runner, k=1, max_attempts=2)
    (result,) = results
    assert result.state is JobState.COMPLETED  # recovered on attempt 2
    assert job.attempt_count == 2
    assert slots.acquired_total == slots.released_total == 2  # one slot per attempt


def test_crash_fails_only_that_job_others_unaffected():
    jobs = fan_out(["boom", "ok"], repeats=1)
    runner = ScriptedRunner({"boom:0": "crash"})
    results, _, _, _ = run_supervisor(jobs, runner, k=2)
    by_key = {job_key(r.job): r for r in results}
    assert by_key["boom:0"].state is JobState.FAILED
    assert by_key["ok:0"].state is JobState.COMPLETED


# --------------------------------------------------------------------------- #
# (e) full path with SQLite + allocator: persistence, restart restore,
#     domain-id reclaim 0 (REQ-ORCH-008/011 — DoD-P4-07 CPU 선행)
# --------------------------------------------------------------------------- #


def test_parallel_run_persists_terminal_states_and_reclaims_domain_ids(tmp_path):
    db = tmp_path / "cv.sqlite3"
    jobs = fan_out(["req-a", "req-b"], repeats=2)  # 4 jobs
    with Store(db) as store:
        runner = ScriptedRunner({"req-b:1": "exit-nonzero"})
        results, _, _, _ = run_supervisor(
            jobs, runner, k=2, store=store, allocator=DomainIdAllocator(store)
        )
        assert len(results) == 4
        assert store.domain_ids_in_use() == {}  # every domain id released (회수 0 누락)
    with Store(db) as reopened:  # 재기동: 새 객체·새 커넥션이 상태를 복원
        by_key = {job_key(j): j for j in reopened.load_jobs()}
        assert by_key["req-b:1"].state is JobState.FAILED
        for key in ("req-a:0", "req-a:1", "req-b:0"):
            assert by_key[key].state is JobState.COMPLETED
        assert all(j.attempt_count == 1 for j in by_key.values())


def test_concurrent_jobs_hold_live_unique_domain_ids(tmp_path):
    with Store(tmp_path / "cv.sqlite3") as store:
        allocator = DomainIdAllocator(store)
        seen: list[dict[int, str]] = []
        lock = threading.Lock()

        class SnapshotRunner:
            def run(self, job: Job) -> JobResult:
                with lock:
                    snapshot = allocator.in_use()
                    seen.append(snapshot)
                    # the running job holds a LIVE id while it runs
                    assert job_key(job) in snapshot.values()
                return JobResult(job=job, state=JobState.COMPLETED, verdict=Verdict.PASS)

        jobs = fan_out(["req"], repeats=6)
        results, _, _, _ = run_supervisor(jobs, SnapshotRunner(), k=3, allocator=allocator)
        assert len(results) == 6
        assert all(len(snapshot) <= 3 for snapshot in seen)  # live ids never exceed k
        assert allocator.in_use() == {}


# --------------------------------------------------------------------------- #
# (f) rollup end to end: repeats=3 혼합 verdict -> any-fail + flakiness
#     (REQ-ORCH-012/013, SR-10 — DoD-P4-08 CPU 선행)
# --------------------------------------------------------------------------- #


def test_parallel_repeats_roll_up_with_flakiness_and_any_fail_verdict():
    jobs = fan_out(["req"], repeats=3)
    runner = ScriptedRunner({"req:1": "fail-verdict"})  # 1 of 3 repeats fails
    results, _, _, _ = run_supervisor(jobs, runner, k=2)
    rollup = roll_up("req", results)
    assert sorted(rollup.verdicts) == sorted([Verdict.PASS, Verdict.PASS, Verdict.FAIL])
    assert rollup.verdict is Verdict.FAIL  # any-fail=fail 정책
    assert rollup.flakiness == 1 / 3  # 불일치는 별도 표기 (모달 불일치 비율)


def test_uniform_parallel_repeats_are_not_flaky():
    jobs = fan_out(["req"], repeats=3)
    results, _, _, _ = run_supervisor(jobs, ScriptedRunner(), k=3)
    rollup = roll_up("req", results)
    assert rollup.verdict is Verdict.PASS
    assert rollup.flakiness == 0.0
