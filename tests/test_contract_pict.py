"""M1 input-space case-planning tests — contract/pict.py.

What this file pins is NOT "PICT works" (it is a reused, MIT, upstream-tested
binary — do-not-reinvent) but the three things WE own on top of it, each of
which silently degrades the gate if it drifts:

* (1) **seeding keeps a suite's history alive.** The measured failure mode: edit
  the model by one line and every case id changes, so every baseline lookup
  misses, so every regression check SKIPS (C-1 / NFR-REPORT-002 — absent
  baseline is normal, never a failure). The gate goes quiet instead of red.
  Both halves are asserted: unseeded regeneration loses the old rows, seeded
  regeneration keeps all of them.
* (2) **the requested k is the k that runs, and a declared repeats is honoured.**
  The gate's coverage claim is only true if nothing downgrades it behind the
  report's back, so ``plan`` cuts ROWS, not the order, and takes ``repeats`` at
  its word. The old walk-down (and with it the MEASURED-flakiness repeats floor)
  survives as the opt-in ``orders="auto"`` branch.
* (3) **truncation is honest.** A budget-cut suite reports the coverage it
  actually achieved, and the prefix curve is monotone so that number means
  something.

Plus the friendly-error surface PICT does not provide (file/line), which the
request plane owes every rejected document.

The binary is resolved from ``$CV_PICT_BIN``; without it the generation tests
skip and the pure ones (validation, coverage arithmetic) still run.
"""

from __future__ import annotations

import os
import shutil
import subprocess

import pytest

from cv_infra.contract import pict

#: The real go2 patrol space (8 axes, 2 constraints, 1,296-cell grid) — the same
#: model whose k=2/3/4 sizes (14/42/112) are quoted in the module docstring.
GO2_MODEL = """\
target:      person, chair
start_x:     -6.3, -6.0, -5.7
start_y:     -1.3, -1.0, -0.7
start_yaw:   1.4208, 1.5708, 1.7208
box_count:   0, 1, 2
desk_count:  0, 1
camera:      on, off
lighting:    bright, dim

IF [target] = "chair" THEN [start_yaw] = 1.5708;
IF [lighting] = "dim" THEN [camera] = "on";
"""

_HAVE_PICT = bool(os.environ.get(pict.PICT_BIN_ENV) or shutil.which("pict"))
needs_pict = pytest.mark.skipif(not _HAVE_PICT, reason=f"${pict.PICT_BIN_ENV} not set")


# --- (0) the arrays this project's numbers rest on ------------------------------------


@needs_pict
@pytest.mark.parametrize(("order", "rows"), [(2, 14), (3, 42), (4, 112)])
def test_go2_space_array_sizes_are_the_measured_ones(order: int, rows: int) -> None:
    """The 1,296-cell grid collapses to these sizes. Every cost table cites them."""
    array = pict.generate(GO2_MODEL, order=order)
    assert len(array) == rows
    assert array.parameters[0] == "target"
    assert all(len(r) == len(array.parameters) for r in array.rows)


@needs_pict
def test_generation_is_deterministic() -> None:
    """Same model, same array — a case id that moved on its own is not an id."""
    assert pict.generate(GO2_MODEL, order=2) == pict.generate(GO2_MODEL, order=2)


@needs_pict
def test_constraints_are_honoured() -> None:
    """A declared constraint must hold in every row, or the suite tests fiction."""
    for row in pict.generate(GO2_MODEL, order=2).as_dicts():
        if row["target"] == "chair":
            assert row["start_yaw"] == "1.5708"
        if row["lighting"] == "dim":
            assert row["camera"] == "on"


# --- (1) seeding keeps regression history alive ---------------------------------------


@needs_pict
def test_unseeded_model_edit_destroys_every_prior_case() -> None:
    """The failure this module exists to prevent — asserted, not assumed.

    One added axis regenerates the array from scratch. If ANY prior row survived
    by luck the guard below would be weaker than the claim, so the assertion is
    the measured zero.
    """
    before = pict.generate(GO2_MODEL, order=2)
    after = pict.generate(GO2_MODEL.replace("\nIF", "\nfloor: flat, ramp\n\nIF", 1), order=2)
    width = len(before.parameters)
    survivors = {r[:width] for r in after.rows} & set(before.rows)
    assert survivors == set()


@needs_pict
def test_seeding_preserves_every_prior_case_across_a_model_edit() -> None:
    """...and the fix: seed the previous array, keep all of it, cover the new axis."""
    before = pict.generate(GO2_MODEL, order=2)
    edited = GO2_MODEL.replace("\nIF", "\nfloor: flat, ramp\n\nIF", 1)
    after = pict.generate(edited, order=2, seed_rows=before)
    width = len(before.parameters)
    assert set(before.rows) <= {r[:width] for r in after.rows}
    assert "floor" in after.parameters


