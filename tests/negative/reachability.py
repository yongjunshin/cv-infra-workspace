"""Deny-list 가드를 **이름**이 아니라 **도달성**으로 판정하는 최소 콜그래프 (G-106 ④).

측정된 실패 (p8c1 QA 감사 §4-1, p8c2 T6 전수 재측정):
``ast.walk(scope)`` 로 **직접 호출 이름만** 세는 가드는, 금지된 호출을 *모듈-로컬
헬퍼 이름 뒤로 한 칸 옮기면* 아무것도 보지 못한다. 프로그램은 그대로 유효하고
회귀도 그대로 살아 있는데 전 스위트가 초록이다 — p8c2 T6 이 이 형태로 **9개** 핀을
뚫었다(전부 1683/1683 GREEN, 인라인으로 쓰면 즉시 RED).

왜 이제 더 위험한가: p8c1 이 채택한 복잡도 래칫(C901 <= 14)은 **헬퍼 추출을 보상**한다.
"핀이 보는 자리 밖으로의 이동"은 앞으로 늘어나고, 그때마다 이름을 하나씩 가드 목록에
추가하는 것(T1 이 ``_settle_world`` 에 대해 한 일)은 **이미 뚫린 다음에야** 가능하다.

그래서 이 모듈은 스코프에서 **도달 가능한** 호출을 돌려준다: 스코프가 부르는
모듈-로컬 함수(그리고 메서드 스코프의 ``self.<method>``, 스코프를 감싸는 함수 안의
중첩 def)를 ``MAX_HOPS`` 단계까지 펼친다.

교차 모듈도 따라간다(``imported_callables``): 헬퍼를 옆 모듈에 두고
``from cv_infra.x import y`` 로 끌어와 부르는 판도 **측정된 구멍**이었다(p8c2 T6:
``recording.reopen_products`` 로 숨기면 1701/1701 초록). 확장 전 오발화를 먼저 쟀다 —
이 저장소의 여섯 스코프 × 전 금지 철자에서 **0건**.

**증명하지 않는 것(정직성)**: 교차 모듈은 **한 칸**이다 — 옆 모듈 헬퍼가 *또 다른*
함수 뒤로 숨기면(2홉) 따라가지 않는다. 그 이상은 이름을 그 모듈의 사전으로 풀어야
하는데, 우리 사전으로 풀면 동명이인에서 오발화한다(실측 1건: ``ros_bridge`` 안의
``run`` 이 운반체의 ``run`` 으로 풀렸다). 동적 디스패치(``getattr``·콜백 테이블)는
어느 정적 도달성도 따라가지 못한다.
"""

from __future__ import annotations

import ast
from dataclasses import dataclass
from pathlib import Path

#: 펼침 깊이. 래칫이 실제로 만드는 모양은 1홉(호출부 -> 헬퍼)이고, 2홉(헬퍼 -> 헬퍼)은
#: p8c2 T6 이 손으로 만들어 확인했다. 3 은 그 위의 여유다 — 무한 재귀는 ``seen`` 이 막는다.
MAX_HOPS = 3


@dataclass(frozen=True)
class Target:
    """펼칠 대상. ``follow=False`` = 몸통만 보고 **그 안에서 더 따라가지 않는다**.

    다른 모듈에서 끌어온 함수가 그렇다: 그 몸통의 이름들은 *그 모듈의* 사전으로 풀려야
    하는데 우리 사전으로 풀면 동명이인이 섞인다(실측: ``reexec_for_bridge_lib`` 안의
    ``run`` 이 운반체의 ``run`` 으로 풀려 오발화). 한 칸만 펼치면 그 문제가 사라지고,
    측정된 교차-모듈 은폐(헬퍼 몸통에 금지 호출이 직접 있는 판)는 그대로 잡힌다.
    """

    node: ast.AST
    follow: bool


@dataclass(frozen=True)
class Reached:
    """도달한 호출 하나. ``via`` 가 비면 스코프에 **인라인으로** 적혀 있다는 뜻."""

    chain: tuple[str, ...]  # 피호출자의 속성 사슬 — ("sim", "pre_reset", "append")
    via: tuple[str, ...]  # 거쳐 온 헬퍼 이름들 (빈 튜플 = 직접)
    site_lineno: int  # **스코프 안** 최외곽 호출의 줄번호 (구간 판정용)

    def __str__(self) -> str:
        where = " via " + " > ".join(self.via) if self.via else ""
        return f"{'.'.join(self.chain)} @line {self.site_lineno}{where}"


def call_chain(func: ast.expr) -> tuple[str, ...]:
    """``a.b.c()`` -> ``("a", "b", "c")``. 루트가 이름이 아니면 그 식을 그대로 쓴다."""
    parts: list[str] = []
    node: ast.expr = func
    while isinstance(node, ast.Attribute):
        parts.append(node.attr)
        node = node.value
    parts.append(node.id if isinstance(node, ast.Name) else ast.unparse(node))
    return tuple(reversed(parts))


