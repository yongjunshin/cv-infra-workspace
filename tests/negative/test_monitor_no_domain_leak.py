"""NEG-3 · 운영뷰 누수 0 — 운영 모니터링에 SUT-도메인 상세가 들어오지 않는다.

DoD §6 NEG-3이 **파일명까지 고정**한 negative 스위트 (REQ-MONITOR-002/004 ·
NFR-MONITOR-002; 게이트 DoD-P4-13). 게이트 문면 2항 ↔ 테스트 매핑:

1. "운영 read model 응답 필드 집합 ∩ 도메인 상세 필드 집합(개별 criteria verdict·
   telemetry·녹화 등) = ∅ 단정"
   -> ``test_domain_detail_field_set_is_derived_and_not_empty``  (★ 비공허 실증)
   -> ``test_operational_and_domain_detail_field_sets_are_disjoint``
   -> ``test_shared_handles_are_not_domain_detail``              (제외 목록 정직성)
   -> ``test_live_operational_response_leaks_no_domain_field_or_value``
2. "운영 projection이 M3 store의 부분 투영임을 구조 확인(도메인 상세 컬럼 미포함)"
   -> ``test_projection_row_is_a_strict_subset_of_the_store_columns``
   -> ``test_projection_sql_never_reads_a_domain_column``        (★ SQL 무장 실증)

**★ 이 게이트의 함정: "교집합 = ∅"은 도메인 집합이 비어 있으면 자동으로 참이다.**
그래서 도메인 상세 집합을 손으로 나열하지 않고 **실제 도메인 생산자 모델에서 파생**
한다(``contract.schema``의 CriterionResult=개별 criteria verdict · Metrics=telemetry ·
Artifacts=녹화 · Scenario/SutRef/오라클 params=요청 명세). 파생이므로 ① 비어 있을 수
없고 ② M1에 도메인 필드가 새로 생기면 **자동으로 게이트에 들어온다**(손 목록처럼
낡지 않는다). 그 위에 "집합이 실제로 살아 있는 필드들인가"를 진짜 ``Result``
인스턴스의 직렬화 키로 한 번 더 대조한다.

**정직 표기 — 도메인 상세로 세지 **않는** 이름들.** 두 평면이 설계상 공유하는 상관
핸들(``request_id``/``job_id``/``envelope_id``/``repeat_index``)과 운영 집계
(``flakiness``·``report_outcome``)는 도메인 *상세*가 아니라 **연결 키/집계**다(게이트
문면의 괄호가 도메인 집합을 "개별 criteria verdict·telemetry·녹화 등"으로 한정한다).
이 제외가 손쉬운 도피구가 되지 않도록 ``test_shared_handles_are_not_domain_detail``이
**제외 목록의 어떤 이름도 도메인 상세 생산자 모델의 필드가 아님**을 단정한다 — 즉
누수를 제외 목록에 숨기는 순간 red가 된다.

**출처 분리 = 구조 강제.** 2항은 표시 필터가 아니라 **SELECT 목록**의 문제다. 그래서
① 물리 테이블 컬럼(외부 sqlite3 커넥션의 ``PRAGMA table_info``)과 ② projection이
실제로 실행한 SQL(``set_trace_callback``)을 본다. 후자의 무장은 같은 추적기가
``store.load_jobs()``의 SQL에서는 ``job_spec``을 **본다**는 대조로 증명한다.

Stdlib + pytest + 이미 핀된 FastAPI/pydantic. 신규 의존 0.
"""

from __future__ import annotations

import json
import sqlite3
import time
from pathlib import Path

import pytest
import yaml
from fastapi.testclient import TestClient
from pydantic import BaseModel

from cv_infra.contract.schema import (
    Artifacts,
    CriterionResult,
    Metrics,
    NoCollisionParams,
    ReachedGoalParams,
    Result,
    Scenario,
    SutRef,
)
from cv_infra.orchestrator.api import create_app
from cv_infra.orchestrator.models import Job, JobResult, JobState, Verdict
from cv_infra.orchestrator.monitor import OperationalRecord, build_operational_record
from cv_infra.orchestrator.store import OperationalJobRow, Store

#: 소비자 시나리오의 플랫폼 사본(M1 표준 입력) — 실제 도메인 값의 출처.
_FIXTURE = Path(__file__).resolve().parents[2] / "tests/fixtures/nova_carter_warehouse_goal.yaml"


