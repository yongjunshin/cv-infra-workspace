"""배포 정체성 하드코딩 0 — 실행이 특정 기계/카드에 분기하지 않는다.

게이트 `DoD-P5-09` 전반절(*"코드/compose에 호스트명·GPU 모델 리터럴 grep = 0"*) ·
DoD §6 보조 negative(*"하드코딩 리터럴 0"*) · REQ-DEPLOY-003 · NFR-DEPLOY-001.
후반절(적응 선택)은 ``tests/test_deploy_gpu_profiles.py``가 집행한다.

**★ 금지의 실체 (1문장).**
    이 저장소가 커밋한 **실행 평면 바이트**(python·셸·Dockerfile·compose·CI YAML)
    어디에서도 **배포 대상의 정체성**(호스트명·GPU 모델명)이 **동작을 좌우하는
    자리**(비교/분기, 또는 변수·env·설정 키에 묶이는 값)에 리터럴로 나타나지 않는다.

**왜 "grep = 0"을 그대로 쓰지 않는가 (문면 교체 권고).** 게이트 문면을 문자 그대로
실행하면(실측, base ``c97ff12``)::

    git grep -n -i -E "etri6000|RTX[ _-]?PRO[ _-]?6000|A100" \
        -- cv_infra docker scripts .github actions      # -> 21행

**21행 중 동작을 묶는 것은 1행**(``scripts/workstation_setup/common.sh:125``, 아래에서
수리)이고 **나머지 20행은 프로방넌스**다(코드 주석 11 + 문서 9) — 예:

    docker/runner/Dockerfile:20  # Tag+digest locked 2026-07-07 at first pull on etri6000 …

이건 G-24가 **요구하는** 증적 앵커다(그 핀이 어디서 확정됐는지를 재현 가능하게 남긴 것).
지우면 이식성은 1밀리미터도 좋아지지 않고 증적만 사라진다. 즉 순진한 grep은 **20/21이
오탐**이고, 통과시키는 가장 쉬운 방법이 **증적 삭제**라서 적극적으로 해롭다. 그래서
이 파일은 리터럴의 **존재**가 아니라 **위치(=binding/branching)** 를 금지한다.

**두 축, 두 규칙** (합집합):

* **R1 — 구조(값 없음)**: 호스트/GPU **정체성 질의의 출력**이 같은 줄에서 비교된다
  (``==``·``!=``·``=~``·``[ x = y ]``·``case``). 값을 열거하지 않으므로 **처음 보는
  호스트명·처음 보는 카드**에도 발화한다 — ``if socket.gethostname() == "prod-gpu-01"``.
* **R2 — 값(유도)**: **알려진 정체성 리터럴**이 binding 자리에 나타난다. GPU 쪽 값
  목록은 손으로 들지 않고 **``profiles/*.yaml``의 ``match_name_pattern``에서 유도**한다
  (G-56 ②의 형태: 목록은 스캔이 아니라 도메인을 정의하는 쪽이 정한다 — 프로파일을
  추가하면 스캔이 자동으로 넓어진다). 호스트 쪽은 이 프로젝트가 배포해 본 유일한 기계
  이름 하나(``_KNOWN_HOSTS``)이고, 그 협소함은 **R1이 값 없이 덮는다**.

**허용(=오탐이면 스캔이 틀린 것)**: 프로방넌스 산문 · ``$(hostname -s)``처럼 정체성을
**런타임에 파생**시키는 기본값(그게 바로 우리가 원하는 형태다) · 문서(``*.md``) ·
적응 데이터 레이어 ``profiles/**``.

**스캔 평면과 그 밖(정직 표기 — 잡을 수 있는 척하지 않는다)**:

* 대상 = ``cv_infra/`` · ``docker/`` · ``scripts/`` · ``.github/`` · ``actions/``
  (M5 D9가 고정한 대상 ``cv_infra/**``·``docker/compose.yaml``·코어의 **상위집합** —
  실측해 보면 실제 정체성 리터럴은 전부 ``scripts/``·``docker/``에 있다).
* 제외 ① ``**/*.md`` — 문서는 실행되지 않고, **프로방넌스가 살아야 하는 바로 그 평면**
  이다. ``test_provenance_anchors_survive``가 그 앵커들이 지워지지 않았음을 단정한다.
* 제외 ② ``profiles/**`` — GPU 모델 지식의 유일한 sanctioned 위치(M5 D9 /
  ``_meta/revision-decisions.md:103``). 정직 표기: **오늘의 프로파일은 스캔에 넣어도
  발화하지 않는다**(지식이 정규식 패턴 + 산문이라 binding 자리가 아니다) — 그 사실과
  제외가 지는 하중을 ``test_profiles_are_outside_the_scan_and_that_exclusion_is_load_
  bearing``이 함께 고정한다.
* 제외 ③ ``tests/`` — 아래 무장 샘플이 일부러 하드코딩을 담고 있고, 테스트는 배포되지
  않는다.
* **못 잡는 것**: 줄 단위 스캔이라 키와 값이 다른 줄에 있는 대입, 조각에서 조립되는
  이름, ``profiles``에 없는 **모델을 특이한 대소문자로** 박는 경우(R2는 GPU 축을
  대소문자 구분으로 본다 — 아래 ``_gpu_tokens`` 주석 참조), 운영자의 미추적 ``.env``.

Stdlib + pytest. 신규 의존 0.
"""

