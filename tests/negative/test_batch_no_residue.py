"""NEG-6 · 배치 잔존 0 — 표본 i는 표본 i-1의 잔존물 위에서 돌지 않는다.

p6 설계 정본 §4(네거티브) 신설 게이트. 운반체(1 컨테이너 = 한 요청의 n 표본,
결정 2026-08-26)가 부팅을 한 번만 치르는 대가로 **표본 사이의 상태가 공유**된다:
같은 스테이지·같은 물리 뷰·같은 SUT·같은 DDS 참가자·같은 텔레메트리 수집기.
그래서 "잔존 0"은 이 개조의 안전 조건이고, 이 파일이 그것을 **관측값으로** 집행한다.

**왜 verdict 재현이 아니라 계측인가**(G-102): 표본은 서로 다른 난수 표본이고 런간
분산이 이미 크므로(D-5 입력), *"두 반복이 같은 결과를 냈다"* 는 잔존 여부에 대해
검정력이 없다. 정본 §4가 고정한 관측 가능 4게이트는 전부 **ROS/아티팩트 계측**이고, p7이 장애물
평면에서 같은 형태의 게이트 하나를 더 얹는다(부록 B §B5.5):

1. **realign 카운터 n/n** — 표본마다 SUT가 다시 씨딩됐다는 관측이 남는다.
2. **반복별 bag 세션 독립** — 표본 i의 녹화는 표본 i의 디렉토리에만 있고 공유되지 않는다.
3. **반복별 seed 라인 n/n** — 표본마다 적용된 설정(seed 포함) 한 줄이 로그에 찍힌다.
4. **record 교체 · soft 경로 bind 0** — 표본의 텔레메트리 누산기는 표본 경계에서
   교체되고(잔존 GT 0), soft 리셋은 수집기를 **다시 bind 하지 않는다**.
5. **표본별 장애물 세트 적용**(p7) — 표본마다 ``obstacle_set_applied=`` 라인 정확히 1,
   그 카운터가 요약의 3키와 일치, 풀 크기는 전 표본 상수(증가 = 반복 중 재생성).

**이 파일이 증명하는 것과 하지 않는 것 (GPU 정직성).**
검사기는 **CPU에서 합성 증적**(W3 실물과 같은 형태)에 대해 돌고, 각 게이트마다
**결핍을 심은 양성 대조**가 상주한다 — 즉 여기서 증명되는 것은 *"이 검사기가
잔존을 실제로 잡는다"* 까지다. **GPU 관측 자체는 재주장하지 않는다**: n=60 실측은
W3(`reports/runner-2026-08-27-p6c4-t2-gpu-w3.md` §3 게이트 4)가 소유한다 —
realign 카운터 60/60 · seed 라인 60 · `=== sample` 마커 60 · `sim_time_monotonic`
60/60 · mcap/mp4 60쌍. 이 파일의 합성 픽스처는 **그 실물 증적의 형태**를 모델링한
것이고(G-25의 픽스처판 함정 = 코드를 통과시키는 형태로 외부 세계를 모델링하는 것),
그래서 형태가 드리프트하지 못하게 **생산 코드에서 기계적으로 유도**한다:
``_ITEM_KEYS_FROM_SOURCE``(batch.run이 실제로 쓰는 항목 키) · ``BatchSummary``
(봉투는 생산 클래스가 직접 만든다) · ``REALIGN_OBSERVATION_KEYS`` ·
``SIM_CONFIG_LOG_PREFIX`` · ``iteration_dir``/``BAG_DIR_NAME``/``VIDEO_NAME``.

**한계(숨기지 않는다)**:
* **카운터는 "발행했다"만 증명한다** — p6c3 T3 §9-2 실측: realign 카운터가 두 런에서
  모두 12/12로 완벽했는데 미션 시작 시점의 AMCL 믿음은 1/12였다(스탬프 결함).
  "씨앗이 도착했나"와 "미션이 어떤 믿음에서 출발하나"는 다른 질문이고, 후자를 보는
  것은 W1의 사전등록 게이트(③④)다.
* 게이트 4의 GT 상한(``gt_pose_samples <= 자기 sim 스팬 × 물리 Hz``)은 **누산기가
  교체되지 않았을 때 반드시 깨지는** 방향의 필요조건이다. 실측 대조(W3 60표본):
  비율 0.718~0.971로 전부 1.0 미만.
* 표본이 초기 포즈를 선언하지 않는 배치(``initial_pose`` 부재)는 realign이
  ``initialpose_subscribers=None``("시도 안 함")을 남기는 것이 정상이다 — 그 경우
  아래 게이트 1은 **적용 대상이 아니다**(p6 랜덤 시나리오는 초기 포즈가 랜덤화 축이라
  항상 선언된다).

구조(AST) 절반은 각 게이트가 **생산 코드에서 실제로 성립하는지**를 본다 — 합성 증적
검사기만으로는 "우리가 만든 증적이 통과한다"밖에 못 말하기 때문이다(G-35).
표본 경계에서의 record 교체 **위치** 핀은 M2 소유(``tests/test_runner_batch.py``
``test_telemetry_accumulator_is_swapped_at_mission_start_not_at_restage``)이므로
여기서 중복하지 않고, 이 파일은 **bind 0**(soft 경로가 수집기를 재바인드하지 않음)를 본다.

Stdlib + pytest. 신규 의존 0.
"""

from __future__ import annotations

import ast
import json
import re
from pathlib import Path, PurePosixPath

import pytest

from cv_infra.contract.job_batch import BATCH_SUMMARY_FILENAME, BATCH_SUMMARY_SCHEMA
from cv_infra.runner import batch as m2_batch
from cv_infra.runner import sim_runtime
from cv_infra.runner.batch import BATCH_RESULTS_DIRNAME, BatchSummary, iteration_dir
from cv_infra.runner.realign import (
    COSTMAP_CLEAR_SERVICES,
    REALIGN_OBSERVATION_KEYS,
    REALIGN_PUBLISH_COUNT,
)
from cv_infra.runner.recording import BAG_DIR_NAME, VIDEO_NAME
from cv_infra.runner.sim_runtime import (
    OBSTACLE_SET_LOG_MARKER,
    SIM_CONFIG_LOG_PREFIX,
    SimConfig,
    obstacle_placement_plan,
    obstacle_pool_paths,
    obstacle_pool_plan,
    obstacle_set_log_line,
    sim_config_log_line,
)
from tests.negative.reachability import local_callables, reaching

#: 물리 스텝률 = GT 표본률. 리터럴이 아니라 생산 기본값에서 유도한다 —
#: ``execution_settings.fixed_dt``가 이것을 바꾸면 게이트 4의 상한도 같이 움직여야 한다.
PHYSICS_HZ = 1.0 / SimConfig.physics_dt

#: 운반체가 표본마다 찍는 경계 마커(batch.run의 f-string). 리터럴이므로
#: ``test_sample_marker_literal_is_the_one_the_carrier_prints``가 소스에 못박는다.
SAMPLE_MARKER = "=== sample "
_MARKER_INDEX = re.compile(r"=== sample \d+/\d+ index=(\d+) job_id=")
_SEED_FIELD = re.compile(r"\bseed=(\S+)")
#: 게이트 5가 읽는 세 카운터. 마커는 생산 상수에서 온다 (리터럴 재타이핑 금지, G-25).
_OBSTACLE_SET_FIELDS = re.compile(
    re.escape(OBSTACLE_SET_LOG_MARKER) + r"(\d+) parked=(\d+) pool=(\d+)"
)


