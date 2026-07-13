"""JobQueue + Store unit tests (M3 §3.3/§3.8) — REQ-ORCH-003/010/011, R-DB.

CPU-only: state-machine home move (queue.py, re-exported from scheduler), the
retry policy (attempt_count++ / re-queue within max_attempts), SQLite(WAL)
persistence of every transition, and restart restore through a FRESH Store
object on the same file (DoD-P4-07 CPU 선행). p4c4 additions: schema
versioning + v1/v2 in-place upgrades (current stamp v3 — jobs.job_spec, the
p4c4 REST glue), the envelope->request registry and request-rollup persistence
(api.py 유실 해소), and the job-riding stage-5 anchor / canonical job_spec
columns.
"""

from __future__ import annotations

import sqlite3

import pytest

import cv_infra.orchestrator.scheduler as scheduler_mod
from cv_infra.orchestrator.fanout import fan_out
from cv_infra.orchestrator.models import JobState, RequestRollup, Verdict
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


def test_release_all_domain_ids_clears_and_counts(tmp_path):
    with Store(tmp_path / "cv.sqlite3") as store:
        store.record_domain_id(5, "job-a")
        store.record_domain_id(9, "job-b")
        assert store.release_all_domain_ids() == 2  # restart reconciliation sweep
        assert store.domain_ids_in_use() == {}
        assert store.release_all_domain_ids() == 0  # idempotent on an empty table


# --------------------------------------------------------------------------- #
# (e) schema versioning: user_version stamp + v1/v2 files upgrade in place
# --------------------------------------------------------------------------- #

# The EXACT v1 schema as shipped by p4c1 (store.py @ 1fcbc85) — a legacy file
# fixture anchored to the released code, not to the current module (G-28).
_V1_SCHEMA = """
CREATE TABLE IF NOT EXISTS jobs (
    request_id    TEXT    NOT NULL,
    repeat_index  INTEGER NOT NULL,
    state         TEXT    NOT NULL,
    attempt_count INTEGER NOT NULL,
    PRIMARY KEY (request_id, repeat_index)
);
CREATE TABLE IF NOT EXISTS ros_domain_ids (
    domain_id INTEGER PRIMARY KEY,
    job_id    TEXT NOT NULL UNIQUE
);
"""

# The EXACT v2 jobs shape as shipped by p4c4 T1 (store.py @ b0dc1f6) — v2 files
# carry oracle_plugin_dir but predate job_spec (same anchoring discipline).
_V2_JOBS_SCHEMA = """
CREATE TABLE IF NOT EXISTS jobs (
    request_id        TEXT    NOT NULL,
    repeat_index      INTEGER NOT NULL,
    state             TEXT    NOT NULL,
    attempt_count     INTEGER NOT NULL,
    oracle_plugin_dir TEXT,
    PRIMARY KEY (request_id, repeat_index)
);
CREATE TABLE IF NOT EXISTS ros_domain_ids (
    domain_id INTEGER PRIMARY KEY,
    job_id    TEXT NOT NULL UNIQUE
);
"""


def _make_v1_file(db):
    legacy = sqlite3.connect(str(db))
    try:
        legacy.executescript(_V1_SCHEMA)  # v1 files carry user_version 0 (never stamped)
        legacy.execute(
            "INSERT INTO jobs (request_id, repeat_index, state, attempt_count)"
            " VALUES ('old-req', 0, 'completed', 1)"
        )
        legacy.commit()
    finally:
        legacy.close()


def _stamped_version(db) -> int:
    external = sqlite3.connect(str(db))
    try:
        return external.execute("PRAGMA user_version").fetchone()[0]
    finally:
        external.close()


def test_v1_file_upgrades_in_place_and_keeps_rows(tmp_path):
    db = tmp_path / "cv.sqlite3"
    _make_v1_file(db)
    with Store(db) as store:  # opening upgrades: adds the columns + new tables + stamp
        (job,) = store.load_jobs()
        assert job.request_id == "old-req"
        assert job.state is JobState.COMPLETED
        assert job.oracle_plugin_dir is None  # v1 rows read back with a NULL anchor
        assert job.job_spec is None  # ... and a NULL spec (v3 column, p4c4 glue)
        store.record_envelope("env-1", ["r0"])  # new tables exist and accept writes
        assert store.load_envelope("env-1") is not None
    assert _stamped_version(db) == 3


def test_v2_file_upgrades_in_place_and_keeps_rows(tmp_path):
    # A p4c4-T1 file (anchor column, no job_spec) opens, gains the v3 column in
    # place and keeps its rows — same additive 방침 as the v1 path.
    db = tmp_path / "cv.sqlite3"
    legacy = sqlite3.connect(str(db))
    try:
        legacy.executescript(_V2_JOBS_SCHEMA)
        legacy.execute(
            "INSERT INTO jobs (request_id, repeat_index, state, attempt_count,"
            " oracle_plugin_dir) VALUES ('v2-req', 0, 'completed', 1, '/abs/anchor')"
        )
        legacy.execute("PRAGMA user_version = 2")
        legacy.commit()
    finally:
        legacy.close()
    with Store(db) as store:
        (job,) = store.load_jobs()
        assert job.oracle_plugin_dir == "/abs/anchor"  # v2 data survives verbatim
        assert job.job_spec is None
        job.job_spec = {"job_id": "v2-req:0", "sut_image_ref": "img:1"}
        store.upsert_job(job)  # the new column accepts writes
    assert _stamped_version(db) == 3