from __future__ import annotations

import re
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[2]

#: 실행 평면(= 커밋된 바이트가 배포에서 실제로 실행/해석되는 곳).
_SCAN_PLANES = ("cv_infra", "docker", "scripts", ".github", "actions")

#: 산문 평면 — 제외하되, 비어 있지 않음을 아래에서 단정한다.
_PROSE_SUFFIXES = (".md",)

#: 적응 데이터 레이어(M5 D9 allowlist). 평면 목록 밖이므로 자동 제외되지만, 이 상수는
#: ``test_allowlist_is_load_bearing``이 "그럼 이게 왜 안 걸리나"를 명시적으로 답하게 한다.
_ALLOWLIST_DIR = "profiles"

#: 정체성 **질의**(R1의 왼쪽). 값이 아니라 출처를 겨냥하므로 처음 보는 이름에도 걸린다.
_IDENTITY_SOURCE = re.compile(
    r"\bhostname\b"  # shell: hostname / hostname -s
    r"|\bHOSTNAME\b"  # shell: $HOSTNAME
    r"|uname\s+-n"
    r"|socket\.gethostname"
    r"|platform\.node"
    r"|os\.uname"
    r"|--query-gpu=name"  # nvidia-smi: the GPU model string
    r"|nvmlDeviceGetName"
)

#: 비교/분기 연산자. **평범한 대입(``X=$(hostname)``)은 일부러 제외** — 정체성에서
#: 기본값을 파생시키는 것은 금지가 아니라 목표다(scripts/workstation_setup/common.sh).
_COMPARISON = re.compile(r"==|!=|=~|\s=\s|\bcase\b")

#: R2 호스트 축. 이 프로젝트가 배포해 본 유일한 기계(agent-comms/decisions/
#: 2026-07-07-workstation-access-ssh-first-alpacon-fallback.md §"동일 호스트 실측 확증"
#: 에 hostname으로 기록). 목록이 협소한 것은 **의도된 분업**이다 — 일반 호스트 축은
#: 값을 모르는 R1이 구조로 덮는다. 대소문자 무시(호스트명 관례).
_KNOWN_HOSTS = ("etri6000",)


def _gpu_tokens() -> tuple[str, ...]:
    """R2 GPU 축의 값 집합을 ``profiles/*.yaml``에서 **유도**한다 (G-56 ②).

    스캔이 자기 목록을 손으로 들면 도메인과 갈리고, 그 차집합이 구멍이 된다. 여기서는
    프로파일이 선언한 ``match_name_pattern``을 그대로 쓴다 — 새 GPU를 지원하면(=프로파일
    추가) 그 모델을 코드에 박는 것도 **자동으로** 금지된다.

    대소문자는 **구분한다**: 드라이버가 보고하는 모델 문자열의 표기를 겨냥하는 것이고,
    무시하면 (a) 소문자 프로파일 id(``--profile a100`` = 합법적 매뉴얼 선택)와
    (b) 16진 다이제스트 안에 우연히 들어간 ``a100`` 같은 부분문자열을 오탐한다.
    """
    tokens = []
    for path in sorted((_REPO_ROOT / _ALLOWLIST_DIR).glob("*.yaml")):
        for line in path.read_text(encoding="utf-8").splitlines():
            if line.startswith("match_name_pattern:"):
                tokens.append(line.split(":", 1)[1].strip())
    return tuple(tokens)


