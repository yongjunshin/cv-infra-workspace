"""Restart reconciliation (R14, M3 §3.9) — reconcile_at_restart, CPU-only (p4c4).

Pins the task-① semantics: a job persisted RUNNING (the orchestrator died
mid-flight) is never lost — after the docker-label sweep it is re-labeled as a
FAILED attempt through the normal retry policy (re-queued while attempts
remain, else terminal ``failed``), stale domain-id liveness rows are cleared,
and still-RUNNING envelopes get a loud restart error marker. The docker client
is a duck-typed injected fake (G-20 — no module stubbing); everything is
verified through a FRESH Store object on the same file (restart simulation).
"""

from __future__ import annotations

from cv_infra.orchestrator.allocator import LABEL_JOB_ID, LABEL_ROS_DOMAIN_ID
from cv_infra.orchestrator.fanout import fan_out
from cv_infra.orchestrator.models import JobState
from cv_infra.orchestrator.queue import JobQueue
from cv_infra.orchestrator.store import Store, job_key
from cv_infra.orchestrator.supervisor import reconcile_at_restart

# --------------------------------------------------------------------------- #
# duck-typed docker fakes — the exact sweep surface (list by label + teardown)
# --------------------------------------------------------------------------- #


class FakeStaleContainer:
    def __init__(self, job_id: str, domain_id: int) -> None:
        self.labels = {LABEL_JOB_ID: job_id, LABEL_ROS_DOMAIN_ID: str(domain_id)}
        self.stop_calls = 0
        self.remove_calls = 0

    def stop(self, timeout=None):
        self.stop_calls += 1

    def remove(self, force=False):
        self.remove_calls += 1


class FakeStaleNetwork:
    def __init__(self, name: str) -> None:
        self.name = name
        self.remove_calls = 0

    def remove(self):
        self.remove_calls += 1


class _ListByLabel:
    def __init__(self, items: list, expected_filters: list) -> None:
        self._items = items
        self._filters_seen = expected_filters

    def list(self, all: bool = False, filters: dict | None = None):  # noqa: A002 — SDK name
        self._filters_seen.append(filters)
        return list(self._items)


class FakeSweepClient:
    """Injected docker client for the restart sweep (containers/networks by label)."""

    def __init__(self, containers=(), networks=()) -> None:
        self.stale_containers = list(containers)
        self.stale_networks = list(networks)
        self.filters_seen: list = []
        self.containers = _ListByLabel(self.stale_containers, self.filters_seen)
        self.networks = _ListByLabel(self.stale_networks, self.filters_seen)


def _crash_running_job(store: Store, request_id: str = "req", domain_id: int = 7):
    """Persist one job as RUNNING with a live domain-id row ('crash' fixture)."""
    (job,) = fan_out([request_id], repeats=1)
    queue = JobQueue([job], store=store)
    popped = queue.pop_next()
    queue.mark_running(popped)
    store.record_domain_id(domain_id, job_key(popped))
    return popped


# --------------------------------------------------------------------------- #
# (a) RUNNING orphan -> FAILED attempt -> re-queue within budget (재큐 경로)
# --------------------------------------------------------------------------- #


def test_running_orphan_requeues_within_budget_and_persists(tmp_path):
    db = tmp_path / "cv.sqlite3"
    with Store(db) as store:
        _crash_running_job(store)
    with Store(db) as reopened:  # restart: fresh Store object, new connection
        queue, report = reconcile_at_restart(reopened, max_attempts=2)
        assert queue.pending() == 1  # the orphan re-entered the queue
        assert report.orphans_requeued == 1
        assert report.orphans_failed == 0
        assert report.domain_ids_cleared == 1
        assert reopened.domain_ids_in_use() == {}  # no ghost liveness row
        (persisted,) = reopened.load_jobs()
        assert persisted.state is JobState.QUEUED  # re-label persisted, not in-memory only
        assert persisted.attempt_count == 1  # the interrupted attempt was consumed
        # the returned queue is driveable to a terminal state right away
        job = queue.pop_next()
        queue.mark_running(job)
        assert queue.record_outcome(job, JobState.COMPLETED) is False
    with Store(db) as final:
        (job,) = final.load_jobs()
        assert job.state is JobState.COMPLETED
        assert job.attempt_count == 2


