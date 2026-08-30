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

**증명하지 않는 것(정직성)**: 펼침은 **같은 모듈 안**으로 한정된다. 다른 모듈에
헬퍼를 두고 import 해서 부르면 이 도달성은 그것을 따라가지 않는다(측정: p8c2 T6
report §4 — 잔여 구멍으로 보고). 교차 모듈 펼침은 ``write_result``/``build_result_dict``
같은 정상 호출까지 끌어와 ``close`` 류 일반 이름에서 오발화하므로, **측정된 변이
클래스(모듈-로컬 추출)** 에만 맞춘 것이 지금의 선택이다.
"""

from __future__ import annotations

import ast
from dataclasses import dataclass

#: 펼침 깊이. 래칫이 실제로 만드는 모양은 1홉(호출부 -> 헬퍼)이고, 2홉(헬퍼 -> 헬퍼)은
#: p8c2 T6 이 손으로 만들어 확인했다. 3 은 그 위의 여유다 — 무한 재귀는 ``seen`` 이 막는다.
MAX_HOPS = 3


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
) -> dict[tuple[str, ...], ast.AST]:
    """이 스코프가 **이름으로 부를 수 있는** 같은-모듈 함수들.

    * 모듈 최상위 ``def`` -> ``("name",)``
    * ``method_of`` 의 메서드 -> ``("self", "name")`` (메서드 스코프의 ``self.helper()``)
    * ``inside`` 안의 중첩 ``def`` -> ``("name",)`` (클로저로 숨기는 판)
    """
    found: dict[tuple[str, ...], ast.AST] = {}
    for node in module.body:
        if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef):
            found[(node.name,)] = node
    if method_of is not None:
        for node in method_of.body:
            if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef):
                found[("self", node.name)] = node
    if inside is not None:
        for node in ast.walk(inside):
            if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef) and node is not inside:
                found.setdefault((node.name,), node)
    return found


def reachable_calls(
    scope: ast.AST,
    callables: dict[tuple[str, ...], ast.AST],
    *,
    hops: int = MAX_HOPS,
) -> list[Reached]:
    """스코프에서 도달 가능한 모든 호출 (직접 + 모듈-로컬 헬퍼 ``hops`` 단계)."""
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
                walk(target, (*via, ".".join(chain)), here, budget - 1, seen | {chain})

    walk(scope, (), 0, hops, frozenset())
    return out


def reaching(
    scope: ast.AST,
    callables: dict[tuple[str, ...], ast.AST],
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
