"""이미지 프로방넌스 — **재빌드가 같은 것을 만드는가**(apt 핀) + **이 이미지는 어느
커밋인가**(revision 스탬프).

두 축은 같은 질문의 앞뒤다. 배송 경로가 **(C) 새 호스트에서 재빌드**로 확정됐으므로
(결정 2026-08-14 **D-6**), 핀되는 것은 *출력 다이제스트*가 아니라 **입력 집합**이다 —
베이스 digest + ``uv.lock`` + **apt 버전** + **소스 커밋**. 앞의 둘은 이미 핀돼 있었고,
이 파일은 나머지 둘을 기계로 고정한다.

**축 A — apt 버전 핀 (D-6의 구속력 있는 선행 조건, CLAUDE §2-7).**
``docker/runner/Dockerfile``이 **이름을 대는** 패키지는 전부 ``=<정확한 버전>``을 달아야
한다. 값 자체는 여기서 검사하지 않는다(값은 실측 산물이고 재핀될 것이다 — G-64) —
검사하는 것은 **핀이 붙어 있는가**이다.

*정직 표기(정량)*: 핀되는 것은 **직접 호명하는 8개**뿐이고 그 **transitive 262개는
비핀**이다(p5c15 이미지 실측: ROS 설치 매니페스트 252행 중 직접 5 → transitive 247,
키링 설치 18개 중 직접 3 → transitive 15). 전량 핀을 택하지 않은 근거는 Dockerfile의
``apt VERSION PINS`` 블록에 있다(우분투 아카이브는 대체된 버전을 인덱스에서 내리므로
270핀 빌드는 몇 주 안에 영구히 깨진다). 이 테스트는 **그 선택을 고정**하는 것이지
transitive까지 덮는다고 주장하지 않는다.

**축 B — revision 스탬프 (G-66).**
러너 이미지는 임시 ``docker build --build-arg``로 스탬프됐는데 **compose가 만든 제어
평면 이미지는 ``rev=[]``** 였다 — 즉 **문서화된 제품 경로가 임시 명령보다 프로방넌스가
낮았다**. ``docker compose build``는 ``--build-arg``/``--label``을 **계승하지 않으므로**
compose 파일이 ``build.args``로 명시해야 한다. 그리고 스탬프 없는 이미지는 **조용히
통과하면 안 된다** — ``scripts/check_plane_skew.sh``가 두 이미지 평면 모두에 대해
fail-closed임을 아래에서 **실행으로** 확인한다(정적 grep이 아니라 스텁 ``docker``를
PATH에 놓고 게이트를 실제로 돌린다).

**증명하지 않는 것(정직 표기)**: 실제 ``docker build``/``compose build``. 이 기기는 CPU고
러너 베이스는 15.5 GB다. 빌드가 실제로 도는지는 워크스테이션 몫이며, 특히 **핀한 버전이
오늘도 아카이브에 있는지는 여기서 알 수 없다**(리포트 참조).

Stdlib + pytest (+ pyyaml, 이미 의존). 신규 의존 0.
"""

from __future__ import annotations

import os
import re
import subprocess
from pathlib import Path

import pytest
import yaml

_REPO_ROOT = Path(__file__).resolve().parents[1]
_RUNNER_DOCKERFILE = _REPO_ROOT / "docker" / "runner" / "Dockerfile"
_ORCHESTRATOR_DOCKERFILE = _REPO_ROOT / "docker" / "orchestrator" / "Dockerfile"
_COMPOSE = _REPO_ROOT / "docker" / "compose.yaml"
_GATE = _REPO_ROOT / "scripts" / "check_plane_skew.sh"

#: 빌드 시점에 소스 커밋을 넣는 build arg. 이름은 세 곳(두 Dockerfile + compose)이
#: 공유하는 wire라 상수로 든다 — 한 곳만 바뀌면 아래 단정들이 red가 된다.
_REVISION_ARG = "CV_SOURCE_REVISION"
_REVISION_LABEL = "org.opencontainers.image.revision"

#: 게이트의 종료 계약 (스크립트 헤더): 2 = 사용법 오류 · 3 = 스큐/미스탬프(fail-closed).
_EXIT_USAGE = 2
_EXIT_SKEW = 3


# --------------------------------------------------------------------------- #
# 축 A — apt 버전 핀
# --------------------------------------------------------------------------- #


def _shell_body(dockerfile: Path) -> str:
    """Dockerfile을 **셸이 보는 형태**로: 주석 줄 제거 + 줄 연속(``\\``) 결합.

    줄 단위로 grep하면 여러 줄에 걸친 ``apt-get install``의 뒷줄들을 놓친다 — 실제
    install 블록이 정확히 그 모양이다.
    """
    lines = [
        line
        for line in dockerfile.read_text(encoding="utf-8").splitlines()
        if not line.lstrip().startswith("#")
    ]
    return " ".join("\n".join(lines).replace("\\\n", " ").split())


