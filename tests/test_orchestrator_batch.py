"""Control-plane batch supervision (p6 §0-10/§0-11/§0-12/§0-14) — CPU, fake runners.

The layer ABOVE ``tests/test_supervisor_batch.py`` (which owns the docker-facing carrier
seam). Here the questions are the control plane's:

* **grouping** — ``JobQueue.pop_group`` (sibling drain, ``repeat_index`` order, FIFO at the
  carrier granularity, retry re-grouping);
* **the 2-factor trigger** — a runner seam WITHOUT the ``run_batch`` capability keeps the
  byte-identical single-job path (which is why every pre-p6 CPU test still passes), one WITH
  it carries a whole waiting sibling group in one flight;
* **carrier accounting** — one slot token + one ROS_DOMAIN_ID + one ``(start|end, request_id)``
  event pair per carrier, and only the group HEAD goes RUNNING while it flies (설계 §0-11:
  ``running_k`` must keep meaning "live carriers", and an executor thread must never write
  the store);
* **failure-mode table rows 10~12** — the per-vehicle OUTER watchdog, the seam crash boundary,
  and restart reconciliation of a carrier that was in flight;
* **min_pass_ratio** (설계 §0-14) — the rollup's opt-in threshold, its denominator, and the
  byte-identical None default.
"""

from __future__ import annotations

import asyncio
import copy
import json
import time
from pathlib import Path

import pytest
import yaml
from fastapi.testclient import TestClient

from cv_infra.orchestrator.allocator import DomainIdAllocator
from cv_infra.orchestrator.api import _min_pass_ratio, create_app
from cv_infra.orchestrator.fake_runner import FakeBatchRunner, FakeRunner
from cv_infra.orchestrator.fanout import fan_out
from cv_infra.orchestrator.models import Job, JobResult, JobState, Verdict
from cv_infra.orchestrator.queue import JobQueue
from cv_infra.orchestrator.rollup import roll_up
from cv_infra.orchestrator.scheduler import SlotAccountant
from cv_infra.orchestrator.store import Store, job_key
from cv_infra.orchestrator.supervisor import (
    BATCH_RESULTS_DIRNAME,
    DEFAULT_JOB_TIMEOUT_S,
    JOB_TIMEOUT_MARKER,
    RESULT_OUT_MOUNT,
    BatchRunJobRunner,
    JobOutcome,
    ParallelSupervisor,
    RunJobRunner,
    allocate_ros_domain_id,
    batch_timeout_s,
    network_name_for,
    reconcile_at_restart,
)
from tests.test_orchestrator_api import _submit, _wait_completed

_FIXTURE = Path(__file__).parent / "fixtures" / "nova_carter_warehouse_goal.yaml"
_CANONICAL_DOC = yaml.safe_load(_FIXTURE.read_text(encoding="utf-8"))


def drive(jobs, runner, *, k=2, store=None, max_attempts=1, **kwargs):
    """Drive jobs through the supervisor (test_orchestrator_parallel 의 run_supervisor 관용구)."""
    queue = JobQueue(jobs, store=store, max_attempts=max_attempts)
    slots = SlotAccountant(k=k)
    supervisor = ParallelSupervisor(queue, slots, runner, **kwargs)
    results = asyncio.run(supervisor.run())
    return results, supervisor, queue, slots


# --------------------------------------------------------------------------- #
# (a) JobQueue.pop_group — sibling drain, order, FIFO, re-grouping
# --------------------------------------------------------------------------- #


def test_pop_group_drains_the_heads_siblings_in_repeat_order():
    queue = JobQueue(
        [
            Job("req-a", 2),
            Job("req-b", 0),
            Job("req-a", 0),
            Job("req-a", 1),
        ]
    )
    group = queue.pop_group()
    # The head is still the OLDEST waiting job; its siblings come with it, sorted by the
    # sample index (specs[i] <-> results/<i> <-> repeat_index i).
    assert [(j.request_id, j.repeat_index) for j in group] == [("req-a", 0), ("req-a", 1)] + [
        ("req-a", 2)
    ]
    assert queue.pending() == 1
    remaining = queue.pop_next()
    assert (remaining.request_id, remaining.repeat_index) == ("req-b", 0)


