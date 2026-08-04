"""NEG-5 · 교차통신 0 — 병렬 러너의 ROS 격리(도메인 + 전용 브리지 네트워크).

DoD §6 NEG-5가 **파일명까지 고정**한 negative 스위트(REQ-ORCH-008 · NFR-ORCH-002;
게이트 DoD-P4-05; LOCKED §7.5; R8). p5c10까지 이 경로에 **파일이 없었다** — 게이트의
검증 명령이 실재하지 않는 파일을 가리키고 있었다(전역 G-05 미충족 실측, p5c9
follow-up ⑥). 게이트 문면 2항 ↔ 테스트 매핑:

1. "잡 A에서 잡 B 토픽 구독 -> window 내 수신 메시지 = 0"
   -> ``test_no_job_pair_shares_both_a_domain_and_a_network``   (**CPU 층 한정** — 아래)
2. "``docker inspect <runner>`` -> 네트워크 모드 != ``host``; 잡별 네트워크/도메인 ID 상이"
   -> ``test_no_container_or_network_uses_host_networking``
   -> ``test_every_concurrent_job_gets_its_own_network_and_domain``
   -> ``test_runner_and_sut_of_one_job_share_that_job_s_own_network_and_domain``
   -> ``test_production_rest_path_hands_each_in_flight_job_a_distinct_live_domain``
      (★ 방어가 **생산 경로에 실제로 배선**돼 있는가)

**★ CPU 층 한정 (정직 표기 — G-35: 안 도는 기능 위의 green을 통과로 세지 않는다).**
이 파일은 **"수신 0"을 측정하지 않는다.** 실 DDS 수신 window 측정은 라이브(GPU) 평면
몫이고 이미 실측돼 있다 — p4c4 교차수신 브래킷(99 -> **0** -> 288)과 p5c4의 콜라이딩 쌍
수신 0(pos 96/67/67). CPU에서 증명 가능한 것은 **교차통신의 전제 조건**이다: DDS가 두 잡
사이를 잇으려면 ① 같은 ``ROS_DOMAIN_ID`` **그리고** ② 도달 가능한 공용 네트워크가
**둘 다** 필요한데, 어떤 잡 쌍도 그 둘을 동시에 공유하지 않음을 (실 ``run_job``이 docker
에 넘기는 인자에서) 단정한다. 그래서 테스트 이름도 "수신 0"이라 부르지 않는다 — 이름이
측정하지 않은 것을 주장하면 그것이 곧 공허한 green이다.

**무장(비공허).** 배치는 **순수-해시로 충돌하는 쌍을 반드시 포함**하도록 구성하고 그
전제를 단정한다(p4c6 ``test_orchestrator_allocator_alignment.py``의 관용구 — 그 파일은
M3 소유의 수리 회귀 가드이고, 이 파일은 DoD §6 게이트 증거다). 그래야 "도메인이 전부
달랐다"가 운이 아니라 store-백드 allocator의 작동이 된다.

**DoD-P4-05 각주(선존 결함) 취급.** 각주는 k>=~6 동시 admission에서 ``run_job``의
순수-해시 도메인 도출이 admission allocator와 불일치해 **동시 잡 도메인 중복**(k=8,
도메인 54)을 실측했다고 기록한다. 그 결함은 p4c6 ``92c00a9``(admission 할당 id를
``Job.ros_domain_id``로 ``run_job``까지 전달)에서 CPU 층 수리됐고 p5c4 라이브에서
k=8 유일성 위반 0으로 확인됐다 — 아래 테스트들은 **약화 없이 원래 불변식 그대로**
겨냥하며, 현재 green이다(재발하면 즉시 red). 순수-해시는 **단독 실행 경로의 폴백**으로만
남아 있고 그 계약은 M3 파일이 고정한다.

Stdlib + pytest + 이미 핀된 FastAPI/pydantic. 신규 의존 0 (docker 데몬 불요 — duck-typed
fake client).
"""

from __future__ import annotations

import asyncio
import json
import threading
import time
from pathlib import Path

import yaml
from fastapi.testclient import TestClient

from cv_infra.orchestrator.allocator import DomainIdAllocator
from cv_infra.orchestrator.api import create_app
from cv_infra.orchestrator.fanout import fan_out
from cv_infra.orchestrator.models import Job, JobResult, JobState, Verdict
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
)
from tests.test_supervisor_min import RUNNER_IMAGE, FakeContainer, FakeNetwork

_FIXTURE = Path(__file__).resolve().parents[1] / "tests/fixtures/nova_carter_warehouse_goal.yaml"

#: 병렬 폭 — 각주가 결함을 실측한 구간(k>=~6)을 덮도록 8로 잡는다.
_PARALLEL_WIDTH = 8

