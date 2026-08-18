"""적응형 GPU 프로파일 — 런타임 탐지가 프로파일을 고른다 (REQ-DEPLOY-003, NFR-DEPLOY-001).

게이트 `DoD-P5-09`의 후반절(*"``detect_gpu``→``profiles/rtx_pro_6000.yaml`` 적응 선택
관찰"*)에 대응한다. 전반절(하드코딩 리터럴)은 negative 쪽
``tests/negative/test_deployment_identity_hardcoding.py``가 집행한다.

**★ 이 파일이 증명하는 것과 아닌 것 (정직 표기).**

증명한다: 선택이 **런타임 질의 결과**로 갈린다는 것. 같은 호스트·같은 코드에서
``nvidia-smi --query-gpu=name``의 출력만 바꾸면 선택된 프로파일이 바뀐다. 스크립트는
GPU 모델 리터럴을 한 개도 갖고 있지 않고(패턴은 프로파일이 선언한다), 호스트 정체성을
보지 않는다.

증명하지 **않는다**: **실제 GPU에서의 관찰.** 여기서 ``nvidia-smi``는 stub 스크립트다
(``CV_NVIDIA_SMI`` 시임 — 스크립트가 이 시임을 갖는 이유의 절반은 GPU 없는 CI에서
배포 경로를 검증하기 위함이다). 아래 후보 문자열 중 **``rtx_4080`` 행 하나만 실측
캡처**이고(p5c17 T1, 두 번째 배포 호스트에서 ``nvidia-smi --query-gpu=name`` 1회 질의),
나머지는 여전히 *가정된 표기*다 — 그래서 프로파일의 ``match_name_pattern``은 일부러
토큰 기반으로 느슨하다. 라이브 확인 = ``./scripts/detect_gpu.sh`` 1회 실행 + 출력 보존.

Stdlib + pytest (+ pyyaml, 이미 의존). 신규 의존 0.
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest
import yaml

_REPO_ROOT = Path(__file__).resolve().parents[1]
_SCRIPT = _REPO_ROOT / "scripts" / "detect_gpu.sh"
_PROFILE_DIR = _REPO_ROOT / "profiles"

#: 프로파일 문법(평평한 ``key: value``)이 허용하는 키 전부. 필드를 늘리려면 소비자를
#: 먼저 만들어라 — 아무도 읽지 않는 필드는 다음 사람이 사실로 믿는 거짓말이 된다.
_ALLOWED_KEYS = {"id", "match_name_pattern", "vram_per_instance_mb", "vram_per_instance_source"}
_REQUIRED_KEYS = {"id", "match_name_pattern"}

#: 스크립트의 종료 계약 (헤더 참조): 3 = 선택 실패(fail-closed, infra/config 클래스).
_EXIT_UNSUPPORTED = 3

#: **가정된** nvidia-smi 표기 후보. 실측 캡처가 아니다(모듈 docstring 참조) — 매처가
#: SKU 표기 변형에 느슨한지를 보는 입력이지, 카드가 이렇게 말한다는 주장이 아니다.
_NAME_SAMPLES = (
    ("NVIDIA RTX PRO 6000 Blackwell Workstation Edition", "rtx_pro_6000"),
    ("NVIDIA RTX PRO 6000 Blackwell Server Edition", "rtx_pro_6000"),
    ("NVIDIA RTX PRO 6000 Blackwell Max-Q Workstation Edition", "rtx_pro_6000"),
    ("nvidia rtx_pro_6000", "rtx_pro_6000"),
    ("NVIDIA A100-SXM4-40GB", "a100"),
    ("NVIDIA A100 80GB PCIe", "a100"),
    # ★ 실측 캡처 (p5c17 T1, CEO 로컬 호스트): nvidia-smi 가 실제로 내놓은 문자열.
    ("NVIDIA GeForce RTX 4080", "rtx_4080"),
)

#: 위 후보 중 **실측 VRAM 이 없는** 프로파일로 가는 것들. 목록을 손으로 들지 않고
#: 프로파일에서 유도한다(G-56 ②) — 프로파일이 숫자를 얻으면 이 목록에서 자동으로 빠지고,
#: 새 미실측 프로파일은 자동으로 들어온다.
_UNMEASURED_SAMPLES = tuple(
    (name, profile_id)
    for name, profile_id in _NAME_SAMPLES
    if not yaml.safe_load((_PROFILE_DIR / f"{profile_id}.yaml").read_text(encoding="utf-8")).get(
        "vram_per_instance_mb"
    )
)


def _stub_smi(tmp_path: Path, *names: str, exit_code: int = 0) -> Path:
    """A fake ``nvidia-smi`` printing one device name per line (the CPU seam)."""
    stub = tmp_path / "nvidia-smi-stub"
    lines = "\n".join(f'echo "{name}"' for name in names)
    stub.write_text(f"#!/usr/bin/env bash\n{lines}\nexit {exit_code}\n", encoding="utf-8")
    stub.chmod(0o755)
    return stub


def _run(*args: str, smi: Path | None = None, env: dict[str, str] | None = None):
    environ = {"PATH": os.environ.get("PATH", ""), "HOME": os.environ.get("HOME", "")}
    if smi is not None:
        environ["CV_NVIDIA_SMI"] = str(smi)
    environ.update(env or {})
    return subprocess.run(
        ["bash", str(_SCRIPT), *args],
        cwd=str(_REPO_ROOT),
        env=environ,
        capture_output=True,
        text=True,
        timeout=60,
    )


def _knob(stdout: str) -> str | None:
    """The single ACTIVE (uncommented) ``CV_VRAM_PER_INSTANCE_MB=`` value, if any."""
    values = [
        line.split("=", 1)[1].strip()
        for line in stdout.splitlines()
        if line.startswith("CV_VRAM_PER_INSTANCE_MB=")
    ]
    assert len(values) <= 1, f"knob emitted more than once: {stdout}"
    return values[0] if values else None


def _profiles() -> list[Path]:
    files = sorted(_PROFILE_DIR.glob("*.yaml"))
    assert files, "profiles/ is empty — detection has nothing to select (arming check)"
    return files


# --------------------------------------------------------------------------- #
# profiles/ — the data layer
# --------------------------------------------------------------------------- #


def test_profiles_are_flat_and_declare_only_consumed_keys():
    """평평한 문법 + 소비자 있는 키만. ``id``는 파일명과 일치해야 한다."""
    for path in _profiles():
        document = yaml.safe_load(path.read_text(encoding="utf-8"))
        assert isinstance(document, dict), f"{path.name}: 최상위가 매핑이 아니다"
        unknown = set(document) - _ALLOWED_KEYS
        assert not unknown, f"{path.name}: 미지 키 {unknown}"
        assert _REQUIRED_KEYS <= set(document), f"{path.name}: 필수 키 누락"
        assert all(
            not isinstance(value, (dict, list)) for value in document.values()
        ), f"{path.name}: 중첩 값 — 셸 파서가 조용히 깨진다"
        assert document["id"] == path.stem, f"{path.name}: id != 파일명"


def test_every_measured_number_carries_its_evidence_path():
    """G-24 기계화: 정량값이 있으면 재현 경로(``*_source``)가 **반드시** 같이 있다.

    §2-4의 "실측 후 기입"은 값의 존재만으로 지켜지지 않는다 — 어디서 나온 값인지 없으면
    다음 사람이 검증할 수 없고, 그때 그 숫자는 실측과 구분되지 않는 추측이 된다.
    """
    for path in _profiles():
        document = yaml.safe_load(path.read_text(encoding="utf-8"))
        value = document.get("vram_per_instance_mb")
        source = document.get("vram_per_instance_source")
        if value in (None, ""):
            assert source in (None, ""), f"{path.name}: 값 없이 출처만 있다"
            continue
        assert isinstance(value, int) and value > 0, f"{path.name}: vram은 양의 정수 MiB"
        assert source and len(str(source)) > 20, f"{path.name}: 앵커 없는 정량값 (G-24)"


# --------------------------------------------------------------------------- #
# 선택 — 런타임 질의 결과가 결정한다
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(("gpu_name", "expected"), _NAME_SAMPLES)
def test_selection_follows_the_runtime_query(tmp_path, gpu_name: str, expected: str):
    """같은 호스트·같은 코드, **질의 출력만** 바꾸면 선택이 바뀐다.

    이것이 "적응형"의 전부다: 분기 입력은 런타임 탐지 결과이지 호스트명이 아니다.
    """
    result = _run(smi=_stub_smi(tmp_path, gpu_name))
    assert result.returncode == 0, result.stderr
    assert f"# profile      : {expected} (profiles/{expected}.yaml)" in result.stdout
    assert f"# detected GPU : {gpu_name}" in result.stdout


def test_selection_ignores_the_host_identity(tmp_path):
    """G-35 반대편: 호스트 정체성을 흔들어도 **같은 답**이 나온다.

    선택이 GPU로 갈린다는 위 단정만으로는 "호스트도 같이 보고 있지 않다"를 배제하지
    못한다. 여기서는 ``HOSTNAME``을 바꿔 가며 같은 stub을 물려 출력(타임스탬프 제외)이
    바이트 동일함을 본다.
    """
    smi = _stub_smi(tmp_path, _NAME_SAMPLES[0][0])
    outputs = []
    for fake_host in ("etri6000", "some-other-box", ""):
        result = _run(smi=smi, env={"HOSTNAME": fake_host})
        assert result.returncode == 0, result.stderr
        outputs.append(
            [line for line in result.stdout.splitlines() if not line.startswith("# generated")]
        )
    assert outputs[0] == outputs[1] == outputs[2]


def test_measured_profile_emits_the_knob_and_its_anchor(tmp_path):
    """실측 프로파일 -> 활성 knob + 출처 주석. 셸 파서와 pyyaml이 **같은 값**을 읽는다.

    두 파서가 갈리면 그 차집합이 구멍이다(G-56의 형태). 프로파일 문법이 평평해야 하는
    이유가 이것이고, 그 사실을 여기서 기계로 고정한다.
    """
    document = yaml.safe_load((_PROFILE_DIR / "rtx_pro_6000.yaml").read_text(encoding="utf-8"))
    result = _run(smi=_stub_smi(tmp_path, _NAME_SAMPLES[0][0]))

    assert result.returncode == 0, result.stderr
    assert _knob(result.stdout) == str(document["vram_per_instance_mb"])
    assert document["vram_per_instance_source"] in result.stdout


@pytest.mark.parametrize(("gpu_name", "expected"), _UNMEASURED_SAMPLES)
def test_unmeasured_profile_refuses_to_guess(tmp_path, gpu_name: str, expected: str):
    """**실측이 없는** 프로파일 전부: 선택은 되되 숫자를 지어내지 않는다.

    knob은 주석 처리되어 나가고(=NVML 2차 가드 OFF, 운영자 상한만으로 도는 문서화된
    기본 계약), 그 사실을 stderr가 **크게** 말한다. 조용한 추측 금지(CLAUDE §2-4).
    """
    assert _UNMEASURED_SAMPLES, "미실측 프로파일 후보가 0 — 이 단정이 공허하다"
    result = _run(smi=_stub_smi(tmp_path, gpu_name))
    assert f"# profile      : {expected}" in result.stdout

    assert result.returncode == 0, result.stderr
    assert _knob(result.stdout) is None
    assert "#CV_VRAM_PER_INSTANCE_MB=" in result.stdout
    assert "TBD" in result.stdout
    assert "NO measured vram_per_instance_mb" in result.stderr


def test_manual_selection_overrides_detection(tmp_path):
    """C-2 고도 = 자동 감지 + **매뉴얼 선택**. ``--profile``은 탐지를 아예 건너뛴다."""
    result = _run("--profile", "a100", smi=tmp_path / "does-not-exist")

    assert result.returncode == 0, result.stderr
    assert "# profile      : a100" in result.stdout
    assert "manual selection" in result.stdout


# --------------------------------------------------------------------------- #
# fail-closed — 모르면 거부한다 (조용한 오설정이 이 게이트의 적)
# --------------------------------------------------------------------------- #


def test_unknown_gpu_is_refused_with_instructions(tmp_path):
    """미지 GPU -> exit 3 + "프로파일을 이렇게 추가하라". 추측 예산을 내주지 않는다.

    이름은 **일부러 실재하지 않는 것**을 쓴다: 여기 실재 카드를 적으면 그 테스트는
    *"우리는 그 카드를 지원하지 않는다"* 를 고정하게 되고, 그 카드를 지원하는 날
    (= 프로파일 추가) 조용히 거짓이 된다. p5c17 이전 이 자리에는 ``NVIDIA GeForce RTX
    4080`` 이 있었고, 정확히 그 이유로 red 가 됐다.
    """
    result = _run(smi=_stub_smi(tmp_path, "NVIDIA Totally Fake 9999"))

    assert result.returncode == _EXIT_UNSUPPORTED
    assert result.stdout == ""
    assert "no profile matches" in result.stderr
    assert "MEASURE" in result.stderr


def test_heterogeneous_multi_gpu_is_refused(tmp_path):
    """모델이 섞인 멀티 GPU -> 하나를 골라 조용히 프로파일링하지 않고 거부."""
    result = _run(smi=_stub_smi(tmp_path, "NVIDIA A100-SXM4-40GB", "NVIDIA RTX PRO 6000 Blackwell"))

    assert result.returncode == _EXIT_UNSUPPORTED
    assert "MORE THAN ONE GPU model" in result.stderr


def test_identical_multi_gpu_is_fine(tmp_path):
    """반대편(G-35): 같은 카드 여러 장은 정상 — 위 거부가 "멀티 GPU 거부"가 아님을 고정."""
    result = _run(smi=_stub_smi(tmp_path, *(["NVIDIA A100-SXM4-40GB"] * 4)))

    assert result.returncode == 0, result.stderr
    assert "# profile      : a100" in result.stdout


def test_missing_or_failing_nvidia_smi_is_refused(tmp_path):
    """드라이버/도구 부재 -> exit 3. GPU 호스트 전제(NFR-DEPLOY-005)를 조용히 넘기지 않음."""
    absent = _run(smi=tmp_path / "no-such-binary")
    assert absent.returncode == _EXIT_UNSUPPORTED
    assert "not found" in absent.stderr

    broken = _run(smi=_stub_smi(tmp_path, exit_code=1))
    assert broken.returncode == _EXIT_UNSUPPORTED
    assert "failed" in broken.stderr


def test_number_without_evidence_anchor_never_reaches_a_deployment(tmp_path):
    """G-24 무장 실증: 앵커 없는 정량값을 **실제로 심으면** 스크립트가 거부한다.

    위 ``test_every_measured_number_carries_its_evidence_path``는 저장소의 현재 상태만
    본다 — 운영자가 자기 호스트에서 프로파일에 숫자만 적어 넣는 경로는 그 테스트가 절대
    보지 못한다. 그 경로를 여기서 막고, 막힌다는 것을 변이로 보인다.
    """
    profile_dir = tmp_path / "profiles"
    profile_dir.mkdir()
    (profile_dir / "fake.yaml").write_text(
        "id: fake\nmatch_name_pattern: FakeCard\nvram_per_instance_mb: 4096\n",
        encoding="utf-8",
    )
    result = _run(
        "--profile-dir",
        str(profile_dir),
        smi=_stub_smi(tmp_path, "NVIDIA FakeCard 1"),
    )

    assert result.returncode == _EXIT_UNSUPPORTED
    assert "vram_per_instance_source" in result.stderr
    assert "G-24" in result.stderr


def test_malformed_profile_is_refused(tmp_path):
    """``id``/``match_name_pattern`` 누락 -> 조용히 건너뛰지 않고 거부(손상 감지)."""
    profile_dir = tmp_path / "profiles"
    profile_dir.mkdir()
    (profile_dir / "broken.yaml").write_text("id: broken\n", encoding="utf-8")
    result = _run("--profile-dir", str(profile_dir), smi=_stub_smi(tmp_path, "anything"))

    assert result.returncode == _EXIT_UNSUPPORTED
    assert "malformed profile" in result.stderr


def test_ambiguous_profiles_are_refused(tmp_path):
    """둘 이상 매칭 -> 임의로 첫 번째를 고르지 않고 거부."""
    profile_dir = tmp_path / "profiles"
    profile_dir.mkdir()
    for name in ("one", "two"):
        (profile_dir / f"{name}.yaml").write_text(
            f"id: {name}\nmatch_name_pattern: Card\n", encoding="utf-8"
        )
    result = _run("--profile-dir", str(profile_dir), smi=_stub_smi(tmp_path, "Vendor Card 9"))

    assert result.returncode == _EXIT_UNSUPPORTED
    assert "ambiguous" in result.stderr
