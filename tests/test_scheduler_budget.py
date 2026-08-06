"""NEG-4 · 과다기동 0 — 자원 예산을 넘겨 러너를 기동하지 않는다.

DoD §6 NEG-4가 **파일명까지 고정**한 negative 스위트(REQ-ORCH-005 · REQ-ORCH-004 ·
NFR-ORCH-003; 게이트 DoD-P4-06). p5c10까지 이 경로에 **파일이 없었다** — 게이트의 검증
명령이 실재하지 않는 파일을 가리키고 있었다(전역 G-05 미충족 실측, p5c9 follow-up ⑥).
게이트 문면 2항 ↔ 테스트 매핑:

1. "batch >> 예산 제출 후 ``docker ps``로 러너 컨테이너 수 주기 샘플 -> 상시 <= k"
   -> ``test_operational_view_samples_running_at_most_k_while_the_rest_wait``
      (**CPU 층 한정** — 아래 참조)
2. "``pytest tests/test_scheduler_budget.py`` — over-launch counter == 0;
   k 초과 admit 시도 시 거부/대기 단정"
   -> ``test_admission_gate_refuses_the_launch_past_k_then_a_release_reopens_it``
   -> ``test_batch_far_over_budget_runs_exactly_k_and_the_rest_wait``
   -> ``test_measured_budget_k_caps_the_launches``      (예산 -> 기동 수 연결)
   -> ``test_over_launch_counter_is_zero_by_construction_and_the_armed_channel_is_concurrency``
      (★ 이 게이트의 함정 — 아래)

**★ 이 게이트의 함정: "over-launch counter == 0"은 카운터가 죽어 있어도 참이다.**
``SlotAccountant.over_launch_count``의 증가 분기는 ``try_acquire``의 게이트가 성립하는
한 **도달 불가능**하다(``in_use < k`` 이면 ``in_use + 1 <= k``). 즉 그 0은 *측정값*이
아니라 **by-construction 상수**다. 그래서 아래 ★ 테스트는 게이트를 제거한 accountant로
같은 배치를 돌려 **카운터는 여전히 0인데 관측 채널(동시성)은 초과를 본다**는 것을
보인다 — 이 게이트의 하중을 지는 증거는 카운터가 아니라 **관측된 동시 기동 수**다.
같은 이유로 ``/monitor.json``의 ``resources.over_launch_count`` 도 증거로 세지 않는다:
P4 구조상 ``SlotAccountant``는 봉투마다 임시 객체라 그 필드는 **아직 실 accountant에
배선돼 있지 않고**(monitor.py 모듈 docstring의 PM 룰링) 샘플이 없으면 그냥 기본값 0이
나온다. 채널 자체가 살아 있음(0이 하드코딩이 아님)만
``test_monitor_over_launch_field_is_a_live_channel_not_a_hardcoded_zero``가 단정한다.

**p5c12 (결정 2026-08-05 D-7 (C)): 죽은 카운터 대신 예산 `k`를 노출한다.** 운영 표면이
``resources.concurrency_budget_k``를 실어 운영자가 ``running_k > k``를 **직접** 판정한다
— 카운터를 살아 보이게 만드는 대신(허위 활성화 금지) 판정 재료를 준다.
``test_surfaced_budget_k_is_the_measured_budget_the_admission_uses``가 그 값이 admission이
실제로 쓰는 예산과 같은 수임을 단정한다.

**CPU 층 한정 (정직 표기 — G-35).** 여기서 세는 "기동"은 **admission + 실행 seam 진입**
이다(fake 러너 = 실행자 스레드). 실제 **러너 컨테이너 수**를 ``docker ps``로 주기
샘플하는 절반은 GPU/라이브 평면 몫이며 p4c5 배치 실측(k=4·k=8, 초과 0)이 그 증거다 —
이 파일은 그것을 대체하지 않는다. 동시성 측정은 mock이 아니라 **실 executor 스레드**의
겹침이다(barrier 랑데부가 겹침을 결정적으로 강제하고, 겹치지 못하면 loud하게 깨진다).

Stdlib + pytest + 이미 핀된 FastAPI/pydantic. 신규 의존 0.
"""