def local_callables(
    module: ast.Module,
    *,
    inside: ast.AST | None = None,
    method_of: ast.ClassDef | None = None,
) -> dict[tuple[str, ...], Target]:
    """이 스코프가 **이름으로 부를 수 있는** 같은-모듈 함수들.

    * 모듈 최상위 ``def`` -> ``("name",)``
    * ``method_of`` 의 메서드 -> ``("self", "name")`` (메서드 스코프의 ``self.helper()``)
    * ``inside`` 안의 중첩 ``def`` -> ``("name",)`` (클로저로 숨기는 판)
    """
    found: dict[tuple[str, ...], Target] = {}
    for node in module.body:
        if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef):
            found[(node.name,)] = Target(node, True)
    if method_of is not None:
        for node in method_of.body:
            if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef):
                found[("self", node.name)] = Target(node, True)
    if inside is not None:
        for node in ast.walk(inside):
            if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef) and node is not inside:
                found.setdefault((node.name,), Target(node, True))
    return found


def imported_callables(
    module: ast.Module,
    root: Path,
    *,
    package: str = "cv_infra",
    sources: dict[str, str] | None = None,
) -> dict[tuple[str, ...], Target]:
    """이 모듈이 ``from <package>.x import y`` 로 끌어온 최상위 함수들 (**한 칸만** 펼침).

    모듈 최상위 import 와 **함수 안 import**(운반체는 GPU 의존을 그렇게 늦춘다) 둘 다
    본다. ``sources`` 는 대조군용 — 디스크 대신 주어진 소스로 그 모듈을 읽는다.
    """
    found: dict[tuple[str, ...], Target] = {}
    for node in ast.walk(module):
        if not isinstance(node, ast.ImportFrom):
            continue
        if node.module is None or not node.module.startswith(package):
            continue
        text = (sources or {}).get(node.module)
        if text is None:
            path = root.joinpath(*node.module.split(".")).with_suffix(".py")
            if not path.is_file():
                continue
            text = path.read_text(encoding="utf-8")
        defined = {
            child.name: child
            for child in ast.parse(text).body
            if isinstance(child, ast.FunctionDef | ast.AsyncFunctionDef)
        }
        for alias in node.names:
            if alias.name in defined:
                found[(alias.asname or alias.name,)] = Target(defined[alias.name], False)
    return found


def callable_index(
    module: ast.Module,
    root: Path,
    *,
    inside: ast.AST | None = None,
    method_of: ast.ClassDef | None = None,
    sources: dict[str, str] | None = None,
) -> dict[tuple[str, ...], Target]:
    """도달성 펼침의 사전 = 끌어온 것 + 이 모듈의 것(로컬이 이긴다 — 섀도잉)."""
    return {
        **imported_callables(module, root, sources=sources),
        **local_callables(module, inside=inside, method_of=method_of),
    }


def reachable_calls(
    scope: ast.AST,
    callables: dict[tuple[str, ...], Target],
    *,
    hops: int = MAX_HOPS,
) -> list[Reached]:
    """스코프에서 도달 가능한 모든 호출 (직접 + 헬퍼 ``hops`` 단계; 외부 함수는 한 칸)."""
    out: list[Reached] = []

    def walk(node: ast.AST, via: tuple[str, ...], site: int, budget: int, seen: frozenset) -> None:
        for child in ast.walk(node):
            if not isinstance(child, ast.Call):
                continue
            chain = call_chain(child.func)
            here = site if via else child.lineno
            out.append(Reached(chain, via, here))
            target = callables.get(chain)
            if target is not None and chain not in seen and budget > 0:
                left = budget - 1 if target.follow else 0
                walk(target.node, (*via, ".".join(chain)), here, left, seen | {chain})

    walk(scope, (), 0, hops, frozenset())
    return out


def reaching(
    scope: ast.AST,
    callables: dict[tuple[str, ...], Target],
    *spellings: str,
    hops: int = MAX_HOPS,
) -> list[Reached]:
    """``spellings`` 중 하나에 **속성 사슬 꼬리**가 일치하는 도달 호출들.

    꼬리 일치라서 수신자 이름을 바꿔 피할 수 없다: ``"pre_reset.append"`` 는
    ``sim.pre_reset.append`` 도 ``runtime.pre_reset.append`` 도 잡는다.
    """
    wanted = [tuple(spelling.split(".")) for spelling in spellings]
    return [
        hit
        for hit in reachable_calls(scope, callables, hops=hops)
        if any(hit.chain[-len(w) :] == w for w in wanted)
    ]