def _model_field_names(model: type[BaseModel], seen: set | None = None) -> set[str]:
    """Every field name of a pydantic model, recursing into nested models."""
    seen = set() if seen is None else seen
    if model in seen:
        return set()
    seen.add(model)
    names: set[str] = set()
    for name, field in model.model_fields.items():
        names.add(name)
        annotation = field.annotation
        candidates = [annotation, *(getattr(annotation, "__args__", ()) or ())]
        for candidate in candidates:
            if isinstance(candidate, type) and issubclass(candidate, BaseModel):
                names |= _model_field_names(candidate, seen)
    return names


def _json_keys(node: object) -> set[str]:
    """Every key name appearing anywhere in a JSON document."""
    if isinstance(node, dict):
        return set(node) | {k for value in node.values() for k in _json_keys(value)}
    if isinstance(node, list):
        return {k for item in node for k in _json_keys(item)}
    return set()


#: 운영 read model의 응답 필드 집합 (M6가 선언한 projection 모델에서 파생).
_OPERATIONAL_FIELDS = _model_field_names(OperationalRecord)

#: 도메인 상세 **생산자** 모델들 — 게이트 괄호("개별 criteria verdict·telemetry·녹화
#: 등")의 실물. 손 목록이 아니라 M1 계약 모델 자체다.
_DOMAIN_DETAIL_MODELS = (
    CriterionResult,  # 개별 criteria verdict
    Metrics,  # telemetry
    Artifacts,  # 녹화
    Scenario,  # 요청 명세 (scene/robot/goal/seed/timeout)
    SutRef,  # SUT 식별 명세
    ReachedGoalParams,  # 오라클 파라미터
    NoCollisionParams,
)

#: 위 모델들을 담는 **컨테이너 키**(모델 필드가 아니라 자료구조 키라 파생에 안 잡힌다).
_DOMAIN_CONTAINER_KEYS = frozenset(
    {
        "criteria_results",
        "metrics",
        "artifacts",
        "acceptance_criteria",
        "params",
        "oracle",
        "scenario",
        "sut",
        "job_spec",  # store 컬럼: scenario/criteria/sut 명세 전체
        "oracle_plugin_dir",  # store 컬럼: 커스텀 오라클 앵커
        "verdict",  # 잡별 도메인 판정 (운영뷰는 COUNT만 — SR-10 재집계 금지)
        "verdicts",  # rollup 판정 리스트 본체
    }
)

_DOMAIN_DETAIL_FIELDS = _DOMAIN_CONTAINER_KEYS.union(
    *(_model_field_names(model) for model in _DOMAIN_DETAIL_MODELS)
)

#: 두 평면이 **설계상 공유**하는 상관 핸들/운영 집계 — 도메인 상세가 아니다(모듈
#: docstring "정직 표기"). 아래 테스트가 이 제외의 정직성을 기계적으로 검사한다.
_SHARED_HANDLES = frozenset(
    {"envelope_id", "request_id", "job_id", "repeat_index", "flakiness", "report_outcome"}
)

#: 도메인 상세 컬럼 (M3 store) — 운영 projection의 SELECT에서 구조적으로 빠져야 한다.
_DOMAIN_COLUMNS = ("job_spec", "oracle_plugin_dir")


# --------------------------------------------------------------------------- #
# (1) 필드 집합 교집합 = ∅ — 그리고 도메인 집합이 비어 있지 않다는 실증
# --------------------------------------------------------------------------- #