# --------------------------------------------------------------------------- #
# 증적 형태 앵커 (G-25) — 합성 픽스처가 실물에서 갈라지지 못하게.
# --------------------------------------------------------------------------- #
def _item_keys_from_source() -> set[str]:
    """``batch.run``이 반복 항목에 **실제로 쓰는** 키 집합 (AST에서 유도).

    합성 픽스처의 키를 손으로 적으면 그 순간부터 실물과 조용히 갈라진다 —
    그리고 이 파일의 검사기는 *자기가 만든 형태*만 통과시키게 된다. 그래서 키는
    운반체 루프의 ``item`` 리터럴 / ``item.update(...)`` / ``item[...] = ...``
    세 자리에서 기계적으로 모은다.
    """
    run = _run_function()
    keys: set[str] = set()
    for node in ast.walk(run):
        if (
            isinstance(node, ast.AnnAssign)
            and ast.unparse(node.target) == "item"
            and isinstance(node.value, ast.Dict)
        ):
            keys |= {key.value for key in node.value.keys}
        elif (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr == "update"
            and ast.unparse(node.func.value) == "item"
        ):
            keys |= {kw.arg for kw in node.keywords}
        elif (
            isinstance(node, ast.Assign)
            and isinstance(node.targets[0], ast.Subscript)
            and ast.unparse(node.targets[0].value) == "item"
        ):
            keys.add(node.targets[0].slice.value)
    return keys


# --------------------------------------------------------------------------- #
# 합성 증적 — W3 실물(`~/cv-infra-p6c1-evidence/p6c4/w3/`)과 같은 형태.
# --------------------------------------------------------------------------- #
def _item(index: int, *, sim_start_s: float, mission_sim_s: float = 20.0) -> dict:
    """건강한 표본 1개의 요약 항목 (실물 값 대역: W3 §3 게이트 4)."""
    sim_end_s = sim_start_s + mission_sim_s
    #: 실측 대역 0.718~0.971 × (자기 스팬 × 60) — 정착창이 분모에 포함되므로 1 미만.
    gt_samples = int(mission_sim_s * PHYSICS_HZ * 0.9)
    return {
        "index": index,
        "job_id": f"env-c06a49f0bb95/r0:{index}",
        "ready": True,
        "readiness_phase": "ready",
        "clock_count": 214 + index,
        "realign": {
            "initialpose_subscribers": 1,
            "initialpose_published": REALIGN_PUBLISH_COUNT,
            "costmaps_cleared": list(COSTMAP_CLEAR_SERVICES),
            "missing": [],
        },
        "sim_time_start_s": sim_start_s,
        "sim_time_end_s": sim_end_s,
        "sim_time_monotonic": True,
        "verdict": "pass",
        "metrics": {
            "time_to_goal_s": 13.083334,
            "min_clearance_m": None,
            "collision_count": 0,
            "path_len_m": 9.6801,
        },
        "artifacts": {
            # 실물과 같이 **컨테이너 안 경로**다 (M3가 /cv/out에 마운트한다) —
            # 그래서 아래 검사기는 절대경로가 아니라 소유 관계(results/<i>/)를 본다.
            "mcap": f"/cv/out/{BATCH_RESULTS_DIRNAME}/{index}/{BAG_DIR_NAME}/bag_0.mcap",
            "mp4": f"/cv/out/{BATCH_RESULTS_DIRNAME}/{index}/{VIDEO_NAME}",
        },
        "video_frames": 196,
        "gt_pose_samples": gt_samples,
        "contact_events": 14160,
        # p7 장애물 카운터. 이 픽스처는 **장애물을 선언하지 않은** 운반체(p6 형태)라
        # 셋 다 0이다 — 풀을 가진 운반체는 아래 ``_obstacle_evidence``가 만든다.
        "obstacles_placed": 0,
        "obstacles_parked": 0,
        "obstacles_pool": 0,
        "timings_s": {"iteration": 34.6, "mission": mission_sim_s, "restage": 0.31},
    }


def _evidence(out_root: Path, n: int = 4) -> tuple[dict, str]:
    """건강한 운반체 1회분의 (batch_summary 문서, 러너 로그) + 디스크 트리.

    봉투는 **생산 ``BatchSummary``가 직접** 만든다(스키마/키/원자 flush 전부 그것의 것).
    """
    summary = BatchSummary(out_root, "env-c06a49f0bb95/r0", n, started_at=1787796540.0)
    summary.doc["boot"] = {"bootstrap_s": 0.0054, "total_s": 26.1299}
    start = 0.0
    for index in range(n):
        item = _item(index, sim_start_s=start)
        start = item["sim_time_end_s"]
        summary.add_iteration(item)
        sample_dir = iteration_dir(out_root, index)
        (sample_dir / BAG_DIR_NAME).mkdir(parents=True, exist_ok=True)
        (sample_dir / BAG_DIR_NAME / "bag_0.mcap").write_bytes(b"mcap")
        (sample_dir / VIDEO_NAME).write_bytes(b"mp4")
        (sample_dir / "result.json").write_text("{}", encoding="utf-8")
    summary.finish(finished_at=1787800313.6)
    return summary.doc, _log(summary.doc)


def _log(summary: dict, obstacle_lines: dict[int, str] | None = None) -> str:
    """러너 stdout — 실물과 같이 **docker의 타임스탬프 접두가 붙은 채로** 캡처된다.

    (실측 W3 `logs/runner.log`: ``2026-08-27T02:09:24.893500865Z [cv-runner] …``.
    줄 시작 앵커로 파싱했다면 실물 증적에서 0줄을 세고도 통과했을 것이다.)
    """
    n = summary["n"]
    stamp = "2026-08-27T02:09:24.893500865Z "
    seed_line = stamp + sim_config_log_line(1 / 60, 1 / 60, 42, "sha256:cb39be9a")
    obstacle_lines = obstacle_lines or {}
    lines = [f"{stamp}[cv-runner] batch admitted: {n} sample(s)", seed_line]
    if 0 in obstacle_lines:
        # 표본 0의 배치는 두 번째 pre-reset 훅이 부팅 중에 한다 — load_scene이 seed
        # 라인을 찍은 **뒤**다(훅은 emit_sim_config 다음에 돈다).
        lines.append(stamp + obstacle_lines[0])
    for item in summary["iterations"]:
        index = item["index"]
        lines.append(
            f"{stamp}[cv-runner] {SAMPLE_MARKER}{index + 1}/{n} "
            f"index={index} job_id={item['job_id']} ==="
        )
        if index:  # 표본 0의 라인은 load_scene이 이미 찍었다 (라인 수 = n, n+1 아님)
            if index in obstacle_lines:  # restage가 emit_sim_config보다 먼저 찍는다
                lines.append(stamp + obstacle_lines[index])
            lines.append(seed_line)
        lines.append(f"{stamp}[cv-runner] sut realign: {item['realign']}")
        lines.append(f"{stamp}[cv-runner] bag topics (4): ['/clock', '/odom']")
    return "\n".join(lines) + "\n"


@pytest.fixture
def evidence(tmp_path):
    doc, log = _evidence(tmp_path)
    return doc, log, tmp_path


# --------------------------------------------------------------------------- #
# 게이트 1 — realign 카운터 n/n
# --------------------------------------------------------------------------- #
def realign_residue(summary: dict) -> list[str]:
    """표본마다 SUT가 다시 씨딩됐다는 **관측**이 있나. 없으면 그 표본은 이전 표본의
    AMCL 믿음·코스트맵 위에서 돈 것이다 (설계 §0-9).

    ``initialpose_subscribers=None`` = "시도 안 함"은 초기 포즈를 선언하지 않은
    시나리오의 정상 관측이지만, 랜덤 초기 포즈 배치에서는 그 자체가 잔존 신호다.
    """
    complaints: list[str] = []
    items = summary["iterations"]
    if len(items) != summary["n"]:
        complaints.append(
            f"realign evidence for {len(items)}/{summary['n']} sample(s) — "
            "the rest cannot be shown to have been re-seeded"
        )
    for item in items:
        index = item["index"]
        observed = item.get("realign")
        if not isinstance(observed, dict):
            complaints.append(f"sample {index}: no realign observation at all")
            continue
        if set(observed) != set(REALIGN_OBSERVATION_KEYS):
            complaints.append(
                f"sample {index}: realign keys {sorted(observed)} != "
                f"{sorted(REALIGN_OBSERVATION_KEYS)} (a key that appears only when it is "
                "interesting is a key nobody can count)"
            )
            continue
        if observed["initialpose_published"] != REALIGN_PUBLISH_COUNT:
            complaints.append(
                f"sample {index}: /initialpose published "
                f"{observed['initialpose_published']}x, contract is {REALIGN_PUBLISH_COUNT}x"
            )
        subscribers = observed["initialpose_subscribers"]
        if subscribers is None:
            complaints.append(f"sample {index}: /initialpose was never attempted (pose=None)")
        elif subscribers < 1:
            complaints.append(
                f"sample {index}: /initialpose had {subscribers} matched subscriber(s) — "
                "the seed went nowhere (G-26)"
            )
        if observed["missing"]:
            complaints.append(f"sample {index}: realign missing {observed['missing']}")
        if list(observed["costmaps_cleared"]) != list(COSTMAP_CLEAR_SERVICES):
            complaints.append(
                f"sample {index}: costmaps cleared {observed['costmaps_cleared']} != "
                f"{list(COSTMAP_CLEAR_SERVICES)} — stale occupancy survives the sample boundary"
            )
    return complaints