def _binding_patterns(token: str, *, flags: int, yaml_like: bool) -> list[re.Pattern[str]]:
    """리터럴이 **묶이는** 자리(값 위치)를 겨냥하는 패턴들.

    ``IDENT=<...token...>``(셸 대입 · Dockerfile ``ENV`` · CLI 플래그 · python 대입) ·
    비교 우변 · YAML ``key: value``. 산문("… at first pull on etri6000 …")은 토큰 앞에
    묶는 연산자가 없으므로 발화하지 않는다.
    """
    forms = [
        rf"[A-Za-z_][A-Za-z0-9_]*\s*=\s*[^\s#]*(?:{token})",  # X=…tok
        rf"[A-Za-z_][A-Za-z0-9_]*\s*=\s*[\"'][^\"']*(?:{token})",  # X="… tok …"
        rf"(?:==|!=|=~|\s=\s)\s*[^\s\"'#]*(?:{token})",  # … == …tok
        rf"(?:==|!=|=~|\s=\s)\s*[\"'][^\"']*(?:{token})",  # … == "… tok …"
    ]
    if yaml_like:
        forms.append(rf"^\s*[A-Za-z_][\w.-]*\s*:\s+[^#]*(?:{token})")  # key: …tok
    return [re.compile(form, flags) for form in forms]


def identity_hardcoding(line: str, *, yaml_like: bool = False) -> bool:
    """이 한 줄이 배포 정체성에 **동작을 묶고 있는가** (R1 ∪ R2).

    이 스캔의 **단일 출처**. 두 곳이 각자 규칙을 들면 갈라지고, 갈라진 차집합이 구멍이
    된다(G-56이 정확히 그 사고였다).
    """
    if _IDENTITY_SOURCE.search(line) and _COMPARISON.search(line):
        return True
    for token in _KNOWN_HOSTS:
        if any(p.search(line) for p in _binding_patterns(token, flags=re.I, yaml_like=yaml_like)):
            return True
    for token in _gpu_tokens():
        if any(p.search(line) for p in _binding_patterns(token, flags=0, yaml_like=yaml_like)):
            return True
    return False


def _scan_files() -> list[Path]:
    files: list[Path] = []
    for plane in _SCAN_PLANES:
        for path in sorted((_REPO_ROOT / plane).rglob("*")):
            if not path.is_file() or "__pycache__" in path.parts:
                continue
            if path.suffix in _PROSE_SUFFIXES:
                continue
            files.append(path)
    return files


def _hits() -> list[str]:
    hits: list[str] = []
    for path in _scan_files():
        yaml_like = path.suffix in (".yml", ".yaml")
        for number, line in enumerate(
            path.read_text(encoding="utf-8", errors="ignore").splitlines(), 1
        ):
            if identity_hardcoding(line, yaml_like=yaml_like):
                hits.append(f"{path.relative_to(_REPO_ROOT)}:{number}: {line.strip()}")
    return hits


# --------------------------------------------------------------------------- #
# 실제 하드코딩(발화해야 함) vs 실재하는 합법 형태(발화하면 안 됨)
# --------------------------------------------------------------------------- #

#: 심으면 배포가 특정 기계/카드에 묶이는 형태들. 8·9번은 **한 줄에 질의가 없는** 경우로,
#: R1(구조)이 못 보는 자리를 R2(값)가 덮는다는 것을 고정한다.
_ARMED_SAMPLES: tuple[str, ...] = (
    # 이 저장소에 실제로 있었던 형태 (p5c11 이전 scripts/workstation_setup/common.sh:125)
    'readonly CV_GH_RUNNER_NAME="${CV_GH_RUNNER_NAME:-etri6000-cv-infra}"',
    'if [[ "$(hostname -s)" == "etri6000" ]]; then',
    # R1은 값을 모른다 — 처음 보는 호스트명에도 발화한다
    'if socket.gethostname() == "prod-gpu-01":',
    '    if platform.node() != "gpu-box-7":',
    'case "$(uname -n)" in',
    '[[ "$HOSTNAME" =~ ^gpu- ]] && CV_MAX_CONCURRENT=8',
    'name = subprocess.check_output(["nvidia-smi", "--query-gpu=name"]) == b"NVIDIA A100"',
    # 질의는 다른 줄에 있고 비교만 남은 형태 (R2 값 축이 덮는다)
    '[[ "$gpu_name" == "NVIDIA A100-SXM4-40GB" ]]',
    "ENV CV_TARGET_GPU=NVIDIA_RTX_PRO_6000",
)

