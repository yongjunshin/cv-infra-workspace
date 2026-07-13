"""JobQueue + Store unit tests (M3 §3.3/§3.8) — REQ-ORCH-003/010/011, R-DB.

CPU-only: state-machine home move (queue.py, re-exported from scheduler), the
retry policy (attempt_count++ / re-queue within max_attempts), SQLite(WAL)
persistence of every transition, and restart restore through a FRESH Store
object on the same file (DoD-P4-07 CPU 선행).
"""

from __future__ import annotations

import sqlite3

import pytest

import cv_infra.orchestrator.scheduler as scheduler_mod
from cv_infra.orchestrator.fanout import fan_out
from cv_infra.orchestrator.models import JobState
from cv_infra.orchestrator.queue import IllegalTransitionError, JobQueue, transition
from cv_infra.orchestrator.store import Store, job_key

# --------------------------------------------------------------------------- #
# (a) state-machine home move: queue.py is canonical, scheduler re-exports
# --------------------------------------------------------------------------- #


def test_state_machine_reexported_from_scheduler_is_same_object():
    # The P1 import path (scheduler.transition) must stay frozen after the move.
    assert scheduler_mod.transition is transition
    assert scheduler_mod.IllegalTransitionError is IllegalTransitionError


# --------------------------------------------------------------------------- #
# (b) JobQueue — admission transitions + retry policy (REQ-ORCH-003/010)
# --------------------------------------------------------------------------- #


def test_queue_pop_mark_running_record_completed():
    q = JobQueue(fan_out(["r"], repeats=2))
    assert q.pending() == 2
    job = q.pop_next()
    q.mark_running(job)
    assert job.state is JobState.RUNNING
    assert q.record_outcome(job, JobState.COMPLETED) is False  # terminal — no re-queue
    assert job.state is JobState.COMPLETED
    assert job.attempt_count == 1
    assert q.pending() == 1


def test_queue_retry_requeues_and_increments_attempts_until_exhausted():
    (job,) = fan_out(["r"], repeats=1)
    q = JobQueue([job], max_attempts=3)
    for expected_attempt in (1, 2):
        j = q.pop_next()
        q.mark_running(j)
        assert q.record_outcome(j, JobState.FAILED) is True  # re-queued
        assert j.attempt_count == expected_attempt
        assert j.state is JobState.QUEUED
    j = q.pop_next()
    q.mark_running(j)
    assert q.record_outcome(j, JobState.FAILED) is False  # attempts exhausted
    assert j.state is JobState.FAILED
    assert j.attempt_count == 3
    assert q.pending() == 0


def test_queue_timeout_retry_is_configurable():
    (job,) = fan_out(["r"], repeats=1)
    q = JobQueue([job], max_attempts=2)  # default retry_on_timeout=True
    j = q.pop_next()
    q.mark_running(j)
    assert q.record_outcome(j, JobState.TIMEOUT) is True

    (job2,) = fan_out(["r2"], repeats=1)
    q2 = JobQueue([job2], max_attempts=2, retry_on_timeout=False)
    j2 = q2.pop_next()
    q2.mark_running(j2)
    assert q2.record_outcome(j2, JobState.TIMEOUT) is False  # terminal on first timeout
    assert j2.state is JobState.TIMEOUT


def test_queue_rejects_non_queued_enqueue_and_bad_policy():
    (job,) = fan_out(["r"], repeats=1)
    job.state = JobState.RUNNING
    with pytest.raises(ValueError):
        JobQueue([job])
    with pytest.raises(ValueError):
        JobQueue([], max_attempts=0)


def test_pop_next_on_empty_queue_returns_none():
    assert JobQueue([]).pop_next() is None


def test_record_outcome_rejects_illegal_outcome_state():
    (job,) = fan_out(["r"], repeats=1)
    q = JobQueue([job])
    j = q.pop_next()
    q.mark_running(j)
    with pytest.raises(IllegalTransitionError):
        q.record_outcome(j, JobState.QUEUED)  # RUNNING -> QUEUED is not an outcome


# --------------------------------------------------------------------------- #
# (c) Store — WAL persistence + restart restore (REQ-ORCH-011, R-DB)
# --------------------------------------------------------------------------- #


def test_store_file_is_wal_mode(tmp_path):
    db = tmp_path / "cv.sqlite3"
    with Store(db):
        pass
    # WAL is a PERSISTENT database property — assert it from an independent
    # connection, not through the Store's own internals.
    external = sqlite3.connect(str(db))
    try:
        assert external.execute("PRAGMA journal_mode").fetchone()[0] == "wal"
    finally:
        external.close()