def test_pop_group_preserves_the_relative_order_of_non_siblings():
    """FIFO at the CARRIER granularity: strangers are drained and re-appended in order."""
    queue = JobQueue([Job("a", 0), Job("b", 0), Job("c", 0), Job("a", 1)])
    assert [j.repeat_index for j in queue.pop_group()] == [0, 1]
    assert [queue.pop_next().request_id for _ in range(2)] == ["b", "c"]
    assert queue.pending() == 0


def test_pop_group_on_an_empty_queue_is_the_same_no_op_answer_as_pop_next():
    queue = JobQueue([])
    assert queue.pop_group() == []
    assert queue.pop_next() is None


def test_pop_group_of_one_is_just_the_head():
    queue = JobQueue([Job("solo", 0), Job("other", 0)])
    group = queue.pop_group()
    assert len(group) == 1 and group[0].request_id == "solo"


def test_pop_group_does_not_transition_or_persist(tmp_path):
    """Same contract as ``pop_next``: transitions belong to the admission gate."""
    with Store(tmp_path / "cv.sqlite3") as store:
        queue = JobQueue(fan_out(["req"], repeats=3), store=store)
        group = queue.pop_group()
        assert all(job.state is JobState.QUEUED for job in group)
        assert all(row.state is JobState.QUEUED for row in store.load_jobs())


def test_requeued_siblings_are_regrouped_on_the_next_admission():
    """재시도 재그룹: a retryable failure re-queues the sample, and the NEXT pop_group
    picks up whatever siblings are waiting together at that moment."""
    queue = JobQueue(fan_out(["req"], repeats=3), max_attempts=2)
    group = queue.pop_group()
    for job in group:
        queue.mark_running(job)
    for job in group:
        assert queue.record_outcome(job, JobState.FAILED) is True  # all three re-queued
    assert [j.repeat_index for j in queue.pop_group()] == [0, 1, 2]


# --------------------------------------------------------------------------- #
# (b) the 2-factor trigger (capability x group size)
# --------------------------------------------------------------------------- #


def test_a_runner_without_the_capability_keeps_the_single_job_path():
    """★ 레거시 보존의 구조적 이유: FakeRunner has no ``run_batch``, so admission never even
    calls ``pop_group`` — which is why every pre-p6 CPU test is untouched."""
    runner = FakeRunner()
    assert getattr(runner, "run_batch", None) is None  # 비공허: the gate really is closed
    results, supervisor, _, slots = drive(fan_out(["req"], repeats=3), runner, k=2)
    assert len(results) == 3
    # One event pair PER JOB, keyed on job_key — the pre-p6 accounting (completion order is
    # nondeterministic at k>1, so only the admission order is asserted positionally).
    assert [key for kind, key in supervisor.events if kind == "start"] == [
        "req:0",
        "req:1",
        "req:2",
    ]
    assert sorted(key for kind, key in supervisor.events if kind == "end") == [
        "req:0",
        "req:1",
        "req:2",
    ]
    assert slots.acquired_total == 3  # one slot per JOB — the pre-p6 accounting


def test_the_production_single_job_wrapper_declares_no_batch_capability(tmp_path):
    """양성 대조 for the gate: the capability is a COMPOSITION choice, not a hidden default."""
    plain = RunJobRunner(out_dir=tmp_path, runner_image="runner:test")
    assert getattr(plain, "run_batch", None) is None
    capable = BatchRunJobRunner(out_dir=tmp_path, runner_image="runner:test")
    assert callable(capable.run_batch) and callable(capable.batch_job_timeout_s)


def test_a_capable_runner_carries_the_whole_sibling_group_in_one_flight():
    runner = FakeBatchRunner()
    results, supervisor, _, slots = drive(fan_out(["req"], repeats=3), runner, k=2)
    assert len(results) == 3  # still one JobResult PER SAMPLE (1잡=1결과 at the sample level)
    assert all(r.state is JobState.COMPLETED and r.verdict is Verdict.PASS for r in results)
    assert len(runner.batch_calls) == 1  # ...through ONE carrier
    assert [j.repeat_index for j in runner.batch_calls[0]] == [0, 1, 2]
    assert runner.single_calls == []
    assert slots.acquired_total == slots.released_total == 1  # ONE slot for the carrier
    assert supervisor.events == [("start", "req"), ("end", "req")]  # keyed on the REQUEST