from __future__ import annotations

import asyncio
import json
import threading
import time
from pathlib import Path

import yaml
from fastapi.testclient import TestClient

from cv_infra.orchestrator.api import create_app
from cv_infra.orchestrator.fanout import fan_out
from cv_infra.orchestrator.models import Job, JobResult, JobState, Verdict
from cv_infra.orchestrator.monitor import ResourceHealthSampler, build_operational_record
from cv_infra.orchestrator.queue import JobQueue
from cv_infra.orchestrator.scheduler import SlotAccountant, compute_k
from cv_infra.orchestrator.store import Store, job_key
from cv_infra.orchestrator.supervisor import ParallelSupervisor

#: 소비자 시나리오의 플랫폼 사본(M1 표준 입력) — REST 배치의 실물 요청.
_FIXTURE = Path(__file__).resolve().parents[1] / "tests/fixtures/nova_carter_warehouse_goal.yaml"

#: 배치가 예산을 확실히 넘도록 하는 배수(batch >> k — 게이트 문면).
_OVER_BUDGET_FACTOR = 4


# --------------------------------------------------------------------------- #
# fakes — 실 executor 스레드에서 도는 러너 seam
# --------------------------------------------------------------------------- #


class _SyncProbeRunner:
    """동시 기동 수를 **실측**하는 fake seam (barrier로 겹침을 결정적으로 만든다).

    ``sync_keys``의 잡들은 ``parties``-party barrier에서 랑데부한다: 그만큼이 동시에
    in-flight가 되지 못하면 timeout으로 barrier가 깨지고 잡이 예외를 던져 **테스트가
    loud하게 실패**한다(조용한 통과 없음). ``max_active``는 어느 순간에도 겹쳐 있던
    최대 실행 수 — 이 게이트의 하중을 지는 관측값이다.
    """

    def __init__(self, sync_keys: frozenset[str], parties: int, timeout_s: float = 5.0) -> None:
        self._lock = threading.Lock()
        self._barrier = threading.Barrier(parties)
        self._sync_keys = sync_keys
        self._timeout_s = timeout_s
        self.active = 0
        self.max_active = 0

    def run(self, job: Job) -> JobResult:
        with self._lock:
            self.active += 1
            self.max_active = max(self.max_active, self.active)
        try:
            if job_key(job) in self._sync_keys:
                self._barrier.wait(timeout=self._timeout_s)
        finally:
            with self._lock:
                self.active -= 1
        return JobResult(job=job, state=JobState.COMPLETED, verdict=Verdict.PASS)


class _UngatedAccountant(SlotAccountant):
    """게이트를 제거한 accountant — **과다기동을 실제로 일으키는** 양성 대조(G-35).

    ``try_acquire``가 늘 True라 admission이 k를 넘겨 채운다. 이 상태에서 무엇이 초과를
    보고 무엇이 못 보는지가 이 게이트의 증거 선택을 결정한다.
    """

    def try_acquire(self) -> bool:
        self.in_use += 1
        self.acquired_total += 1
        if self.in_use > self.k:
            self.over_launch_count += 1
        self.max_concurrent_observed = max(self.max_concurrent_observed, self.in_use)
        return True


class _GatedRunner:
    """게이트가 열릴 때까지 실행을 붙잡아 두는 fake seam (RUNNING 창 관측용)."""

    def __init__(self) -> None:
        self.gate = threading.Event()

    def run(self, job: Job) -> JobResult:
        assert self.gate.wait(timeout=10.0), "test never opened the gate"
        return JobResult(job=job, state=JobState.COMPLETED, verdict=Verdict.PASS)