def test_healthy_carrier_leaves_realign_evidence_for_every_sample(evidence):
    summary, _, _ = evidence
    assert realign_residue(summary) == []


@pytest.mark.parametrize(
    ("label", "plant"),
    [
        ("한 표본이 통째로 빠졌다", lambda s: s["iterations"].pop()),
        (
            "관측 dict 자체가 없다",
            lambda s: s["iterations"][1].pop("realign"),
        ),
        (
            "키 하나가 사라졌다",
            lambda s: s["iterations"][1]["realign"].pop("missing"),
        ),
        (
            "버스트가 짧아졌다",
            lambda s: s["iterations"][1]["realign"].__setitem__("initialpose_published", 1),
        ),
        (
            "아무도 듣지 않았다",
            lambda s: s["iterations"][1]["realign"].__setitem__("initialpose_subscribers", 0),
        ),
        (
            "시도조차 안 했다",
            lambda s: s["iterations"][1]["realign"].__setitem__("initialpose_subscribers", None),
        ),
        (
            "서비스가 응답하지 않았다",
            lambda s: s["iterations"][1]["realign"].__setitem__("missing", ["/global_costmap/…"]),
        ),
        (
            "코스트맵 하나만 지웠다",
            lambda s: s["iterations"][1]["realign"].__setitem__(
                "costmaps_cleared", [COSTMAP_CLEAR_SERVICES[0]]
            ),
        ),
    ],
)
def test_realign_gate_fires_on_planted_residue(evidence, label, plant):
    """양성 대조 (G-59): 결핍을 하나씩 심으면 게이트가 **그 표본을 지목해** 운다."""
    summary, _, _ = evidence
    plant(summary)
    complaints = realign_residue(summary)
    assert complaints, label


# --------------------------------------------------------------------------- #
# 게이트 2 — 반복별 bag 세션 독립
# --------------------------------------------------------------------------- #
def bag_session_residue(summary: dict, out_root: Path | None = None) -> list[str]:
    """표본 i의 녹화가 **표본 i의 것**인가.

    두 층으로 본다. ① 요약이 이름하는 경로가 ``results/<i>/`` 소유인가(그리고 두
    표본이 같은 파일을 이름하지 않는가) ② ``out_root``가 주어지면 디스크에 표본당
    **정확히 하나의** bag 세션이 있는가 — 한 세션이 반복을 넘겨 살아 있으면 다음
    표본의 미션이 이전 표본의 mcap에 이어 쓰인다.
    """
    complaints: list[str] = []
    items = summary["iterations"]
    if len(items) != summary["n"]:
        complaints.append(f"recording evidence for {len(items)}/{summary['n']} sample(s)")
    owner: dict[str, int] = {}
    for item in items:
        index = item["index"]
        artifacts = item.get("artifacts") or {}
        expected = {
            "mcap": f"{BATCH_RESULTS_DIRNAME}/{index}/{BAG_DIR_NAME}",
            "mp4": f"{BATCH_RESULTS_DIRNAME}/{index}",
        }
        for kind, parent in expected.items():
            named = artifacts.get(kind)
            if named is None:
                complaints.append(
                    f"sample {index}: no {kind} of its own (a sample that recorded nothing "
                    "cannot show its recording is not the previous sample's)"
                )
                continue
            if named in owner:
                complaints.append(
                    f"sample {index} and sample {owner[named]} name the SAME {kind} "
                    f"{named} — one recording session spanning two samples"
                )
            owner[named] = index
            if not str(PurePosixPath(named).parent).endswith(parent):
                complaints.append(
                    f"sample {index}: {kind} {named} does not live under {parent}/ "
                    "(the wire invariant is specs[i] <-> results/<i>)"
                )
    if out_root is not None:
        for item in items:
            index = item["index"]
            sample_dir = iteration_dir(out_root, index)
            mcaps = sorted((sample_dir / BAG_DIR_NAME).glob("*.mcap"))
            if len(mcaps) != 1:
                complaints.append(
                    f"sample {index}: {len(mcaps)} .mcap under {sample_dir / BAG_DIR_NAME} "
                    "— one bag session per sample, opened and closed inside the iteration"
                )
            if not (sample_dir / VIDEO_NAME).is_file():
                complaints.append(f"sample {index}: no {VIDEO_NAME} on disk under {sample_dir}")
    return complaints


def test_healthy_carrier_gives_every_sample_its_own_bag_session(evidence):
    summary, _, out_root = evidence
    assert bag_session_residue(summary, out_root) == []


@pytest.mark.parametrize(
    ("label", "plant"),
    [
        (
            "두 표본이 한 bag을 공유한다",
            lambda s, root: s["iterations"][1]["artifacts"].__setitem__(
                "mcap", s["iterations"][0]["artifacts"]["mcap"]
            ),
        ),
        (
            "표본 1의 mp4가 표본 0의 디렉토리에 있다",
            lambda s, root: s["iterations"][1]["artifacts"].__setitem__(
                "mp4", f"/cv/out/{BATCH_RESULTS_DIRNAME}/0/{VIDEO_NAME}"
            ),
        ),
        (
            "녹화가 아예 없다",
            lambda s, root: s["iterations"][1]["artifacts"].__setitem__("mcap", None),
        ),
        (
            "한 디렉토리에 세션이 둘 (레코더가 반복을 넘겨 살았다)",
            lambda s, root: (iteration_dir(root, 1) / BAG_DIR_NAME / "bag_1.mcap").write_bytes(
                b"leaked"
            ),
        ),
        (
            "디스크에 mcap이 없다 (요약만 있다)",
            lambda s, root: (iteration_dir(root, 1) / BAG_DIR_NAME / "bag_0.mcap").unlink(),
        ),
    ],
)
def test_bag_session_gate_fires_on_planted_residue(evidence, label, plant):
    summary, _, out_root = evidence
    plant(summary, out_root)
    assert bag_session_residue(summary, out_root), label


# --------------------------------------------------------------------------- #
# 게이트 3 — 반복별 seed 라인 n/n
# --------------------------------------------------------------------------- #
def seed_line_residue(log: str, n: int) -> list[str]:
    """표본마다 **적용된 설정 한 줄**(DoD-P2-06 ①)이 찍혔나.

    라인 수 = n 이지 n+1이 아니다: 표본 0의 라인은 ``load_scene``이 부팅 중에 찍고
    루프는 ``position > 0``에서만 찍는다(p6c3 T2 보고 ⑥). 그래서 총량만 세지 않고
    **마커로 구간을 갈라** 표본마다 정확히 한 줄인지 본다 — 총량만 보면 "운반체가
    부팅 때 한 번 찍고 반복은 안 찍었다"와 "표본마다 찍었다"를 못 가른다.
    """
    complaints: list[str] = []
    lines = log.splitlines()
    markers = [i for i, line in enumerate(lines) if SAMPLE_MARKER in line]
    indices = [int(_MARKER_INDEX.search(lines[i]).group(1)) for i in markers]
    if indices != list(range(n)):
        complaints.append(f"sample markers {indices} != {list(range(n))} (n={n})")
        return complaints
    bounds = [0, *markers, len(lines)]
    for position in range(len(bounds) - 1):
        segment = lines[bounds[position] : bounds[position + 1]]
        seeds = [line for line in segment if SIM_CONFIG_LOG_PREFIX in line]
        # 구간 0 = 부팅(표본 0의 라인), 구간 1 = 표본 0(루프가 찍지 않는다), 그 뒤 = 표본 i.
        expected = 0 if position == 1 else 1
        if len(seeds) != expected:
            where = "boot" if position == 0 else f"sample {position - 1}"
            complaints.append(
                f"{where}: {len(seeds)} applied-settings line(s), expected {expected} "
                "(one per sample; sample 0's is emitted by load_scene)"
            )
        for line in seeds:
            found = _SEED_FIELD.search(line)
            if found is None or found.group(1) == "none":
                complaints.append(f"applied-settings line carries no seed: {line.strip()}")
    return complaints


