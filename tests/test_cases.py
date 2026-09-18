"""Case identity + expansion tests — contract/cases.py (pure, no binary, no docker).

What is pinned here is what a WRONG derivation costs, not the arithmetic:

* an id that moves when the model is reordered kills every baseline lookup, and an
  absent baseline SKIPS — the gate goes quiet instead of red;
* an argv that splits ``--axis value`` into two tokens lets a value beginning with
  ``-`` be read as the next flag;
* a seed that is random (or shared between repeats) makes a flake unreproducible (or
  makes N repeats one draw repeated N times);
* an expansion that is repeat-major loses a case's other repeats to budget truncation,
  and a half-sampled pass ratio is a fabricated regression.
"""

from __future__ import annotations

from cv_infra.contract.cases import argv_for, case_id_for, expand, seed_for
from cv_infra.contract.pict import CoveringArray

ARRAY = CoveringArray(
    parameters=("lighting", "speed"),
    rows=(("bright", "0.2"), ("dim", "0.4")),
    order=2,
)


# --- (1) identity ---------------------------------------------------------------------


def test_case_id_is_independent_of_axis_order_and_stable():
    first = case_id_for({"lighting": "dim", "speed": "0.2"})
    second = case_id_for({"speed": "0.2", "lighting": "dim"})

    assert first == second  # a reordered model must not orphan every baseline
    assert first.startswith("sha256:")
    assert first == case_id_for({"lighting": "dim", "speed": "0.2"})


def test_case_id_separates_different_assignments():
    assert case_id_for({"lighting": "dim"}) != case_id_for({"lighting": "bright"})


# --- (2) argv -------------------------------------------------------------------------


def test_argv_is_the_script_then_one_token_per_axis_in_model_order():
    argv = argv_for("verify/sim.py", {"lighting": "dim", "speed": "-0.2"})

    assert argv == ("verify/sim.py", "--lighting=dim", "--speed=-0.2")


# --- (3) seed -------------------------------------------------------------------------


def test_seed_is_derived_and_distinguishes_repeats():
    case_id = case_id_for({"lighting": "dim"})

    assert seed_for(case_id, 0) == seed_for(case_id, 0)  # a flake is re-runnable
    assert seed_for(case_id, 0) != seed_for(case_id, 1)  # repeats are not one draw
    assert 0 <= seed_for(case_id, 0) < 2**32


def test_seed_differs_across_cases():
    assert seed_for(case_id_for({"a": "1"}), 0) != seed_for(case_id_for({"a": "2"}), 0)


# --- (4) expansion --------------------------------------------------------------------


def test_expansion_is_case_major_so_truncation_loses_whole_cases():
    runs = expand(ARRAY, sim_script="verify/sim.py", repeats=2)

    assert [(run.case_index, run.repeat) for run in runs] == [(0, 0), (0, 1), (1, 0), (1, 1)]
    assert runs[0].case_id == runs[1].case_id != runs[2].case_id
    assert runs[0].axes == {"lighting": "bright", "speed": "0.2"}
    assert runs[2].argv == ("verify/sim.py", "--lighting=dim", "--speed=0.4")


def test_declared_repeats_are_honoured_verbatim():
    """The historical repeats FLOOR is gone: 1 means 1 (the report labels it, the plan
    does not overrule it)."""
    assert len(expand(ARRAY, sim_script="verify/sim.py", repeats=1)) == 2
