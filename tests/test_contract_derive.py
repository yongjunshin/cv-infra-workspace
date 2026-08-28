"""M1 sample DERIVATION tests (p6c3 T1) — contract/derive.py.

The notation itself is tests/test_contract_random.py; this file pins what the
platform DRAWS from it, which is the part a stored result can never be
re-derived from if it moves silently:

* (1) golden draws — the exact samples seed 42 produces today, so a change to
  seeding/draw order/rounding cannot land unnoticed (and the same values are
  reproduced twice, in-process and across a fresh interpreter);
* (2) per-sample independence — sample i does not depend on 0..i-1 having been
  materialized, which is what lets the REST fan-out loop and the CLI's
  single-sample path agree;
* (3) static in, SAME OBJECT out — the identity (``is``) that keeps every
  pre-p6 path byte-for-byte where it was;
* (4) draw accounting — static/absent fields consume no draw (with the
  positive control that a real distribution in that slot DOES shift the
  stream, G-35);
* (5) the RANDOM_FIELDS <-> schema bind, BOTH ways (G-25: a randomizable field
  added to one side and not the other would never be drawn / would crash);
* (6) the stamp and what it must not touch (seed carries over; the submitted
  document is not mutated);
* (7) the p7 obstacle walk — the same five properties for the LIST half of the
  draw order (its own golden, its own accounting), plus the two things only a
  list can get wrong: the expansion re-validates as a submitted document, and
  an empty expansion is ``None`` (never ``[]``, which would move an identity
  key — tests/test_report_regression.py holds the other end of that).
"""

from __future__ import annotations

import copy
import json
import subprocess
import sys
import typing

import pytest

from cv_infra.contract import schema as schema_mod
from cv_infra.contract.derive import (
    DERIVE_VERSION,
    OBSTACLE_FIELDS,
    RANDOM_FIELDS,
    UNIFORM_DECIMALS,
    distribution_fields,
    materialize_request,
    sample_rng,
    unmaterialized_fields,
)
from cv_infra.contract.schema import Choice, Scenario, Uniform, VerificationRequest

_DIST_DOC = {
    "apiVersion": "cv-infra/v1",
    "scenario": {
        "scene": "nova_carter_warehouse",
        "robot": "nova_carter",
        # goal.x = choice (verbatim), goal.yaw = uniform, goal.y = static neighbour
        "goal": {"x": {"choice": [-6.0, 5.0]}, "y": 5.0, "yaw": {"uniform": [0.0, 3.1416]}},
        "seed": 42,
        "timeout_s": 120,
        "initial_pose": {"x": {"uniform": [-6.5, -5.5]}, "y": -1.0, "yaw": 3.1416},
        "debug_obstacle": {"x": {"uniform": [-7.0, -5.0]}, "y": 2.0},
    },
    "sut": {"image_ref": "carter-sut:a"},
    "acceptance_criteria": [{"oracle": "reached_goal"}],
    "execution_settings": {"repeats": 3},
}

_STATIC_DOC = {
    **_DIST_DOC,
    "scenario": {
        **_DIST_DOC["scenario"],
        "goal": {"x": -6.0, "y": 5.0, "yaw": 1.5708},
        "initial_pose": {"x": -6.0, "y": -1.0, "yaw": 3.1416},
        "debug_obstacle": {"x": -6.0, "y": 2.0},
    },
}

# GOLDEN DRAWS — measured 2026-08-26 on this implementation (seed 42, samples
# 0..2 of _DIST_DOC). They are not "the right numbers" in any physical sense;
# they are THIS derivation rule's fingerprint. A red here means the rule moved,
# which is exactly when DERIVE_VERSION must move with it (and every stored
# sample stamped with the old version becomes un-reproducible).
_GOLDEN: dict[int, dict[str, float]] = {
    0: {
        "initial_pose.x": -6.3035,
        "goal.x": -6.0,
        "goal.yaw": 0.4777,
        "debug_obstacle.x": -5.8333,
    },
    1: {
        "initial_pose.x": -6.3265,
        "goal.x": 5.0,
        "goal.yaw": 3.0016,
        "debug_obstacle.x": -6.7749,
    },
    2: {
        "initial_pose.x": -6.1798,
        "goal.x": -6.0,
        "goal.yaw": 2.9857,
        "debug_obstacle.x": -6.1013,
    },
}


