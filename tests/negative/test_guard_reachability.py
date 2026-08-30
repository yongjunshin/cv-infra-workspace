"""deny-list 가드는 **이름**이 아니라 **도달성**으로 판정한다 (G-106 ④ · p8c2 T6).

측정된 실패(p8c1 QA 감사 §4-1 → p8c2 T6 전수 재측정): 금지된 호출을 **모듈-로컬 헬퍼
이름 뒤로 한 칸** 옮기면, 스코프의 *직접* 호출 이름만 세는 가드는 아무것도 보지 못한다.
프로그램은 유효하고 회귀는 살아 있는데 전 스위트가 **1683/1683 초록**이다 — 같은 회귀를
인라인으로 쓰면 즉시 red 다. 이 클래스로 뚫린 핀이 저장소에 **9개** 있었다.

이 파일은 그중 **직접-이름 쌍둥이가 구현팀 소유 파일에 사는 다섯**을 QA 쪽에서 봉합한다:
소유자 파일을 고치지 않고(결정 2026-06-25-tests-ownership) **빠진 이빨만** 여기에 단다.
쌍둥이는 각 행의 ``hardens`` 가 이름으로 가리킨다. NEG-6 자신의 파일 안에서 뚫린 넷은
``test_batch_no_residue.py`` 에서 직접 봉합됐다(같은 primitive).

**이름을 재타이핑하지 않는다**(G-25): 금지 목록은 소유자 가드의 소스에서 유도한다 —
목록이 자라면 이 도달성 판정도 같이 자란다.

각 행은 **실측된 변이**를 자기 대조군으로 들고 있다(G-35/G-59): 그 변이를 생산 소스에
적용하면 행이 발화해야 하고, 현행 소스에서는 침묵해야 한다.
"""

from __future__ import annotations

import ast
from dataclasses import dataclass
from pathlib import Path

import pytest

from cv_infra.runner import batch as m2_batch
from cv_infra.runner import main as m2_main
from cv_infra.runner import sim_runtime
from tests.negative.reachability import local_callables, reachable_calls, reaching

_OWNER_GUARD = Path(__file__).resolve().parents[1] / "test_runner_batch.py"

_MODULES = {
    "batch": Path(m2_batch.__file__),
    "main": Path(m2_main.__file__),
    "sim_runtime": Path(sim_runtime.__file__),
}


# --------------------------------------------------------------------------- #
# 금지 목록은 소유자 가드에서 유도한다 (재타이핑 0 — 목록이 자라면 여기도 자란다)
# --------------------------------------------------------------------------- #
def _parametrized_strings(path: Path, test_name: str, argname: str) -> tuple[str, ...]:
    """``@pytest.mark.parametrize("<argname>", [ ... ])`` 의 문자열 리터럴들."""
    tree = ast.parse(path.read_text(encoding="utf-8"))
    target = next(
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.FunctionDef) and node.name == test_name
    )
    for decorator in target.decorator_list:
        if not isinstance(decorator, ast.Call) or len(decorator.args) != 2:
            continue
        first, second = decorator.args
        if not (isinstance(first, ast.Constant) and first.value == argname):
            continue
        if isinstance(second, ast.List | ast.Tuple):
            return tuple(
                element.value
                for element in second.elts
                if isinstance(element, ast.Constant) and isinstance(element.value, str)
            )
    raise AssertionError(f"{path.name}::{test_name} no longer parametrizes {argname!r}")


def _module_strings(path: Path, name: str) -> tuple[str, ...]:
    """모듈 최상위 ``NAME = (...)`` 의 문자열 리터럴들."""
    tree = ast.parse(path.read_text(encoding="utf-8"))
    for node in tree.body:
        if isinstance(node, ast.Assign) and ast.unparse(node.targets[0]) == name:
            return tuple(
                element.value
                for element in node.value.elts
                if isinstance(element, ast.Constant) and isinstance(element.value, str)
            )
    raise AssertionError(f"{path.name} no longer defines {name}")