def test_a_capable_runner_still_takes_the_single_job_path_for_a_lone_sample():
    runner = FakeBatchRunner()
    results, supervisor, _, _ = drive(fan_out(["req"], repeats=1), runner, k=2)
    assert len(results) == 1
    assert runner.batch_calls == []
    assert [job_key(j) for j in runner.single_calls] == ["req:0"]
    assert supervisor.events == [("start", "req:0"), ("end", "req:0")]  # job_key, unchanged


def test_a_mixed_envelope_gets_one_carrier_per_request():
    """봉투에 요청이 섞여도 운반체는 요청당 1개 — 형제만 같이 탄다."""
    jobs = [Job("req-a", 0), Job("req-a", 1), Job("req-a", 2), Job("req-b", 0)]
    runner = FakeBatchRunner()
    results, supervisor, _, slots = drive(jobs, runner, k=2)
    assert len(results) == 4
    assert [len(call) for call in runner.batch_calls] == [3]  # req-a rides one carrier
    assert [job_key(j) for j in runner.single_calls] == ["req-b:0"]  # req-b runs alone
    assert sorted(key for kind, key in supervisor.events if kind == "start") == [
        "req-a",
        "req-b:0",
    ]
    assert slots.acquired_total == 2  # two carriers, not four


def test_a_retried_sample_regroups_and_a_leftover_of_one_takes_the_frozen_path():
    runner = FakeBatchRunner(slot_outcomes={1: (JobState.FAILED, None)})
    results, _, queue, _ = drive(fan_out(["req"], repeats=3), runner, k=1, max_attempts=2)
    assert len(results) == 3
    assert len(runner.batch_calls) == 1  # attempt 1 = the 3-sample carrier
    assert [job_key(j) for j in runner.single_calls] == ["req:1"]  # attempt 2 = a lone sample
    assert queue.pending() == 0
    by_index = {r.job.repeat_index: r for r in results}
    assert by_index[1].state is JobState.COMPLETED  # the retry recovered it
    assert by_index[1].job.attempt_count == 2


# --------------------------------------------------------------------------- #
# (c) carrier accounting: one domain id per carrier, reclaimed exactly once
# --------------------------------------------------------------------------- #


def test_one_domain_id_is_allocated_per_carrier_and_stamped_on_every_sample(tmp_path):
    with Store(tmp_path / "cv.sqlite3") as store:
        allocator = DomainIdAllocator(store)
        jobs = fan_out(["req"], repeats=4)
        results, _, _, _ = drive(jobs, FakeBatchRunner(), k=2, store=store, allocator=allocator)
        assert len(results) == 4
        stamped = {job.ros_domain_id for job in jobs}
        assert len(stamped) == 1  # ONE domain for the carrier (LOCKED §7.5 dual isolation)
        assert stamped == {allocate_ros_domain_id("req")}  # allocated under the REQUEST key
        assert allocator.in_use() == {}  # released exactly once — 회수 누락 0


def test_only_the_group_head_goes_running_while_the_carrier_flies(tmp_path):
    """설계 §0-11: n RUNNING rows for one container would read as ``running_k > k``
    over-launch on the operational view, and the executor thread cannot write the store."""

    async def admit_and_snapshot(store):
        queue = JobQueue(fan_out(["req"], repeats=3), store=store)
        supervisor = ParallelSupervisor(queue, SlotAccountant(k=2), FakeBatchRunner())
        in_flight: dict = {}
        supervisor._admit(asyncio.get_running_loop(), in_flight)
        snapshot = {job_key(row): row.state for row in store.load_jobs()}
        await asyncio.wait(in_flight)  # let the flight finish before the loop closes
        return snapshot

    with Store(tmp_path / "cv.sqlite3") as store:
        snapshot = asyncio.run(admit_and_snapshot(store))
    assert snapshot == {
        "req:0": JobState.RUNNING,  # the head
        "req:1": JobState.QUEUED,
        "req:2": JobState.QUEUED,
    }
    assert list(snapshot.values()).count(JobState.RUNNING) == 1  # running_k == carrier count