def unpinned_apt_packages(text: str) -> list[str]:
    """``apt-get install``이 **버전 없이** 호명하는 패키지 전부 (없으면 빈 리스트).

    이 스캔의 단일 출처. 명령의 피연산자만 본다: 플래그(``-``로 시작)는 건너뛰고,
    셸 구분자(``;`` ``||`` ``&&`` ``|``)에서 멈춘다.
    """
    unpinned: list[str] = []
    for match in re.finditer(r"apt-get\s+install\b", text):
        tail = text[match.end() :]
        operands = re.split(r"\|\||&&|[;|]", tail, maxsplit=1)[0]
        for token in operands.split():
            if token.startswith("-"):
                continue
            if "=" not in token:
                unpinned.append(token)
    return unpinned


def test_every_apt_package_the_runner_image_names_is_version_pinned():
    """축 A 본 게이트: 러너 이미지의 apt 입력이 핀돼 있다 (D-6 선행, `CLAUDE §2-7`)."""
    assert unpinned_apt_packages(_shell_body(_RUNNER_DOCKERFILE)) == []


def test_apt_pin_scan_is_not_vacuous():
    """무장 실증 + 오탐 대조 — 두 방향이 쌍으로 있어야 증거다 (G-35).

    발화만 보면 "전부 금지"하는 스캔이 통과하고, 오탐만 보면 아무것도 안 잡는 스캔이
    통과한다.
    """
    armed = "RUN apt-get update; apt-get install -y --no-install-recommends curl gnupg;"
    assert unpinned_apt_packages(armed) == ["curl", "gnupg"]
    # 한 개만 빠져도 잡힌다 (실제 회귀는 이 모양으로 들어온다)
    half = "apt-get install -y --no-install-recommends ca-certificates=20240203 curl;"
    assert unpinned_apt_packages(half) == ["curl"]
    # 오탐 대조: 핀된 형태 · 플래그 · install이 아닌 서브커맨드
    assert unpinned_apt_packages("apt-get install -y --no-install-recommends foo=1.2-3;") == []
    assert unpinned_apt_packages("apt-get update; apt-get clean;") == []
    # 셸 구분자에서 멈춘다: || 뒤의 에러 메시지 단어를 패키지로 오독하지 않는다
    assert unpinned_apt_packages('apt-get install -y foo=1 || { echo "re-pin bar baz"; };') == []


def test_recommends_stay_off_on_every_apt_install():
    """G-11: recommends는 **비핀 transitive**를 끌어온다. 핀과 세트로 유지한다."""
    body = _shell_body(_RUNNER_DOCKERFILE)
    installs = re.findall(r"apt-get\s+install\b[^;|&]*", body)
    assert installs, "apt-get install을 하나도 못 찾았다 — 스캔이 공허하다"
    for install in installs:
        assert "--no-install-recommends" in install, install


def test_the_repin_instrument_survives():
    """전량 핀을 포기한 대가로 **재핀 수단**은 반드시 살아 있어야 한다.

    이 레이어는 설치한 전 패키지를 빌드 로그에 찍는다(``comm -13``). 다음 사람이
    핀을 갱신하는 유일한 근거이고, D-6의 "비핀 표면을 숨기지 않는다"의 실체다.
    지우면 이 파일의 정량 표기가 검증 불가능한 주장이 된다(G-24).
    """
    body = _shell_body(_RUNNER_DOCKERFILE)
    assert "dpkg-query" in body and "comm -13" in body
    assert "rosbag2 apt layer manifest" in body


# --------------------------------------------------------------------------- #
# 축 B — revision 스탬프 (정적 배선)
# --------------------------------------------------------------------------- #


def _compose_document() -> dict:
    return yaml.safe_load(_COMPOSE.read_text(encoding="utf-8"))


def test_compose_passes_the_revision_into_the_build():
    """G-66 수리의 핵심 한 줄: compose의 ``build.args``에 revision이 배선돼 있다.

    이게 없으면 ``docker compose build``는 build arg를 **전달하지 않고**, 제품 경로가
    임시 ``docker build``보다 프로방넌스를 잃는다(2026-08-14 실측: 제어 평면 이미지
    ``rev=[]``).
    """
    build = _compose_document()["services"]["orchestrator"]["build"]
    assert _REVISION_ARG in build.get("args", {}), "compose build.args에 revision이 없다"


