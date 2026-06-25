"""Control-plane CPU-skeleton unit tests (DoD-P1-06, M3 §5 / ws3).

Proves the GPU-decoupled control-plane invariants against the fake Runner:
2-axis fan-out, k cap (over-launch 0), retry / ``attempt_count``, state-machine
legality, and request-level rollup / flakiness. The end-to-end test walks the
ws3 scenario (size-2 envelope × repeats=2 → 4 jobs → k=2 → fake result → rollup
→ aggregate). Stdlib + pytest only.
"""

from __future__ import annotations

import pytest

from cv_infra.orchestrator.fake_runner import FakeRunner
from cv_infra.orchestrator.fanout import fan_out
from cv_infra.orchestrator.models import Job, JobResult, JobState, Verdict
from cv_infra.orchestrator.rollup import roll_up
from cv_infra.orchestrator.scheduler import IllegalTransitionError, Scheduler, transition

# ---------------------------------------------------------------------------
# (a) 2-axis fan-out — REQ-ORCH-001/002
# ---------------------------------------------------------------------------


def test_fanout_envelope2_repeats2_yields_four_unique_jobs():
    jobs = fan_out(["req-a", "req-b"], repeats=2)
    assert len(jobs) == 4  # 2 requests × 2 repeats
    keys = {(j.request_id, j.repeat_index) for j in jobs}
    assert len(keys) == 4  # (request_id, repeat_index) unique
    assert all(j.state is JobState.QUEUED and j.attempt_count == 0 for j in jobs)
    for rid in ("req-a", "req-b"):
        assert sorted(j.repeat_index for j in jobs if j.request_id == rid) == [0, 1]


def test_fanout_single_request_repeats1_is_minimal_path():
    jobs = fan_out(["only"], repeats=1)
    assert len(jobs) == 1
    assert jobs[0].repeat_index == 0


def test_fanout_rejects_repeats_below_one():
    with pytest.raises(ValueError):
        fan_out(["req"], repeats=0)


# ---------------------------------------------------------------------------
# (b) k cap — REQ-ORCH-004, NFR-ORCH-003
# ---------------------------------------------------------------------------


def test_scheduler_caps_concurrency_and_never_over_launches():
    jobs = fan_out(["req-a", "req-b"], repeats=2)  # 4 jobs (ws3 scenario)
    sched = Scheduler(runner=FakeRunner(), k=2)
    results = sched.run(jobs)
    assert sched.max_concurrent_observed <= 2
    assert sched.over_launch_count == 0
    assert len(results) == 4
    assert all(r.state is JobState.COMPLETED for r in results)
    assert all(j.state is JobState.COMPLETED for j in jobs)


def test_scheduler_queues_remainder_above_k():
    jobs = fan_out(["r"], repeats=5)  # 5 jobs, k=2 → at most 2 running, 3 wait
    sched = Scheduler(runner=FakeRunner(), k=2)
    results = sched.run(jobs)
    assert sched.max_concurrent_observed <= 2
    assert sched.over_launch_count == 0
    assert len(results) == 5


def test_scheduler_rejects_non_positive_k():
    with pytest.raises(ValueError):
        Scheduler(runner=FakeRunner(), k=0)


# ---------------------------------------------------------------------------
# (c) retry / attempt_count — REQ-ORCH-010
# ---------------------------------------------------------------------------


def test_failed_job_retries_until_max_attempts_then_terminal():
    jobs = fan_out(["r"], repeats=1)
    sched = Scheduler(
        runner=FakeRunner(state=JobState.FAILED, verdict=Verdict.FAIL),
        k=1,
        max_attempts=3,
    )
    results = sched.run(jobs)
    assert jobs[0].attempt_count == 3  # ran 3 times, then gave up
    assert jobs[0].state is JobState.FAILED
    assert len(results) == 1
    assert results[0].state is JobState.FAILED


def test_completed_job_is_not_retried():
    jobs = fan_out(["r"], repeats=1)
    sched = Scheduler(runner=FakeRunner(), k=1, max_attempts=3)
    sched.run(jobs)
    assert jobs[0].attempt_count == 1
    assert jobs[0].state is JobState.COMPLETED


