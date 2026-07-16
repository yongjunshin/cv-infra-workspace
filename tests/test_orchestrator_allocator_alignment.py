"""§7-1 ROS_DOMAIN_ID allocator 정합 (p4c6 T1) — CPU + fake docker client.

Pins the p4c5 부수 발견 수리: admission의 store-백드 충돌회피 ``DomainIdAllocator``가
할당한 id가 ``run_job``까지 전달돼 컨테이너 env/라벨에 박히고, ``run_job``은 더 이상
순수-해시로 도메인을 재도출하지 않는다(충돌회피 없는 재도출이 k>=~6 동시 admission에서
두 잡에 같은 ``ROS_DOMAIN_ID``를 주던 결함 — history 2026-07-15 부수 발견).

세 요구 (task):
 1. 동시 admission 도메인 중복 0 — k>=8, 해시-충돌 배치를 실 run_job(가짜 docker)로
    구동해 동시 in-flight 잡들의 컨테이너 env ROS_DOMAIN_ID 집합에 중복 0.
 2. 전달 실증 — admission이 할당한 id == run_job이 컨테이너 env/라벨에 넣는 id.
 3. 폴백 불변 — ``ros_domain_id=None``(단독 경로)이면 기존 순수-해시 도출과 동일 동작,
    값이 오면 그대로 사용(P2 ``cv-infra run`` 계약 불변).

Positive control (G-35): 배치는 순수-해시로 *충돌하는* 쌍을 반드시 포함하도록 구성하고
그 전제를 단정한다 — 수리 전(``run_job`` line 534가 재도출) 요구 1/2가 red, 수리 후 green.
"""

from __future__ import annotations

import asyncio
import json
import threading
from pathlib import Path

from cv_infra.orchestrator.allocator import DomainIdAllocator
from cv_infra.orchestrator.fanout import fan_out
from cv_infra.orchestrator.models import Job, JobState
from cv_infra.orchestrator.queue import JobQueue
from cv_infra.orchestrator.scheduler import SlotAccountant
from cv_infra.orchestrator.store import Store, job_key
from cv_infra.orchestrator.supervisor import (
    LABEL_JOB_ID,
    LABEL_ROS_DOMAIN_ID,
    ROS_DOMAIN_ID_SPACE,
    ParallelSupervisor,
    RunJobRunner,
    allocate_ros_domain_id,
    network_name_for,
    run_job,
)
from tests.test_supervisor_min import (
    JOB_ID,
    RUNNER_IMAGE,
    SUT_IMAGE,
    FakeClient,
    FakeContainer,
    FakeNetwork,
    make_spec,
    put_result,
)

# --------------------------------------------------------------------------- #
# Thread-safe multi-job fake docker client for the REAL run_job path.
# --------------------------------------------------------------------------- #


class _ParallelNetworks:
    def __init__(self, client: _ParallelFakeDocker) -> None:
        self._client = client

    def create(self, name, driver=None, labels=None):
        return FakeNetwork(name, self._client.events)


class _ParallelContainers:
    def __init__(self, client: _ParallelFakeDocker) -> None:
        self._client = client

    def run(self, image, **kwargs):
        c = self._client
        with c._lock:
            c.runs.append((image, kwargs))
        # runner scripted running->exited (clean exit 0) so supervision ends and
        # the pre-seeded result.json is collected; SUT just stays running.
        if str(kwargs.get("name", "")).endswith("-runner"):
            return FakeContainer("runner", ("running", "exited"), 0, c.events)
        return FakeContainer("sut", ("running",), 0, c.events)