#: YAML 평면(compose·CI)의 ``key: value`` 형태.
_ARMED_YAML_SAMPLES: tuple[str, ...] = (
    "  runs-on: etri6000-cv-infra",
    '  image: "registry.local/cv-infra@etri6000"',
)

#: 저장소에 **실재하는** 합법 형태 verbatim — 여기 걸리면 스캔이 틀린 것이다.
#: 앞의 다수는 G-24 프로방넌스(핀이 어디서 확정됐는지의 증적), 뒤는 정체성을 런타임에
#: 파생시키는 올바른 형태. 파일이 바뀌어도 이 대조군은 남는다(G-35).
_LEGITIMATE_SAMPLES: tuple[str, ...] = (
    # --- 프로방넌스 산문 (docker/, scripts/) ---
    "# Tag+digest locked 2026-07-07 at first pull on etri6000 (= Python 3.11.15,",
    "# locked 2026-07-07 at first pull on etri6000); keep the two in sync.",
    "# Measured 2026-07-07 (etri6000, docker inspect + in-container id/ls):",
    "#     EXECUTION stage (2026-06-26, host etri6000) against the live download.docker.com /",
    "# Host platform. These scripts target exactly ONE OS (recon: etri6000, Ubuntu 24.04.4);",
    "# LOCKED 2026-07-03 from the first pull's RepoDigests on etri6000 (manifest-list",
    "#                      DKMS (Secure Boot is off on etri6000 — unsigned OK).",
    "# KNOWN HOST DEBT (discovered 2026-07-03): etri6000 carries an orphan (non-dpkg)",
    "# on etri6000 — non-dpkg, not removable via the sudo whitelist) only warn.",
    "# 2026-07-03 on etri6000; NOT root//root/.cache as the pre-measurement R2 note",
    "# Authorized user: etri  (the workstation SSH account on etri6000)",
    "# GPU-passthrough smoke image (DoD-P1-02). CUDA 12.8+ covers Blackwell; the in-container",
    # --- 정체성을 런타임에 파생시키는 형태 = 우리가 원하는 것 ---
    'readonly CV_GH_RUNNER_NAME="${CV_GH_RUNNER_NAME:-$(hostname -s)-cv-infra}"',
    'default_identity="$(id -un)@$(hostname)"',
    'CV_RECORD_HOST="$(hostname)" \\',
    'names="$("$CV_NVIDIA_SMI" --query-gpu=name --format=csv,noheader 2>/dev/null)" \\',
    # --- 16진 다이제스트 (소문자 a100 부분문자열 오탐 대조) ---
    'readonly CV_X_DIGEST="${CV_X_DIGEST:-sha256:133c78a100303be34164d0b90137a042172bdf60}"',
    # --- 매뉴얼 선택(운영자 입력)은 하드코딩이 아니다 ---
    "CV_GPU_PROFILE=a100",
    "./scripts/detect_gpu.sh --profile rtx_pro_6000",
)

#: 스캔이 각 평면을 **실제로 읽고 있음**을 보이는 실재 앵커(부재가 조용한 통과로 둔갑하지
#: 못하게 — p5c8이 실측한 함정의 이 게이트판).
_SCAN_SENTINELS = (
    ("cv_infra", "cv_infra/orchestrator/scheduler.py"),
    ("docker", "docker/compose.yaml"),
    ("scripts", "scripts/detect_gpu.sh"),
    (".github", ".github/workflows/ci.yml"),
)

#: 이 스캔을 통과시키려고 **지우면 안 되는** 증적 앵커(G-24). 위 문면 논의 참조.
_PROVENANCE_ANCHORS = (
    "docker/runner/Dockerfile",
    "docker/orchestrator/Dockerfile",
    "scripts/workstation_setup/common.sh",
    "scripts/workstation_setup/realign_driver_r580.sh",
    "scripts/isaac_smoke/run_smoke.sh",
)