def _request(doc: dict | None = None) -> VerificationRequest:
    return VerificationRequest.model_validate(copy.deepcopy(doc or _DIST_DOC))


def _drawn(request: VerificationRequest) -> dict[str, float]:
    scenario = request.scenario
    return {
        f"{block}.{field}": getattr(getattr(scenario, block), field)
        for block, field in (
            ("initial_pose", "x"),
            ("goal", "x"),
            ("goal", "yaw"),
            ("debug_obstacle", "x"),
        )
    }


# --------------------------------------------------------------------------- #
# (1) golden draws + determinism
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("index", sorted(_GOLDEN))
def test_samples_match_the_golden_draws(index):
    assert _drawn(materialize_request(_request(), index)) == _GOLDEN[index]


def test_the_same_input_derives_the_same_sample_twice():
    first = materialize_request(_request(), 1).model_dump(mode="json", by_alias=True)
    second = materialize_request(_request(), 1).model_dump(mode="json", by_alias=True)
    assert json.dumps(first, sort_keys=True) == json.dumps(second, sort_keys=True)


def test_determinism_survives_a_fresh_interpreter():
    """In-process repetition cannot see a dependency on module import order or on
    the global ``random`` state — a fresh process can (and PYTHONHASHSEED differs
    per process, so a hash-order dependency would surface here)."""
    code = (
        "import json;"
        "from cv_infra.contract.schema import VerificationRequest;"
        "from cv_infra.contract.derive import materialize_request;"
        f"doc={_DIST_DOC!r};"
        "req=VerificationRequest.model_validate(doc);"
        "print(json.dumps(materialize_request(req,1).scenario.model_dump(),sort_keys=True))"
    )
    out = subprocess.run(
        [sys.executable, "-c", code], capture_output=True, text=True, check=True
    ).stdout
    expected = json.dumps(materialize_request(_request(), 1).scenario.model_dump(), sort_keys=True)
    assert out.strip() == expected


def test_different_seeds_and_indices_draw_differently():
    """Positive control for the pins above: they would also pass if every sample
    were the same constant."""
    other_seed = copy.deepcopy(_DIST_DOC)
    other_seed["scenario"]["seed"] = 43
    assert _drawn(materialize_request(_request(), 0)) != _drawn(materialize_request(_request(), 1))
    assert _drawn(materialize_request(_request(other_seed), 0)) != _GOLDEN[0]


def test_sample_rng_is_seeded_per_sample_not_per_request():
    a0, a1 = sample_rng(42, 0), sample_rng(42, 1)
    assert [a0.random() for _ in range(3)] != [a1.random() for _ in range(3)]
    # neighbouring (seed, index) pairs must not collide onto one stream
    assert sample_rng(42, 0).random() != sample_rng(41, 1).random()
    assert sample_rng(42, 0).random() == sample_rng(42, 0).random()


# --------------------------------------------------------------------------- #
# (2) per-sample independence (the fan-out property)
# --------------------------------------------------------------------------- #
def test_sample_three_is_the_same_whether_or_not_zero_to_two_were_derived():
    alone = _drawn(materialize_request(_request(), 3))
    request = _request()
    for index in range(3):
        materialize_request(request, index)
    assert _drawn(materialize_request(request, 3)) == alone


# --------------------------------------------------------------------------- #
# (3) static in, same object out
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("index", [0, 1, 7])
def test_a_static_request_is_returned_unchanged_by_identity(index):
    request = _request(_STATIC_DOC)
    assert materialize_request(request, index) is request