def test_the_stamped_value_comes_from_the_environment_not_a_literal():
    """값의 출처 = **빌드 시점의 실제 git revision**. 하드코딩 금지.

    compose는 명령을 실행할 수 없으므로 값은 환경에서 온다. 여기에 sha를 적어 넣으면
    그 순간부터 모든 빌드가 **같은 거짓 커밋**을 주장한다 — 스탬프가 없는 것보다 나쁘다
    (게이트가 통과해 버린다).
    """
    value = str(_compose_document()["services"]["orchestrator"]["build"]["args"][_REVISION_ARG])
    assert value.startswith(f"${{{_REVISION_ARG}"), value
    assert not re.search(r"\b[0-9a-f]{7,40}\b", value), f"리터럴 sha가 박혀 있다: {value}"


@pytest.mark.parametrize("dockerfile", (_RUNNER_DOCKERFILE, _ORCHESTRATOR_DOCKERFILE))
def test_both_image_producing_paths_stamp_and_refuse_to_build_unstamped(dockerfile: Path):
    """G-66 ①: 이미지를 만드는 **모든 경로**가 같은 스탬프를 남긴다 — 한 경로만
    검증하면 나머지가 조용히 벗어난다(그게 정확히 이번 결함이었다).

    그리고 스탬프 부재는 **빌드 시점에** loud 실패다. 게이트가 어차피 fail-closed지만,
    빌드에서 죽는 편이 더 크고 싸다.
    """
    text = dockerfile.read_text(encoding="utf-8")
    assert re.search(rf"^ARG\s+{_REVISION_ARG}\s*$", text, re.MULTILINE), dockerfile.name
    assert re.search(
        rf'test -n "\$\{{{_REVISION_ARG}\}}"', text
    ), f"{dockerfile.name}: 스탬프가 비어 있어도 빌드가 성공한다"
    assert re.search(
        rf"^LABEL\s+{re.escape(_REVISION_LABEL)}=\${_REVISION_ARG}\s*$", text, re.MULTILINE
    ), dockerfile.name


# --------------------------------------------------------------------------- #
# 축 B — 게이트가 실제로 fail-closed인가 (스텁 docker로 **실행**)
# --------------------------------------------------------------------------- #


def _git(repo: Path, *args: str) -> str:
    completed = subprocess.run(
        ["git", "-C", str(repo), *args],
        capture_output=True,
        text=True,
        check=True,
        env={**os.environ, "GIT_CONFIG_GLOBAL": "/dev/null", "GIT_CONFIG_SYSTEM": "/dev/null"},
    )
    return completed.stdout.strip()


@pytest.fixture
def plane_repo(tmp_path: Path) -> tuple[Path, str, str]:
    """두 커밋짜리 git 저장소 — (경로, 오래된 커밋, 릴리즈 커밋)."""
    repo = tmp_path / "src"
    repo.mkdir()
    _git(repo, "init", "-q", "-b", "main")
    _git(repo, "config", "user.email", "gate@test.invalid")
    _git(repo, "config", "user.name", "gate test")
    revisions = []
    for index in (0, 1):
        (repo / "file.txt").write_text(f"{index}\n", encoding="utf-8")
        _git(repo, "add", "file.txt")
        _git(repo, "commit", "-q", "-m", f"c{index}")
        revisions.append(_git(repo, "rev-parse", "HEAD"))
    return repo, revisions[0], revisions[1]


def _stub_docker(tmp_path: Path, labels: dict[str, str | None]) -> Path:
    """PATH에 놓을 가짜 ``docker``. ``image inspect <ref> --format …``만 답한다.

    값이 ``None`` = 그 ref를 모른다(→ inspect 실패). 그 외는 revision 라벨 문자열이며,
    ``<no value>``는 라벨이 **없는** 이미지에서 docker가 실제로 내는 출력이다.
    """
    bindir = tmp_path / "bin"
    bindir.mkdir(exist_ok=True)
    cases = "\n".join(
        f'  {ref!r}) {"exit 1" if value is None else f"echo {value!r}"} ;;'.replace("'", '"')
        for ref, value in labels.items()
    )
    (bindir / "docker").write_text(
        "#!/usr/bin/env bash\n"
        '[ "$1" = "image" ] && [ "$2" = "inspect" ] || exit 1\n'
        'case "$3" in\n'
        f"{cases}\n"
        "  *) exit 1 ;;\n"
        "esac\n",
        encoding="utf-8",
    )
    (bindir / "docker").chmod(0o755)
    return bindir


def _run_gate(*args: str, bindir: Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["bash", str(_GATE), *args],
        capture_output=True,
        text=True,
        timeout=60,
        env={
            "PATH": f"{bindir}{os.pathsep}{os.environ.get('PATH', '')}",
            "HOME": str(bindir.parent),
        },
    )


