"""Sample derivation from a randomized request (M1 — p6 설계 정본 §0-4/§2).

ONE submitted document + a sample index -> the CONCRETE request that sample
runs. Pure: stdlib + ``contract.schema`` only (no I/O, no clock, no module-level
RNG), so it is CPU-testable and stays inside the foundational import layer
(``.importlinter``: ``cv_infra.contract`` imports no sibling package).

Three properties every consumer depends on:

* PER-SAMPLE INDEPENDENT SEEDING — sample ``i`` draws from ``MT(sha256(
  "cv-derive/1:<seed>:<i>"))``, NOT from one stream advanced ``i`` times, so
  materializing sample 3 never requires materializing 0..2 first. The REST
  fan-out materializes each job in a loop and the CLI materializes sample 0
  alone; both must emit the same bytes for the same (document, index).
* STATIC IN, SAME OBJECT OUT — a document that declares no distribution is
  returned UNCHANGED (``materialize_request(req, i) is req``). Every pre-p6
  request therefore keeps its exact JOB_SPEC bytes and its
  ``request_identity_key``: the p6 path is provably not on it.
* FIXED DRAW ORDER — ``RANDOM_FIELDS`` below. A randomizable field added later
  APPENDS to that tuple (never inserts), or every existing seed silently starts
  producing different samples. The scalar blocks are drawn FIRST, in that order;
  then — only if the document declares ``scenario.obstacles`` — each group in
  list order: its ``count`` (one draw iff ``Randint``), then instance ``j`` in
  ascending order over ``OBSTACLE_FIELDS``. A document with no ``obstacles``
  consumes nothing there, which is why this whole extension left
  ``DERIVE_VERSION`` where it was.

WHAT IS NOT DERIVED: ``scenario.seed`` rides the sample UNCHANGED (it is the
sim's determinism seed — the same one every sample of a request uses, exactly
as pre-p6 repeats did) and the ``request_identity_key`` is derived from the
SUBMITTED document, never from a sample: "which request is this" must not move
per sample — the distribution + seed ARE the request (report/regression.py).
"""

from __future__ import annotations

import hashlib
import random

from cv_infra.contract.schema import (
    Choice,
    Obstacle,
    Randint,
    Scenario,
    Uniform,
    VerificationRequest,
)

#: Derivation algorithm version — stamped onto every materialized sample
#: (``Scenario.derivation.version``). Bump ONLY together with a change that
#: moves draws (seeding, draw order, rounding): a stored sample must be able to
#: say which rule produced it.
DERIVE_VERSION = "cv-derive/1"

#: Uniform draws are rounded to this many decimals. Why round at all: the raw
#: double rides the JOB_SPEC and the result document as text, and 4 decimals is
#: 0.1 mm on the metre axes / 0.006 deg on yaw — below any physical meaning the
#: sim can honour, while keeping the wire readable and diffable by a human
#: reading two samples side by side. ``choice`` values are NEVER rounded: those
#: are the consumer's own literals (verbatim, §2).
UNIFORM_DECIMALS = 4

#: The randomizable fields of the SCALAR blocks, in FIXED DRAW ORDER —
#: ``(scenario block, field)``. This is their single definition of "which fields
#: may carry a distribution"; inside a LIST (``Scenario.obstacles``) that job
#: belongs to ``OBSTACLE_FIELDS`` + its own walk, because a 2-tuple + one
#: ``getattr`` hop cannot address a list index. ``tests/test_contract_derive.py``
#: binds the UNION of the two BOTH ways to the schema (every entry is a
#: ``RandomizableFloat`` field, and every ``RandomizableFloat`` field in the
#: schema is an entry), so a field added to one side and not the other fails
#: loudly instead of never being drawn (G-25).
RANDOM_FIELDS: tuple[tuple[str, str], ...] = (
    ("initial_pose", "x"),
    ("initial_pose", "y"),
    ("initial_pose", "yaw"),
    ("goal", "x"),
    ("goal", "y"),
    ("goal", "yaw"),
    ("debug_obstacle", "x"),
    ("debug_obstacle", "y"),
)

#: 장애물 인스턴스 1개 안의 FIXED DRAW ORDER. 위 ``RANDOM_FIELDS``와 같은 규칙이 적용된다
#: (append, never insert). 리스트 안이라 (block, field) 튜플로 주소지정할 수 없어 별도로 산다.
OBSTACLE_FIELDS: tuple[str, ...] = ("x", "y", "yaw")


def sample_rng(seed: int, index: int) -> random.Random:
    """The RNG for sample ``index`` of a request seeded ``seed``.

    ``random.Random`` (Mersenne Twister) seeded from the first 64 bits of
    ``sha256("<DERIVE_VERSION>:<seed>:<index>")``. The hash — rather than e.g.
    ``seed + index`` — is what makes neighbouring (seed, index) pairs land in
    unrelated states, so sample 0 of seed 42 and sample 1 of seed 41 share
    nothing. stdlib only: reproducible across hosts and interpreter runs
    (MT is a specified algorithm, not a platform detail).
    """
    digest = hashlib.sha256(f"{DERIVE_VERSION}:{seed}:{index}".encode()).hexdigest()
    return random.Random(int(digest[:16], 16))