def test_a_static_request_without_optional_blocks_is_also_returned_unchanged():
    doc = copy.deepcopy(_STATIC_DOC)
    del doc["scenario"]["initial_pose"]
    del doc["scenario"]["debug_obstacle"]
    request = _request(doc)
    assert materialize_request(request, 0) is request


def test_a_randomized_request_yields_a_new_object_and_leaves_the_original_alone():
    request = _request()
    sample = materialize_request(request, 0)
    assert sample is not request
    assert isinstance(request.scenario.goal.x, Choice)  # submitted doc untouched
    assert isinstance(request.scenario.initial_pose.x, Uniform)
    assert request.scenario.derivation is None


# --------------------------------------------------------------------------- #
# (4) draw accounting — only distributions consume the stream
# --------------------------------------------------------------------------- #
def test_static_and_absent_fields_consume_no_draw():
    """A static ``initial_pose`` sits BEFORE goal in the draw order; adding one
    must not shift what goal draws, or a consumer pinning a spawn pose would
    silently re-roll every other axis.

    Positive control (G-35): making that same field RANDOM does shift the stream
    — so the equality above is a fact about draw accounting, not about the
    fields being unrelated.
    """
    absent = copy.deepcopy(_DIST_DOC)
    del absent["scenario"]["initial_pose"]
    static = copy.deepcopy(_DIST_DOC)
    static["scenario"]["initial_pose"] = {"x": -6.0, "y": -1.0, "yaw": 3.1416}

    goal_of = lambda doc: materialize_request(_request(doc), 0).scenario.goal.yaw  # noqa: E731
    assert goal_of(absent) == goal_of(static)
    assert goal_of(_DIST_DOC) != goal_of(static)  # the control: a real draw shifts it


def test_uniform_is_rounded_and_choice_is_verbatim():
    doc = copy.deepcopy(_DIST_DOC)
    doc["scenario"]["goal"] = {
        "x": {"choice": [1.23456789]},
        "y": {"uniform": [2.0, 2.0]},  # degenerate: the bound itself
        "yaw": {"uniform": [0.0, 1.0]},
    }
    goal = materialize_request(_request(doc), 0).scenario.goal
    assert goal.x == 1.23456789  # verbatim — the consumer's own literal
    assert goal.y == 2.0
    assert goal.yaw == round(goal.yaw, UNIFORM_DECIMALS) and 0.0 <= goal.yaw <= 1.0


@pytest.mark.parametrize("index", range(6))
def test_every_draw_lands_inside_its_declared_support(index):
    scenario = materialize_request(_request(), index).scenario
    assert -6.5 <= scenario.initial_pose.x <= -5.5
    assert scenario.goal.x in (-6.0, 5.0)
    assert 0.0 <= scenario.goal.yaw <= 3.1416
    assert -7.0 <= scenario.debug_obstacle.x <= -5.0
    # static neighbours are carried over verbatim
    assert (scenario.goal.y, scenario.initial_pose.y, scenario.debug_obstacle.y) == (5.0, -1.0, 2.0)


# --------------------------------------------------------------------------- #
# (5) RANDOM_FIELDS <-> schema, both ways (G-25)
# --------------------------------------------------------------------------- #
def _randomizable_schema_fields() -> set[tuple[str, str]]:
    """Every ``(scenario block, field)`` the SCHEMA annotates RandomizableFloat.

    Derived from the models (never a retyped list): the scenario's own block
    fields are unwrapped to their model class, then each model field's
    annotation is compared against the union. pydantic hoists the outer
    ``Annotated[...]`` metadata into ``FieldInfo.metadata``, so the comparable
    object is the union INSIDE ``RandomizableFloat`` — taken from the schema's
    own symbol, never retyped here.
    """
    union = typing.get_args(schema_mod.RandomizableFloat)[0]
    found: set[tuple[str, str]] = set()
    for block_name, block_field in schema_mod.Scenario.model_fields.items():
        # One unwrap reaches a block model (``DebugObstacle | None``); a LIST of
        # them (``list[Obstacle] | None``) needs a second one, or the obstacle
        # fields would be invisible to this extractor and the bind would pass by
        # never looking (the exact G-25 shape this function exists to prevent).
        outer = (block_field.annotation, *getattr(block_field.annotation, "__args__", ()))
        for candidate in (*outer, *(a for c in outer for a in getattr(c, "__args__", ()))):
            fields = getattr(candidate, "model_fields", None)
            if not fields:
                continue
            for field_name, field in fields.items():
                if field.annotation == union:
                    found.add((block_name, field_name))
    return found