def test_gate_passes_only_when_both_image_planes_are_stamped_at_the_release_ref(
    plane_repo, tmp_path
):
    """착지(0) — 반대편이 없으면 아래 탐지들이 "항상 red"와 구분되지 않는다 (G-35)."""
    repo, _, release = plane_repo
    bindir = _stub_docker(tmp_path, {"runner:ok": release, "orch:ok": release})

    result = _run_gate(
        "--src", str(repo), "--tag", release,
        "--image", "runner:ok", "--orchestrator-image", "orch:ok",
        bindir=bindir,
    )  # fmt: skip

    assert result.returncode == 0, result.stderr
    assert "IN SYNC" in result.stdout
    assert "control image : orch:ok" in result.stdout


def test_unstamped_control_plane_image_is_fail_closed(plane_repo, tmp_path):
    """★ G-66의 실제 상태: compose가 만든 이미지에 라벨이 없다 → **exit 3**.

    조용한 통과가 이 게이트의 유일한 실패 모드다(플랫폼이 옛 코드를 도는 줄 모른 채
    라이브 leg를 태운다). 라벨 부재 시 docker가 내는 문자열 그대로를 스텁이 낸다.
    """
    repo, _, release = plane_repo
    bindir = _stub_docker(tmp_path, {"runner:ok": release, "orch:bare": "<no value>"})

    result = _run_gate(
        "--src", str(repo), "--tag", release,
        "--image", "runner:ok", "--orchestrator-image", "orch:bare",
        bindir=bindir,
    )  # fmt: skip

    assert result.returncode == _EXIT_SKEW
    assert "CONTROL-PLANE IMAGE UNSTAMPED" in result.stderr
    assert _REVISION_ARG in result.stderr  # 재빌드 레시피가 함께 나온다


def test_stale_control_plane_image_is_detected(plane_repo, tmp_path):
    """스탬프가 있어도 **옛 커밋**이면 스큐다 — 부재와 별개의 실패 모드."""
    repo, older, release = plane_repo
    bindir = _stub_docker(tmp_path, {"runner:ok": release, "orch:stale": older})

    result = _run_gate(
        "--src", str(repo), "--tag", release,
        "--image", "runner:ok", "--orchestrator-image", "orch:stale",
        bindir=bindir,
    )  # fmt: skip

    assert result.returncode == _EXIT_SKEW
    assert "PLANE SKEW DETECTED (②')" in result.stderr
    assert "1 commit(s) behind" in result.stderr


def test_the_new_plane_is_required_not_optional(plane_repo, tmp_path):
    """평면을 **안 보는** 게이트는 거짓 초록을 낸다(G-43) — 그래서 인자는 필수다.

    옵션으로 두면 기존 호출이 그대로 통과하면서 새 평면만 조용히 빠진다. 그 상태와
    "게이트가 그 평면을 본다"는 구분되지 않는다.
    """
    repo, _, release = plane_repo
    bindir = _stub_docker(tmp_path, {"runner:ok": release})

    result = _run_gate("--src", str(repo), "--tag", release, "--image", "runner:ok", bindir=bindir)

    assert result.returncode == _EXIT_USAGE
    assert "--orchestrator-image" in result.stderr


def test_runner_image_plane_still_fails_closed_after_the_refactor(plane_repo, tmp_path):
    """회귀 방지: 두 평면이 같은 헬퍼를 쓰게 리팩터했으므로 ③도 다시 확인한다.

    (한쪽만 고치고 다른 쪽이 조용히 약해지는 것이 G-25 계열의 전형이다.)
    """
    repo, older, release = plane_repo
    bindir = _stub_docker(tmp_path, {"runner:bare": "", "orch:ok": release})

    unstamped = _run_gate(
        "--src", str(repo), "--tag", release,
        "--image", "runner:bare", "--orchestrator-image", "orch:ok",
        bindir=bindir,
    )  # fmt: skip
    assert unstamped.returncode == _EXIT_SKEW
    assert "RUNNER-IMAGE PLANE UNSTAMPED" in unstamped.stderr

    bindir = _stub_docker(tmp_path, {"runner:stale": older, "orch:ok": release})
    stale = _run_gate(
        "--src", str(repo), "--tag", release,
        "--image", "runner:stale", "--orchestrator-image", "orch:ok",
        bindir=bindir,
    )  # fmt: skip
    assert stale.returncode == _EXIT_SKEW
    assert "PLANE SKEW DETECTED (③)" in stale.stderr


def test_unknown_image_ref_is_fail_closed(plane_repo, tmp_path):
    """오타·미빌드 이미지는 **조용히 통과하지 않는다**(G-26 fail-closed)."""
    repo, _, release = plane_repo
    bindir = _stub_docker(tmp_path, {"runner:ok": release})

    result = _run_gate(
        "--src", str(repo), "--tag", release,
        "--image", "runner:ok", "--orchestrator-image", "orch:does-not-exist",
        bindir=bindir,
    )  # fmt: skip

    assert result.returncode == _EXIT_SKEW
    assert "cannot inspect control-plane image" in result.stderr