#: 반복 경계가 **프림을 저작하지 않는다** 는 금지 철자 (소유자: test_runner_batch.py).
_PRIM_AUTHORING = _parametrized_strings(
    _OWNER_GUARD, "test_the_iteration_boundary_never_authors_a_prim", "forbidden"
)
#: 표본 루프에서 **sim 시간을 전진시키는** 철자 (같은 소유자).
_SIM_ADVANCING = _module_strings(_OWNER_GUARD, "_SIM_ADVANCING_CALLS")


def test_the_borrowed_deny_lists_are_not_empty():
    """비공허 대조: 목록을 소유자 소스에서 유도하는 배관이 실제로 무언가를 길어 온다."""
    assert "RemovePrim" in _PRIM_AUTHORING and len(_PRIM_AUTHORING) >= 9
    assert "step_and_spin" in _SIM_ADVANCING and len(_SIM_ADVANCING) >= 5


# --------------------------------------------------------------------------- #
# 스코프 해석
# --------------------------------------------------------------------------- #
def _scope(source: str, spec: str) -> tuple[ast.AST, dict[tuple[str, ...], ast.AST]]:
    """``"batch:run"`` / ``"batch:run:loop"`` / ``"sim_runtime:SimRuntime.restage"``."""
    tree = ast.parse(source)
    _, _, target = spec.partition(":")
    if "." in target:
        class_name, method_name = target.split(".")
        klass = next(
            node
            for node in ast.walk(tree)
            if isinstance(node, ast.ClassDef) and node.name == class_name
        )
        method = next(
            node
            for node in klass.body
            if isinstance(node, ast.FunctionDef) and node.name == method_name
        )
        return method, local_callables(tree, inside=method, method_of=klass)
    name, _, inner = target.partition(":")
    function = next(
        node for node in tree.body if isinstance(node, ast.FunctionDef) and node.name == name
    )
    callables = local_callables(tree, inside=function)
    if inner == "loop":
        loop = next(
            node
            for node in ast.walk(function)
            if isinstance(node, ast.For) and ast.unparse(node.iter) == "enumerate(specs)"
        )
        return loop, callables
    return function, callables


# --------------------------------------------------------------------------- #
# 행 — (스코프, 금지 철자, 실측된 헬퍼-숨김 변이, 이 행이 보강하는 쌍둥이)
# --------------------------------------------------------------------------- #
@dataclass(frozen=True)
class _Row:
    label: str
    module: str
    scope: str
    forbidden: tuple[str, ...]
    hardens: str
    edits: tuple[tuple[str, str], ...]  # (verbatim 앵커, 대체 텍스트)