#: docker 네트워크 모드 중 격리를 무너뜨리는 값(계약 문면: host networking 금지).
_FORBIDDEN_NETWORK_MODE = "host"


# --------------------------------------------------------------------------- #
# thread-safe multi-job fake docker client (실 run_job 경로를 그대로 태운다)
# --------------------------------------------------------------------------- #


class _Networks:
    def __init__(self, client: _ParallelFakeDocker) -> None:
        self._client = client

    def create(self, name, driver=None, labels=None):
        with self._client.lock:
            self._client.network_calls.append({"name": name, "driver": driver, "labels": labels})
        return FakeNetwork(name, self._client.events)


class _Containers:
    def __init__(self, client: _ParallelFakeDocker) -> None:
        self._client = client

    def run(self, image, **kwargs):
        client = self._client
        with client.lock:
            client.runs.append((image, kwargs))
        # runner: running -> exited(0) so supervision ends and the seeded result is
        # collected; SUT just stays running until teardown.
        if str(kwargs.get("name", "")).endswith("-runner"):
            return FakeContainer("runner", ("running", "exited"), 0, client.events)
        return FakeContainer("sut", ("running",), 0, client.events)


class _ParallelFakeDocker:
    """k개 잡이 동시에 두드려도 안전한 기록용 fake (docker 데몬 불요).

    ``runs``는 모든 ``containers.run(image, **kwargs)``를, ``network_calls``는 모든
    ``networks.create(...)``를 기록한다 — 즉 ``docker inspect``가 볼 값들이 실제로
    무엇으로 넘어갔는지를 CPU에서 검사할 수 있다.
    """

    def __init__(self) -> None:
        self.lock = threading.Lock()
        self.events: list = []
        self.runs: list[tuple[str, dict]] = []
        self.network_calls: list[dict] = []
        self.networks = _Networks(self)
        self.containers = _Containers(self)

    def spawned(self, suffix: str) -> dict[str, dict]:
        """``{job_key: kwargs}`` for every container whose name ends with ``suffix``."""
        return {
            kwargs["labels"][LABEL_JOB_ID]: kwargs
            for _image, kwargs in self.runs
            if str(kwargs.get("name", "")).endswith(suffix)
        }


# --------------------------------------------------------------------------- #
# helpers
# --------------------------------------------------------------------------- #


def _collision_batch(minimum: int = _PARALLEL_WIDTH) -> list[str]:
    """>= ``minimum``개의 request id — 순수-해시 도메인이 **충돌하는 쌍을 반드시 포함**.

    비공허 실증의 전제(G-35): 충돌 없는 배치는 allocator가 죽어 있어도 "도메인 전부
    상이"를 통과시킨다.
    """
    ids: list[str] = []
    by_domain: dict[int, list[str]] = {}
    index = 0
    while True:
        request_id = f"neg5-{index}"
        index += 1
        ids.append(request_id)
        by_domain.setdefault(allocate_ros_domain_id(f"{request_id}:0"), []).append(request_id)
        if len(ids) >= minimum and any(len(members) >= 2 for members in by_domain.values()):
            return ids


def _specced_jobs(request_ids: list[str]) -> list[Job]:
    jobs = fan_out(request_ids, repeats=1)
    for job in jobs:
        job.job_spec = {
            "job_id": job_key(job),
            "sut_image_ref": "carter-sut:neg5",
            "scenario": {"scene": "warehouse"},
        }
    return jobs


def _seed_result_json(out_dir: Path, key: str) -> None:
    """러너가 썼을 자리에 result.json을 미리 둔다(수집 불변식 충족 — REQ-EXEC-013)."""
    result_dir = out_dir / network_name_for(key) / "result"
    result_dir.mkdir(parents=True, exist_ok=True)
    (result_dir / "result.json").write_text(
        json.dumps({"job_id": key, "verdict": "pass"}), encoding="utf-8"
    )


def _run_concurrent_batch(tmp_path: Path) -> tuple[_ParallelFakeDocker, list[str]]:
    """충돌 배치를 **한 번에 전원 동시 admission**으로 구동하고 fake docker를 돌려준다."""
    request_ids = _collision_batch()
    jobs = _specced_jobs(request_ids)
    keys = [job_key(job) for job in jobs]
    for key in keys:
        _seed_result_json(tmp_path, key)

    docker = _ParallelFakeDocker()
    runner = RunJobRunner(
        out_dir=tmp_path,
        runner_image=RUNNER_IMAGE,
        docker_client=docker,
        runner_gpus=False,  # CPU: no docker.types.DeviceRequest
        readiness_probe=lambda container: container.status == "running",
    )
    with Store(tmp_path / "cv.sqlite3") as store:
        supervisor = ParallelSupervisor(
            JobQueue(jobs, store=store, max_attempts=1),
            SlotAccountant(k=len(jobs)),  # k == batch -> 전원이 동시에 in-flight
            runner,
            allocator=DomainIdAllocator(store),
        )
        results = asyncio.run(supervisor.run())
    assert len(results) == len(jobs)
    assert all(result.state is JobState.COMPLETED for result in results), results
    return docker, keys