def test_running_orphan_with_exhausted_budget_is_terminal_failed(tmp_path):
    db = tmp_path / "cv.sqlite3"
    with Store(db) as store:
        _crash_running_job(store)
    with Store(db) as reopened:
        queue, report = reconcile_at_restart(reopened, max_attempts=1)
        assert queue.pending() == 0
        assert report.orphans_requeued == 0
        assert report.orphans_failed == 1
        (persisted,) = reopened.load_jobs()
        assert persisted.state is JobState.FAILED  # failed-with-label, not lost
        assert persisted.attempt_count == 1


def test_reconcile_without_orphans_is_a_no_op_on_jobs(tmp_path):
    db = tmp_path / "cv.sqlite3"
    with Store(db) as store:
        q = JobQueue(fan_out(["req"], repeats=2), store=store)
        done = q.pop_next()
        q.mark_running(done)
        q.record_outcome(done, JobState.COMPLETED)
    with Store(db) as reopened:
        queue, report = reconcile_at_restart(reopened)
        assert queue.pending() == 1  # the still-QUEUED job re-enters, untouched
        assert report.orphans_requeued == report.orphans_failed == 0
        states = sorted(j.state for j in reopened.load_jobs())
        assert states == [JobState.COMPLETED, JobState.QUEUED]


# --------------------------------------------------------------------------- #
# (b) docker-label sweep: stale containers/networks torn down BEFORE re-queue
# --------------------------------------------------------------------------- #


def test_sweep_removes_labeled_containers_and_networks(tmp_path):
    db = tmp_path / "cv.sqlite3"
    with Store(db) as store:
        orphan = _crash_running_job(store, domain_id=7)
        client = FakeSweepClient(
            containers=[
                FakeStaleContainer(job_key(orphan), 7),  # runner
                FakeStaleContainer(job_key(orphan), 7),  # SUT (same labels)
            ],
            networks=[FakeStaleNetwork("cvj-req-0-deadbeef")],
        )
        queue, report = reconcile_at_restart(store, client, max_attempts=2)
        assert report.containers_removed == 2
        assert report.networks_removed == 1
        for container in client.stale_containers:
            assert container.stop_calls == 1
            assert container.remove_calls == 1
        assert client.stale_networks[0].remove_calls == 1
        # both list calls filtered on the reconciliation label (never 'all containers')
        assert client.filters_seen == [{"label": LABEL_JOB_ID}] * 2
        assert queue.pending() == 1  # sweep first, THEN the orphan re-queued


def test_reconcile_without_docker_client_skips_sweep_only(tmp_path):
    with Store(tmp_path / "cv.sqlite3") as store:
        _crash_running_job(store)
        queue, report = reconcile_at_restart(store, max_attempts=2)
        assert report.containers_removed == report.networks_removed == 0
        assert report.orphans_requeued == 1  # store-side reconciliation still runs
        assert report.domain_ids_cleared == 1
        assert queue.pending() == 1


# --------------------------------------------------------------------------- #
# (c) envelope marker: still-RUNNING envelopes fail loudly (stuck-'running' 금지)
# --------------------------------------------------------------------------- #


def test_running_envelope_gets_loud_restart_error_marker(tmp_path):
    db = tmp_path / "cv.sqlite3"
    with Store(db) as store:
        store.record_envelope("env-live", ["env-live/r0"])
        store.record_envelope("env-done", ["env-done/r0"])
        store.complete_envelope("env-done", report_outcome="pass")
    with Store(db) as reopened:
        _, report = reconcile_at_restart(reopened)
        assert report.envelopes_failed == 1
        live = reopened.load_envelope("env-live")
        assert live.status == "completed"
        assert live.error is not None
        assert "restarted" in live.error  # loud marker names the cause
        done = reopened.load_envelope("env-done")  # terminal envelope untouched
        assert (done.report_outcome, done.error) == ("pass", None)