def test_no_deployment_identity_is_hardcoded_into_the_runtime_planes():
    """본 게이트: 실행 평면에 정체성 binding 0 (REQ-DEPLOY-003, NFR-DEPLOY-001)."""
    assert _hits() == []


def test_scan_patterns_are_not_vacuous():
    """무장 실증 + 오탐 대조 — 두 방향이 **쌍으로** 있어야 증거가 된다 (G-35).

    발화만 보면 "전부 금지"하는 스캔이 통과하고(그러면 다음 사람이 증적을 지우게 된다),
    오탐만 보면 아무것도 안 잡는 스캔이 통과한다.
    """
    for sample in _ARMED_SAMPLES:
        assert identity_hardcoding(sample), f"실제 하드코딩을 놓쳤다: {sample}"
    for sample in _ARMED_YAML_SAMPLES:
        assert identity_hardcoding(sample, yaml_like=True), f"YAML 하드코딩을 놓쳤다: {sample}"
    for sample in _LEGITIMATE_SAMPLES:
        assert not identity_hardcoding(sample), f"합법 형태에 오탐: {sample}"


def test_structural_rule_needs_no_value_list():
    """R1이 **값을 모른 채** 발화함을 고정 — 값 목록만 있는 스캔은 새 기계를 못 본다.

    호스트명을 하나도 모르는 상태에서도 "정체성 질의 + 비교"라는 **모양**만으로 잡힌다.
    G-56의 교훈("스캔이 값을 열거하면 가드보다 좁다")의 이식성 축 판.
    """
    unknown_host = "machine-we-have-never-heard-of-42"
    assert unknown_host not in str(_KNOWN_HOSTS)
    assert identity_hardcoding(f'if [ "$(hostname)" = "{unknown_host}" ]; then')
    assert identity_hardcoding(f'if socket.gethostname() == "{unknown_host}":')
    # 반대편: 같은 이름이 그냥 산문에 있으면 발화하지 않는다.
    assert not identity_hardcoding(f"# provisioned first on {unknown_host} (2026-01-01)")


def _concrete_name_for(token: str) -> str:
    """``token``(정규식)이 실제로 매칭하는 **구체 문자열** 하나.

    선택적 문자 클래스(``[ _-]?``)를 지우면 그 패턴이 매칭하는 최단 인스턴스가 된다
    (``RTX[ _-]?PRO[ _-]?6000`` -> ``RTXPRO6000``). 아래에서 자기검사를 걸어, 더 복잡한
    패턴이 들어와 이 축약이 성립하지 않으면 **조용히 통과하지 않고 red**가 되게 한다.
    """
    return re.sub(r"\[[^\]]*\]\?", "", token)


def test_gpu_value_axis_is_derived_from_the_profiles():
    """R2 GPU 값 목록의 출처 = ``profiles/*.yaml``. 손으로 든 두 번째 목록이 없다.

    프로파일을 추가하면 그 모델을 코드에 박는 것이 **자동으로** 금지된다 — 목록을 스캔이
    들고 있으면 도메인과 갈리고 그 차집합이 구멍이 된다(G-56 ②).
    """
    tokens = _gpu_tokens()
    assert len(tokens) >= 2, f"프로파일에서 유도된 토큰이 없다시피 하다: {tokens}"
    for token in tokens:
        name = _concrete_name_for(token)
        assert re.search(token, name), f"인스턴스화 실패 — 이 단정이 공허해진다: {token}"
        assert identity_hardcoding(f'if [[ "$gpu" == "{name}" ]]; then')
        assert identity_hardcoding(f"ENV CV_TARGET_GPU={name}")


def test_profiles_are_outside_the_scan_and_that_exclusion_is_load_bearing():
    """``profiles/**``가 스캔 평면 밖이라는 **구조적 사실** + 그 제외가 하중을 진다는 것.

    정직 표기: **오늘의 프로파일 파일들은 스캔 안에 넣어도 발화하지 않는다** — 지식이
    정규식 패턴과 산문으로만 들어 있고 구체 모델명을 binding 자리에 묶지 않기 때문이다.
    그래도 제외는 장식이 아니다: 프로파일이 허용된 다른 형태(구체 이름을 키에 묶는 선언)를
    취하는 순간 그 줄은 **발화하는 모양**이고, 그때 이 디렉토리를 봐주는 근거가 M5 D9의
    allowlist다. 아래 합성 줄이 그 형태를 고정한다.
    """
    assert not any(path.parts[0] == _ALLOWLIST_DIR for path in _scan_files())
    assert identity_hardcoding("match_name_exact: NVIDIA A100-SXM4-40GB", yaml_like=True)