def _domain_of(kwargs: dict) -> int:
    return int(kwargs["environment"]["ROS_DOMAIN_ID"])


# --------------------------------------------------------------------------- #
# (1) 잡별 네트워크/도메인 상이 (게이트 2항 후단)
# --------------------------------------------------------------------------- #


def test_every_concurrent_job_gets_its_own_network_and_domain(tmp_path):
    """동시 in-flight 잡들이 **각자의** 브리지 네트워크와 도메인 id를 갖는다.

    비공허: 이 배치는 순수-해시로는 **충돌한다**(아래 첫 단정) — 통과는 store-백드
    allocator가 실제로 일했다는 뜻이지 우연이 아니다. DoD-P4-05 각주의 결함(동시 잡
    도메인 중복)이 재발하면 여기서 즉시 red.
    """
    keys_preview = [f"{rid}:0" for rid in _collision_batch()]
    pure = [allocate_ros_domain_id(key) for key in keys_preview]
    assert len(set(pure)) < len(pure), "배치가 순수-해시 충돌을 담지 않는다 — 대조가 공허하다"

    docker, keys = _run_concurrent_batch(tmp_path)
    runners = docker.spawned("-runner")
    assert set(runners) == set(keys)  # 잡마다 러너 1개 (1:1)

    domains = {key: _domain_of(kwargs) for key, kwargs in runners.items()}
    networks = {key: kwargs["network"] for key, kwargs in runners.items()}
    assert len(set(domains.values())) == len(keys), f"도메인 중복: {domains}"
    assert len(set(networks.values())) == len(keys), f"네트워크 중복: {networks}"
    assert all(0 <= domain < ROS_DOMAIN_ID_SPACE for domain in domains.values())  # LOCKED §7.5

    # 라벨도 실제 사용 도메인을 들고 있다 (R14 크래시 재조정이 읽는 값).
    for key, kwargs in runners.items():
        assert kwargs["labels"][LABEL_ROS_DOMAIN_ID] == str(domains[key])


def test_runner_and_sut_of_one_job_share_that_job_s_own_network_and_domain(tmp_path):
    """한 잡 **안에서는** 러너와 SUT가 같은 네트워크/도메인 — 격리는 잡 경계에만 선다.

    (이게 없으면 "전부 다르다"를 SUT까지 갈라서 통과시킬 수 있고, 그러면 잡 내부 DDS
    핸드셰이크가 죽는다 — 격리 negative가 기능을 죽이지 않았음을 보이는 착지 단정.)
    """
    docker, keys = _run_concurrent_batch(tmp_path)
    runners, suts = docker.spawned("-runner"), docker.spawned("-sut")
    assert set(suts) == set(keys)  # 잡마다 SUT 1개

    for key in keys:
        assert _domain_of(suts[key]) == _domain_of(runners[key])
        assert suts[key]["network"] == runners[key]["network"] == network_name_for(key)


# --------------------------------------------------------------------------- #
# (2) host networking 미사용 (게이트 2항 전단 — docker inspect 등가)
# --------------------------------------------------------------------------- #


def test_no_container_or_network_uses_host_networking(tmp_path):
    """모든 컨테이너가 **잡 전용 브리지**에 붙고, host 네트워킹은 어디에도 없다."""
    docker, keys = _run_concurrent_batch(tmp_path)
    assert docker.runs  # 무장: 실제로 컨테이너를 띄웠다

    for _image, kwargs in docker.runs:
        assert kwargs.get("network"), f"네트워크 미지정 컨테이너: {kwargs.get('name')}"
        assert kwargs["network"] != _FORBIDDEN_NETWORK_MODE
        assert "network_mode" not in kwargs, kwargs  # host 모드로 새는 가장 흔한 문법
        assert _FORBIDDEN_NETWORK_MODE not in [
            value for value in kwargs.values() if isinstance(value, str)
        ], kwargs

    assert len(docker.network_calls) == len(keys)  # 잡마다 전용 네트워크 1개
    for call in docker.network_calls:
        assert call["driver"] == "bridge", call  # 전용 bridge (LOCKED §7.5 이중 격리)
        assert call["labels"][LABEL_JOB_ID] in keys