def test_transitions_persist_and_fresh_store_restores_state(tmp_path):
    db = tmp_path / "cv.sqlite3"
    jobs = fan_out(["req-a", "req-b"], repeats=2)  # 4 jobs
    with Store(db) as store:
        q = JobQueue(jobs, store=store)  # enqueue persists QUEUED
        done = q.pop_next()
        q.mark_running(done)
        q.record_outcome(done, JobState.COMPLETED)
        failed = q.pop_next()
        q.mark_running(failed)
        q.record_outcome(failed, JobState.FAILED)  # max_attempts=1 -> terminal
    # "restart": a FRESH Store object (new connection) on the same file.
    with Store(db) as reopened:
        by_key = {job_key(j): j for j in reopened.load_jobs()}
        assert len(by_key) == 4
        assert by_key[job_key(done)].state is JobState.COMPLETED
        assert by_key[job_key(done)].attempt_count == 1
        assert by_key[job_key(failed)].state is JobState.FAILED
        assert by_key[job_key(failed)].attempt_count == 1
        queued = [j for j in by_key.values() if j.state is JobState.QUEUED]
        assert len(queued) == 2
        assert all(j.attempt_count == 0 for j in queued)


def test_restored_queue_resumes_only_queued_jobs_to_terminal(tmp_path):
    db = tmp_path / "cv.sqlite3"
    with Store(db) as store:
        q = JobQueue(fan_out(["req"], repeats=3), store=store)
        first = q.pop_next()
        q.mark_running(first)
        q.record_outcome(first, JobState.COMPLETED)
    with Store(db) as reopened:
        restored = JobQueue.restore(reopened)
        assert restored.pending() == 2  # only the QUEUED jobs re-enter
        while (job := restored.pop_next()) is not None:
            restored.mark_running(job)
            restored.record_outcome(job, JobState.COMPLETED)
    with Store(db) as final:
        assert all(j.state is JobState.COMPLETED for j in final.load_jobs())


def test_timeout_state_persists_and_restores_via_fresh_store(tmp_path):
    # QA probe 1 promoted (p4c1 follow-up ③, scratchpad/qa-p4c1/qa_probe_gaps.py):
    # TIMEOUT is the one M3 §3.3 state the shipped tests never persisted
    # explicitly — pin it to SQLite (raw SQL, no Store internals) AND through a
    # fresh Store restore, attempt_count included.
    db = tmp_path / "cv.sqlite3"
    (job,) = fan_out(["req"], repeats=1)
    with Store(db) as store:
        q = JobQueue([job], store=store, retry_on_timeout=False)
        j = q.pop_next()
        q.mark_running(j)
        assert q.record_outcome(j, JobState.TIMEOUT) is False  # terminal
    external = sqlite3.connect(str(db))
    try:
        assert external.execute("SELECT state, attempt_count FROM jobs").fetchone() == (
            "timeout",
            1,
        )
    finally:
        external.close()
    with Store(db) as reopened:
        (restored,) = reopened.load_jobs()
        assert restored.state is JobState.TIMEOUT
        assert restored.attempt_count == 1


def test_restore_leaves_running_orphans_for_reconciliation(tmp_path):
    # A job persisted RUNNING (orchestrator died mid-flight) must NOT be
    # silently re-queued — docker-label reconciliation owns it (M3 §3.9, R14).
    db = tmp_path / "cv.sqlite3"
    (job,) = fan_out(["req"], repeats=1)
    with Store(db) as store:
        q = JobQueue([job], store=store)
        j = q.pop_next()
        q.mark_running(j)  # persisted RUNNING; "crash" here
    with Store(db) as reopened:
        assert JobQueue.restore(reopened).pending() == 0
        (orphan,) = reopened.load_jobs()
        assert orphan.state is JobState.RUNNING


# --------------------------------------------------------------------------- #
# (d) Store — ROS_DOMAIN_ID rows (M3 §3.6 D-O)
# --------------------------------------------------------------------------- #


def test_domain_id_rows_roundtrip_and_loud_accounting(tmp_path):
    with Store(tmp_path / "cv.sqlite3") as store:
        store.record_domain_id(5, "job-a")
        assert store.domain_ids_in_use() == {5: "job-a"}
        with pytest.raises(sqlite3.IntegrityError):
            store.record_domain_id(5, "job-b")  # live id re-recorded = collision
        store.release_domain_id("job-a")
        assert store.domain_ids_in_use() == {}
        with pytest.raises(KeyError):
            store.release_domain_id("job-a")  # release without allocation is loud