# --- (2) the requested order runs; rows are what the budget cuts -----------------------


@needs_pict
def test_plan_generates_exactly_the_requested_order() -> None:
    """42 rows x 3 repeats / 4 concurrency at 300 s/run = 2.6 h — a 3 h budget holds 3-wise."""
    plan = pict.plan(
        GO2_MODEL,
        budget=pict.Budget(wallclock_s=3 * 3600, repeats=3),
        cost_s_per_run=300.0,
        order=3,
        concurrency=4,
    )
    assert plan.requested_order == 3
    assert plan.cases == 42
    assert plan.truncated_from is None
    assert plan.coverage == 1.0


@needs_pict
def test_plan_truncates_rows_rather_than_downgrading_the_requested_order() -> None:
    """The M5 repair. Same workload, a 1 h budget: 3-wise no longer fits, and the OLD
    behaviour quietly returned a full pairwise array — a report that says "3-wise" while
    covering pairs only. Now the order stands and the array is cut, which ``coverage``
    reports as the fraction it is."""
    plan = pict.plan(
        GO2_MODEL,
        budget=pict.Budget(wallclock_s=3600, repeats=3),
        cost_s_per_run=300.0,
        order=3,
        concurrency=4,
    )
    assert plan.requested_order == 3
    assert plan.truncated_from == 42
    assert plan.cases == 16  # 3600 s / (300 s x 3 repeats / 4)
    assert plan.coverage < 1.0


@needs_pict
def test_plan_honours_a_declared_repeats_of_one() -> None:
    """``repeats:`` is a consumer input, not a suggestion. The floor that used to
    override it now lives only in the opt-in walk-down (test below)."""
    plan = pict.plan(
        GO2_MODEL,
        budget=pict.Budget(wallclock_s=3600, repeats=1),
        cost_s_per_run=300.0,
        concurrency=4,
    )
    assert plan.repeats == 1
    assert plan.runs == plan.cases


@needs_pict
def test_an_undeclared_repeats_defaults_to_one_not_to_the_floor() -> None:
    """The floor must not survive as a dataclass default either: a caller that says
    nothing about repeats gets the contract default (1), not three silent extra runs."""
    plan = pict.plan(
        GO2_MODEL,
        budget=pict.Budget(wallclock_s=3600),
        cost_s_per_run=300.0,
        concurrency=4,
    )
    assert plan.repeats == 1


@needs_pict
def test_plan_refuses_a_budget_that_affords_no_case_at_all() -> None:
    """A budget too small for ONE case is a rejected request, not an empty suite."""
    with pytest.raises(pict.PictError, match="affords no cases"):
        pict.plan(
            GO2_MODEL,
            budget=pict.Budget(wallclock_s=60, repeats=3),
            cost_s_per_run=300.0,
            concurrency=1,
        )


@needs_pict
def test_plan_rejects_an_order_wider_than_the_space() -> None:
    """No clamping in the default branch: PICT's own reject is the honest answer, and
    ``inputs`` pre-empts it with a friendlier exit-2 before any GPU time is spent."""
    with pytest.raises(pict.PictError):
        pict.plan(
            "a: 1, 2\nb: 3, 4\n",
            budget=pict.Budget(wallclock_s=3600, repeats=1),
            cost_s_per_run=1.0,
            order=3,
        )


# --- (2b) the opt-in walk-down (orders="auto") -----------------------------------------


@needs_pict
def test_auto_takes_the_richest_order_the_budget_affords() -> None:
    """Opt in, and the old behaviour is back: 1 h buys pairwise, not a cut 3-wise."""
    plan = pict.plan(
        GO2_MODEL,
        budget=pict.Budget(wallclock_s=3600, repeats=3),
        cost_s_per_run=300.0,
        concurrency=4,
        orders="auto",
    )
    assert plan.requested_order == 2
    assert plan.cases == 14
    assert plan.truncated_from is None


@needs_pict
def test_auto_cuts_cases_not_repeats_when_the_budget_is_tight() -> None:
    """The design opinion, now scoped to this branch: statistics survive, coverage is
    spent. MEASURED flakiness 0.333 makes a single-draw verdict noise, so a budget the
    caller asked us to reduce against never takes repeats below the floor."""
    plan = pict.plan(
        GO2_MODEL,
        budget=pict.Budget(wallclock_s=1800, repeats=1),
        cost_s_per_run=300.0,
        concurrency=4,
        orders="auto",
    )
    assert plan.repeats == pict.MIN_REPEATS
    assert plan.cases < 14
    assert plan.truncated_from == 14
    assert plan.est_wallclock_s <= 1800