def test_healthy_carrier_prints_one_applied_settings_line_per_sample(evidence):
    summary, log, _ = evidence
    assert seed_line_residue(log, summary["n"]) == []


@pytest.mark.parametrize(
    ("label", "plant"),
    [
        (
            "운반체가 부팅 때 한 번만 찍었다",
            lambda log: "\n".join(
                line
                for position, line in enumerate(log.splitlines())
                if SIM_CONFIG_LOG_PREFIX not in line or position < 5
            ),
        ),
        (
            "마지막 표본의 라인이 빠졌다",
            lambda log: "\n".join(log.splitlines()[:-4] + log.splitlines()[-3:]),
        ),
        (
            "seed가 none으로 찍혔다",
            lambda log: log.replace("seed=42", "seed=none"),
        ),
        (
            "표본 하나가 아예 안 돌았다",
            lambda log: "\n".join(line for line in log.splitlines() if "index=2 " not in line),
        ),
    ],
)
def test_seed_line_gate_fires_on_planted_residue(evidence, label, plant):
    summary, log, _ = evidence
    assert seed_line_residue(plant(log), summary["n"]), label


# --------------------------------------------------------------------------- #
# 게이트 4 — record 교체 (누산기 잔존 0) · /clock 되감기 0
# --------------------------------------------------------------------------- #
def record_residue(summary: dict, physics_hz: float = PHYSICS_HZ) -> list[str]:
    """표본의 텔레메트리가 **자기 미션만** 담고 있나.

    세 관측:
    * ``time_to_goal_s == 0.0`` — p6c3 T3 §4가 실측한 잔존 서명이다. 누산기를 restage
      **전에** 교체하면 첫 GT 표본이 이전 표본의 (목표 근처) 포즈에서 찍히고,
      ``reached_goal``이 t=0 도달로 읽는다(11/11 표본에서 재현, path_len도 텔레포트
      거리만큼 부풀었다). 미도달 표본의 ``None``("정직한 부재")은 잔존이 아니다.
    * ``gt_pose_samples`` 상한 — 누산기가 교체되지 않았다면 표본 i의 계열은 운반체
      전체 스팬을 담게 되므로 **자기 sim 스팬 × 물리 Hz**를 넘는다. (실측 W3: 비율
      0.718~0.971 — 분모에 정착창이 포함돼 1 미만이다.)
    * ``sim_time_monotonic`` — 표본의 시작 sim-시각이 이전 표본의 끝보다 앞이면
      ``/clock``이 되감긴 것이고, 그 창의 ``/initialpose`` 스탬프는 SUT의 미래에 떨어진다
      (soft 리셋이 이 되감기를 없앤 것이 p6c2의 관측이다 — 여기서는 그 부재를 확인한다).
    """
    complaints: list[str] = []
    for item in summary["iterations"]:
        index = item["index"]
        if not item.get("sim_time_monotonic", False):
            complaints.append(f"sample {index}: /clock rewound into the previous sample's timeline")
        time_to_goal = (item.get("metrics") or {}).get("time_to_goal_s")
        if time_to_goal == 0.0:
            complaints.append(
                f"sample {index}: time_to_goal_s = 0.0 — the first GT sample was taken at the "
                "PREVIOUS sample's pose (the accumulator was not replaced at mission start)"
            )
        span_s = item["sim_time_end_s"] - item["sim_time_start_s"]
        allowed = span_s * physics_hz
        if span_s <= 0:
            complaints.append(f"sample {index}: sim span {span_s} is not positive")
        elif item["gt_pose_samples"] > allowed:
            complaints.append(
                f"sample {index}: {item['gt_pose_samples']} GT samples over a {span_s:.3f} "
                f"sim-second span (at most {allowed:.0f} fit) — the accumulator carries "
                "another sample's mission"
            )
    return complaints


def test_healthy_carrier_starts_every_sample_with_a_fresh_accumulator(evidence):
    summary, _, _ = evidence
    assert record_residue(summary) == []


@pytest.mark.parametrize(
    ("label", "plant"),
    [
        (
            "첫 GT가 이전 표본의 포즈에서 찍혔다",
            lambda s: s["iterations"][1]["metrics"].__setitem__("time_to_goal_s", 0.0),
        ),
        (
            "누산기가 운반체 전체를 담고 있다",
            lambda s: s["iterations"][2].__setitem__(
                "gt_pose_samples", sum(i["gt_pose_samples"] for i in s["iterations"][:3])
            ),
        ),
        (
            "/clock이 되감겼다",
            lambda s: s["iterations"][1].__setitem__("sim_time_monotonic", False),
        ),
    ],
)
def test_record_gate_fires_on_planted_residue(evidence, label, plant):
    summary, _, _ = evidence
    plant(summary)
    assert record_residue(summary), label


def test_a_non_reaching_sample_is_not_residue(evidence):
    """G-35 반대편: 미도달 표본의 ``time_to_goal_s = None``(정직한 부재)은 통과해야 한다.
    실측 W3에서 12/60이 그랬다 — 이것을 잔존으로 읽으면 게이트가 건강한 런을 죽인다."""
    summary, _, _ = evidence
    summary["iterations"][1]["metrics"]["time_to_goal_s"] = None
    summary["iterations"][1]["verdict"] = "timeout"
    assert record_residue(summary) == []


# --------------------------------------------------------------------------- #
# 게이트 5 — 장애물 세트 잔존 0 (p7)
# --------------------------------------------------------------------------- #
#: 장애물을 선언한 운반체의 표본별 그룹 구성 (chair, desk, forklift 개수).
#: CEO 목표 표현력 그대로 — "의자 1 · 책상 n={0~5} · 지게차 2". 표본 2는 **0개**다:
#: ``[]``(이 표본은 아무것도 놓지 않는다)와 ``None``(풀이 없다)의 갈림을 픽스처가
#: 실제로 밟지 않으면 게이트는 그 함정에 대해 아무 말도 못 한다(G-59).
_OBSTACLE_GROUPS = ((1, 0, 2), (1, 5, 2), (0, 0, 0), (1, 2, 2))


def _obstacle_entries(chairs: int, desks: int, forklifts: int) -> list[dict]:
    """구체형(싱글턴 전개 후) 장애물 엔트리 — M1 materialize가 내려보내는 그 모양."""
    entries = []
    for asset, count in (("chair", chairs), ("desk", desks), ("forklift", forklifts)):
        for j in range(count):
            entries.append({"asset": asset, "count": 1, "x": 1.0 + j, "y": 2.0 - j, "yaw": 0.3 * j})
    return entries


def _obstacle_evidence(out_root: Path) -> tuple[dict, str]:
    """장애물 풀을 가진 건강한 운반체 1회분 (요약 문서 + 러너 로그).

    라인·경로·개수는 전부 **생산 순수 함수에서 유도**한다(``obstacle_pool_plan`` ->
    ``obstacle_pool_paths`` -> ``obstacle_placement_plan`` -> ``obstacle_set_log_line``).
    손으로 적으면 이 검사기는 *자기가 만든 형태*만 통과시키게 된다(G-25).

    표본 0의 세트 적용 라인은 **부팅 구간**에 있다: 배치는 두 번째 pre-reset 훅이고
    루프는 ``if position:``에서만 restage 한다 — seed 라인과 정확히 같은 구조(n+1 아님).
    부팅은 이 밖에도 ``obstacle pool spawned:`` 1줄과 멤버당 ``obstacle physics:`` 감사
    1줄을 찍지만, 게이트 5는 그것을 소비하지 않는다(그 층은 W1 소유).
    """
    per_sample = [_obstacle_entries(*groups) for groups in _OBSTACLE_GROUPS]
    pool = obstacle_pool_paths(obstacle_pool_plan(per_sample))
    n = len(per_sample)
    summary = BatchSummary(out_root, "env-obstacles/r0", n, started_at=1787796540.0)
    summary.doc["boot"] = {"bootstrap_s": 0.0054, "total_s": 31.4}
    set_lines: dict[int, str] = {}
    start = 0.0
    for index, entries in enumerate(per_sample):
        placed, parked = obstacle_placement_plan(entries, pool)
        set_lines[index] = obstacle_set_log_line(placed, parked)
        item = _item(index, sim_start_s=start)
        item["obstacles_placed"] = len(placed)
        item["obstacles_parked"] = len(parked)
        item["obstacles_pool"] = len(placed) + len(parked)
        start = item["sim_time_end_s"]
        summary.add_iteration(item)
    summary.finish(finished_at=1787800313.6)
    return summary.doc, _log(summary.doc, set_lines)