class _ParallelFakeDocker:
    """Records (image, kwargs) for every ``containers.run`` across k parallel jobs.

    Unlike the single-job ``FakeClient`` (test_supervisor_min), this one keeps NO
    per-job mutable slot, so the k executor threads that drive ``run_job`` in
    parallel never clobber each other — the only shared write (``runs``) is
    locked. That lets a k-wide batch be inspected for the ROS_DOMAIN_ID env/label
    each container was spawned with.
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self.events: list = []
        self.runs: list[tuple[str, dict]] = []
        self.networks = _ParallelNetworks(self)
        self.containers = _ParallelContainers(self)


class _RecordingAllocator(DomainIdAllocator):
    """``DomainIdAllocator`` that remembers what it handed each job (전달 실증용)."""

    def __init__(self, store: Store) -> None:
        super().__init__(store)
        self.allocated: dict[str, int] = {}

    def allocate(self, job_id: str) -> int:
        domain = super().allocate(job_id)
        self.allocated[job_id] = domain
        return domain


# --------------------------------------------------------------------------- #
# helpers
# --------------------------------------------------------------------------- #


def _collision_batch(minimum: int = 8) -> list[str]:
    """Deterministic request ids (>= ``minimum``) whose job_keys carry at least one
    PURE-HASH ``ROS_DOMAIN_ID`` collision — the minimal trigger of the defect, so
    the positive control below is non-vacuous (G-35)."""
    ids: list[str] = []
    by_domain: dict[int, list[str]] = {}
    i = 0
    while True:
        rid = f"req-{i}"
        i += 1
        domain = allocate_ros_domain_id(f"{rid}:0")  # pure hash, no in_use — the fallback
        ids.append(rid)
        by_domain.setdefault(domain, []).append(rid)
        collides = any(len(members) >= 2 for members in by_domain.values())
        if len(ids) >= minimum and collides:
            return ids


def _specced_jobs(request_ids: list[str]) -> list[Job]:
    jobs = fan_out(request_ids, repeats=1)
    for job in jobs:
        key = job_key(job)
        job.job_spec = {"job_id": key, "sut_image_ref": "sut:test", "scenario": {}}
    return jobs


def _seed_result_json(out_dir: Path, key: str) -> None:
    result_dir = Path(out_dir) / network_name_for(key) / "result"
    result_dir.mkdir(parents=True, exist_ok=True)
    (result_dir / "result.json").write_text(
        json.dumps({"job_id": key, "verdict": "pass"}), encoding="utf-8"
    )


def _run_batch(jobs, runner, *, store, allocator):
    """k == len(jobs) so every job is admitted (and allocated) concurrently in the
    first admission pass — the whole batch is in flight at once."""
    queue = JobQueue(jobs, store=store, max_attempts=1)
    supervisor = ParallelSupervisor(queue, SlotAccountant(k=len(jobs)), runner, allocator=allocator)
    return asyncio.run(supervisor.run())


def _make_runner(docker: _ParallelFakeDocker, out_dir: Path) -> RunJobRunner:
    return RunJobRunner(
        out_dir=out_dir,
        runner_image=RUNNER_IMAGE,
        docker_client=docker,
        runner_gpus=False,  # CPU: no docker.types.DeviceRequest
        readiness_probe=lambda ct: ct.status == "running",
    )


def _runner_domains_by_job(docker: _ParallelFakeDocker) -> dict[str, tuple[int, int]]:
    """``{job_key: (env_domain, label_domain)}`` over every RUNNER container spawned."""
    out: dict[str, tuple[int, int]] = {}
    for _image, kwargs in docker.runs:
        if not str(kwargs.get("name", "")).endswith("-runner"):
            continue
        key = kwargs["labels"][LABEL_JOB_ID]
        out[key] = (
            int(kwargs["environment"]["ROS_DOMAIN_ID"]),
            int(kwargs["labels"][LABEL_ROS_DOMAIN_ID]),
        )
    return out


# --------------------------------------------------------------------------- #
# (1) 동시 admission 도메인 중복 0 (positive control — 수리 전 red)
# --------------------------------------------------------------------------- #


def test_concurrent_admission_container_domain_ids_are_unique(tmp_path):
    request_ids = _collision_batch()  # >=8, contains a pure-hash collision
    jobs = _specced_jobs(request_ids)
    keys = [job_key(job) for job in jobs]
    # 비공허 실증 (G-35): the FALLBACK (pure-hash) derivation WOULD collide on this
    # batch — so a passing test really exercises the allocator hand-off, not luck.
    pure = [allocate_ros_domain_id(key) for key in keys]
    assert len(set(pure)) < len(pure), "batch must trigger a pure-hash collision"

    for key in keys:
        _seed_result_json(tmp_path, key)
    docker = _ParallelFakeDocker()
    with Store(tmp_path / "cv.sqlite3") as store:
        results = _run_batch(
            jobs, _make_runner(docker, tmp_path), store=store, allocator=DomainIdAllocator(store)
        )
    assert len(results) == len(jobs)
    assert all(r.state is JobState.COMPLETED for r in results)

    domains = _runner_domains_by_job(docker)
    assert len(domains) == len(jobs)  # every runner container captured
    env_ids = [env for env, _label in domains.values()]
    assert len(set(env_ids)) == len(jobs)  # 동시 in-flight 도메인 중복 0
    assert all(0 <= env < ROS_DOMAIN_ID_SPACE for env in env_ids)  # LOCKED §7.5 space


# --------------------------------------------------------------------------- #
# (2) 전달 실증: admission 할당 id == 컨테이너 env + 라벨 id (수리 전 red)
# --------------------------------------------------------------------------- #


def test_admission_allocated_id_is_the_container_env_and_label(tmp_path):
    request_ids = _collision_batch()
    jobs = _specced_jobs(request_ids)
    keys = [job_key(job) for job in jobs]
    for key in keys:
        _seed_result_json(tmp_path, key)

    docker = _ParallelFakeDocker()
    with Store(tmp_path / "cv.sqlite3") as store:
        allocator = _RecordingAllocator(store)
        results = _run_batch(jobs, _make_runner(docker, tmp_path), store=store, allocator=allocator)
    assert all(r.state is JobState.COMPLETED for r in results)
    assert set(allocator.allocated) == set(keys)  # each job allocated exactly once

    domains = _runner_domains_by_job(docker)
    for key in keys:
        env_domain, label_domain = domains[key]
        assert env_domain == allocator.allocated[key]  # 전달: env carries the ALLOCATED id
        assert label_domain == allocator.allocated[key]  # R14 라벨도 실제 사용 id (pin 4)


# --------------------------------------------------------------------------- #
# (3) 폴백 불변: None -> 순수-해시, 값 -> 그대로 (P2 단독 경로 불변)
# --------------------------------------------------------------------------- #


def test_none_falls_back_to_pure_hash_and_a_value_overrides(tmp_path):
    put_result(tmp_path)  # result.json for JOB_ID (test_supervisor_min 관용구)

    # (a) None (default) == the frozen pure-hash single-run derivation.
    client = FakeClient()
    outcome = run_job(make_spec(), tmp_path, RUNNER_IMAGE, SUT_IMAGE, client, poll_interval_s=0.0)
    assert outcome.infra_error is None
    (_, runner_kwargs), (_, sut_kwargs) = client.run_calls
    expected = allocate_ros_domain_id(JOB_ID)
    assert int(runner_kwargs["environment"]["ROS_DOMAIN_ID"]) == expected
    assert runner_kwargs["labels"][LABEL_ROS_DOMAIN_ID] == str(expected)
    assert int(sut_kwargs["environment"]["ROS_DOMAIN_ID"]) == expected

    # (b) an explicit value is used verbatim on env + label + SUT, even != pure hash.
    override = (expected + 7) % ROS_DOMAIN_ID_SPACE
    assert override != expected
    client2 = FakeClient()
    outcome2 = run_job(
        make_spec(),
        tmp_path,
        RUNNER_IMAGE,
        SUT_IMAGE,
        client2,
        poll_interval_s=0.0,
        ros_domain_id=override,
    )
    assert outcome2.infra_error is None
    (_, r2), (_, s2) = client2.run_calls
    assert int(r2["environment"]["ROS_DOMAIN_ID"]) == override
    assert r2["labels"][LABEL_ROS_DOMAIN_ID] == str(override)
    assert int(s2["environment"]["ROS_DOMAIN_ID"]) == override  # SUT shares the passed domain