def test_newer_schema_version_refuses_loudly(tmp_path):
    db = tmp_path / "cv.sqlite3"
    external = sqlite3.connect(str(db))
    try:
        external.execute("PRAGMA user_version = 99")
        external.commit()
    finally:
        external.close()
    with pytest.raises(RuntimeError, match="newer"):
        Store(db)


def test_job_oracle_plugin_dir_roundtrips_via_fresh_store(tmp_path):
    db = tmp_path / "cv.sqlite3"
    anchored, plain = fan_out(["req-a", "req-b"], repeats=1)
    anchored.oracle_plugin_dir = "/abs/scenario-dir"
    with Store(db) as store:
        store.upsert_job(anchored)
        store.upsert_job(plain)
    with Store(db) as reopened:
        by_key = {job_key(j): j for j in reopened.load_jobs()}
        assert by_key["req-a:0"].oracle_plugin_dir == "/abs/scenario-dir"


def test_job_spec_roundtrips_via_fresh_store(tmp_path):
    # p4c4 glue (재기동 대비 영속): the canonical JOB_SPEC dict rides the job
    # row as JSON and restores IDENTICAL through a fresh Store; spec-less jobs
    # stay None (CPU-skeleton compatibility).
    db = tmp_path / "cv.sqlite3"
    specced, plain = fan_out(["req-a", "req-b"], repeats=1)
    spec = {
        "job_id": "req-a:0",
        "scenario": {"scene_ref": "s.usd", "mission": {"nested": [1, 2]}},
        "sut_image_ref": "ghcr.io/x/sut@sha256:abc",
        "interface": {"type": "ros2"},
        "acceptance_criteria": [{"oracle": "reached_goal", "params": {"explicit_null": None}}],
    }
    specced.job_spec = spec
    with Store(db) as store:
        store.upsert_job(specced)
        store.upsert_job(plain)
    with Store(db) as reopened:
        by_key = {job_key(j): j for j in reopened.load_jobs()}
        assert by_key["req-a:0"].job_spec == spec  # deep equality incl. explicit null param
        assert by_key["req-b:0"].job_spec is None
        assert by_key["req-b:0"].oracle_plugin_dir is None


# --------------------------------------------------------------------------- #
# (f) envelope->request registry + request rollups persist (p4c4 유실 해소)
# --------------------------------------------------------------------------- #


def test_envelope_registry_roundtrips_via_fresh_store(tmp_path):
    db = tmp_path / "cv.sqlite3"
    with Store(db) as store:
        store.record_envelope("env-1", ["env-1/r0", "env-1/r1"], [None, "/abs/plugins"])
    with Store(db) as reopened:  # 재기동: 새 객체·새 커넥션이 레지스트리를 복원
        stored = reopened.load_envelope("env-1")
        assert stored is not None
        assert stored.request_ids == ["env-1/r0", "env-1/r1"]  # submission order
        assert stored.oracle_plugin_dirs == [None, "/abs/plugins"]
        assert stored.status == "running"
        assert stored.report_outcome is None
        assert stored.error is None
        assert reopened.load_envelope("env-nope") is None


def test_envelope_completion_persists_outcome_and_error_paths(tmp_path):
    db = tmp_path / "cv.sqlite3"
    with Store(db) as store:
        store.record_envelope("env-ok", ["env-ok/r0"])
        store.record_envelope("env-boom", ["env-boom/r0"])
        store.complete_envelope("env-ok", report_outcome="pass")
        store.complete_envelope("env-boom", error="RuntimeError: kaput")
        with pytest.raises(KeyError):
            store.complete_envelope("env-nope")  # completing an unknown envelope is loud
    with Store(db) as reopened:
        ok = reopened.load_envelope("env-ok")
        assert (ok.status, ok.report_outcome, ok.error) == ("completed", "pass", None)
        boom = reopened.load_envelope("env-boom")
        assert (boom.status, boom.report_outcome, boom.error) == (
            "completed",
            None,
            "RuntimeError: kaput",
        )


def test_fail_running_envelopes_marks_only_running(tmp_path):
    with Store(tmp_path / "cv.sqlite3") as store:
        store.record_envelope("env-done", ["env-done/r0"])
        store.complete_envelope("env-done", report_outcome="pass")
        store.record_envelope("env-live", ["env-live/r0"])
        assert store.fail_running_envelopes("restarted") == 1
        live = store.load_envelope("env-live")
        assert (live.status, live.error) == ("completed", "restarted")
        done = store.load_envelope("env-done")  # already-terminal envelope untouched
        assert (done.status, done.report_outcome, done.error) == ("completed", "pass", None)


def test_request_rollup_roundtrips_via_fresh_store(tmp_path):
    db = tmp_path / "cv.sqlite3"
    mixed = RequestRollup(
        request_id="env-1/r0",
        verdicts=[Verdict.PASS, Verdict.FAIL, Verdict.PASS],
        flakiness=1 / 3,
        verdict=Verdict.FAIL,
    )
    empty = RequestRollup(request_id="env-1/r1")  # all repeats verdict-less (infra)
    with Store(db) as store:
        store.upsert_rollup(mixed)
        store.upsert_rollup(empty)
    with Store(db) as reopened:
        assert reopened.load_rollup("env-1/r0") == mixed  # verdict order preserved
        assert reopened.load_rollup("env-1/r1") == empty
        assert reopened.load_rollup("env-1/r9") is None
