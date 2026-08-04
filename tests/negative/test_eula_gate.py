"""NEG-2 · EULA 자동수락 금지 — 동의 없이는 Isaac이 기동하지 않는다.

DoD §6 NEG-2가 **파일명까지 고정**한 negative 스위트 (REQ-DEPLOY-008/009/010/011 ·
NFR-DEPLOY-004; 게이트 DoD-P5-06; 결정 2026-07-03-p1-eula-runtime-consent).
게이트 문면 3항 ↔ 테스트 매핑:

1. "동의 미기록 상태 ``docker compose up`` -> 러너/Isaac 부트 거부 로그 + exit 3"
   -> ``test_runner_entrypoint_refuses_without_consent_with_exit_3``
   -> ``test_cpu_substitute_runs_the_image_entrypoint``   (등가 앵커)
   -> ``test_consent_released_run_gets_past_the_gate``    (G-35 반대편/착지)
2. "``grep -rn "ACCEPT_EULA=Y" docker/ cv_infra/ scripts/ docs/`` -> 하드코딩 박힘 0"
   -> ``test_no_acceptance_literal_is_baked_into_the_gate_dirs``
   -> ``test_no_acceptance_value_injection_in_any_syntax``  (G-21 전 구문형)
   -> ``test_gate_scan_reads_real_non_empty_directories``   (스캔 무장 실증)
   -> ``test_scan_patterns_are_not_vacuous``                (패턴 비공허 + 오탐 대조)
   -> ``test_ci_plane_bakes_no_automatic_consent``          (계약의 CI 절, p5c10)
3. "동의 레코드 없음 -> 기동 차단 단정; 동의(identity+timestamp) 기록 후 -> 기동 해제 단정"
   -> ``test_boot_guard_blocks_when_no_consent_is_recorded``
   -> ``test_boot_guard_releases_once_consent_is_recorded``
   -> ``test_boot_refuses_before_any_isaac_import``       (거부가 Isaac보다 먼저)

**★ CPU 등가로 대체한 항 (정직 표기 — 숨기지 않는다).**

* 1항의 ``docker compose up``은 **이 기기(CPU)에서 실행 불가**하고, 애초에
  **저장소에 compose 파일이 아직 없다**(``docker/compose/``는 빈 디렉토리 — M5 산출물
  미도래). 그래서 여기서는 compose가 결국 띄우는 **바로 그 프로세스**를 직접 띄운다:
  ``python -m cv_infra.runner.main`` = 러너 이미지의 ENTRYPOINT
  (``docker/runner/Dockerfile``). 등가 주장이 말뿐이 되지 않도록
  ``test_cpu_substitute_runs_the_image_entrypoint``이 Dockerfile의 ENTRYPOINT와
  아래 subprocess가 실행하는 모듈이 **같음을 기계적으로 단정**한다 — ENTRYPOINT가
  바뀌면 이 등가 주장이 red로 무너진다.
  **커버 못 하는 것**: docker 데몬/compose 파싱/이미지·GPU 계층. 컨테이너 경계에서의
  exit-3 전파는 GPU 사이클 실측 몫이다(본 파일은 프로세스 exit 코드까지).
* 3항의 "동의(identity+timestamp) **기록**"은 **아직 제품에 없다**. 결정
  2026-07-03-p1-eula-runtime-consent가 명시하듯 정식 동의 게이트(identity+timestamp
  영속 기록, ``scripts/consent/*``)는 **Phase 5 / M5 산출물이며 미구현**이고, 현재
  구현된 것은 **런타임 운영자 입력(per-run env)** 브리지뿐이다. 따라서 여기서 검증
  가능한 것은 "동의 입력 부재 -> 차단 / 동의 입력 존재 -> 해제"까지이며, 레코드의
  identity·timestamp 필드 자체는 **미검증**(그런 레코드가 존재하지 않는다).

**G-35 무장 실증.** "부트가 안 됐다"는 negative는 *다른 이유로 죽어도* 참이다. 그래서
차단 단정마다 **반대편(해제)** 을 같은 형태로 함께 돌려, 차단이 EULA 게이트 때문임을
보인다(해제 시 실행이 게이트를 지나 Isaac import까지 전진). grep 항은 **스캔 대상
디렉토리가 실재·비공허함**과 **패턴이 실제 주입 형태에 발화함**을 각각 단정한다 —
p5c8이 실측한 함정(문면의 ``profiles/`` 부재로 grep이 exit 2를 내고 "0 매치"와
구분되지 않던 것)이 정확히 "스캔이 아무것도 안 읽어도 통과"하는 공허함이다.

**동의 값 리터럴 0.** 이 파일은 ``ACCEPT_EULA=Y`` 같은 **수락 값 리터럴을 텍스트로
담지 않는다**(G-21) — 양성 대조 샘플은 키 상수에서 **런타임 조립**한다. 덕분에
저장소 전역 grep이 이 테스트 자신에 걸리지 않는다.

**★ p5c10 공허화 보강 2건**(p5c9 Step 5 자기 발견 -> 본 사이클 재현·특정). 두 벡터
모두 **실측으로** 확인했다 — 각각을 심으면 이 파일(그리고 전 스위트 790개)이 그대로
green이었다:

* **(a) 호출/콤마 구문 값 주입**: ``environment.setdefault("ACCEPT_EULA", "Y")`` 를
  ``supervisor.run_job``의 러너 컨테이너 env 조립부에 심으면 **오케스트레이터가 모든
  잡의 EULA를 대신 수락**하는데, 등호·콜론만 겨냥한 패턴은 이를 못 본다(G-21의
  세 번째 구문형). -> 값 주입 패턴의 구분자에 ``,`` 를 추가.
* **(b) CI 평면 미스캔**: ``.github/workflows/ci.yml``에 수락 리터럴을 넣어도 게이트
  문면의 스캔 대상 4개 디렉토리에 ``.github/`` 가 없어 무사통과한다(``M8``의
  ``test_gh_wiring_static.py``는 ``verify.yml``+composite action **2개 파일만** 본다).
  NEG-2 계약은 *"CI도 동의 자동 우회 금지"* 를 명시하므로 그 절을 아래
  ``test_ci_plane_bakes_no_automatic_consent``가 집행한다(게이트 문면의 4개 디렉토리
  스캔은 그대로 — 축소 아님, 계약 절의 추가 집행).

Stdlib + pytest (+ 기존 픽스처 YAML). 신규 의존 0.
"""