def test_domain_detail_field_set_is_derived_and_not_empty():
    """★ 비공허 실증: 교집합 ∅은 도메인 집합이 공집합이면 공짜로 참이다.

    도메인 집합이 ① 비어 있지 않고 ② 게이트 괄호가 지목한 세 갈래(개별 criteria
    verdict · telemetry · 녹화)를 **모델에서 파생해** 실제로 담고 있으며 ③ 그 이름들이
    진짜 ``Result`` 직렬화에 살아 있는 필드임을 확인한다.
    """
    for model in _DOMAIN_DETAIL_MODELS:
        fields = set(model.model_fields)
        assert fields, f"{model.__name__}에 필드가 없다 — 파생이 공허하다"
        assert fields <= _DOMAIN_DETAIL_FIELDS, model.__name__

    # 게이트 괄호의 세 갈래가 이름 단위로 실재한다.
    assert {"oracle", "passed", "detail"} <= _DOMAIN_DETAIL_FIELDS  # 개별 criteria verdict
    assert {"time_to_goal_s", "min_clearance_m", "collision_count"} <= _DOMAIN_DETAIL_FIELDS
    assert {"mcap", "mp4"} <= _DOMAIN_DETAIL_FIELDS  # 녹화

    # 살아 있는 필드인지 실물 대조: 진짜 Result 하나를 직렬화해 키를 뽑는다.
    live = _json_keys(
        Result(
            job_id="req-0:0",
            verdict="pass",
            metrics=Metrics(time_to_goal_s=12.5),
            criteria_results=[CriterionResult(oracle="reached_goal", passed=True)],
            artifacts=Artifacts(mcap="/cv/out/req-0-0.mcap"),
        ).model_dump(mode="json")
    )
    assert live & _DOMAIN_DETAIL_FIELDS >= {
        "criteria_results",
        "metrics",
        "artifacts",
        "oracle",
        "passed",
        "mcap",
        "verdict",
    }
    assert _OPERATIONAL_FIELDS, "운영 필드 집합이 비었다 — 반대편도 공허하다"


def test_operational_and_domain_detail_field_sets_are_disjoint():
    """게이트 1항 본문: 운영 read model 필드 ∩ 도메인 상세 필드 = ∅."""
    leaked = _OPERATIONAL_FIELDS & _DOMAIN_DETAIL_FIELDS
    assert leaked == set(), f"운영뷰가 도메인 상세 필드를 실었다: {sorted(leaked)}"


def test_shared_handles_are_not_domain_detail():
    """제외 목록의 정직성: 누수를 '공유 핸들'이라 부르며 숨길 수 없게 한다.

    ① 제외 목록의 어떤 이름도 도메인 상세 생산자 모델의 필드가 아니고
    ② 전부 실제로 운영 표면에 있는 이름이다(허수 padding 금지).
    """
    for model in _DOMAIN_DETAIL_MODELS:
        assert not (_SHARED_HANDLES & set(model.model_fields)), model.__name__
    assert _SHARED_HANDLES <= _OPERATIONAL_FIELDS


# --------------------------------------------------------------------------- #
# (2) 살아 있는 운영 응답 — 필드도 값도 도메인이 타지 않는다
# --------------------------------------------------------------------------- #


class _PassRunner:
    """Minimal fake runner seam (CPU): every job completes pass."""

    def run(self, job: Job) -> JobResult:
        return JobResult(job=job, state=JobState.COMPLETED, verdict=Verdict.PASS)


def _wait_completed(client: TestClient, envelope_id: str, timeout_s: float = 10.0) -> None:
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        if client.get(f"/envelopes/{envelope_id}").json()["status"] == "completed":
            return
        time.sleep(0.02)
    raise AssertionError(f"envelope {envelope_id} never completed")


def test_live_operational_response_leaks_no_domain_field_or_value(tmp_path):
    """실제 제출 -> 실제 ``/monitor.json``: 필드 교집합 ∅ + 도메인 값 0.

    G-35 착지/양성 대조를 같은 창에서 함께 잡는다: 같은 실행의 store에는 도메인 상세가
    **분명히 들어 있다**(``job_spec``에 scene/robot/sut/oracle 문자열이 실재) — 그런데도
    운영 표면에는 그 필드도 그 값도 없다. 대조군이 없으면 "누수 0"은 아무 잡도 돌지
    않았을 때도 참이다.
    """
    document = yaml.safe_load(_FIXTURE.read_text(encoding="utf-8"))
    with Store(tmp_path / "cv.sqlite3") as store:
        app = create_app(store, _PassRunner(), k=2)
        with TestClient(app) as client:
            response = client.post("/envelopes", json={"requests": [document, document]})
            assert response.status_code == 202, response.text
            envelope_id = response.json()["envelope_id"]
            _wait_completed(client, envelope_id)
            raw_json = client.get("/monitor.json").text
            raw_html = client.get("/monitor").text

        # 양성 대조 (비공허): 도메인 상세는 정말로 store에 적재돼 있다.
        specs = [job.job_spec for job in store.load_jobs()]
        assert specs and all(spec is not None for spec in specs)
        spec_text = json.dumps(specs)
        domain_values = (
            document["scenario"]["scene"],
            document["scenario"]["robot"],
            document["sut"]["image_ref"],
            document["acceptance_criteria"][0]["oracle"],
            document["acceptance_criteria"][1]["params"]["chassis_path"],
        )
        for value in domain_values:
            assert value in spec_text, f"양성 대조 실패 — store에 {value!r}가 없다"

    body = json.loads(raw_json)
    assert body["requests"], "운영 표면이 요청을 하나도 안 실었다 — 공허하다"

    # 게이트 1항: 응답의 **모든 계층** 키 ∩ 도메인 상세 = ∅.
    leaked_fields = _json_keys(body) & _DOMAIN_DETAIL_FIELDS
    assert leaked_fields == set(), f"운영 응답에 도메인 필드: {sorted(leaked_fields)}"
    assert _json_keys(body) <= _OPERATIONAL_FIELDS  # 선언된 projection 밖의 키도 0

    # 값 층: 두 운영 표면 어디에도 도메인 문자열이 타지 않는다.
    for value in domain_values:
        assert value not in raw_json, f"도메인 값 누수(/monitor.json): {value!r}"
        assert value not in raw_html, f"도메인 값 누수(/monitor): {value!r}"