def test_every_sample_reaches_a_terminal_state_and_is_persisted(tmp_path):
    with Store(tmp_path / "cv.sqlite3") as store:
        results, _, queue, _ = drive(fan_out(["req"], repeats=3), FakeBatchRunner(), store=store)
        assert queue.pending() == 0
        persisted = {job_key(row): (row.state, row.attempt_count) for row in store.load_jobs()}
    assert persisted == {
        "req:0": (JobState.COMPLETED, 1),
        "req:1": (JobState.COMPLETED, 1),
        "req:2": (JobState.COMPLETED, 1),
    }
    assert len(results) == 3


def test_a_carrier_folds_mixed_per_sample_outcomes_independently():
    """P5-13 at the control plane: the carrier is shared, the JUDGEMENTS are not."""
    runner = FakeBatchRunner(
        slot_outcomes={
            1: (JobState.COMPLETED, Verdict.FAIL),
            2: (JobState.TIMEOUT, None),
        }
    )
    results, _, _, _ = drive(fan_out(["req"], repeats=3), runner, k=1)
    by_index = {r.job.repeat_index: (r.state, r.verdict) for r in results}
    assert by_index == {
        0: (JobState.COMPLETED, Verdict.PASS),
        1: (JobState.COMPLETED, Verdict.FAIL),
        2: (JobState.TIMEOUT, None),
    }


# --------------------------------------------------------------------------- #
# (d) failure-mode table rows 10~12 (the control-plane half)
# --------------------------------------------------------------------------- #


class _SlowBatchRunner(FakeBatchRunner):
    """A carrier that blocks in the executor thread — outer-watchdog fodder."""

    def __init__(self, sleep_s: float, **kwargs) -> None:
        super().__init__(**kwargs)
        self._sleep_s = sleep_s

    def run_batch(self, jobs):
        time.sleep(self._sleep_s)
        return super().run_batch(jobs)


@pytest.mark.parametrize(
    ("row_id", "runner_factory", "state", "reason_fragment"),
    [
        # 10. the OUTER (per-vehicle) watchdog fired: every sample of that carrier is
        #     classified TIMEOUT — one container, and the fold cannot know which sample was
        #     in flight (the ones that finished are recovered by the seam's own per-slot
        #     collection, not by this classification).
        (
            "10-outer-watchdog",
            lambda: _SlowBatchRunner(0.5, batch_timeout_s=0.02, job_timeout_s=0.01),
            JobState.TIMEOUT,
            JOB_TIMEOUT_MARKER,
        ),
        # 11. the seam RAISED instead of returning outcomes: every sample of that carrier
        #     fails, carrying the exception message (a bare 'failed' is untraceable).
        (
            "11-seam-crash",
            lambda: FakeBatchRunner(batch_error=RuntimeError("simulated carrier seam crash")),
            JobState.FAILED,
            "runner seam crashed: RuntimeError: simulated carrier seam crash",
        ),
    ],
)
def test_group_boundary_classifies_every_sample_of_the_carrier(
    row_id, runner_factory, state, reason_fragment
):
    results, _, _, slots = drive(
        fan_out(["req"], repeats=3), runner_factory(), k=1, job_timeout_s=0.05
    )
    assert len(results) == 3, row_id
    assert all(r.state is state and r.verdict is None for r in results)
    assert all(reason_fragment in r.infra_error for r in results)
    assert all(r.job.infra_error == r.infra_error for r in results)  # persisted breadcrumb
    assert slots.acquired_total == slots.released_total == 1  # the slot came back
    assert slots.in_use == 0


def test_a_crashing_carrier_does_not_touch_another_requests_carrier():
    """Crash isolation (NFR-EXEC-004 받침) survives the regrouping."""

    class _SelectiveCrash(FakeBatchRunner):
        def run_batch(self, jobs):
            if jobs[0].request_id == "bad":
                raise RuntimeError("only this carrier")
            return super().run_batch(jobs)

    runner = _SelectiveCrash()
    jobs = [Job("bad", 0), Job("bad", 1), Job("good", 0), Job("good", 1)]
    results, _, _, _ = drive(jobs, runner, k=1)
    by_request = {}
    for result in results:
        by_request.setdefault(result.job.request_id, set()).add(result.state)
    assert by_request == {"bad": {JobState.FAILED}, "good": {JobState.COMPLETED}}