@pytest.fixture
def obstacle_evidence(tmp_path):
    doc, log = _obstacle_evidence(tmp_path)
    return doc, log, tmp_path


def obstacle_set_residue(summary: dict, log: str) -> list[str]:
    """게이트 5 — 표본 i는 표본 i-1의 **장애물 배치** 위에서 돌지 않는다.

    잔존의 형태 셋: ① 표본마다 ``obstacle_set_applied=`` 라인 정확히 1(0이면 이전
    표본의 배치가 그대로 살아 있다 — 가장 조용한 잔존이다: 로그도 요약도 아무 말을
    안 하고 표본 i가 표본 i-1의 장애물 위에서 판정된다) ② 라인의 placed/parked/pool이
    그 표본 항목의 3키와 일치하고 ``placed + parked == pool`` ③ pool이 전 표본
    상수(증가 = 반복 중 재생성의 서명 — p6c2 §2.1이 실측한 그 증식).

    **한계(숨기지 않는다)**: 카운터는 "배치 호출이 돌았다"만 증명한다. "prim이 실제로
    그 좌표에 있다"는 GPU 관측이고 W0 ⓓ/W1이 소유한다 — realign 카운터가 12/12인데
    AMCL 믿음은 1/12였던 p6c3 T3 §9-2와 같은 층위 구분이다.

    장애물을 선언하지 않은 배치(풀 0)에서는 ①②③이 **공허**하다. 그 경우 이 검사기가
    보는 것은 하나뿐이다: 풀이 없는데 세트 라인이 찍혔는가(찍혔다면 러너가 아무도
    선언하지 않은 장애물을 놓고 있다). 무장된 팔은 ``obstacle_evidence`` 픽스처다(G-35).
    """
    complaints: list[str] = []
    items = summary["iterations"]
    n = summary["n"]
    if len(items) != n:
        complaints.append(f"obstacle evidence for {len(items)}/{n} sample(s)")
    missing = [item["index"] for item in items if item.get("obstacles_pool") is None]
    if missing:
        complaints.append(f"sample(s) {missing}: no obstacle counters at all — nothing to check")
        return complaints

    lines = log.splitlines()
    markers = [i for i, line in enumerate(lines) if SAMPLE_MARKER in line]
    indices = [int(_MARKER_INDEX.search(lines[i]).group(1)) for i in markers]
    if indices != list(range(n)):
        complaints.append(f"sample markers {indices} != {list(range(n))} (n={n})")
        return complaints
    bounds = [0, *markers, len(lines)]
    segments = [
        [
            found.groups()
            for line in lines[bounds[position] : bounds[position + 1]]
            if (found := _OBSTACLE_SET_FIELDS.search(line))
        ]
        for position in range(len(bounds) - 1)
    ]

    pools = [item["obstacles_pool"] for item in items]
    if set(pools) == {0}:
        stray = sum(len(found) for found in segments)
        if stray:
            complaints.append(
                f"{stray} {OBSTACLE_SET_LOG_MARKER} line(s) in a batch that declared no "
                "obstacles — the runner is placing something nobody asked for"
            )
        return complaints
    if len(set(pools)) != 1:
        complaints.append(
            f"pool size is not constant across samples ({pools}) — a pool that GROWS is the "
            "signature of re-spawning inside the sample loop (p6c2 §2.1: 2 -> 48 material "
            "prims over 24 respawns)"
        )

    for position, found in enumerate(segments):
        # 구간 0 = 부팅(표본 0의 배치 훅), 구간 1 = 표본 0(루프가 restage 하지 않는다),
        # 그 뒤 = 표본 i. seed 라인과 같은 구조다.
        expected = 0 if position == 1 else 1
        if len(found) != expected:
            where = "boot" if position == 0 else f"sample {position - 1}"
            complaints.append(
                f"{where}: {len(found)} obstacle-set line(s), expected {expected} (one per "
                "sample; sample 0's is applied by the boot hook). A sample with no line ran "
                "on the PREVIOUS sample's placement"
            )
    for item in items:
        index = item["index"]
        position = 0 if index == 0 else index + 1
        found = segments[position] if position < len(segments) else []
        if len(found) != 1:
            continue  # 라인 수는 위에서 이미 울었다
        placed, parked, pool = (int(value) for value in found[0])
        counters = (item["obstacles_placed"], item["obstacles_parked"], item["obstacles_pool"])
        if (placed, parked, pool) != counters:
            complaints.append(
                f"sample {index}: log says placed/parked/pool {(placed, parked, pool)} but the "
                f"summary says {counters} — the line and the counters describe different worlds"
            )
        if placed + parked != pool:
            complaints.append(
                f"sample {index}: placed {placed} + parked {parked} != pool {pool} — some pool "
                "member was neither placed nor parked, i.e. it kept the pose it had in the "
                "PREVIOUS sample"
            )
    return complaints


def test_healthy_carrier_applies_one_obstacle_set_per_sample(obstacle_evidence):
    summary, log, _ = obstacle_evidence
    assert obstacle_set_residue(summary, log) == []
    # 픽스처가 실제로 무장돼 있는지: 0개 표본 하나 + 최대 다중도 표본 하나를 밟는다.
    assert [item["obstacles_placed"] for item in summary["iterations"]] == [3, 8, 0, 5]
    assert {item["obstacles_pool"] for item in summary["iterations"]} == {8}


def test_a_carrier_that_declared_no_obstacles_is_not_residue(evidence):
    """반대편(G-35): 장애물이 없는 배치는 세트 라인이 0인 것이 정상이다.

    이 팔은 **공허하다** — 그리고 그것이 요점이다. 기능이 꺼진 배치에서 게이트가
    울면 p6 배치가 전부 red가 된다. 무장 증명은 위/아래의 장애물 픽스처가 한다.
    """
    summary, log, _ = evidence
    assert {item["obstacles_pool"] for item in summary["iterations"]} == {0}
    assert obstacle_set_residue(summary, log) == []


def test_a_pool_without_a_declaration_is_residue(evidence):
    """공허한 팔의 무장 실증: 풀 0인 배치에 세트 라인이 하나라도 있으면 운다."""
    summary, log, _ = evidence
    log += f"[cv-runner] {OBSTACLE_SET_LOG_MARKER}2 parked=0 pool=2 placed=[] parked_paths=[]\n"
    assert obstacle_set_residue(summary, log)


def _drop_last(log: str, needle: str) -> str:
    """마지막으로 ``needle``을 담은 줄 하나를 지운다 (심는 결핍용)."""
    lines = log.splitlines()
    for position in range(len(lines) - 1, -1, -1):
        if needle in lines[position]:
            del lines[position]
            break
    return "\n".join(lines)


def _rewrite_set_lines(log: str, *, first_index: int, parked_delta: int, pool_delta: int) -> str:
    """표본 ``first_index`` 이후의 세트 라인 카운터를 다시 쓴다 (심는 결핍용)."""
    counters = re.compile(r"parked=(\d+) pool=(\d+)")
    index = -1
    rewritten = []
    for line in log.splitlines():
        found = _MARKER_INDEX.search(line)
        if found:
            index = int(found.group(1))
        if index >= first_index and OBSTACLE_SET_LOG_MARKER in line:
            line = counters.sub(
                lambda m: f"parked={int(m.group(1)) + parked_delta} "
                f"pool={int(m.group(2)) + pool_delta}",
                line,
            )
        rewritten.append(line)
    return "\n".join(rewritten)