# --------------------------------------------------------------------------- #
# (3) 게이트 2항 — projection은 store의 **부분 투영**이다 (구조 확인)
# --------------------------------------------------------------------------- #


def test_projection_row_is_a_strict_subset_of_the_store_columns(tmp_path):
    """물리 테이블 컬럼 대비 projection 행이 **진부분집합**이고, 빠진 것이 도메인 컬럼.

    양성 대조: 도메인 컬럼이 store에 **실재**함을 외부 sqlite3 커넥션으로 확인한다 —
    테이블에 애초에 없으면 "미포함"은 공짜다.
    """
    database = tmp_path / "cv.sqlite3"
    with Store(database):
        pass

    connection = sqlite3.connect(str(database))
    try:
        columns = {row[1] for row in connection.execute("PRAGMA table_info(jobs)")}
    finally:
        connection.close()

    assert set(_DOMAIN_COLUMNS) <= columns, "양성 대조 실패 — 도메인 컬럼이 store에 없다"
    projected = set(OperationalJobRow.__dataclass_fields__)
    assert projected < columns, "projection이 부분 투영이 아니다"
    assert not (projected & set(_DOMAIN_COLUMNS))
    assert columns - projected >= set(_DOMAIN_COLUMNS)


def test_projection_sql_never_reads_a_domain_column(tmp_path):
    """★ SQL 무장 실증: projection이 **실행한 SQL**에 도메인 컬럼이 없다.

    타입 선언만 보면 "SELECT는 읽었는데 버렸다"를 구분할 수 없다. 그래서 커넥션의
    trace 콜백으로 실제 문장을 잡는다. 그리고 **같은 추적기**가 ``store.load_jobs()``
    (도메인을 읽는 정당한 경로)의 SQL에서는 ``job_spec``을 본다는 것을 확인한다 —
    이게 없으면 "0 매치"는 추적기가 아무것도 못 봤다는 뜻일 수도 있다.
    """
    with Store(tmp_path / "cv.sqlite3") as store:
        store.upsert_job(
            Job(
                request_id="env-1/r0",
                repeat_index=0,
                state=JobState.COMPLETED,
                job_spec={"job_id": "env-1/r0:0", "sut_image_ref": "carter-sut:p2"},
                oracle_plugin_dir="/tmp/plugins",
            )
        )

        statements: list[str] = []
        store._conn.set_trace_callback(statements.append)  # audit seam (read-only)
        try:
            build_operational_record(store)
            projection_sql = list(statements)
            statements.clear()
            store.load_jobs()  # 무장 대조군: 도메인을 실제로 읽는 경로
            domain_sql = list(statements)
        finally:
            store._conn.set_trace_callback(None)

    assert projection_sql, "추적기가 SQL을 하나도 못 봤다 — 무장되지 않았다"
    for column in _DOMAIN_COLUMNS:
        assert not [sql for sql in projection_sql if column in sql], column
    assert any("job_spec" in sql for sql in domain_sql), "대조군에서도 못 봤다 — 추적기가 눈멀었다"


@pytest.mark.parametrize("column", _DOMAIN_COLUMNS)
def test_operational_row_type_declares_no_domain_column(column):
    """타입 층 회귀 가드: 행 타입에 도메인 컬럼이 생기면 즉시 red (G-17 리뷰 게이트)."""
    assert column not in OperationalJobRow.__dataclass_fields__