def test_restart_reconciliation_fails_only_the_group_head_row(tmp_path):
    """12. 재시작 reconcile: the crash interrupted ONE carrier, and the store says so with
    exactly one RUNNING row — the head. Its siblings were still QUEUED, so they are simply
    restored to the queue (and will be re-grouped by the next admission)."""
    db = tmp_path / "cv.sqlite3"
    with Store(db) as store:
        jobs = fan_out(["req"], repeats=3)
        queue = JobQueue(jobs, store=store)
        queue.mark_running(jobs[0])  # the carrier head, exactly as _admit leaves it
        store.record_domain_id(7, "req")  # allocated under the CARRIER key
    with Store(db) as store:
        restored, report = reconcile_at_restart(store)
        assert (report.orphans_failed, report.orphans_requeued) == (1, 0)  # max_attempts=1
        assert report.domain_ids_cleared == 1
        states = {job_key(row): row.state for row in store.load_jobs()}
        assert states == {
            "req:0": JobState.FAILED,  # the interrupted attempt, counted (poison-job safety)
            "req:1": JobState.QUEUED,
            "req:2": JobState.QUEUED,
        }
        assert "crashed/restarted" in store.load_jobs()[0].infra_error
        # ...and the survivors regroup into one carrier on the next admission.
        assert [j.repeat_index for j in restored.pop_group()] == [1, 2]


# --------------------------------------------------------------------------- #
# (e) the per-vehicle OUTER cap arithmetic (설계 §0-12)
# --------------------------------------------------------------------------- #


class _DeclaringRunner:
    """A seam that declares BOTH inner watchdogs (the production wrapper's shape)."""

    job_timeout_s = 1800.0

    def __init__(self, inner_batch_s: float) -> None:
        self._inner_batch_s = inner_batch_s

    def batch_job_timeout_s(self, jobs) -> float:
        return self._inner_batch_s

    def run(self, job) -> JobResult:  # pragma: no cover - not driven here
        raise AssertionError

    def run_batch(self, jobs) -> list[JobResult]:  # pragma: no cover - not driven here
        raise AssertionError


def _supervisor_with(runner, outer_s):
    return ParallelSupervisor(JobQueue([]), SlotAccountant(k=1), runner, job_timeout_s=outer_s)


def test_outer_cap_is_scaled_by_the_carriers_own_inner_budget():
    """A FIXED outer cap fires FIRST on any sizeable batch (inner grows with n) — the exact
    dual-watchdog inversion the coherence gate exists to prevent."""
    jobs = fan_out(["req"], repeats=6)
    runner = _DeclaringRunner(inner_batch_s=3000.0)
    supervisor = _supervisor_with(runner, 13727.0)
    assert supervisor._group_timeout_s(jobs) == 13727.0 + (3000.0 - 1800.0)
    assert supervisor._group_timeout_s(jobs) > runner.batch_job_timeout_s(jobs)  # 코히어런스 유지


def test_outer_cap_is_not_scaled_when_the_seam_declares_nothing():
    supervisor = _supervisor_with(FakeBatchRunner(), 900.0)
    assert supervisor._group_timeout_s(fan_out(["req"], repeats=3)) == 900.0


def test_no_outer_cap_stays_no_wait_for():
    supervisor = _supervisor_with(_DeclaringRunner(3000.0), None)
    assert supervisor._group_timeout_s(fan_out(["req"], repeats=3)) is None


# --------------------------------------------------------------------------- #
# (f) BatchRunJobRunner: the production carrier wrapper
# --------------------------------------------------------------------------- #


def _job_with_spec(request_id: str, index: int, *, timeout_s: float = 120.0, **extra) -> Job:
    job = Job(request_id, index, **extra)
    job.job_spec = {
        "job_id": f"{request_id}:{index}",
        "sut_image_ref": "carter-sut:test",
        "scenario": {"scene": "warehouse", "timeout_s": timeout_s},
    }
    return job