from __future__ import annotations

import ast
import inspect
import json
import os
import re
import subprocess
import sys
from importlib.util import find_spec
from pathlib import Path

import pytest
import yaml

from cv_infra.cli.exit_codes import EXIT_INFRA
from cv_infra.runner.main import EXIT_PLATFORM
from cv_infra.runner.sim_runtime import EulaNotAcceptedError, SimRuntime, eula_boot_guard

_REPO_ROOT = Path(__file__).resolve().parents[2]
_FIXTURE = _REPO_ROOT / "tests" / "fixtures" / "nova_carter_warehouse_goal.yaml"
_DOCKERFILE = _REPO_ROOT / "docker" / "runner" / "Dockerfile"

#: The runner module the runner image's ENTRYPOINT executes (등가 앵커 — the
#: Dockerfile equality is asserted, never assumed).
_RUNNER_MODULE = "cv_infra.runner.main"

#: Consent env keys (NAMES are the cross-team wire; VALUES are operator input).
_CONSENT_KEY = "ACCEPT_EULA"
_PRIVACY_KEY = "PRIVACY_CONSENT"

#: Opaque consent token — the repo idiom (tests/test_cli_run.py): the guard is a
#: truthiness check, so no acceptance VALUE literal is ever committed (G-21).
_OPAQUE_CONSENT = "operator-consent-token"

# --------------------------------------------------------------------------- #
# grep 항 (2) — the gate's scan, its arming, and its non-vacuity
# --------------------------------------------------------------------------- #

#: DoD §6 NEG-2 2항의 스캔 대상 **verbatim** (2026-08-03 정정본 — 저장소에 없는
#: ``profiles/`` 제거됨). 게이트가 고정한 목록이라 늘리거나 줄이지 않는다.
_GATE_DIRS = ("docker", "cv_infra", "scripts", "docs")

#: 게이트 문면의 리터럴 그대로. 조립해 만든다 — 이 파일에 수락 값이 박히지 않도록.
_GATE_LITERAL = f"{_CONSENT_KEY}=Y"