@needs_pict
def test_auto_clamps_an_order_the_space_cannot_hold() -> None:
    """A 2-axis space asked for 3-wise: give it all of the space, do not reject."""
    plan = pict.plan(
        "a: 1, 2\nb: 3, 4\n",
        budget=pict.Budget(wallclock_s=3600, repeats=1),
        cost_s_per_run=1.0,
        orders=[3],
    )
    assert plan.requested_order == 2


def test_plan_rejects_nonsense_arguments() -> None:
    """Programmer errors (not consumer input): loud ValueError, no PICT invocation."""
    budget = pict.Budget(wallclock_s=3600, repeats=1)
    with pytest.raises(ValueError, match="concurrency"):
        pict.plan(GO2_MODEL, budget=budget, cost_s_per_run=1.0, concurrency=0)
    with pytest.raises(ValueError, match="repeats"):
        pict.plan(GO2_MODEL, budget=pict.Budget(wallclock_s=1.0, repeats=0), cost_s_per_run=1.0)
    with pytest.raises(ValueError, match="cost_s_per_run"):
        pict.plan(GO2_MODEL, budget=budget, cost_s_per_run=0.0)
    with pytest.raises(ValueError, match="orders must not be empty"):
        pict.plan(GO2_MODEL, budget=budget, cost_s_per_run=1.0, orders=[])


# --- (3) truncation is honest ---------------------------------------------------------


@needs_pict
def test_prefix_coverage_is_monotone_and_ends_at_one() -> None:
    """Truncation only means something if PICT's greedy prefix really is a partial
    covering array. MEASURED curve: 6/14 = 76.0 %, 10/14 = 94.2 %, 14/14 = 100 %."""
    array = pict.generate(GO2_MODEL, order=2)
    curve = [pict.coverage_of_prefix(array, n, 2) for n in range(1, len(array) + 1)]
    assert curve == sorted(curve)
    assert curve[-1] == 1.0
    assert 0.70 < curve[5] < 0.80
    assert 0.93 < curve[9] < 0.96


@needs_pict
def test_truncated_plan_reports_the_coverage_it_actually_achieved() -> None:
    plan = pict.plan(
        GO2_MODEL,
        budget=pict.Budget(wallclock_s=1800, repeats=3),
        cost_s_per_run=300.0,
        concurrency=4,
    )
    assert plan.coverage == pict.coverage_of_prefix(
        pict.generate(GO2_MODEL, order=2), plan.cases, 2
    )
    assert plan.coverage < 1.0
    assert "TRUNCATED" in plan.summary()


@needs_pict
def test_max_cases_caps_the_suite_independently_of_time() -> None:
    plan = pict.plan(
        GO2_MODEL,
        budget=pict.Budget(wallclock_s=100 * 3600, repeats=3, max_cases=5),
        cost_s_per_run=300.0,
        concurrency=4,
    )
    assert plan.cases == 5
    assert plan.truncated_from is not None


# --- (4) the friendly errors PICT does not give ---------------------------------------


def test_parameter_after_constraint_is_located_by_line() -> None:
    """PICT says 'Missing opening bracket or misplaced keyword' with NO line."""
    with pytest.raises(pict.PictError) as exc:
        pict.validate_model(
            "a: 1, 2\n\nIF [a] = 1 THEN [a] = 1;\nb: 3, 4\n", source_path="space.pict"
        )
    assert exc.value.source_line == 4
    assert exc.value.source_path == "space.pict"
    assert "declared after the first constraint" in str(exc.value)


def test_duplicate_parameter_is_rejected_with_both_lines() -> None:
    with pytest.raises(pict.PictError) as exc:
        pict.validate_model("a: 1, 2\nb: 3\na: 5, 6\n", source_path="space.pict")
    assert exc.value.source_line == 3
    assert "line 1" in str(exc.value)


def test_constraint_on_an_undeclared_parameter_is_rejected() -> None:
    with pytest.raises(pict.PictError) as exc:
        pict.validate_model("a: 1, 2\n\nIF [a] = 1 THEN [ghost] = 2;\n")
    assert exc.value.source_line == 3
    assert "ghost" in str(exc.value)


def test_empty_model_is_rejected() -> None:
    with pytest.raises(pict.PictError, match="no parameters"):
        pict.validate_model("# nothing but a comment\n")


def test_a_parameter_with_no_values_is_rejected() -> None:
    with pytest.raises(pict.PictError, match="declares no values") as exc:
        pict.validate_model("a: 1\nb:  ,  \n")
    assert exc.value.source_line == 2