def _recording_run_batch(calls: list[dict], outcomes_for):
    def fake_run_batch(specs, out_dir, runner_image, sut_image, docker_client=None, **kwargs):
        calls.append(
            {
                "specs": copy.deepcopy(specs),
                "out_dir": out_dir,
                "runner_image": runner_image,
                "sut_image": sut_image,
                "docker_client": docker_client,
                **kwargs,
            }
        )
        return outcomes_for(specs, out_dir, kwargs["batch_id"])

    return fake_run_batch


def test_batch_wrapper_hands_the_seam_ordered_specs_and_the_derived_watchdog(tmp_path):
    calls: list[dict] = []
    runner = BatchRunJobRunner(
        out_dir=tmp_path,
        runner_image="runner:test",
        docker_client="fake-client",
        runner_env={"ACCEPT_EULA": "token"},
        run_batch_fn=_recording_run_batch(
            calls,
            lambda specs, out_dir, batch_id: [
                JobOutcome(spec["job_id"], None, 3, "carrier died") for spec in specs
            ],
        ),
    )
    jobs = [_job_with_spec("req", 2), _job_with_spec("req", 0), _job_with_spec("req", 1)]
    jobs[0].oracle_plugin_dir = "/anchor"
    jobs[1].oracle_plugin_dir = "/anchor"
    jobs[2].oracle_plugin_dir = "/anchor"
    for job in jobs:
        job.ros_domain_id = 11
        job.request_identity_key = "sha256:k"
    results = runner.run_batch(jobs)

    (call,) = calls
    # 배열 순서 = repeat_index (the wire invariant), whatever order admission handed them in.
    assert [spec["job_id"] for spec in call["specs"]] == ["req:0", "req:1", "req:2"]
    assert (call["runner_image"], call["sut_image"]) == ("runner:test", "carter-sut:test")
    assert call["docker_client"] == "fake-client"
    assert call["batch_id"] == "req"  # the carrier key IS the request
    assert call["batch_timeout_s"] == batch_timeout_s(call["specs"])  # 단일 산식
    assert call["runner_env"] == {"ACCEPT_EULA": "token"}
    assert (call["oracle_plugin_dir"], call["ros_domain_id"]) == ("/anchor", 11)
    assert call["request_identity_key"] == "sha256:k"
    # ...and every sample gets its own JobResult, in the same order.
    assert [r.job.repeat_index for r in results] == [0, 1, 2]
    assert all(r.state is JobState.FAILED and r.runner_exit_code == 3 for r in results)


def test_batch_wrapper_folds_recovered_verdicts_and_hostifies_against_the_carrier_root(tmp_path):
    """설계 §0-13 end to end: the doc's container-frame artifacts become HOST paths exactly
    once — the pre-repair fold nested ``results/<i>`` twice and the uploader found nothing."""
    batch_id = "req"
    root = Path(tmp_path) / network_name_for(batch_id) / "result"

    def outcomes_for(specs, out_dir, bid):
        made = []
        for index, spec in enumerate(specs):
            slot = root / BATCH_RESULTS_DIRNAME / str(index)
            slot.mkdir(parents=True, exist_ok=True)
            path = slot / "result.json"
            path.write_text(
                json.dumps(
                    {
                        "job_id": spec["job_id"],
                        "verdict": "pass" if index else "fail",
                        "metrics": {"path_len_m": float(index)},
                        "artifacts": {
                            "mcap": f"{RESULT_OUT_MOUNT}/{BATCH_RESULTS_DIRNAME}/{index}/bag.mcap",
                            "mp4": None,
                        },
                    }
                ),
                encoding="utf-8",
            )
            made.append(JobOutcome(spec["job_id"], path, 0, None))
        return made

    runner = BatchRunJobRunner(
        out_dir=tmp_path,
        runner_image="runner:test",
        run_batch_fn=_recording_run_batch([], outcomes_for),
    )
    results = runner.run_batch([_job_with_spec(batch_id, i) for i in range(2)])
    assert [r.verdict for r in results] == [Verdict.FAIL, Verdict.PASS]
    assert all(r.state is JobState.COMPLETED for r in results)
    for index, result in enumerate(results):
        expected = str(root / BATCH_RESULTS_DIRNAME / str(index) / "bag.mcap")
        assert result.result_doc["artifacts"]["mcap"] == expected
        assert f"{BATCH_RESULTS_DIRNAME}/{index}/{BATCH_RESULTS_DIRNAME}" not in expected
        assert result.result_doc["metrics"] == {"path_len_m": float(index)}
        assert result.result_json_path == str(
            root / BATCH_RESULTS_DIRNAME / str(index) / "result.json"
        )