#: G-21: 값 주입은 등호형 하나가 아니다. 콜론/JSON/subscript 대입 **+ 호출 인자
#: (콤마)** 까지 겨냥한 상위집합 (기존 ``tests/test_gh_wiring_static.py``의 관용구를
#: 넓힌 형태 — 그 패턴은 dict 콜론형 ``{"ACCEPT_EULA": "Y"}``를 놓친다[p5c9 실측] 그리고
#: 호출형 ``setdefault("ACCEPT_EULA", "Y")``도 놓친다[p5c10 실측, 모듈 docstring (a)]).
#: 콤마 구분자가 저장소의 합법적인 **키 튜플 나열**(``("ACCEPT_EULA", "PRIVACY_CONSENT")``)
#: 을 잡지 않는 이유는 값 자리가 truthy 리터럴로 한정되기 때문 — 아래 오탐 대조가 단정.
_ANY_SYNTAX = re.compile(
    rf"({_CONSENT_KEY}|{_PRIVACY_KEY})[\"']?\s*\]?\s*[:=,]\s*[\"']?\s*(Y|y|yes|Yes|true|True|1)\b"
)

#: 실제 주입이 이렇게 생겼다면 잡혀야 한다(패턴 비공허 — 런타임 조립).
_INJECTION_SAMPLES = (
    f"ENV {_CONSENT_KEY}=Y",
    f'{_CONSENT_KEY}="Y"',
    f'environment = {{"{_CONSENT_KEY}": "Y"}}',
    f"      {_PRIVACY_KEY}: yes",
    f'os.environ["{_CONSENT_KEY}"] = "1"',
    f"- {_CONSENT_KEY}=true",
    # p5c10 (a): 호출 인자형 — 실제로 심어 본 벡터 그대로.
    f'environment.setdefault("{_CONSENT_KEY}", "Y")',
    f'os.environ.setdefault("{_PRIVACY_KEY}", "yes")',
    f'os.putenv("{_CONSENT_KEY}", "1")',
)

#: 스캔이 각 게이트 디렉토리를 **실제로 읽고 있음**을 보이는 실재 문자열(무장 실증).
#: 전부 동의 경로에 실재한다: 러너 이미지 주석(NEG-2), M3 전달 키 상수, 스모크 래퍼의
#: 운영자 입력 게이트, 배포 매뉴얼의 consent env 지시.
_SCAN_SENTINELS = (
    ("docker", "NEG-2"),
    ("cv_infra", "CONSENT_ENV_KEYS"),
    ("scripts", "CV_EULA_CONSENT"),
    ("docs", _CONSENT_KEY),
)

#: 저장소에 **실재하는 합법 형태**(런타임 주입·키 이름 참조) — 여기 걸리면 오탐이다.
_LEGITIMATE_SAMPLES = (
    'CV_EULA_DOCKER_ARGS=(-e "ACCEPT_EULA=${_consent_upper:0:1}")',
    'CONSENT_ENV_KEYS = ("ACCEPT_EULA", "PRIVACY_CONSENT")',
    'if not environ.get("ACCEPT_EULA"):',
    "-e ACCEPT_EULA=<consent> -e PRIVACY_CONSENT=<consent>",
    'consent_env_present=["ACCEPT_EULA","PRIVACY_CONSENT"]',
)


#: NEG-2 계약의 **CI 절**("CI도 동의 자동 우회 금지, self-test 선행")이 걸리는 평면.
#: 게이트 문면의 스캔 4개 디렉토리 밖이라 p5c10 이전에는 이 파일이 한 줄도 안 읽었다
#: (모듈 docstring (b) — 실측: ``ci.yml``에 수락 리터럴을 심어도 790/790 green).
_CI_DIRS = (".github",)

#: CI 평면 스캔이 **실재 파일을 읽고 있음**을 보이는 앵커(이름이 바뀌면 red — 부재가
#: 조용한 통과로 둔갑하던 p5c8 함정의 CI판).
_CI_SENTINEL_FILES = (".github/workflows/ci.yml", ".github/workflows/verify.yml")