def test_random_fields_matches_the_schema_both_ways():
    """The union of the two walks (scalar blocks + the obstacle list) must be
    exactly what the schema annotates randomizable — neither more nor less."""
    schema_fields = _randomizable_schema_fields()
    assert schema_fields, "extraction went empty (positive control, G-07)"
    assert schema_fields == set(RANDOM_FIELDS) | {("obstacles", f) for f in OBSTACLE_FIELDS}
    assert len(RANDOM_FIELDS) == len(set(RANDOM_FIELDS)) == 8  # design §0-1: scalars stay 8
    assert OBSTACLE_FIELDS == ("x", "y", "yaw")  # draw order INSIDE one instance


def test_distribution_fields_reports_wire_paths_in_draw_order():
    assert distribution_fields(_request().scenario) == (
        "scenario.initial_pose.x",
        "scenario.goal.x",
        "scenario.goal.yaw",
        "scenario.debug_obstacle.x",
    )
    assert distribution_fields(_request(_STATIC_DOC).scenario) == ()


def test_a_materialized_sample_carries_no_distribution_left():
    """The post-condition the runner's leak check (§0-5) depends on: after
    materialization there is nothing left for the execution plane to choke on."""
    assert distribution_fields(materialize_request(_request(), 0).scenario) == ()


# --------------------------------------------------------------------------- #
# (6) the stamp, and what derivation must NOT touch
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("index", [0, 1, 4])
def test_each_sample_is_stamped_with_the_rule_and_its_index(index):
    derivation = materialize_request(_request(), index).scenario.derivation
    assert (derivation.version, derivation.index) == (DERIVE_VERSION, index)


def test_derive_version_is_the_pinned_wire_string():
    # A golden literal: the stamp rides stored results, so the string is a wire
    # value, not an implementation detail.
    assert DERIVE_VERSION == "cv-derive/1"


def test_the_seed_and_everything_outside_the_scenario_carry_over_unchanged():
    """``scenario.seed`` is the SIM's determinism seed — shared by every sample of
    a request, exactly as pre-p6 repeats were (design §0-4). Only the drawn
    fields and the stamp may differ from the submitted document."""
    request = _request()
    sample = materialize_request(request, 2)
    assert sample.scenario.seed == request.scenario.seed == 42
    assert sample.sut == request.sut
    assert sample.execution_settings == request.execution_settings
    assert sample.acceptance_criteria == request.acceptance_criteria
    submitted = request.scenario.model_dump()
    derived = sample.scenario.model_dump()
    changed = {k for k in submitted if submitted[k] != derived[k]}
    assert changed == {"goal", "initial_pose", "debug_obstacle", "derivation"}


def test_repeats_one_with_a_distribution_still_derives_sample_zero():
    """repeats is the SAMPLE COUNT (B-U): one sample is still a DRAW from the
    declared distribution, not the notation leaking through untouched."""
    doc = copy.deepcopy(_DIST_DOC)
    doc["execution_settings"] = {"repeats": 1}
    sample = materialize_request(_request(doc), 0)
    assert sample.scenario.derivation.index == 0
    assert _drawn(sample) == _GOLDEN[0]  # repeats does not enter the derivation