def test_batch_wrapper_refuses_a_spec_less_job(tmp_path):
    """G-26: a REST job that lost its spec must never silently no-op — same loud message the
    single-job path uses (the supervisor's crash boundary records that attempt FAILED)."""
    runner = BatchRunJobRunner(out_dir=tmp_path, runner_image="runner:test")
    jobs = [_job_with_spec("req", 0), Job("req", 1)]
    with pytest.raises(ValueError, match="carries no job_spec"):
        runner.run_batch(jobs)


def test_batch_wrapper_watchdog_uses_its_own_coefficients(tmp_path):
    runner = BatchRunJobRunner(
        out_dir=tmp_path,
        runner_image="runner:test",
        batch_boot_allowance_s=2000.0,
        batch_wall_factor=3.0,
        batch_iter_overhead_s=5.0,
    )
    jobs = [_job_with_spec("req", 0, timeout_s=10), _job_with_spec("req", 1, timeout_s=20)]
    assert runner.batch_job_timeout_s(jobs) == 2000.0 + (10 * 3 + 5) + (20 * 3 + 5)
    # the single-job watchdog it inherits is untouched (one wrapper, two budgets)
    assert runner.job_timeout_s == DEFAULT_JOB_TIMEOUT_S


# --------------------------------------------------------------------------- #
# (g) min_pass_ratio (설계 §0-14)
# --------------------------------------------------------------------------- #


def _results(*verdicts: Verdict | None) -> list[JobResult]:
    return [
        JobResult(
            job=Job("req", index),
            state=JobState.COMPLETED if verdict is not None else JobState.FAILED,
            verdict=verdict,
        )
        for index, verdict in enumerate(verdicts)
    ]


_P, _F = Verdict.PASS, Verdict.FAIL


@pytest.mark.parametrize(
    ("case", "verdicts", "ratio", "expected"),
    [
        ("none-is-any-fail", (_P, _P, _P, _P, _F), None, _F),
        ("4-of-5-meets-0.8", (_P, _P, _P, _P, _F), 0.8, _P),
        ("3-of-5-misses-0.8", (_P, _P, _P, _F, _F), 0.8, _F),
        ("1.0-is-all-pass", (_P, _P, _P, _P, _F), 1.0, _F),
        ("1.0-passes-when-uniform", (_P, _P, _P), 1.0, _P),
        ("boundary-is-inclusive", (_P, _P, _F, _F), 0.5, _P),
        ("just-below-the-boundary", (_P, _F, _F, _F), 0.5, _F),
        # verdict-less (infra) samples are already OUT of the denominator: 2 of the 3 judged
        # samples passed, and the errored one is not a cheap 'fail' the ratio can average away.
        ("verdict-less-excluded", (_P, _P, _F, None, None), 0.66, _P),
    ],
)
def test_min_pass_ratio_verdict_boundaries(case, verdicts, ratio, expected):
    rollup = roll_up("req", _results(*verdicts), min_pass_ratio=ratio)
    assert rollup.verdict is expected, case


def test_min_pass_ratio_none_is_byte_identical_to_the_frozen_call():
    results = _results(_P, _F, _P)
    assert roll_up("req", results) == roll_up("req", results, min_pass_ratio=None)


def test_min_pass_ratio_leaves_flakiness_and_the_verdict_list_untouched():
    """정책만 바뀐다: the metric and the frozen wire shape do not move with the threshold."""
    results = _results(_P, _P, _P, _P, _F)
    strict = roll_up("req", results)
    relaxed = roll_up("req", results, min_pass_ratio=0.8)
    assert relaxed.verdicts == strict.verdicts
    assert relaxed.flakiness == strict.flakiness == 1 / 5
    assert (relaxed.verdict, strict.verdict) == (_P, _F)  # 비공허: the knob really moved it