def _scan_files(dirs: tuple[str, ...] = _GATE_DIRS) -> list[Path]:
    """Every regular file under ``dirs`` (``grep -r`` 등가).

    ``__pycache__`` only holds compiled copies of sources already scanned, so it is
    skipped to keep the scan deterministic; nothing else is filtered.
    """
    files: list[Path] = []
    for name in dirs:
        for path in sorted((_REPO_ROOT / name).rglob("*")):
            if path.is_file() and "__pycache__" not in path.parts:
                files.append(path)
    return files


def _hits(predicate, dirs: tuple[str, ...] = _GATE_DIRS) -> list[str]:
    return [
        f"{path.relative_to(_REPO_ROOT)}:{number}: {line.strip()}"
        for path in _scan_files(dirs)
        for number, line in enumerate(
            path.read_text(encoding="utf-8", errors="ignore").splitlines(), 1
        )
        if predicate(line)
    ]


def test_no_acceptance_literal_is_baked_into_the_gate_dirs():
    """게이트 문면 그대로: ``ACCEPT_EULA=Y`` 하드코딩 박힘 0."""
    assert _hits(lambda line: _GATE_LITERAL in line) == []


def test_no_acceptance_value_injection_in_any_syntax():
    """G-21 상위집합: 등호/콜론/JSON/subscript/**호출 인자** 어떤 구문으로도 수락 값 주입 0.

    호출 인자형은 p5c10 보강분 (a) — ``environment.setdefault("<key>", "Y")`` 를
    ``run_job``의 러너 env 조립부에 심어 실측했다: 보강 전에는 전 스위트 790/790 green.
    """
    assert _hits(lambda line: bool(_ANY_SYNTAX.search(line))) == []


def test_gate_scan_reads_real_non_empty_directories():
    """스캔 무장 실증 (p5c8 이슈 3): 이름이 틀린 디렉토리는 **조용히 통과**한다.

    p5c8이 실측했듯 게이트 문면이 부재 경로(``profiles/``)를 포함하면 grep은 exit 2
    (오류)를 내는데, 셸에서 이것이 "0 매치(exit 1)"와 구분되지 않아 게이트를 무심코
    통과시킬 수 있었다. 여기서는 **부재 = 즉시 red**다.

    임의 파일 수 임계 대신 **디렉토리마다 실재하는 동의 관련 문자열**을 찾게 한다 —
    자동수락이 숨을 바로 그 파일들까지 스캔이 실제로 읽고 있음을 보이는 형태.
    """
    for name in _GATE_DIRS:
        assert (_REPO_ROOT / name).is_dir(), f"NEG-2 게이트가 지목한 디렉토리 부재: {name}/"

    scanned = _scan_files()
    for directory, needle in _SCAN_SENTINELS:
        root = _REPO_ROOT / directory
        found = any(
            needle in path.read_text(encoding="utf-8", errors="ignore")
            for path in scanned
            if path.is_relative_to(root)
        )
        assert found, f"{directory}/ 스캔이 실재하는 {needle!r}조차 못 읽었다 — 공허하다"


def test_ci_plane_bakes_no_automatic_consent():
    """계약 문면의 CI 절 집행: CI 워크플로가 동의를 자동 수락하지 않는다.

    NEG-2 계약은 *"CI도 동의 자동 우회 금지(self-test 선행)"* 를 명시하는데, 검증 명령의
    grep 대상 4개 디렉토리에는 ``.github/``가 없다 — 즉 **계약의 한 절이 스캔 밖**이었다
    (p5c10 실측: ``ci.yml``에 수락 리터럴 1줄을 심어도 이 파일 10/10·전 스위트 790/790
    green). M8의 ``test_gh_wiring_static.py``가 CI를 보긴 하지만 ``verify.yml`` +
    composite action **2개 파일만**이고 패턴도 등호/콜론형뿐이다.

    무장 실증을 함께 건다(부재가 통과로 둔갑하지 못하게): 스캔이 실재 워크플로 파일을
    읽고 있음을 앵커로 단정한다.
    """
    scanned = _scan_files(_CI_DIRS)
    names = {path.relative_to(_REPO_ROOT).as_posix() for path in scanned}
    assert set(_CI_SENTINEL_FILES) <= names, f"CI 평면 스캔이 워크플로를 못 읽었다: {sorted(names)}"
    assert any(
        "runs-on" in path.read_text(encoding="utf-8", errors="ignore") for path in scanned
    ), "CI 평면 스캔이 잡 정의조차 못 봤다 — 공허하다"

    assert _hits(lambda line: _GATE_LITERAL in line, _CI_DIRS) == []
    assert _hits(lambda line: bool(_ANY_SYNTAX.search(line)), _CI_DIRS) == []