class _FakeGauge:
    """주입식 VRAM 게이지 (실측-후-기입 규율: 여기 숫자는 **테스트 입력**이지 NFR 값이 아니다)."""

    def __init__(self, available_mb: float) -> None:
        self._available_mb = available_mb

    def available_vram_mb(self) -> float:
        return self._available_mb


def _run_batch(jobs, runner, slots, **kwargs):
    """한 배치를 실 ParallelSupervisor로 구동하고 (results, supervisor)를 돌려준다."""
    supervisor = ParallelSupervisor(JobQueue(jobs, max_attempts=1), slots, runner, **kwargs)
    return asyncio.run(supervisor.run()), supervisor


def _max_in_flight(supervisor: ParallelSupervisor) -> int:
    """이벤트 로그(start/end 시간순)에서 관측된 최대 in-flight 깊이."""
    depth = maximum = 0
    for kind, _key in supervisor.events:
        depth += 1 if kind == "start" else -1
        maximum = max(maximum, depth)
    return maximum


# --------------------------------------------------------------------------- #
# (1) admission: k 초과 시도 -> 거부, 반납 -> 재개 (게이트 문면 2항 후단)
# --------------------------------------------------------------------------- #


def test_admission_gate_refuses_the_launch_past_k_then_a_release_reopens_it():
    """k 초과 admit 시도는 **거부**되고(기동 자체가 없음), 슬롯 반납 후 **대기**가 풀린다."""
    k = 3
    slots = SlotAccountant(k=k)
    assert [slots.try_acquire() for _ in range(k)] == [True] * k
    assert slots.in_use == k

    assert slots.try_acquire() is False  # 거부 — 기동은 일어나지 않는다
    assert slots.in_use == k  # 거부가 상태를 오염시키지 않는다
    assert slots.acquired_total == k  # 거부는 취득으로 세지 않는다

    slots.release()  # 러너 종료 -> 슬롯 반환 (REQ-ORCH-006)
    assert slots.try_acquire() is True  # 대기하던 기동이 이제 허용된다
    assert slots.max_concurrent_observed == k  # 상시 <= k
    assert slots.over_launch_count == 0


# --------------------------------------------------------------------------- #
# (2) batch >> 예산: 정확히 k 병렬, 나머지 대기, 초과 0
# --------------------------------------------------------------------------- #


def test_batch_far_over_budget_runs_exactly_k_and_the_rest_wait():
    """예산의 4배 배치를 던져도 동시 기동은 상시 <= k, 나머지는 대기 후 완주한다.

    비공허(G-35): barrier가 **정확히 k개의 동시 실행을 요구**하므로 통과는 "아무것도 안
    돌았다"와 구분된다 — 겹침이 k에 못 미치면 barrier가 깨져 loud하게 실패한다.
    """
    k = 3
    jobs = fan_out(["req"], repeats=k * _OVER_BUDGET_FACTOR)  # 12 잡
    keys = [job_key(job) for job in jobs]
    probe = _SyncProbeRunner(sync_keys=frozenset(keys[:k]), parties=k)
    slots = SlotAccountant(k=k)

    results, supervisor = _run_batch(jobs, probe, slots)

    assert len(results) == len(jobs)
    assert all(result.state is JobState.COMPLETED for result in results)  # 나머지도 완주
    assert probe.max_active == k  # 실 스레드 겹침 == k (초과 0 · 미달 0)
    assert _max_in_flight(supervisor) == k  # 이벤트 로그에서도 상시 <= k
    assert slots.max_concurrent_observed == k
    assert slots.over_launch_count == 0  # 게이트 문면의 카운터 (해석은 ★ 테스트 참조)
    assert slots.in_use == 0
    assert slots.acquired_total == slots.released_total == len(jobs)  # 회수 누락 0