@pytest.mark.parametrize("name", ["2lighting", "light ing", "--lighting"])
def test_an_axis_name_that_is_not_a_usable_flag_is_rejected(name: str) -> None:
    """PICT accepts these happily; they explode as `--2lighting=dim` inside the GPU
    container, hours later. The model is where that is cheap to say."""
    with pytest.raises(pict.PictError, match="long-flag") as exc:
        pict.validate_model(f"speed: 0.2\n{name}: bright, dim\n", source_path="space.pict")
    assert exc.value.source_line == 2 and exc.value.source_path == "space.pict"


@pytest.mark.parametrize("name", sorted(pict.RESERVED_AXIS_NAMES))
def test_an_axis_named_after_the_scripts_own_help_is_rejected(name: str) -> None:
    """`--help=on` prints usage and exits 0 — a case that ran nothing, reported green."""
    with pytest.raises(pict.PictError, match="--help") as exc:
        pict.validate_model(f"{name}: on, off\n")
    assert exc.value.source_line == 1


def test_lines_that_are_neither_parameters_nor_constraints_are_ignored() -> None:
    """PICT tolerates stray text; the pre-check must not invent a rejection for it."""
    pict.validate_model("a: 1, 2\nnot a declaration\n")
    assert pict._declared_parameters("a: 1, 2\nnot a declaration\n") == ["a"]


def test_missing_binary_names_the_env_var() -> None:
    with pytest.raises(pict.PictError, match=pict.PICT_BIN_ENV):
        pict.resolve_binary("/nonexistent/pict-binary-that-is-not-there")


# --- (5) the shapes downstream depends on ---------------------------------------------


@needs_pict
def test_as_dicts_and_tsv_round_trip() -> None:
    """``as_dicts`` is what derive substitutes from; ``to_tsv`` is what /e seeds from."""
    array = pict.generate(GO2_MODEL, order=2)
    dicts = array.as_dicts()
    assert len(dicts) == len(array)
    assert set(dicts[0]) == set(array.parameters)
    lines = array.to_tsv().splitlines()
    assert lines[0].split("\t") == list(array.parameters)
    assert len(lines) == len(array) + 1


@needs_pict
def test_an_untruncated_plan_summary_states_full_coverage() -> None:
    plan = pict.plan(
        GO2_MODEL,
        budget=pict.Budget(wallclock_s=3600, repeats=1),
        cost_s_per_run=1.0,
    )
    assert "TRUNCATED" not in plan.summary()
    assert "100.0% coverage" in plan.summary()


@needs_pict
def test_coverage_of_a_whole_array_is_one_by_construction() -> None:
    """``coverage`` normalises an array against the combinations THAT ARRAY realises
    (constraints legitimately forbid the rest), so a complete array is 1.0 and the
    honest partial number is ``coverage_of_prefix``'s job."""
    assert pict.coverage(pict.generate(GO2_MODEL, order=2), 2) == 1.0


@pytest.mark.parametrize(
    "fn", [pict.coverage, lambda array, order: pict.coverage_of_prefix(array, 1, order)]
)
def test_coverage_of_a_space_narrower_than_the_order_is_one(fn) -> None:
    """One axis has no PAIRS to miss — 0/0 is full coverage, not a crash."""
    single = pict.CoveringArray(parameters=("a",), rows=(("1",),), order=2)
    assert fn(single, 2) == 1.0


@needs_pict
def test_a_model_pict_itself_rejects_is_located_by_line() -> None:
    """The shape validate_model deliberately leaves to PICT (a type mismatch): its
    terse prose becomes a located, friendly rejection."""
    model = (
        "speed: 0.2, 0.4\nlighting: bright, dim\n\n"
        'IF [speed] = "0.2" THEN [lighting] = bright;\n'
    )
    with pytest.raises(pict.PictError, match="Incorrect numeric value") as exc:
        pict.generate(model, source_path="space.pict")
    assert exc.value.source_line == 4 and exc.value.source_path == "space.pict"


def test_pict_output_that_is_not_a_table_is_rejected() -> None:
    """Both parse guards: no rows at all, and a row that is ragged because a value
    contained a TAB. Neither can be reproduced through the binary, so they are
    asserted against the parser directly."""
    with pytest.raises(pict.PictError, match="no rows"):
        pict._parse("\n", order=2, source_path=None)
    with pytest.raises(pict.PictError, match="3 values for 2 parameters"):
        pict._parse("a\tb\n1\t2\t3\n", order=2, source_path=None)


def test_a_silent_pict_rejection_still_says_something() -> None:
    """PICT is not obliged to print prose on a nonzero exit, and an unquotable
    complaint has no line to point at — neither may be invented."""
    proc = subprocess.CompletedProcess(args=["pict"], returncode=7, stdout=" \n", stderr="")
    assert pict._pict_message(proc) == "PICT exited 7 without a message."
    assert pict._locate("a: 1, 2\n", "") is None