def test_scan_patterns_are_not_vacuous():
    """패턴이 실제 주입 형태에 발화하고, 합법 형태에는 발화하지 않는다."""
    for sample in _INJECTION_SAMPLES:
        assert _ANY_SYNTAX.search(sample), f"실제 값 주입을 놓쳤다: {sample}"
    assert _GATE_LITERAL in _INJECTION_SAMPLES[0]  # 게이트 리터럴도 살아 있다
    for sample in _LEGITIMATE_SAMPLES:
        assert not _ANY_SYNTAX.search(sample), f"합법 런타임 주입에 오탐: {sample}"


# --------------------------------------------------------------------------- #
# 부트 게이트 (1항·3항) — guard seam
# --------------------------------------------------------------------------- #


@pytest.fixture(autouse=True)
def no_ambient_consent(monkeypatch: pytest.MonkeyPatch) -> None:
    """Hermetic baseline: 개발자 셸의 잔여 동의 env가 차단 단정을 무력화하지 못하게."""
    for key in (_CONSENT_KEY, _PRIVACY_KEY):
        monkeypatch.delenv(key, raising=False)


def test_boot_guard_blocks_when_no_consent_is_recorded():
    """동의 레코드 없음 -> 기동 차단 (부재·빈 값 모두)."""
    for environ in ({}, {_CONSENT_KEY: ""}, {_PRIVACY_KEY: _OPAQUE_CONSENT}):
        with pytest.raises(EulaNotAcceptedError):
            eula_boot_guard(environ)


def test_boot_guard_releases_once_consent_is_recorded():
    """동의 기록 후 -> 기동 해제 (G-35 반대편: 게이트가 상시 거부하는 게 아님).

    한계(정직): 현재 "기록"은 **런타임 운영자 입력(env)** 이다 — identity+timestamp
    영속 레코드는 M5/P5 산출물로 아직 없다(모듈 docstring 참조). 따라서 이 단정은
    "동의 입력이 있으면 게이트가 해제된다"까지만 증명한다.
    """
    assert eula_boot_guard({_CONSENT_KEY: _OPAQUE_CONSENT}) is None


def test_boot_refuses_before_any_isaac_import():
    """거부가 **Isaac을 건드리기 전에** 일어난다 — 자동수락 우회로가 낄 틈 없음.

    두 층으로 본다.
    * 정적(호스트 무관): ``SimRuntime.boot``의 AST에서 ``eula_boot_guard()`` 호출이
      첫 ``isaacsim``/``omni`` import보다 **앞**임을 단정. 순서를 뒤집으면 red.
    * 동적(이 CPU 호스트): ``isaacsim``이 없는 호스트에서 ``boot()``가
      ``ModuleNotFoundError``가 아니라 ``EulaNotAcceptedError``를 낸다 = import에
      도달하기 전에 거부했다는 관찰.
    """
    tree = ast.parse(inspect.getsource(SimRuntime.boot).strip())
    guard_line = next(
        node.lineno
        for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Name)
        and node.func.id == "eula_boot_guard"
    )
    isaac_import_line = next(
        node.lineno
        for node in ast.walk(tree)
        if isinstance(node, ast.ImportFrom)
        and node.module is not None
        and node.module.split(".")[0] in {"isaacsim", "omni", "carb"}
    )
    assert guard_line < isaac_import_line, "EULA 게이트가 Isaac import보다 뒤에 있다"

    runtime = SimRuntime.__new__(SimRuntime)  # boot() only needs the guard to fire
    with pytest.raises(EulaNotAcceptedError):
        SimRuntime.boot(runtime)


# --------------------------------------------------------------------------- #
# 1항 CPU 등가 — the process the runner image's ENTRYPOINT starts
# --------------------------------------------------------------------------- #


def _job_spec(job_id: str = "neg2-job-0") -> str:
    document = yaml.safe_load(_FIXTURE.read_text(encoding="utf-8"))
    return json.dumps({"job_id": job_id, **document})