def test_measured_budget_k_caps_the_launches():
    """예산에서 나온 k가 **기동 수를 실제로 제한**한다 (계약 문면의 k=min(...) 연결).

    운영자 상한(8)보다 VRAM 항이 더 빡빡한 구성을 만들어 k=2를 얻고, 그 k로 배치를
    돌려 동시 기동이 2를 넘지 않음을 본다. 숫자는 **테스트 입력**이지 NFR 값이 아니다
    (실측-후-기입 §2-4 — 실 VRAM/k는 nfr-measurement-notes 소관).
    """
    k = compute_k(8, vram_gauge=_FakeGauge(5000.0), vram_per_instance_mb=2000.0)
    assert k == 2 < 8  # 예산 항이 운영자 상한보다 작다 — 대조가 성립한다

    jobs = fan_out(["req"], repeats=k * _OVER_BUDGET_FACTOR)
    keys = [job_key(job) for job in jobs]
    probe = _SyncProbeRunner(sync_keys=frozenset(keys[:k]), parties=k)
    slots = SlotAccountant(k=k)

    results, supervisor = _run_batch(jobs, probe, slots)

    assert all(result.state is JobState.COMPLETED for result in results)
    assert probe.max_active == k
    assert _max_in_flight(supervisor) == k
    assert slots.over_launch_count == 0


# --------------------------------------------------------------------------- #
# (3) ★ 카운터의 성질: 0은 by-construction이고, 하중은 관측된 동시성이 진다
# --------------------------------------------------------------------------- #


def test_over_launch_counter_is_zero_by_construction_and_the_armed_channel_is_concurrency():
    """★ 게이트를 **제거**해 실제 과다기동을 일으키면: 카운터는 여전히 0, 동시성은 초과를 본다.

    ``SlotAccountant.over_launch_count``의 증가 분기는 게이트가 성립하는 한 도달 불가능
    (``in_use < k`` -> ``in_use + 1 <= k``)이므로, 정상 경로의 ``== 0``은 측정이 아니라
    **상수**다. 여기서는 게이트를 없앤 accountant로 k+1개가 실제로 겹치게 만든다:

    * barrier(parties = k+1)가 **랑데부에 성공한다** = k를 넘겨 기동했다는 직접 관측
      (게이트가 살아 있으면 이 랑데부는 성립할 수 없어 barrier가 깨진다 — 대조군 A).
    * 그럼에도 ``over_launch_count``는... 이 서브클래스에서만 세진다 — 즉 게이트를 통과한
      과다 admission을 원래 카운터는 **구조적으로 못 본다**.

    결론(게이트 판정에 그대로 쓴다): NEG-4의 증거는 카운터 값이 아니라 **관측된 동시
    기동 수 <= k**이며, 라이브 평면에서는 그 관측이 ``docker ps`` 주기 샘플이다.
    """
    k = 3
    total = k * _OVER_BUDGET_FACTOR
    sync_keys = frozenset(job_key(job) for job in fan_out(["req"], repeats=total)[: k + 1])

    # 대조군 A — 정상 게이트: k+1 랑데부는 성립할 수 없다(과다기동이 없으므로).
    # barrier가 깨지면 그 예외는 실행 seam 안에서 나므로 supervisor의 크래시 경계가
    # 그 잡을 FAILED로 기록한다 — 그 기록이 "겹치지 못했다"의 관측이다.
    gated_probe = _SyncProbeRunner(sync_keys=sync_keys, parties=k + 1, timeout_s=1.0)
    gated_results, _ = _run_batch(fan_out(["req"], repeats=total), gated_probe, SlotAccountant(k=k))
    broken = [r for r in gated_results if r.state is JobState.FAILED]
    assert broken, "k+1 랑데부가 성립했다 — 게이트가 이미 새고 있다"
    assert all("BrokenBarrierError" in (r.infra_error or "") for r in broken), broken
    assert gated_probe.max_active <= k

    # 대조군 B — 게이트 제거: 같은 랑데부가 성립한다 = 실제 과다기동.
    ungated_probe = _SyncProbeRunner(sync_keys=sync_keys, parties=k + 1)
    ungated_slots = _UngatedAccountant(k=k)
    results, supervisor = _run_batch(fan_out(["req"], repeats=total), ungated_probe, ungated_slots)

    assert len(results) == total
    assert ungated_probe.max_active > k  # 관측 채널은 초과를 본다
    assert _max_in_flight(supervisor) > k
    assert ungated_slots.max_concurrent_observed > k

    # 그리고 원래 카운터의 성질: 정상 게이트에서는 증가 분기가 도달 불가능하다.
    baseline = SlotAccountant(k=k)
    while baseline.try_acquire():
        pass
    assert baseline.in_use == k and baseline.over_launch_count == 0