_ROWS = (
    _Row(
        label="반복 경계(restage)가 헬퍼 뒤에서 프림을 저작한다",
        module="sim_runtime",
        scope="sim_runtime:SimRuntime.restage",
        forbidden=_PRIM_AUTHORING,
        hardens="test_runner_batch.py::test_the_iteration_boundary_never_authors_a_prim",
        edits=(
            (
                "\nclass SimRuntime:",
                "\ndef _purge_stale(path: str) -> None:  # pragma: no cover - GPU path\n"
                "    RemovePrim(path)\n"
                "\n"
                "\nclass SimRuntime:",
            ),
            (
                "        if obstacle is not None:\n"
                "            self.move_debug_obstacle(obstacle)\n",
                "        _purge_stale('/World/stale')\n"
                "        if obstacle is not None:\n"
                "            self.move_debug_obstacle(obstacle)\n",
            ),
        ),
    ),
    _Row(
        label="반복 경계(apply_obstacle_set)가 self.<메서드> 뒤에서 프림을 저작한다",
        module="sim_runtime",
        scope="sim_runtime:SimRuntime.apply_obstacle_set",
        forbidden=_PRIM_AUTHORING,
        hardens="test_runner_batch.py::test_the_iteration_boundary_never_authors_a_prim",
        edits=(
            (
                "    def apply_obstacle_set(self, entries: list[dict]) -> None:",
                "    def _purge_stale(self) -> None:  # pragma: no cover - GPU path\n"
                "        RemovePrim('/World/stale')\n"
                "\n"
                "    def apply_obstacle_set(self, entries: list[dict]) -> None:",
            ),
            (
                "        placed, parked = obstacle_placement_plan(entries, pool)\n",
                "        self._purge_stale()\n"
                "        placed, parked = obstacle_placement_plan(entries, pool)\n",
            ),
        ),
    ),
    _Row(
        label="batch.run 이 헬퍼 뒤에서 벤더 객체를 close 한다 (G-62)",
        module="batch",
        scope="batch:run",
        forbidden=("close",),
        hardens="test_runner_batch.py::test_batch_run_closes_no_vendor_object_on_its_terminal_path",
        edits=(
            (
                "\ndef run(env: dict | None = None) -> int:",
                "\ndef _shutdown(sim: object) -> None:\n"
                "    sim.simulation_app.close()\n"
                "\n"
                "\ndef run(env: dict | None = None) -> int:",
            ),
            ("        summary.finish()\n", "        _shutdown(sim)\n        summary.finish()\n"),
        ),
    ),
    _Row(
        label="main.run 이 헬퍼 뒤에서 벤더 객체를 close 한다 (G-62)",
        module="main",
        scope="main:run",
        forbidden=("close",),
        hardens="test_runner_exit_contract.py::test_run_closes_no_vendor_object_on_its_terminal_path",
        edits=(
            (
                "\ndef run(env: dict | None = None) -> int:",
                "\ndef _shutdown(sim: object) -> None:\n"
                "    sim.simulation_app.close()\n"
                "\n"
                "\ndef run(env: dict | None = None) -> int:",
            ),
            ("\n    finally:\n", "\n        _shutdown(sim)\n    finally:\n"),
        ),
    ),
    _Row(
        label="표본 루프가 헬퍼 뒤에서 풀을 다시 spawn 한다",
        module="batch",
        scope="batch:run:loop",
        forbidden=("spawn_obstacle_pool",),
        hardens=(
            "test_runner_batch.py::"
            "test_the_pool_is_spawned_once_at_boot_and_never_inside_the_sample_loop"
        ),
        edits=(
            (
                "\ndef run(env: dict | None = None) -> int:",
                "\ndef _respawn_pool(sim: object, plan: object) -> None:\n"
                "    sim.spawn_obstacle_pool(plan)\n"
                "\n"
                "\ndef run(env: dict | None = None) -> int:",
            ),
            (
                "            out_dir = iteration_dir(out_root, index)\n",
                "            out_dir = iteration_dir(out_root, index)\n"
                "            _respawn_pool(sim, staging.pool_plan)\n",
            ),
        ),
    ),
)


def _mutated(row: _Row) -> str:
    source = _MODULES[row.module].read_text(encoding="utf-8")
    for anchor, replacement in row.edits:
        assert source.count(anchor) == 1, (
            f"{row.label}: 변이 앵커가 소스에 verbatim 으로 없다 — 이 대조군은 "
            "재조준될 때까지 무장 해제 상태다"
        )
        source = source.replace(anchor, replacement)
    return source


@pytest.mark.parametrize("row", _ROWS, ids=lambda row: row.scope)
def test_no_forbidden_call_is_reachable_from_the_pinned_scope(row: _Row):
    """현행 소스: 금지 철자는 스코프에서 **도달 불가**여야 한다."""
    scope, callables = _scope(_MODULES[row.module].read_text(encoding="utf-8"), row.scope)
    hits = reaching(scope, callables, *row.forbidden)
    assert hits == [], f"{row.label}: {[str(hit) for hit in hits]} (보강 대상 {row.hardens})"


@pytest.mark.parametrize("row", _ROWS, ids=lambda row: row.scope)
def test_the_reachability_pin_fires_when_the_call_hides_behind_a_helper(row: _Row):
    """대조군: 실측된 헬퍼-숨김 변이에서 같은 행이 발화한다.

    이 변이들은 **봉합 전 전 스위트 1683/1683 초록**이었다(p8c2 T6 실측). 같은 회귀를
    인라인으로 쓰면 소유자의 직접-이름 가드가 잡는다 — 헬퍼 한 칸이 그 red 를 지웠다.
    """
    scope, callables = _scope(_mutated(row), row.scope)
    hits = reaching(scope, callables, *row.forbidden)
    assert hits, f"{row.label}: 도달성 판정이 헬퍼 한 칸을 못 본다"
    assert any(hit.via for hit in hits), f"{row.label}: 헬퍼를 거치지 않고 잡혔다 — 변이가 틀렸다"