def test_timeout_retry_is_configurable():
    # Default retry_on_timeout=True → TIMEOUT retried like FAILED.
    jobs = fan_out(["r"], repeats=1)
    Scheduler(runner=FakeRunner(state=JobState.TIMEOUT, verdict=None), k=1, max_attempts=2).run(
        jobs
    )
    assert jobs[0].attempt_count == 2
    assert jobs[0].state is JobState.TIMEOUT

    # retry_on_timeout=False → terminal on the first timeout.
    jobs2 = fan_out(["r"], repeats=1)
    Scheduler(
        runner=FakeRunner(state=JobState.TIMEOUT, verdict=None),
        k=1,
        max_attempts=2,
        retry_on_timeout=False,
    ).run(jobs2)
    assert jobs2[0].attempt_count == 1
    assert jobs2[0].state is JobState.TIMEOUT


# ---------------------------------------------------------------------------
# (d) state machine — REQ-ORCH-003
# ---------------------------------------------------------------------------


def test_state_machine_allows_legal_transitions():
    assert transition(JobState.QUEUED, JobState.RUNNING) is JobState.RUNNING
    assert transition(JobState.RUNNING, JobState.COMPLETED) is JobState.COMPLETED
    assert transition(JobState.RUNNING, JobState.FAILED) is JobState.FAILED
    assert transition(JobState.FAILED, JobState.QUEUED) is JobState.QUEUED


@pytest.mark.parametrize(
    ("current", "target"),
    [
        (JobState.COMPLETED, JobState.RUNNING),  # terminal is immutable
        (JobState.COMPLETED, JobState.QUEUED),
        (JobState.QUEUED, JobState.COMPLETED),  # must pass through RUNNING
        (JobState.RUNNING, JobState.QUEUED),  # no direct re-queue from running
    ],
)
def test_state_machine_rejects_illegal_transitions(current, target):
    with pytest.raises(IllegalTransitionError):
        transition(current, target)


def test_completed_is_strictly_terminal():
    for target in JobState:  # no legal outgoing transition from COMPLETED
        with pytest.raises(IllegalTransitionError):
            transition(JobState.COMPLETED, target)


# ---------------------------------------------------------------------------
# (e) rollup / flakiness — REQ-ORCH-012/013, SR-10
# ---------------------------------------------------------------------------


def _results(request_id: str, verdicts: list[Verdict | None]) -> list[JobResult]:
    return [
        JobResult(job=Job(request_id, i), state=JobState.COMPLETED, verdict=v)
        for i, v in enumerate(verdicts)
    ]


def test_rollup_uniform_verdicts_is_not_flaky():
    rollup = roll_up("r", _results("r", [Verdict.PASS, Verdict.PASS, Verdict.PASS]))
    assert rollup.request_id == "r"
    assert rollup.verdicts == [Verdict.PASS, Verdict.PASS, Verdict.PASS]
    assert rollup.flakiness == 0.0


def test_rollup_mixed_verdicts_flags_flakiness():
    rollup = roll_up("r", _results("r", [Verdict.PASS, Verdict.PASS, Verdict.FAIL]))
    assert rollup.verdicts == [Verdict.PASS, Verdict.PASS, Verdict.FAIL]
    assert rollup.flakiness is not None
    assert rollup.flakiness > 0.0


def test_rollup_skips_verdictless_results():
    results = _results("r", [Verdict.PASS, None])  # one job carried no verdict
    rollup = roll_up("r", results)
    assert rollup.verdicts == [Verdict.PASS]
    assert rollup.flakiness == 0.0


# ---------------------------------------------------------------------------
# end-to-end: ws3 full path — fan-out → schedule → rollup
# ---------------------------------------------------------------------------


def test_ws3_end_to_end_fanout_schedule_rollup():
    request_ids = ["req-a", "req-b"]
    jobs = fan_out(request_ids, repeats=2)  # 4 jobs
    sched = Scheduler(runner=FakeRunner(), k=2)  # 2 parallel, 2 queued
    results = sched.run(jobs)
    assert sched.max_concurrent_observed <= 2
    assert sched.over_launch_count == 0
    rollups = {
        rid: roll_up(rid, [r for r in results if r.job.request_id == rid]) for rid in request_ids
    }
    assert set(rollups) == {"req-a", "req-b"}
    for rollup in rollups.values():
        assert rollup.verdicts == [Verdict.PASS, Verdict.PASS]
        assert rollup.flakiness == 0.0