def _the_last_sample_never_applied_its_set(summary: dict, log: str) -> tuple[dict, str]:
    return summary, _drop_last(log, OBSTACLE_SET_LOG_MARKER)


def _the_surplus_was_never_parked(summary: dict, log: str) -> tuple[dict, str]:
    summary["iterations"][2]["obstacles_parked"] = 0
    return summary, log


def _the_pool_grew_from_sample_2(summary: dict, log: str) -> tuple[dict, str]:
    """반복 중 재생성 — 로그와 요약이 **함께** 커진다.

    라인↔카운터 일치도, ``placed + parked == pool``도 여전히 성립하므로 이 결핍을
    잡을 수 있는 규칙은 상수 규칙 하나뿐이다. 요약만 키우면 불일치 규칙이 먼저 울고,
    그러면 이 대조는 상수 규칙이 지워져도 초록으로 남는다(G-59: 양성 대조는 겨냥한
    규칙을 고립시켜야 한다 — 실측으로 확인했다).
    """
    for item in summary["iterations"][2:]:
        item["obstacles_parked"] += 1
        item["obstacles_pool"] += 1
    return summary, _rewrite_set_lines(log, first_index=2, parked_delta=1, pool_delta=1)


def _a_member_was_neither_placed_nor_parked(summary: dict, log: str) -> tuple[dict, str]:
    """라인 자체가 안 맞는다: placed + parked < pool.

    카운터도 같이 줄이므로 라인↔요약 일치 규칙은 침묵한다 — 남는 것은 합 규칙뿐이고,
    그것이 뜻하는 바는 "어떤 풀 멤버가 배치도 파킹도 되지 않았다" = 그 멤버는 직전
    표본의 포즈를 그대로 들고 있다.
    """
    for item in summary["iterations"][2:]:
        item["obstacles_parked"] -= 1
    return summary, _rewrite_set_lines(log, first_index=2, parked_delta=-1, pool_delta=0)


@pytest.mark.parametrize(
    ("label", "plant"),
    [
        ("한 표본이 세트를 적용하지 않았다", _the_last_sample_never_applied_its_set),
        ("잉여가 파킹되지 않았다", _the_surplus_was_never_parked),
        ("반복 중 풀이 커졌다 (재생성)", _the_pool_grew_from_sample_2),
        ("어떤 멤버도 배치도 파킹도 되지 않았다", _a_member_was_neither_placed_nor_parked),
    ],
)
def test_obstacle_set_gate_fires_on_planted_residue(obstacle_evidence, label, plant):
    """양성 대조 (G-59): 결핍을 하나씩 심으면 게이트가 그 표본을 지목해 운다."""
    summary, log, _ = obstacle_evidence
    summary, log = plant(summary, log)
    assert obstacle_set_residue(summary, log), label


# --------------------------------------------------------------------------- #
# 구조 절반 — 게이트들이 **생산 코드에서** 성립하는가 (합성 증적으로는 못 보는 층)
# --------------------------------------------------------------------------- #
def _carrier_source() -> str:
    return Path(m2_batch.__file__).read_text(encoding="utf-8")


def _run_function(source: str | None = None) -> ast.FunctionDef:
    tree = ast.parse(_carrier_source() if source is None else source)
    return next(n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name == "run")


def _sample_loop(source: str | None = None) -> ast.For:
    """운반체의 표본 루프 = ``for position, parsed in enumerate(specs)``."""
    return next(
        node
        for node in ast.walk(_run_function(source))
        if isinstance(node, ast.For) and ast.unparse(node.iter) == "enumerate(specs)"
    )


def _callables(source: str | None = None) -> dict[tuple[str, ...], ast.AST]:
    """``run``이 **이름으로 부를 수 있는** 같은-모듈 함수들 (도달성 펼침의 사전).

    금지 호출을 모듈-로컬 헬퍼 뒤로 한 칸 옮기면 직접-이름 스캔은 아무것도 보지
    못한다(p8c2 T6 실측: 전 스위트 1683/1683 초록). 아래 게이트들은 그래서 이름이
    아니라 **도달성**으로 판정한다 — G-106 ④ / ``tests/negative/reachability.py``.
    """
    tree = ast.parse(_carrier_source() if source is None else source)
    run = next(n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name == "run")
    return local_callables(tree, inside=run)


def _call_lines(node: ast.AST) -> dict[str, list[int]]:
    calls: dict[str, list[int]] = {}
    for child in ast.walk(node):
        if isinstance(child, ast.Call):
            name = (
                child.func.attr
                if isinstance(child.func, ast.Attribute)
                else ast.unparse(child.func)
            )
            calls.setdefault(name, []).append(child.lineno)
    return calls


def test_synthetic_evidence_carries_exactly_the_keys_the_carrier_writes(evidence):
    """G-25 앵커: 픽스처 ↔ 생산 코드. 운반체가 키를 하나 더 쓰거나 빼면 red."""
    summary, _, _ = evidence
    for item in summary["iterations"]:
        assert set(item) == _item_keys_from_source()
    assert summary["schema"] == BATCH_SUMMARY_SCHEMA
    assert set(summary) == {
        "schema",
        "request_id",
        "n",
        "started_at",
        "finished_at",
        "boot",
        "error",
        "iterations",
    }


def test_the_summary_lands_where_the_supervisor_looks_for_it(evidence):
    """하트비트 파일명은 M1 계약 상수 — 검사기가 읽는 문서가 M3가 읽는 그 문서다."""
    _, _, out_root = evidence
    written = json.loads((out_root / BATCH_SUMMARY_FILENAME).read_text(encoding="utf-8"))
    assert written["n"] == 4 and len(written["iterations"]) == 4


def test_sample_marker_literal_is_the_one_the_carrier_prints():
    """게이트 3의 구간 분할은 이 마커에 의존한다 — 소스에 못박는다(G-25)."""
    source = Path(m2_batch.__file__).read_text(encoding="utf-8")
    assert f'f"[cv-runner] {SAMPLE_MARKER}' in source


def test_the_realigner_outlives_the_iteration_but_realigns_inside_it():
    """게이트 1의 구조: 재정렬은 표본마다(루프 안), 발행자는 운반체당 하나(루프 밖).

    발행자를 반복 안에서 만들면 구독자에게 **발견되기 전에** 발행하고, 카운터는
    '재정렬했다'를 찍은 채 아무 일도 안 한다(G-26).
    """
    loop = _sample_loop()
    callables = _callables()
    assert len(_call_lines(loop).get("realign", [])) == 1
    boot_lines = set(_call_lines(_run_function()).get("SutRealigner", []))
    assert len(boot_lines) == 1
    assert not any(loop.lineno <= line <= loop.end_lineno for line in boot_lines)
    # ...and not one hop away either: a ``realigner = _fresh_realigner(adapter)`` in
    # the loop leaves the two asserts above untouched (the construction is spelled
    # at module level, outside the loop) while building a publisher per sample.
    hidden = reaching(loop, callables, "SutRealigner")
    assert hidden == [], (
        f"the realigner is built inside the sample loop behind a helper: {[str(h) for h in hidden]}"
        " — a per-sample publisher is never discovered in time (G-26)"
    )


def test_the_bag_session_is_per_sample_and_the_render_product_is_per_carrier():
    """게이트 2의 구조: mcap/mp4는 표본마다 열고 닫히고, 렌더 프로덕트는 운반체당 1개.

    레코더를 루프 밖으로 끌어올리면 한 세션이 n 표본을 이어 담고(잔존), 렌더
    프로덕트를 루프 안으로 내리면 p6c2가 제거한 VRAM 증가항이 돌아온다.
    """
    loop = _sample_loop()
    callables = _callables()
    loop_calls = _call_lines(loop)
    for per_sample in ("plan_artifacts", "RosbagRecorder", "begin_iteration", "end_iteration"):
        assert len(loop_calls.get(per_sample, [])) == 1, per_sample
    # 도달성으로 판정한다(이름이 아니라). ``_reopen_products(video)`` 한 줄이면
    # 직접-이름 스캔은 침묵하고 VRAM 증가항만 돌아온다 — p8c2 T6 실측.
    hidden = reaching(loop, callables, "open_render_product")
    assert hidden == [], (
        f"the render product is re-opened per sample: {[str(h) for h in hidden]} — "
        "this is the VRAM growth term p6c2 removed"
    )
    opens = reaching(_run_function(), callables, "open_render_product")
    assert (
        len(opens) == 1
    ), f"exactly one render product per carrier, found {[str(o) for o in opens]}"