def _run_entrypoint(tmp_path, *, consent: bool) -> tuple[int, str, Path]:
    """Run the runner entrypoint as a REAL process; return (exit code, stderr, result path).

    The env is built from scratch (no ambient leak — G-41 in spirit): only PATH/HOME
    plus the M2 seam variables, and the consent key only when asked for.
    """
    result_path = tmp_path / ("consent" if consent else "no-consent") / "result.json"
    result_path.parent.mkdir(parents=True, exist_ok=True)
    env = {
        "PATH": os.environ.get("PATH", ""),
        "HOME": str(tmp_path),
        "JOB_SPEC": _job_spec(),
        "RESULT_OUT": str(result_path),
    }
    if consent:
        env[_CONSENT_KEY] = _OPAQUE_CONSENT
    completed = subprocess.run(
        [sys.executable, "-m", _RUNNER_MODULE],
        cwd=str(_REPO_ROOT),
        env=env,
        capture_output=True,
        text=True,
        timeout=120,
    )
    return completed.returncode, completed.stderr, result_path


def test_cpu_substitute_runs_the_image_entrypoint():
    """등가 앵커: 아래 subprocess가 실행하는 모듈 = 러너 이미지의 ENTRYPOINT.

    이게 없으면 "compose up 대신 CPU에서 같은 걸 돌렸다"는 주장이 말뿐이다. 덤으로
    이미지가 수락 env를 굽지 않음도 단정한다(NEG-2 계약의 이미지 절반).
    """
    dockerfile = _DOCKERFILE.read_text(encoding="utf-8")
    entrypoint = next(line for line in dockerfile.splitlines() if line.startswith("ENTRYPOINT"))
    assert json.loads(entrypoint[len("ENTRYPOINT") :].strip()) == [
        "./python.sh",
        "-m",
        _RUNNER_MODULE,
    ]
    assert not re.search(rf"^\s*ENV\s+.*{_CONSENT_KEY}", dockerfile, re.MULTILINE)
    assert not re.search(rf"^\s*ENV\s+.*{_PRIVACY_KEY}", dockerfile, re.MULTILINE)


def test_runner_entrypoint_refuses_without_consent_with_exit_3(tmp_path):
    """동의 미기록 -> 부트 거부 로그 + exit 3 (프로세스 반환값, 손으로 쓴 상수 아님)."""
    code, stderr, result_path = _run_entrypoint(tmp_path, consent=False)

    assert code == EXIT_PLATFORM == EXIT_INFRA == 3
    assert "EULA" in stderr and "refused" in stderr, stderr
    assert "NEG-2" in stderr, stderr
    assert not result_path.exists(), "거부된 실행이 결과를 남겼다 — 미션이 돌았다는 뜻"
    assert "Traceback" not in stderr  # 친절 에러 경로 (NFR-INTAKE-001 정신)


@pytest.mark.skipif(
    find_spec("isaacsim") is not None,
    reason="Isaac이 설치된 호스트에서는 이 대조군이 실제 GPU 부팅을 시도하게 된다",
)
def test_consent_released_run_gets_past_the_gate(tmp_path):
    """G-35 착지: 동의가 있으면 **같은 실행**이 게이트를 지나 Isaac import까지 간다.

    이 대조군이 없으면 위 exit 3은 "아무 이유로든 죽었다"와 구분되지 않는다. 이 CPU
    호스트에는 Isaac이 없으므로 전진의 종착점은 ``ModuleNotFoundError: isaacsim`` —
    거부 로그는 사라지고, 러너는 정규 error result를 남긴다(REQ-EXEC-013).
    **한계**: CPU에서는 여기까지다. 실제 Isaac 기동은 GPU 사이클 몫.
    """
    code, stderr, result_path = _run_entrypoint(tmp_path, consent=True)

    assert "boot refused" not in stderr, stderr  # 게이트가 해제됐다
    assert "isaacsim" in stderr, stderr  # 게이트를 지나 Isaac import까지 전진
    assert result_path.exists()  # 미션이 시작됐으므로 정규 result가 남는다
    assert code == EXIT_PLATFORM  # Isaac 부재는 플랫폼 실패 — 여전히 3
    assert _OPAQUE_CONSENT not in stderr  # 동의 값은 어떤 로그에도 타지 않는다 (G-21)