def distribution_fields(scenario: Scenario) -> tuple[str, ...]:
    """Wire paths of the scenario fields that still carry a DISTRIBUTION.

    Returned paths are ``scenario.<block>.<field>`` — the path in BOTH wires a
    consumer of this function reads (the request document and the JOB_SPEC),
    so the string can go straight into a rejection message.

    Callers: ``materialize_request`` (nothing to derive -> hand the request back
    untouched) and the leak check that guards the execution plane (§0-5): the
    union extension means ``extra="forbid"`` no longer stops a distribution
    from reaching it, so the runner rejects one explicitly — an unmaterialized
    ``{uniform: [...]}`` is a platform bug, not a pose. ``unmaterialized_fields``
    below composes this with the groups that are not EXPANDED yet — that wider
    window is what the execution plane needs once a document declares obstacles.

    Order: the scalar paths first (draw order), then the obstacle paths, group by
    group. An obstacle path names the DECLARATION (``scenario.obstacles[1].x``),
    not an instance — the instances do not exist until materialization.
    """
    scalar = tuple(
        f"scenario.{block_name}.{field_name}"
        for block_name, field_name in RANDOM_FIELDS
        if (block := getattr(scenario, block_name, None)) is not None
        and isinstance(getattr(block, field_name), (Uniform, Choice))
    )
    obstacles = tuple(
        f"scenario.obstacles[{index}].{field_name}"
        for index, group in enumerate(scenario.obstacles or ())
        for field_name in OBSTACLE_FIELDS
        if isinstance(getattr(group, field_name), (Uniform, Choice))
    )
    return scalar + obstacles


def unmaterialized_fields(scenario: Scenario) -> tuple[str, ...]:
    """실행 평면이 그대로 실행할 수 **없는** 필드의 와이어 경로 — 러너의 단일 창구.

    두 종류를 합친다: 아직 분포인 필드(``distribution_fields``)와, 아직 전개되지 않은
    장애물 그룹(``count``가 1이 아니거나 ``Randint``). 후자는 분포가 아니지만 러너가
    받으면 선언된 개수보다 적게 놓고 조용히 오판정한다(G-25).
    """
    unexpanded = tuple(
        f"scenario.obstacles[{index}].count"
        for index, group in enumerate(scenario.obstacles or ())
        if isinstance(group.count, Randint) or group.count != 1
    )
    return distribution_fields(scenario) + unexpanded


def _draw(value: float | Uniform | Choice, rng: random.Random) -> float:
    """분포면 draw 1회, 정적이면 그 값 그대로(draw 비소비) — 두 walk의 단일 정의."""
    if isinstance(value, Uniform):
        low, high = value.uniform
        return round(rng.uniform(low, high), UNIFORM_DECIMALS)
    if isinstance(value, Choice):
        return rng.choice(value.choice)
    return value


def _expand_obstacles(groups: list[Obstacle], rng: random.Random) -> list[dict]:
    """제출형 그룹 목록 -> 구체형(싱글턴) 목록. 순서 = 파생 순서 = 러너의 prim 배정 순서."""
    expanded: list[dict] = []
    for group in groups:
        template = group.model_dump()
        count = group.count
        n = rng.randint(*count.randint) if isinstance(count, Randint) else int(count)
        # The generator is consumed by ``extend`` one entry at a time, in order,
        # so each instance draws its own fields at exactly the point the append
        # loop drew them — draw ORDER and draw COUNT are unchanged (the golden
        # expansions in tests/test_contract_derive.py are the gate).
        expanded.extend(
            {
                **template,
                "count": 1,
                **{name: _draw(getattr(group, name), rng) for name in OBSTACLE_FIELDS},
            }
            for _ in range(n)
        )
    return expanded


def materialize_request(request: VerificationRequest, index: int) -> VerificationRequest:
    """The submitted request -> the CONCRETE request for sample ``index``.

    Draws ``RANDOM_FIELDS`` in order from ``sample_rng(scenario.seed, index)``,
    consuming a draw ONLY for a field that actually declares a distribution (a
    static field next to a random one must not shift the stream), then EXPANDS
    ``scenario.obstacles`` (each group -> ``count`` singleton entries, header),
    and stamps ``scenario.derivation = {version, index}`` so the sample carries
    its own provenance. Absent blocks (no ``initial_pose`` / no
    ``debug_obstacle`` / no ``obstacles``) consume nothing.

    Returns the ORIGINAL object when there is nothing to derive — identity
    (``is``), not equality: the static/repeats=1/self-test paths keep
    byte-identical JOB_SPECs and cannot be broken by a change in here. Declaring
    ``obstacles`` is derivation even with no distribution in it (the expansion
    IS the derivation), so such a document gets a new object and a stamp.
    """
    scenario = request.scenario
    if not distribution_fields(scenario) and scenario.obstacles is None:
        return request

    rng = sample_rng(scenario.seed, index)
    materialized = scenario.model_dump()
    for block_name, field_name in RANDOM_FIELDS:
        block = getattr(scenario, block_name, None)
        if block is None:
            continue
        materialized[block_name][field_name] = _draw(getattr(block, field_name), rng)
    if scenario.obstacles is not None:
        expanded = _expand_obstacles(scenario.obstacles, rng)
        # An empty expansion is "this sample has no obstacles" — and that is
        # spelled ``None``, never ``[]``: a list rides the identity projection
        # verbatim (report/regression.py prunes nulls, not lists).
        materialized["obstacles"] = expanded or None
    materialized["derivation"] = {"version": DERIVE_VERSION, "index": index}
    # Re-validated (not assigned) so the sample is a genuinely valid document:
    # the drawn values pass every Scenario constraint the submitted one did.
    # ``deep=True`` makes each sample an independent document — n samples of one
    # request never share a mutable sub-model.
    return request.model_copy(update={"scenario": Scenario.model_validate(materialized)}, deep=True)