# --------------------------------------------------------------------------- #
# (3) 교차통신 전제 조건 — CPU에서 증명 가능한 절반 (게이트 1항)
# --------------------------------------------------------------------------- #


def test_no_job_pair_shares_both_a_domain_and_a_network(tmp_path):
    """어떤 잡 쌍도 도메인과 네트워크를 **동시에** 공유하지 않는다.

    **CPU 층 한정**: 이것은 "수신 0"의 *측정*이 아니라 그 **전제 조건의 부재**다 — DDS가
    두 잡을 잇으려면 같은 도메인 **그리고** 도달 가능한 공용 네트워크가 둘 다 필요하다.
    실 수신 window 측정은 라이브 평면 몫이며 이미 0으로 실측됐다(p4c4 브래킷 99 -> 0 ->
    288, p5c4 콜라이딩 쌍 수신 0). 여기서 red가 나면 라이브에서 수신 0을 다시 재야 한다.
    """
    docker, keys = _run_concurrent_batch(tmp_path)
    runners = docker.spawned("-runner")
    pairs = [(a, b) for index, a in enumerate(keys) for b in keys[index + 1 :]]
    assert pairs  # 무장: 비교할 쌍이 실제로 있다 (단일 잡이면 이 게이트는 공허하다)

    for a, b in pairs:
        same_domain = _domain_of(runners[a]) == _domain_of(runners[b])
        same_network = runners[a]["network"] == runners[b]["network"]
        assert not (same_domain and same_network), f"{a} <-> {b} 교차통신 전제 성립"
        assert not same_domain and not same_network  # 실제로는 둘 다 갈라져 있다(이중 격리)


# --------------------------------------------------------------------------- #
# (4) ★ 방어가 생산 경로에 배선돼 있는가 (REST 제출 -> admission 할당)
# --------------------------------------------------------------------------- #


class _DomainRecordingRunner:
    """in-flight 잡이 들고 있는 ``ros_domain_id``를 기록하고 게이트까지 붙잡는 fake seam."""

    def __init__(self) -> None:
        self.gate = threading.Event()
        self.lock = threading.Lock()
        self.in_flight: dict[str, int | None] = {}

    def run(self, job: Job) -> JobResult:
        with self.lock:
            self.in_flight[job_key(job)] = job.ros_domain_id
        assert self.gate.wait(timeout=10.0), "test never opened the gate"
        return JobResult(job=job, state=JobState.COMPLETED, verdict=Verdict.PASS)


def test_production_rest_path_hands_each_in_flight_job_a_distinct_live_domain(tmp_path):
    """★ 생산 경로(REST 제출)가 잡마다 **살아 있는 고유 도메인 id**를 실제로 준다.

    ``ParallelSupervisor``의 도메인 할당은 allocator가 **주입됐을 때만** 동작한다
    (없으면 ``job.ros_domain_id`` = None -> ``run_job``이 순수-해시로 재도출 = 각주가
    실측한 중복 결함의 경로). 그래서 "allocator가 있다"를 소스로 읽지 않고 **제출 API를
    실제로 태워** 동시 in-flight 잡들이 들고 있는 id로 확인한다 — 생산 배선이 빠지면 즉시
    red.
    """
    k = 4
    document = yaml.safe_load(_FIXTURE.read_text(encoding="utf-8"))
    runner = _DomainRecordingRunner()

    with Store(tmp_path / "cv.sqlite3") as store:
        app = create_app(store, runner, k=k)
        with TestClient(app) as client:
            response = client.post("/envelopes", json={"requests": [document] * (k * 2)})
            assert response.status_code == 202, response.text
            envelope_id = response.json()["envelope_id"]

            deadline = time.monotonic() + 10.0
            while True:
                with runner.lock:
                    snapshot = dict(runner.in_flight)
                if len(snapshot) >= k:
                    break
                assert time.monotonic() < deadline, f"k개 동시 in-flight를 못 봤다: {snapshot}"
                time.sleep(0.02)

            runner.gate.set()
            drain = time.monotonic() + 10.0
            while client.get(f"/envelopes/{envelope_id}").json()["status"] != "completed":
                assert time.monotonic() < drain, "배치가 완주하지 못했다"
                time.sleep(0.02)

    concurrent = list(snapshot.values())[:k]
    assert all(domain is not None for domain in concurrent), snapshot  # allocator 배선 실증
    assert len(set(concurrent)) == len(concurrent), snapshot  # 동시 잡 도메인 중복 0
    assert all(0 <= domain < ROS_DOMAIN_ID_SPACE for domain in concurrent)