# --------------------------------------------------------------------------- #
# (7) obstacles — the LIST half of the draw order (p7)
# --------------------------------------------------------------------------- #

# The CEO's own example ("의자 1개, 책상/상자 n={0~5}개, 자동차 2개"), narrowed to
# n={0~3}. ``debug_obstacle`` is ABSENT on purpose: the legacy box and this list
# are mutually exclusive (schema ``_one_home_for_world_obstacles``), so a document
# that declares obstacles cannot carry it.
_OBSTACLE_DOC = {
    **_DIST_DOC,
    "scenario": {
        **{k: v for k, v in _DIST_DOC["scenario"].items() if k != "debug_obstacle"},
        "obstacles": [
            {"asset": "chair", "x": {"uniform": [-2.0, 2.0]}, "y": 1.0},
            {
                "asset": "box",
                "count": {"randint": [0, 3]},
                "x": {"uniform": [-6.0, 6.0]},
                "y": {"uniform": [-3.0, 3.0]},
                "yaw": {"choice": [0.0, 1.5708]},
                "height": 0.5,
            },
            {"asset": "car", "count": 2, "x": {"uniform": [4.0, 5.0]}, "y": 2.0, "yaw": 3.1416},
        ],
    },
}

# GOLDEN EXPANSIONS — measured 2026-08-28 on this implementation (seed 42, samples
# 0..2 of _OBSTACLE_DOC), the sibling of ``_GOLDEN`` for the list walk. Read the
# rows as the wire: ``count`` is 1 on every one of them (a materialized entry IS
# one obstacle), the static fields are broadcast verbatim to every instance of
# their group, and the ORDER is the order the runner assigns prims in.
_OBSTACLE_GOLDEN: dict[int, list[dict]] = {
    0: [
        {"asset": "chair", "count": 1, "x": 0.3333, "y": 1.0, "yaw": 0.0},
        {"asset": "box", "count": 1, "x": -4.9205, "y": -2.5721, "yaw": 0.0, "height": 0.5},
        {"asset": "car", "count": 1, "x": 4.9142, "y": 2.0, "yaw": 3.1416},
        {"asset": "car", "count": 1, "x": 4.9293, "y": 2.0, "yaw": 3.1416},
    ],
    1: [
        {"asset": "chair", "count": 1, "x": -1.5497, "y": 1.0, "yaw": 0.0},
        {"asset": "box", "count": 1, "x": 3.2747, "y": 1.324, "yaw": 1.5708, "height": 0.5},
        {"asset": "box", "count": 1, "x": -5.7696, "y": -0.9704, "yaw": 1.5708, "height": 0.5},
        {"asset": "car", "count": 1, "x": 4.2538, "y": 2.0, "yaw": 3.1416},
        {"asset": "car", "count": 1, "x": 4.1814, "y": 2.0, "yaw": 3.1416},
    ],
    2: [
        {"asset": "chair", "count": 1, "x": -0.2027, "y": 1.0, "yaw": 0.0},
        {"asset": "car", "count": 1, "x": 4.2268, "y": 2.0, "yaw": 3.1416},
        {"asset": "car", "count": 1, "x": 4.5301, "y": 2.0, "yaw": 3.1416},
    ],
}


def _obstacles_of(request: VerificationRequest) -> list[dict] | None:
    return request.scenario.model_dump(exclude_none=True).get("obstacles")


@pytest.mark.parametrize("index", sorted(_OBSTACLE_GOLDEN))
def test_obstacle_expansions_match_the_golden(index):
    assert (
        _obstacles_of(materialize_request(_request(_OBSTACLE_DOC), index))
        == _OBSTACLE_GOLDEN[index]
    )