def test_the_applied_settings_line_is_emitted_once_per_sample_after_the_first():
    """게이트 3의 구조: 루프의 ``emit_sim_config``는 ``if position:`` 아래 정확히 1회.

    가드가 사라지면 라인 수가 n+1이 되고(표본 0이 두 번), 호출이 사라지면 표본들이
    **부팅 때의 설정 한 줄**을 공유한다 = 반복별 증적 소멸.
    """
    loop = _sample_loop()
    emits = [
        node
        for node in ast.walk(loop)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr == "emit_sim_config"
    ]
    assert len(emits) == 1
    guards = [
        node
        for node in ast.walk(loop)
        if isinstance(node, ast.If)
        and ast.unparse(node.test) == "position"
        and node.lineno <= emits[0].lineno <= node.end_lineno
    ]
    assert guards, "emit_sim_config is no longer guarded by `if position:` (line count becomes n+1)"


def test_every_sample_hands_its_own_obstacle_set_to_the_restage():
    """게이트 5의 구조: 표본마다 **그 표본의** 세트를 넘기는가.

    합성 증적만으로는 이 층을 볼 수 없다 — 운반체가 ``obstacle_set`` 인자를 통째로
    빼도 요약·로그 픽스처는 그대로 초록이다(실측: M11 변이가 NEG-6 38개를 전부
    통과시켰다). 그래서 인자 자체를 AST 에서 본다.

    표본 0의 배치는 **부팅 훅**(``apply_obstacle_set``)이 하고 루프는 ``if position:``
    아래에서만 restage 하므로, 등록도 호출도 각각 정확히 하나여야 한다.
    """
    loop = _sample_loop()
    restages = [
        node
        for node in ast.walk(loop)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr == "restage"
    ]
    assert len(restages) == 1, "the sample loop restages exactly once"
    assert "obstacle_set" in [kw.arg for kw in restages[0].keywords], (
        "restage no longer receives this sample's obstacle set — every sample after 0 "
        "would run on sample 0's placement, and nothing else in this file would notice"
    )
    run = _run_function()
    applies = [
        node
        for node in ast.walk(run)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr == "append"
        and ast.unparse(node.func.value) == "sim.pre_reset"
        and "apply_obstacle_set" in ast.unparse(node)
    ]
    assert len(applies) == 1, "sample 0's placement is exactly one boot hook"
    assert not any(loop.lineno <= node.lineno <= loop.end_lineno for node in applies)
    # 등록 자체가 헬퍼 뒤로 숨는 판: ``_register_set(sim, ...)`` 를 루프에서 부르면
    # 위 두 단정은 그대로 통과하고(등록 코드가 모듈 최상위에 있으므로) 훅만 표본마다
    # 쌓인다 = 부팅 훅이 n번 돈다.
    registrations = reaching(loop, _callables(), "pre_reset.append")
    assert registrations == [], (
        f"a pre-reset hook is registered from inside the sample loop: "
        f"{[str(r) for r in registrations]} — boot hooks must be registered exactly once"
    )


# --------------------------------------------------------------------------- #
# 전 게이트 공통 — 표본별 호출이 **이 표본의 값**을 받는가 (G-106 ① 동종 점검)
# --------------------------------------------------------------------------- #
#: 게이트별 "이 표본의 값" 이음매: (이음매, 루프가 부르는 이름, 그 값이 서는 자리,
#: 거기 서야 하는 이름, 그 이름을 이 표본에서 길어 오는 식). 자리 = 위치 인자 색인(int)
#: 또는 키워드 이름(str).
_PER_SAMPLE_PAYLOADS = (
    ("realign", "realign", 0, "pose", "request.scenario.initial_pose"),
    ("plan_artifacts", "plan_artifacts", 0, "out_dir", "iteration_dir(out_root, index)"),
    ("restage", "restage", "obstacle_set", "obstacle_set", "obstacle_specs(request)"),
)


def _calls_named(node: ast.AST, name: str) -> list[ast.Call]:
    found = []
    for child in ast.walk(node):
        if isinstance(child, ast.Call):
            func = child.func
            called = func.attr if isinstance(func, ast.Attribute) else getattr(func, "id", None)
            if called == name:
                found.append(child)
    return found


def per_sample_payload_residue(loop: ast.For) -> list[str]:
    """표본별 증적을 만드는 호출이 **이 표본에서 길어 온 값**을 받고 있나.

    G-106 ①의 동종 점검이 찾아낸 구멍이다. 기존 구조 핀들은 *"호출이 루프 안에
    정확히 한 번 있다"* 까지만 보고, **그 호출에 무엇이 들어가는지**는 보지 않았다.
    그래서 인자를 운반체 상수로 바꾸는 편측 변이가 전부 초록이었다 (실측 2026-08-28,
    전 스위트 1543/1543 통과):

    * ``realigner.realign(pose)`` -> ``realign(None)`` — 표본마다 "시도 안 함"이
      찍히고 표본 i는 표본 i-1의 AMCL 믿음 위에서 돈다 (게이트 1이 겨냥한 그 잔존).
    * ``plan_artifacts(out_dir)`` -> ``plan_artifacts(iteration_dir(out_root, 0))``
      — 전 표본의 녹화가 ``results/0/``에 겹쳐 쌓인다 (게이트 2).
    * ``obstacle_set=obstacle_set`` -> ``head_obstacles`` — 표본 1..n-1이 표본 0의
      배치 위에서 판정된다 (게이트 5. 인자 **이름**만 보던 p7c2 핀은 이걸 못 봤다).

    합성 증적으로는 원리상 볼 수 없는 층이다 — 픽스처는 언제나 완벽한 증적을 만든다.
    시드는 호출이 아니라 대입이라 아래에서 따로 본다.
    """
    complaints: list[str] = []
    for seam, called, where, name, origin in _PER_SAMPLE_PAYLOADS:
        calls = _calls_named(loop, called)
        if len(calls) != 1:
            complaints.append(f"{seam}: {len(calls)} call(s) in the sample loop, expected 1")
            continue
        call = calls[0]
        if isinstance(where, int):
            passed = ast.unparse(call.args[where]) if len(call.args) > where else None
        else:
            passed = next((ast.unparse(kw.value) for kw in call.keywords if kw.arg == where), None)
        if passed != name:
            complaints.append(
                f"{seam}: receives {passed!r} where this sample's {name!r} belongs — "
                "every sample would be handed the same thing, and no synthetic evidence "
                "in this file would look any different"
            )
            continue
        bound = [
            node
            for node in ast.walk(loop)
            if isinstance(node, ast.Assign) and ast.unparse(node.targets[0]) == name
        ]
        if len(bound) != 1 or origin not in ast.unparse(bound[0].value):
            complaints.append(
                f"{seam}: {name!r} is no longer drawn from {origin} inside the loop "
                f"({len(bound)} binding(s) found)"
            )
    seeds = [
        node
        for node in ast.walk(loop)
        if isinstance(node, ast.Assign) and ast.unparse(node.targets[0]) == "sim.config.seed"
    ]
    if len(seeds) != 1 or ast.unparse(seeds[0].value) != "request.scenario.seed":
        complaints.append(
            "sim.config.seed: this sample's seed is not applied before the applied-settings "
            "line is emitted — every line would carry boot's seed while gate 3 stays green "
            "(it only asks that a seed field is present and not 'none')"
        )
    return complaints


def test_every_per_sample_call_is_handed_this_samples_value():
    assert per_sample_payload_residue(_sample_loop()) == []