# --------------------------------------------------------------------------- #
# 구간 판정 — record 교체와 미션 사이에서 sim 을 전진시키는 것이 **도달 가능**한가
# --------------------------------------------------------------------------- #
def _loop_band(source: str) -> tuple[list[ast.stmt], list[ast.stmt]]:
    """표본 루프 본문을 (교체 이전, 교체와 미션 사이)로 가른다."""
    loop, _ = _scope(source, "batch:run:loop")
    swap = next(
        position
        for position, statement in enumerate(loop.body)
        for node in ast.walk(statement)
        if isinstance(node, ast.Assign)
        and ast.unparse(node.targets[0]) == "sampler.record"
        and ast.unparse(node.value) == "TelemetryRecord()"
    )
    mission = next(
        position
        for position, statement in enumerate(loop.body)
        for node in ast.walk(statement)
        if isinstance(node, ast.Call) and ast.unparse(node.func) == "adapter.drive_mission"
    )
    return loop.body[:swap], loop.body[swap + 1 : mission]


def _advancing(statements: list[ast.stmt], callables) -> list[str]:
    wanted = [tuple(name.split(".")) for name in _SIM_ADVANCING]
    hits: list[str] = []
    for statement in statements:
        for hit in reachable_calls(statement, callables):
            if any(hit.chain[-len(w) :] == w for w in wanted):
                hits.append(str(hit))
    return hits


def test_nothing_reachable_advances_the_sim_between_the_record_swap_and_the_mission():
    """소유자 가드(``_SIM_ADVANCING_CALLS``)의 도달성 판. 이름 목록은 **거기서** 온다.

    이름 목록만 보는 판은 p8c1 에 이미 한 번 뚫렸다 — 정착 펌프가 ``_settle_world`` 로
    추출되자 호출부가 목록의 어떤 이름도 철자하지 않게 됐고, T1 이 그 이름을 손으로
    추가해 닫았다. 도달성으로 판정하면 **다음 추출은 손으로 추가하기 전에** 잡힌다.
    """
    source = _MODULES["batch"].read_text(encoding="utf-8")
    _, callables = _scope(source, "batch:run:loop")
    before, between = _loop_band(source)
    # 비공허(G-07): 어휘가 이 루프가 세계를 전진시키는 방식을 여전히 이름한다.
    assert _advancing(before, callables), (
        f"교체 이전 구간에서 sim 전진 호출이 하나도 안 보인다 — {_SIM_ADVANCING} 가 "
        "이 루프를 더 이상 설명하지 못한다(가드가 공허해졌다)"
    )
    assert _advancing(between, callables) == [], (
        f"{_advancing(between, callables)} 가 record 교체와 미션 사이에서 sim 을 전진시킨다 "
        "— 경계의 contact-LOST 보고가 이 표본에 청구된다"
    )


def test_the_band_pin_fires_when_the_pump_hides_behind_a_helper():
    """대조군: ``_breathe(adapter)`` 한 줄(봉합 전 1683/1683 초록)에서 발화한다."""
    source = _MODULES["batch"].read_text(encoding="utf-8")
    helper_anchor = "\ndef run(env: dict | None = None) -> int:"
    swap_anchor = "            sampler.record = TelemetryRecord()\n"
    assert source.count(helper_anchor) == 1 and source.count(swap_anchor) == 1
    mutated = source.replace(
        helper_anchor,
        "\ndef _breathe(adapter: object) -> None:\n    adapter.step_and_spin()\n"
        "\n" + helper_anchor,
    ).replace(swap_anchor, swap_anchor + "            _breathe(adapter)\n")
    _, callables = _scope(mutated, "batch:run:loop")
    _, between = _loop_band(mutated)
    hits = _advancing(between, callables)
    assert hits and "via" in hits[0], f"헬퍼 뒤의 정착 펌프를 못 봤다: {hits}"