def test_profiles_actually_hold_the_model_knowledge():
    """반대편: 허용된 자리에 지식이 **실제로 있다**.

    프로파일이 비면 스캔은 여전히 green이지만 배포는 아무 GPU도 인식하지 못한다 — 게이트가
    "지식 없음"을 "하드코딩 없음"으로 오독하지 않게 고정한다.
    """
    tokens = _gpu_tokens()
    for path in sorted((_REPO_ROOT / _ALLOWLIST_DIR).glob("*.yaml")):
        text = path.read_text(encoding="utf-8")
        assert any(re.search(token, text) for token in tokens), f"{path.name}: 모델 지식 없음"


def test_detect_gpu_script_carries_no_model_literal():
    """M5 D9의 allowlist는 ``scripts/detect_gpu.sh``의 매핑 테이블까지 허용하지만,
    구현은 그 여유를 **쓰지 않는다** — 패턴은 프로파일이 선언하고 스크립트는 모델을
    하나도 모른다. 그래서 이 스크립트는 다른 코드와 동일하게 스캔 대상이다(예외 0).
    """
    script = (_REPO_ROOT / "scripts" / "detect_gpu.sh").read_text(encoding="utf-8")
    for token in _gpu_tokens():
        assert not re.search(token, script), f"detect_gpu.sh가 모델 리터럴을 들고 있다: {token}"
    assert str(_SCAN_PLANES).count("scripts") == 1  # 그 파일이 스캔 평면 안이라는 앵커


def test_scan_reads_real_non_empty_planes():
    """스캔 무장: 평면 이름이 틀리면 **조용히** 0 매치가 된다(p5c8 실측 함정)."""
    scanned = {path.relative_to(_REPO_ROOT).as_posix() for path in _scan_files()}
    for plane, sentinel in _SCAN_SENTINELS:
        assert (_REPO_ROOT / plane).is_dir(), f"스캔 평면 부재: {plane}/"
        assert sentinel in scanned, f"{plane}/ 스캔이 실재 파일 {sentinel}조차 안 읽었다"
    assert len(scanned) > 30, f"스캔이 읽은 파일이 너무 적다: {len(scanned)}"


def test_provenance_anchors_survive():
    """★ 반대 방향의 위험: 스캔을 통과시키려고 **증적을 지우는 것**을 막는다.

    이 게이트의 자연스러운 실패 모드는 "grep이 걸리니 주석에서 호스트명을 지우자"이다.
    그러면 통과는 하지만 핀이 어디서 확정됐는지가 사라지고(G-24 위반) 이식성은 그대로다.
    아래 파일들은 그 앵커를 **계속 들고 있어야** 한다.
    """
    for relative in _PROVENANCE_ANCHORS:
        text = (_REPO_ROOT / relative).read_text(encoding="utf-8")
        assert any(host in text.lower() for host in _KNOWN_HOSTS), (
            f"{relative}: 프로방넌스 앵커가 사라졌다 — 스캔을 통과시키려고 증적을 지우면"
            " G-24 위반이고 이식성은 좋아지지 않는다"
        )


def test_documentation_plane_is_excluded_on_purpose_and_is_not_empty():
    """제외 ①의 반대편: 문서에는 정체성 언급이 **실제로 살아 있다**.

    제외가 "어차피 아무것도 없는 평면"을 봐주는 공허한 문장이 아님을 보인다.
    """
    docs = [
        path
        for plane in _SCAN_PLANES
        for path in (_REPO_ROOT / plane).rglob("*")
        if path.is_file() and path.suffix in _PROSE_SUFFIXES
    ]
    assert docs, "산문 평면이 비었다 — 제외 규칙이 공허하다"
    assert any(
        host in path.read_text(encoding="utf-8", errors="ignore").lower()
        for path in docs
        for host in _KNOWN_HOSTS
    ), "문서에 호스트 언급이 하나도 없다 — 제외 규칙이 공허하다"