#: 위 핀이 실제로 무장돼 있는지의 대조군. 변이는 **생산 소스에 그대로 적용**한다 —
#: 미니어처를 손으로 적으면 그것은 실물이 아니라 내가 만든 형태를 검사하게 된다(G-25).
#: 각 항목은 정확히 한 규칙만 울려야 한다(G-59: 대조는 겨냥한 규칙을 고립해야 한다).
_MEASURED_MUTATIONS = (
    (
        "게이트 1 — 이 표본의 포즈로 재정렬하지 않는다",
        "realigner.realign(pose)",
        "realigner.realign(None)",
        "realign",
    ),
    (
        "게이트 2 — 전 표본이 표본 0의 디렉토리에 녹화한다",
        "plan = plan_artifacts(out_dir)",
        "plan = plan_artifacts(iteration_dir(out_root, 0))",
        "plan_artifacts",
    ),
    (
        "게이트 3 — 이 표본의 시드가 적용되지 않는다",
        "            sim.config.seed = request.scenario.seed\n",
        "",
        "sim.config.seed",
    ),
    (
        "게이트 5 — 전 표본이 표본 0의 배치 위에서 돈다",
        "obstacle_set=obstacle_set",
        "obstacle_set=head_obstacles",
        "restage",
    ),
)


@pytest.mark.parametrize(("label", "old", "new", "seam"), _MEASURED_MUTATIONS)
def test_the_per_sample_payload_pin_fires_on_each_measured_mutation(label, old, new, seam):
    source = _carrier_source()
    assert source.count(old) == 1, (
        f"{label}: the mutation anchor {old!r} is no longer in the carrier verbatim — "
        "this positive control is disarmed until it is re-aimed"
    )
    complaints = per_sample_payload_residue(_sample_loop(source.replace(old, new)))
    assert len(complaints) == 1, f"{label}: expected exactly one rule to fire, got {complaints}"
    assert complaints[0].startswith(f"{seam}:"), f"{label}: the wrong rule fired — {complaints[0]}"


def test_the_soft_path_never_rebinds_the_telemetry_sampler():
    """게이트 4의 구조: bind 0.

    ``bind``는 부팅의 pre-reset 훅으로 **한 번** 등록되고, ``restage``는 그 훅들을
    다시 돌리지 않는다 — soft 리셋이 물리 시뮬레이션 뷰를 파괴하지 않으므로 텐서 뷰가
    운반체 수명 내내 유효하기 때문이다(p6c2 실측: 60반복, bind 추가 호출 0). 루프 안에서
    다시 bind 하면 반복마다 ContactReportAPI Apply + 텐서 뷰 생성이 쌓인다.

    표본 경계에서의 **교체 위치** 핀은 M2 소유(test_runner_batch.py) — 여기서는
    교체가 루프 안에 정확히 하나 있다는 것만 확인하고 중복하지 않는다.
    """
    loop = _sample_loop()
    rebinds = reaching(loop, _callables(), "bind")
    assert rebinds == [], (
        f"the sampler is re-bound inside the sample loop: {[str(r) for r in rebinds]} — "
        "every iteration would stack a ContactReportAPI Apply + a tensor view (p6c2)"
    )
    binds = [
        node
        for node in ast.walk(_run_function())
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr == "append"
        and ast.unparse(node.func.value) == "sim.pre_reset"
        and any(ast.unparse(arg) == "sampler.bind" for arg in node.args)
    ]
    assert len(binds) == 1
    assert not any(loop.lineno <= node.lineno <= loop.end_lineno for node in binds)

    swaps = [
        node
        for node in ast.walk(loop)
        if isinstance(node, ast.Assign)
        and ast.unparse(node.targets[0]) == "sampler.record"
        and ast.unparse(node.value) == "TelemetryRecord()"
    ]
    assert len(swaps) == 1

    restage = next(
        node
        for node in ast.walk(ast.parse(Path(sim_runtime.__file__).read_text(encoding="utf-8")))
        if isinstance(node, ast.FunctionDef) and node.name == "restage"
    )
    assert "pre_reset" not in ast.unparse(restage)


# --------------------------------------------------------------------------- #
# 위 네 봉합의 대조군 — "헬퍼 이름 뒤로 한 칸" 변이 (p8c2 T6 실측, G-106 ④)
# --------------------------------------------------------------------------- #
#: 운반체 소스에 헬퍼를 심는 자리. 리터럴이 사라지면 아래 대조군이 **무장 해제**되므로
#: 심기 전에 verbatim 존재를 확인한다(``_MEASURED_MUTATIONS``와 같은 규율).
_HELPER_ANCHOR = "\ndef run(env: dict | None = None) -> int:"
_LOOP_ANCHOR = "            out_dir = iteration_dir(out_root, index)\n"

#: (라벨, 모듈-로컬 헬퍼 정의, 루프에 심을 호출, 그 변이가 되살리는 회귀를 잡는 철자).
#: 넷 다 **인라인으로 쓰면 예전 가드도 red**였다 — 헬퍼 한 칸이 그 red를 지웠다
#: (p8c2 T6 실측: 봉합 전 전 스위트 1683/1683 초록).
_HELPER_HIDING_MUTATIONS = (
    (
        "게이트 2 — 렌더 프로덕트가 표본마다 다시 열린다 (p6c2 VRAM 증가항 부활)",
        "def _reopen_products(video: object) -> None:\n    video.open_render_product()\n",
        "            _reopen_products(video)\n",
        "open_render_product",
    ),
    (
        "게이트 4 — soft 경로가 표본마다 수집기를 다시 bind 한다",
        "def _rebind_sampler(sampler: object, world: object) -> None:\n"
        "    sampler.bind(world)\n",
        "            _rebind_sampler(sampler, sim.world)\n",
        "bind",
    ),
    (
        "게이트 1 — 발행자가 표본마다 새로 만들어진다 (G-26)",
        "def _fresh_realigner(adapter: object) -> object:\n"
        "    step, clock = adapter.step_and_spin, lambda: adapter.sim_time_s\n"
        "    return SutRealigner(adapter.node, step, clock)\n",
        "            realigner = _fresh_realigner(adapter)\n",
        "SutRealigner",
    ),
    (
        "게이트 5 — 부팅 훅이 표본마다 다시 등록된다",
        "def _register_set(sim: object, entries: object) -> None:\n"
        "    sim.pre_reset.append(lambda _world: sim.apply_obstacle_set(entries))\n",
        "            _register_set(sim, staging.head_obstacles)\n",
        "pre_reset.append",
    ),
)

_HIDDEN_SPELLINGS = tuple(spelling for *_, spelling in _HELPER_HIDING_MUTATIONS)


def _hidden_behind_a_helper(helper: str, loop_call: str) -> str:
    """운반체 소스에 모듈-로컬 헬퍼를 하나 심고 표본 루프에서 그것을 부른다."""
    source = _carrier_source()
    assert source.count(_HELPER_ANCHOR) == 1, "헬퍼를 심을 자리가 사라졌다 — 대조군 무장 해제"
    assert source.count(_LOOP_ANCHOR) == 1, "루프 앵커가 사라졌다 — 대조군 무장 해제"
    return source.replace(_HELPER_ANCHOR, "\n" + helper + _HELPER_ANCHOR).replace(
        _LOOP_ANCHOR, _LOOP_ANCHOR + loop_call
    )


@pytest.mark.parametrize(("label", "helper", "loop_call", "spelling"), _HELPER_HIDING_MUTATIONS)
def test_the_sealed_pins_see_through_one_helper_hop(label, helper, loop_call, spelling):
    """봉합된 네 단정은 호출이 **헬퍼 이름 뒤로 한 칸** 옮겨져도 발화한다.

    옛 판(직접 호출 이름만 세는 스캔)에서는 이 변이 넷이 **전부 초록**이었다.
    각 변이는 겨냥한 철자 **하나만** 울려야 한다(G-59: 대조는 규칙을 고립해야 한다).
    """
    mutated = _hidden_behind_a_helper(helper, loop_call)
    loop, callables = _sample_loop(mutated), _callables(mutated)
    assert reaching(loop, callables, spelling), f"{label}: 봉합된 핀이 헬퍼 한 칸을 못 본다"
    others = [other for other in _HIDDEN_SPELLINGS if other != spelling]
    assert reaching(loop, callables, *others) == [], f"{label}: 겨냥하지 않은 철자가 같이 울렸다"


def test_the_healthy_carrier_reaches_none_of_the_hidden_spellings():
    """비공허 대조: 현행 운반체에서는 네 철자 중 어느 것도 루프에서 도달되지 않는다."""
    assert reaching(_sample_loop(), _callables(), *_HIDDEN_SPELLINGS) == []