# --------------------------------------------------------------------------- #
# (4) 운영 표면: 실행 중 주기 샘플 — 상시 <= k, 나머지 queued (CPU 층 한정)
# --------------------------------------------------------------------------- #


def test_operational_view_samples_running_at_most_k_while_the_rest_wait(tmp_path):
    """게이트 1항의 CPU 대응: 배치 >> 예산 실행 중 **주기 샘플**에서 running <= k.

    **CPU 층 한정**: 여기서 세는 것은 store의 RUNNING 잡 수(= admission이 띄운 실행
    seam)이지 ``docker ps``의 컨테이너 수가 아니다. 실 컨테이너 수 샘플은 GPU/라이브
    평면 몫(p4c5 batch-8 k=4/k=8 실측, 초과 0).
    """
    k = 2
    total = k * _OVER_BUDGET_FACTOR
    document = yaml.safe_load(_FIXTURE.read_text(encoding="utf-8"))
    runner = _GatedRunner()  # 실행을 붙잡아 RUNNING 창을 결정적으로 만든다

    with Store(tmp_path / "cv.sqlite3") as store:
        app = create_app(store, runner, k=k)
        with TestClient(app) as client:
            response = client.post("/envelopes", json={"requests": [document] * total})
            assert response.status_code == 202, response.text
            envelope_id = response.json()["envelope_id"]

            # 주기 샘플: 어느 샘플에서도 running <= k, 나머지는 대기(queued).
            deadline = time.monotonic() + 10.0
            samples = []
            while len(samples) < 5:
                assert time.monotonic() < deadline, f"RUNNING 창을 못 봤다: {samples}"
                resources = client.get("/monitor.json").json()["resources"]
                assert resources["running_k"] <= k, resources  # 상시 <= k
                # p5c12 D-7 (C): 예산이 같은 응답에 실려 있으므로 초과 판정이 표면
                # 내부에서 성립한다 (운영자가 외부 지식 없이 running_k > k를 읽는다).
                assert resources["concurrency_budget_k"] == k, resources
                assert resources["running_k"] <= resources["concurrency_budget_k"], resources
                if resources["running_k"] == k:
                    samples.append(resources)
                time.sleep(0.02)
            assert all(s["queue_depth"] == total - k for s in samples)  # 나머지는 대기

            runner.gate.set()
            drain = time.monotonic() + 10.0
            while client.get(f"/envelopes/{envelope_id}").json()["status"] != "completed":
                assert time.monotonic() < drain, "배치가 완주하지 못했다"
                time.sleep(0.02)

            done = client.get("/monitor.json").json()
        assert done["resources"]["running_k"] == 0
        assert done["resources"]["queue_depth"] == 0
        states = [job["state"] for req in done["requests"] for job in req["jobs"]]
        assert states and all(state == "completed" for state in states)  # 대기분도 완주