def test_all_verdict_less_stays_none_whatever_the_ratio():
    """P5-13: an all-infra request is exit-3 territory, never a fabricated pass/fail."""
    assert roll_up("req", _results(None, None), min_pass_ratio=0.1).verdict is None


@pytest.mark.parametrize(
    ("case", "dump", "expected"),
    [
        ("declared", {"execution_settings": {"min_pass_ratio": 0.8}}, 0.8),
        ("null", {"execution_settings": {"min_pass_ratio": None}}, None),
        ("absent-key", {"execution_settings": {"repeats": 3}}, None),
        ("no-settings", {"scenario": {}}, None),
        ("not-a-mapping", {"execution_settings": 0.8}, None),
        ("int-is-a-ratio", {"execution_settings": {"min_pass_ratio": 1}}, 1.0),
        ("bool-is-not-a-ratio", {"execution_settings": {"min_pass_ratio": True}}, None),
    ],
)
def test_min_pass_ratio_is_read_off_the_request_dump(case, dump, expected):
    assert _min_pass_ratio(dump) == expected, case


def test_min_pass_ratio_reaches_the_live_and_persisted_rollup(tmp_path):
    """End to end through the REST surface: a request that declares 0.8 tolerates ONE failing
    sample out of five, and the SAME policy lands on the persisted rollup + report row."""

    class _FailFirstSample:
        def run(self, job: Job) -> JobResult:
            verdict = Verdict.FAIL if job.repeat_index == 0 else Verdict.PASS
            return JobResult(job=job, state=JobState.COMPLETED, verdict=verdict)

    doc = copy.deepcopy(_CANONICAL_DOC)
    doc["execution_settings"] = {"repeats": 5, "min_pass_ratio": 0.8}
    with Store(tmp_path / "cv.sqlite3") as store:
        app = create_app(store, _FailFirstSample(), k=2)
        with TestClient(app) as client:
            envelope_id = _submit(client, [doc])
            body = _wait_completed(client, envelope_id)
            report = client.get(f"/envelopes/{envelope_id}/report").json()
        (rollup,) = body["rollups"]
        assert rollup["verdicts"] == ["fail", "pass", "pass", "pass", "pass"]
        assert rollup["verdict"] == "pass"  # 4/5 >= 0.8 (any-fail would have said 'fail')
        assert rollup["flakiness"] == 1 / 5  # the instability is still surfaced, separately
        assert report["matrix"][0]["rollup"]["verdict"] == "pass"
        assert store.load_rollup(f"{envelope_id}/r0").verdict is Verdict.PASS
        # SCOPE BOUNDARY, pinned deliberately (설계 §2 "요청 레벨 exit 는 기존
        # rollup->report_outcome 경로 무변경"): ``report_outcome_of`` folds the per-JOB
        # verdicts directly and never consults a rollup, so the ENVELOPE outcome (and the
        # M8 exit code keyed off it) still reads the one failing sample as a fail. Whether a
        # request-level threshold should also relax the envelope gate is a POLICY question
        # this cycle did not answer — pinned here so the answer is a deliberate change, not
        # a surprise (report 미해결 항목).
        assert body["report_outcome"] == "fail"


def test_without_the_ratio_the_same_run_is_a_fail(tmp_path):
    """양성 대조 (G-35): remove ONLY the threshold and the identical run flips to fail — so
    the test above is measuring the policy, not the fixture."""

    class _FailFirstSample:
        def run(self, job: Job) -> JobResult:
            verdict = Verdict.FAIL if job.repeat_index == 0 else Verdict.PASS
            return JobResult(job=job, state=JobState.COMPLETED, verdict=verdict)

    doc = copy.deepcopy(_CANONICAL_DOC)
    doc["execution_settings"] = {"repeats": 5}
    with Store(tmp_path / "cv.sqlite3") as store:
        app = create_app(store, _FailFirstSample(), k=2)
        with TestClient(app) as client:
            body = _wait_completed(client, _submit(client, [doc]))
    assert body["rollups"][0]["verdict"] == "fail"
    assert body["report_outcome"] == "fail"