def test_the_scalar_draws_are_untouched_by_the_obstacle_walk():
    """The reason ``DERIVE_VERSION`` did not move: the list walk APPENDS.

    The obstacle document draws the same scalars as ``_DIST_DOC`` — checked
    against the pre-p7 ``_GOLDEN`` literal itself, on the three scalar fields
    this document has (it drops ``debug_obstacle``, which was drawn LAST of the
    eight, so the shared prefix is untouched).
    """
    for index, golden in _GOLDEN.items():
        scenario = materialize_request(_request(_OBSTACLE_DOC), index).scenario
        assert {
            "initial_pose.x": scenario.initial_pose.x,
            "goal.x": scenario.goal.x,
            "goal.yaw": scenario.goal.yaw,
        } == {k: v for k, v in golden.items() if k != "debug_obstacle.x"}


def test_the_count_sequence_is_deterministic_per_seed():
    """A random COUNT is a draw like any other: same (seed, index) -> same n.

    Positive control (G-35): another seed gives a different sequence, so the pin
    above is not "every sample expands to the same thing".
    """
    counts = [
        len(materialize_request(_request(_OBSTACLE_DOC), i).scenario.obstacles) for i in range(6)
    ]
    assert counts == [4, 5, 3, 3, 6, 5]

    other_seed = copy.deepcopy(_OBSTACLE_DOC)
    other_seed["scenario"] = {**other_seed["scenario"], "seed": 43}
    assert [
        len(materialize_request(_request(other_seed), i).scenario.obstacles) for i in range(6)
    ] != counts


def test_the_expansion_re_validates_as_a_submitted_document():
    """Why there is no parallel ``MaterializedObstacle`` model: the expansion is
    the SAME schema (that is what the runner re-validates), and it leaves nothing
    for the execution plane to choke on."""
    sample = materialize_request(_request(_OBSTACLE_DOC), 1)
    dumped = sample.scenario.model_dump()
    assert Scenario.model_validate(dumped).obstacles == sample.scenario.obstacles
    assert all(entry.count == 1 for entry in sample.scenario.obstacles)
    assert unmaterialized_fields(sample.scenario) == ()


def test_a_static_group_broadcasts_and_consumes_no_draw():
    """``count: 3`` with static x/y/yaw = three identical instances, zero draws.

    Positive control (G-35): the draws of the group AFTER it are unchanged by its
    presence, and DO shift when the same group's x becomes a distribution — so
    "consumes no draw" is a fact about accounting, not about the groups being
    unrelated. (Obstacles are last in the draw order, so the neighbour that can
    witness a shift is the next GROUP, not the scalar blocks.)
    """
    car = {"asset": "car", "count": 2, "x": {"uniform": [4.0, 5.0]}, "y": 2.0, "yaw": 3.1416}
    static_group = {"asset": "box", "count": 3, "x": -1.0, "y": 2.0, "yaw": 0.5, "height": 0.5}

    def cars(*groups) -> list[float]:
        doc = copy.deepcopy(_OBSTACLE_DOC)
        doc["scenario"] = {**doc["scenario"], "obstacles": copy.deepcopy(list(groups))}
        sample = materialize_request(_request(doc), 0)
        return [o.x for o in sample.scenario.obstacles if o.asset == "car"]

    assert cars(static_group, car) == cars(car)
    assert cars({**static_group, "x": {"uniform": [-1.0, 1.0]}}, car) != cars(car)

    doc = copy.deepcopy(_OBSTACLE_DOC)
    doc["scenario"] = {**doc["scenario"], "obstacles": [static_group]}
    expanded = _obstacles_of(materialize_request(_request(doc), 0))
    assert expanded == [{**static_group, "count": 1}] * 3  # broadcast, verbatim


def test_an_empty_expansion_leaves_no_obstacles_key_at_all():
    """``n = 0`` for every group = "this sample has no obstacles", spelled the way
    a document with no obstacles spells it: the key is ABSENT from the wire.
    ``[]`` would be a different identity key (report/regression.py prunes nulls,
    never lists) — the other end of this is pinned in test_report_regression.py.
    """
    doc = copy.deepcopy(_OBSTACLE_DOC)
    doc["scenario"] = {
        **doc["scenario"],
        "obstacles": [{"asset": "box", "count": {"randint": [0, 0]}, "x": 1.0, "y": 2.0}],
    }
    sample = materialize_request(_request(doc), 0)
    assert sample.scenario.obstacles is None
    assert "obstacles" not in sample.scenario.model_dump(exclude_none=True)


