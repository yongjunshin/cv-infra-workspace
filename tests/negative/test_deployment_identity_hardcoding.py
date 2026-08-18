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
  대소문자 구분으로 본다 — 아래 ``_gpu_tokens`` 주석 참조), 운영자의 로컬 ``.env``.
  **★ p5c17 정정 — 마지막 항은 문장으로만 존재했다**(NEG-2 스캔에서 실제로 터진 것과
  같은 결함, QA findings §12-5). 이 파일의 ``_scan_files`` 도 ``docker/**`` 를 ``rglob``
  했으므로 운영자 ``.env`` 가 스캔에 들어와 있었다. **오늘 red 가 아니었던 이유는
  스코프가 아니라 술어의 운이다**: ``②'`` 절차가 ``detect_gpu.sh >> docker/.env`` 를
  시키므로 **모든 배포 호스트의 ``.env`` 는 GPU 모델 리터럴을 담는데**(실측:
  ``# detected GPU : NVIDIA GeForce RTX 4080``), 그 줄들이 주석이라 R1(구조)·R2(값)의
  binding/branching 요건을 만족하지 못했을 뿐이다. 술어를 조이는 날 — 또는 누가 그 값을
  주석 아닌 형태로 넣는 날 — **전 배포 호스트가 red** 가 된다. 이제 제외를 git 에게
  묻는다(``test_eula_gate.git_ignored_under`` 공유).

Stdlib + pytest. 신규 의존 0.
"""

from __future__ import annotations

import re
import subprocess
from pathlib import Path

from tests.negative.test_eula_gate import git_ignored_under

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


def _scan_files(root: Path = _REPO_ROOT) -> list[Path]:
    """실행 평면의 파일 전부 — 산문(``.md``)과 **git 이 무시하는 것**은 빼고.

    제외 축은 **"무시 여부"이지 "추적 여부"가 아니다**(D-5 에서 세운 판단과 동일):
    아직 커밋되지 않았지만 무시되지도 않은 새 파일은 **그대로 스캔된다**. 추적 여부로
    좁혔다면 갓 만든 소스 파일이 빠져 그게 진짜 구멍이 됐을 것이다.
    제외 규칙의 정의는 ``test_eula_gate.git_ignored_under`` **하나**다(G-56).
    """
    ignored = git_ignored_under(_SCAN_PLANES, root)
    files: list[Path] = []
    for plane in _SCAN_PLANES:
        for path in sorted((root / plane).rglob("*")):
            if not path.is_file() or "__pycache__" in path.parts:
                continue
            if path.suffix in _PROSE_SUFFIXES:
                continue
            if path in ignored:
                continue
            files.append(path)
    return files


def _hits(root: Path = _REPO_ROOT) -> list[str]:
    hits: list[str] = []
    for path in _scan_files(root):
        yaml_like = path.suffix in (".yml", ".yaml")
        for number, line in enumerate(
            path.read_text(encoding="utf-8", errors="ignore").splitlines(), 1
        ):
            if identity_hardcoding(line, yaml_like=yaml_like):
                hits.append(f"{path.relative_to(root)}:{number}: {line.strip()}")
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
    "#                  which is the SSH account on the first workstation and a WRONG",
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

#: sudo 드롭인 템플릿 — 여기 계정 이름을 박으면 다른 호스트에서 **틀린 값**이 된다.
#: (p5c17 이전: 첫 워크스테이션의 SSH 계정이 리터럴로 박혀 있었다.)
_SUDOERS_TEMPLATE = "scripts/workstation_setup/sudoers.d-cv-infra"

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


def _sudoers_user_field(text: str) -> str | None:
    """드롭인의 user spec(``<user> ALL=(root) …``)에서 **user 필드**만 뽑는다."""
    for line in text.splitlines():
        stripped = line.strip()
        if stripped.startswith("#") or not stripped:
            continue
        match = re.match(r"^(\S+)\s+ALL=", stripped)
        if match:
            return match.group(1)
    return None


def test_sudo_dropin_does_not_hardcode_an_operator_account():
    """NOPASSWD 드롭인의 계정 이름은 **호스트마다 다르다** — 템플릿이어야 한다.

    ``scripts/`` 안의 정체성 리터럴이므로 `DoD-P5-09`의 관심사인데, 위 R1/R2 스캔은
    이 축(=OS 계정)을 보지 않는다(호스트명·GPU 모델만 본다). 그래서 여기서 별도로 건다.

    비공허 실증(G-35): 같은 추출기에 **박힌 형태**를 물리면 플레이스홀더가 아니라고
    답해야 한다 — 추출기가 항상 None 을 돌려주면 위 단정은 저절로 통과한다.
    """
    text = (_REPO_ROOT / _SUDOERS_TEMPLATE).read_text(encoding="utf-8")
    user = _sudoers_user_field(text)

    assert user is not None, f"{_SUDOERS_TEMPLATE}: user spec 을 찾지 못했다"
    assert user == "cv_infra_operator", (
        f"{_SUDOERS_TEMPLATE}: user 필드가 '{user}' — 특정 호스트의 계정 이름을 박지 말고 "
        "플레이스홀더를 두고 설치 시점에 치환하라 (README Step A)"
    )
    # 무장 실증: 박힌 계정이면 위 단정이 실제로 깨진다.
    assert _sudoers_user_field("etri ALL=(root) NOPASSWD: CV_INFRA_MAINT") == "etri"


# --------------------------------------------------------------------------- #
# 호스트 전제 누출 0 — NFR-DEPLOY-005 (p5c17 QA: 산문 앵커를 실행 가능한 것으로 교체)
# --------------------------------------------------------------------------- #

#: **호스트에 미리 설치돼 있어야 한다고 요구하는** 형태만 겨냥한다. 컨테이너가 스스로
#: 갖는 것(``nvidia/cuda@sha256:…`` 이미지 참조, 이미지 내부 경로 ``/isaac-sim``)은
#: 정확히 이 NFR 이 원하는 것이므로 겨냥하지 않는다 — 축은 *CUDA/Isaac 이 언급되는가* 가
#: 아니라 *호스트 파일시스템/호스트 패키지를 전제하는가* 다.
_HOST_PREREQ_LEAK = re.compile(
    r"(?:/usr/local/cuda"
    r"|/opt/nvidia/isaac[-_]?sim"
    r"|\bnvcc\b"
    r"|apt(?:-get)?\s+install[^\n]*\b(?:cuda[-\w]*|isaac[-\w]*)\b)",
    re.IGNORECASE,
)

#: 심으면 잡혀야 하는 것(무장 실증).
_PREREQ_ARMED_SAMPLES = (
    "ENV LD_LIBRARY_PATH=/usr/local/cuda/lib64",
    "  nvcc --version || die 'install the CUDA toolkit first'",
    "sudo -n apt-get install -y cuda-toolkit-12-3",
    '    "/opt/nvidia/isaac-sim/python.sh",',
)

#: 잡히면 **오탐**인 것 — 이 NFR 이 요구하는 바로 그 형태들.
_PREREQ_LEGITIMATE_SAMPLES = (
    'readonly CV_CUDA_TEST_IMAGE="nvidia/cuda:13.0.1-base-ubuntu24.04"',
    "  docker run --rm --gpus all $img nvidia-smi",
    "WORKDIR /isaac-sim",  # 이미지 **내부** 경로 = 동반배포의 증거
    '  -v "$CACHE_ROOT/cache/kit:/isaac-sim/kit/cache:rw"',
    "FROM nvcr.io/nvidia/isaac-sim:5.1.0@sha256:f3563cb2",
)


def test_deploy_plane_requires_no_host_cuda_or_isaac_install():
    """`NFR-DEPLOY-005` 를 **실행 가능한 앵커로** 고정한다 — 호스트 전제는 드라이버+toolkit 뿐.

    ★ 왜 이 테스트가 생겼나(p5c17 QA). 이 NFR 의 근거는 그동안 산문 한 줄이었고
    (*"호스트에 CUDA 툴킷·Isaac 미설치 상태로 성공"*), 그 문장은 **거짓이었다**:
    이 프로젝트의 GPU 호스트 두 대 모두 호스트 CUDA 를 갖고 있다 — 두 번째 호스트는
    ``cuda-*-12-3`` + ``/usr/bin/nvcc``(QA 실측 2026-08-19), 첫 호스트는 저장소 자신이
    ``scripts/workstation_setup/realign_driver_r580.sh`` 헤더에 *"host CUDA toolkit 12.0
    packages (libcudart12 etc.)"* 로 적어 두고 있다. 즉 **부재로는 이 NFR 을 증명한 적이
    없고 앞으로도 못 한다** — 증명해야 하는 것은 *부재* 가 아니라 **미의존** 이다.

    그래서 판정을 관측 가능한 것으로 바꾼다: **배포 평면이 호스트 CUDA/Isaac 설치를
    한 번도 전제하지 않는다.** 컨테이너 쪽 참조(핀된 CUDA 이미지, 이미지 내부
    ``/isaac-sim``)는 반대 방향의 증거이므로 통과시킨다.

    이 테스트가 덮지 **않는 것**(정직 표기): 런타임 마운트에 호스트 CUDA 경로가 섞이지
    않는다는 것은 여기서 정적으로 볼 수 없다. 그쪽은 라이브 관측이 앵커다
    (p5c17: 러너 마운트 8/8 이 전부 cv-infra 디렉토리, 컨테이너 CUDA 13.0 ≠ 호스트 12.3).
    """
    leaks = [
        f"{path.relative_to(_REPO_ROOT)}:{number}: {line.strip()}"
        for path in _scan_files()
        for number, line in enumerate(
            path.read_text(encoding="utf-8", errors="ignore").splitlines(), 1
        )
        if _HOST_PREREQ_LEAK.search(line)
    ]
    assert leaks == [], f"배포 평면이 호스트 CUDA/Isaac 설치를 전제한다: {leaks}"

    # 무장 실증 (G-35): 심으면 잡히고, 정당한 형태는 안 잡힌다.
    for sample in _PREREQ_ARMED_SAMPLES:
        assert _HOST_PREREQ_LEAK.search(sample), f"무장 실패 — 놓친 벡터: {sample!r}"
    for sample in _PREREQ_LEGITIMATE_SAMPLES:
        assert not _HOST_PREREQ_LEAK.search(sample), f"오탐 — 정당한 형태를 잡았다: {sample!r}"

    # 스캔이 실제로 무언가를 읽었는가(부재가 통과로 둔갑하지 못하게).
    assert len(_scan_files()) > 50


# --------------------------------------------------------------------------- #
# 운영자 로컬 파일 carve-out — 헤더가 약속한 것을 실행 가능하게 (p5c17 QA, D-5 동형)
# --------------------------------------------------------------------------- #


def _fake_deployment(root: Path, *, baked: bool) -> Path:
    """운영자 ``.env`` 가 살아 있는 **배포 체크아웃**의 최소 복제(진짜 git 저장소).

    ``docker/.env`` 는 ``.gitignore`` 가 무시한다 — 그리고 **실제 배포의 그것처럼**
    호스트 정체성을 담는다(``②'`` 가 ``detect_gpu.sh >> docker/.env`` 를 시키므로 GPU
    모델 줄은 항상 들어간다; 여기서는 술어가 실제로 발화하는 ``HOSTNAME=`` 형태까지
    넣어 **carve-out 이 없으면 반드시 red 가 되도록** 만든다 = 대조군이 공허하지 않다).
    ``baked`` 면 무시되지 **않는** 배송 파일에 같은 정체성을 굽는다.
    """
    for plane in _SCAN_PLANES:
        (root / plane).mkdir(parents=True, exist_ok=True)
    (root / ".gitignore").write_text(".env\n", encoding="utf-8")
    (root / "docker" / ".env").write_text(
        f"# detected GPU : NVIDIA {_gpu_tokens()[0]}\nHOSTNAME={_KNOWN_HOSTS[0]}\n",
        encoding="utf-8",
    )
    shipped = f"HOSTNAME={_KNOWN_HOSTS[0]}\n" if baked else "HOSTNAME=$(hostname -s)\n"
    (root / "docker" / "entrypoint.sh").write_text(f"#!/bin/sh\n{shipped}", encoding="utf-8")
    subprocess.run(["git", "init", "-q", str(root)], check=True, capture_output=True)
    return root


def test_the_operator_env_carve_out_is_real_and_armed(tmp_path):
    """헤더가 약속한 carve-out(*"운영자의 로컬 ``.env``"*)을 **양방향으로** 고정한다.

    NEG-2 에서 실제로 터진 것과 같은 결함의 두 번째 인스턴스다(QA findings §12-5):
    그쪽은 배포 호스트에서 **red 였고**, 여기는 술어가 주석을 안 잡아서 **아직 안
    터졌을 뿐** 이었다. 고쳐진 것과 안 터진 것은 다르다.

    ⓐ 운영자 ``.env`` 만 -> **0 hit**(carve-out 실재).
    ⓑ ★ **positive control (G-35)**: 무시되지 **않는** 파일에 같은 정체성 -> **red**.
       좁히기가 스캔을 죽였다면 여기도 0 이 나온다 — 그건 수리가 아니라 삭제다.
    """
    # ⓐ — 대조군이 공허하지 않음을 먼저 보인다: 이 .env 의 내용은 스캔되면 발화한다.
    clean = _fake_deployment(tmp_path / "clean", baked=False)
    env_lines = (clean / "docker" / ".env").read_text(encoding="utf-8").splitlines()
    assert any(identity_hardcoding(line) for line in env_lines), "대조군이 공허하다"
    assert _hits(clean) == []

    # ⓑ — 배송되는 파일이면 잡힌다.
    armed = _fake_deployment(tmp_path / "armed", baked=True)
    armed_hits = _hits(armed)
    assert len(armed_hits) == 1, f"좁히기가 스캔을 죽였다: {armed_hits}"
    assert "docker/entrypoint.sh:2" in armed_hits[0]