def test_monitor_over_launch_field_is_a_live_channel_not_a_hardcoded_zero(tmp_path):
    """운영 표면의 ``over_launch_count``가 **살아 있는 채널**임을 보인다 — 그리고 그 한계.

    이 필드는 리소스 샘플에서 오는데, 샘플이 없으면 기본값 0이 나온다. 즉 "0을 봤다"는
    것 자체는 증거가 아니다(무장해제된 하네스의 green — G-35). 여기서는 provider가 준
    값이 그대로 표면화함을 보여 **0이 하드코딩이 아님**만 단정한다.

    **미배선 표기(정직)**: 현재 이 provider는 실 ``SlotAccountant``에 배선돼 있지 않다 —
    P4 구조상 accountant가 봉투마다 임시 객체이기 때문(monitor.py 모듈 docstring의 PM
    룰링, P5 follow-up). 따라서 NEG-4 판정에는 이 필드를 쓰지 않는다.
    """
    with Store(tmp_path / "cv.sqlite3") as store:
        assert build_operational_record(store).resources.over_launch_count == 0  # 샘플 없음 기본값

        sentinel = 7  # provider가 준 값 — 채널이 살아 있으면 그대로 나와야 한다
        ResourceHealthSampler(
            store,
            over_launch_provider=lambda: sentinel,
            nvml_snapshot_fn=lambda _index: None,  # CPU 호스트: NVML 부재 경로 고정
        ).sample_once()
        assert build_operational_record(store).resources.over_launch_count == sentinel


def test_surfaced_budget_k_is_the_measured_budget_the_admission_uses(tmp_path):
    """★ p5c12 (D-7 (C)): 운영 표면의 ``concurrency_budget_k`` = admission이 쓰는 예산.

    죽은 카운터를 대신하는 판정 재료이므로 **그 수가 진짜 예산과 같아야** 의미가 있다.
    여기서는 ``compute_k``가 VRAM 항으로 깎은 예산(2)을 그대로 앱에 주입하고, 표면이
    그 수를 싣는지 본다.

    비공허(G-35/G-59): ① 예산이 운영자 상한(8)과 **다른 수**라 "상한을 그냥 베꼈다"와
    구분되고 ② 다른 예산(1)로 앱을 하나 더 세워 표면 값이 **따라 움직임**을 보이므로
    상수 하드코딩과 구분되며 ③ 앱 없는 store-only projection은 ``None``(선택적 분기)을
    낸다 — 없는 예산을 0으로 날조하지 않는다.
    """
    k = compute_k(8, vram_gauge=_FakeGauge(5000.0), vram_per_instance_mb=2000.0)
    assert k == 2 < 8  # 예산 항이 운영자 상한보다 작다 — 대조가 성립한다

    with Store(tmp_path / "cv.sqlite3") as store:
        assert build_operational_record(store).resources.concurrency_budget_k is None

        with TestClient(create_app(store, _GatedRunner(), k=k)) as client:
            assert client.get("/monitor.json").json()["resources"]["concurrency_budget_k"] == k
        with TestClient(create_app(store, _GatedRunner(), k=1)) as client:
            other = client.get("/monitor.json").json()["resources"]["concurrency_budget_k"]
    assert other == 1 != k  # 값이 앱의 예산을 따라간다 (상수가 아니다)


# --------------------------------------------------------------------------- #
# (5) 회귀 가드: 배치가 정말 예산을 넘겼는지 (전제 자체의 비공허)
# --------------------------------------------------------------------------- #


def test_the_batches_above_really_exceed_the_budget():
    """전제 점검: 이 파일이 쓰는 배치가 실제로 k보다 훨씬 크다.

    배수가 1로 줄면 위 테스트들은 "초과가 없었다"를 공짜로 통과한다 — 게이트 문면의
    ``batch >> 예산``이 상수 하나에 걸려 있으므로 그 상수를 단정해 둔다.
    """
    assert _OVER_BUDGET_FACTOR >= 2
    for k in (2, 3):
        assert len(fan_out(["req"], repeats=k * _OVER_BUDGET_FACTOR)) > k


def test_fixture_batch_shape_is_what_the_rest_path_admits():
    """REST 배치가 쓰는 시나리오 픽스처가 실재·파싱 가능하다(스캔 무장 실증의 배치판)."""
    document = yaml.safe_load(_FIXTURE.read_text(encoding="utf-8"))
    assert document["scenario"] and document["sut"]  # 도메인 명세가 실린 실물 요청
    assert json.dumps(document)  # 그대로 REST 본문에 실린다