def test_a_document_with_obstacles_is_materialized_even_with_no_distribution():
    """Declaring obstacles IS derivation (the expansion is the derived thing), so
    the static short-circuit does not apply — the sample is a new object and
    carries the stamp. The ``is`` asymmetry with a static document is deliberate.
    """
    doc = copy.deepcopy(_STATIC_DOC)
    doc["scenario"] = {
        **{k: v for k, v in doc["scenario"].items() if k != "debug_obstacle"},
        "obstacles": [{"asset": "chair", "count": 2, "x": 1.0, "y": 2.0}],
    }
    request = _request(doc)
    sample = materialize_request(request, 0)
    assert sample is not request
    assert sample.scenario.derivation.index == 0
    assert [o.count for o in sample.scenario.obstacles] == [1, 1]


def test_obstacle_determinism_survives_a_fresh_interpreter():
    """``rng.randint`` rides the same ``_randbelow`` machinery ``rng.choice``
    already does — this is the cross-process pin that says so (R1)."""
    code = (
        "import json;"
        "from cv_infra.contract.schema import VerificationRequest;"
        "from cv_infra.contract.derive import materialize_request;"
        f"doc={_OBSTACLE_DOC!r};"
        "req=VerificationRequest.model_validate(doc);"
        "print(json.dumps(materialize_request(req,1).scenario.model_dump(),sort_keys=True))"
    )
    out = subprocess.run(
        [sys.executable, "-c", code], capture_output=True, text=True, check=True
    ).stdout
    expected = json.dumps(
        materialize_request(_request(_OBSTACLE_DOC), 1).scenario.model_dump(), sort_keys=True
    )
    assert out.strip() == expected


def test_distribution_fields_reports_the_obstacle_paths_after_the_scalars():
    """The wire path names the DECLARATION (group index), which is exactly what
    ``errors.render_loc`` prints for the same violation."""
    assert distribution_fields(_request(_OBSTACLE_DOC).scenario) == (
        "scenario.initial_pose.x",
        "scenario.goal.x",
        "scenario.goal.yaw",
        "scenario.obstacles[0].x",
        "scenario.obstacles[1].x",
        "scenario.obstacles[1].y",
        "scenario.obstacles[1].yaw",
        "scenario.obstacles[2].x",
    )


def test_unmaterialized_fields_reports_unexpanded_counts_too():
    """The runner's single window (§A5.3): ``count: 3`` is not a distribution, but
    handing it to the execution plane would put ONE box where three were asked
    for and judge the run anyway (G-25). After materialization: nothing left.
    """
    doc = copy.deepcopy(_OBSTACLE_DOC)
    doc["scenario"] = {
        **doc["scenario"],
        "goal": {"x": -6.0, "y": 5.0, "yaw": 1.5708},
        "initial_pose": {"x": -6.0, "y": -1.0, "yaw": 3.1416},
        "obstacles": [
            {"asset": "box", "count": 3, "x": 1.0, "y": 2.0},  # static, but NOT expanded
            {"asset": "chair", "count": {"randint": [0, 2]}, "x": 1.0, "y": 2.0},
            {"asset": "car", "count": 1, "x": 1.0, "y": 2.0},  # already concrete
        ],
    }
    scenario = _request(doc).scenario
    assert distribution_fields(scenario) == ()  # no DISTRIBUTION anywhere...
    assert unmaterialized_fields(scenario) == (  # ...but two groups are unexpanded
        "scenario.obstacles[0].count",
        "scenario.obstacles[1].count",
    )
    assert unmaterialized_fields(materialize_request(_request(doc), 0).scenario) == ()
