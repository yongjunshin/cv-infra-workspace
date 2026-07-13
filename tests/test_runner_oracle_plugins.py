"""CPU unit tests — loader-mediated engine composition + ``CV_ORACLE_PLUGIN_DIR``
consumption (D-1 (4), DoD-P3-04 runner side).

The engine composes from the REQUEST's criteria through the REAL M1 loader
(``cv_infra.oracles.base.load_oracle`` — never mocked, per the task's G-28
note): MVP names resolve via the ``cv_infra.oracles`` entry-point group and a
custom ``module:Class`` via the plugin dir the runner put on sys.path
(``insert_oracle_plugin_dir``). A load failure maps to the friendly exit-2
path (defence-in-depth — the first rejection is admit's), with no traceback
and no result.json. GPU wiring stays out of scope (T-GPU leg).
"""

import importlib
import json
import sys
from pathlib import Path

import pytest

from cv_infra.oracles.no_collision import NoCollisionOracle
from cv_infra.oracles.reached_goal import ReachedGoalOracle
from cv_infra.runner import main, sim_runtime
from cv_infra.runner.evaluate import EvaluationEngine
from cv_infra.runner.telemetry import PoseSample, TelemetryRecord

# The plugin dir = the fixtures dir itself: the custom .py sits NEXT TO the
# scenario YAML, exactly the D-1 (a) consumer layout (scenario-adjacent module).
PLUGIN_DIR = Path(__file__).parent / "fixtures"
PLUGIN_MODULE = "custom_oracle_plugin"
CUSTOM_ORACLE = f"{PLUGIN_MODULE}:ParamVerdictOracle"

REACHED_GOAL = {"oracle": "reached_goal", "params": {"position_tolerance_m": 0.1}}
NO_COLLISION = {"oracle": "no_collision", "params": {"chassis_path": "/World/carter/chassis"}}


def _spec(criteria: list[dict]) -> dict:
    """A minimal canonical JOB_SPEC (Phase-2 wire) carrying ``criteria``."""
    return {
        "job_id": "job-0001",
        "scenario": {
            "scene": "omniverse://assets/warehouse.usd",
            "robot": "omniverse://assets/nova_carter_ros.usd",
            "goal": {"x": 3.0, "y": 0.0, "yaw": 0.0},
            "seed": 7,
            "timeout_s": 120.0,
        },
        "sut_image_ref": "carter-sut:p3",
        "acceptance_criteria": criteria,
    }


def _request(criteria: list[dict]):
    request, _ = main.parse_request(_spec(criteria))
    return request


def _record() -> TelemetryRecord:
    """GT trajectory (0,0,0)->(3,0,0) over t=0..3 — reaches the spec goal."""
    samples = [
        PoseSample(
            sim_time_s=float(i),
            position=(float(i), 0.0, 0.0),
            orientation_wxyz=(1.0, 0.0, 0.0, 0.0),
        )
        for i in range(4)
    ]
    return TelemetryRecord(gt_pose_samples=samples, contact_events=[])


@pytest.fixture()
def plugin_import_state(monkeypatch):
    """Isolate sys.path + the fixture-module cache (order-independent tests).

    ``insert_oracle_plugin_dir`` mutates sys.path in place, so bind a copy for
    the test; pop the plugin module before AND after so a cached import can
    never stand in for the sys.path insertion (G-29 spirit: an import that
    succeeds from cache is not evidence the plugin dir works).
    """
    monkeypatch.setattr(sys, "path", list(sys.path))
    sys.modules.pop(PLUGIN_MODULE, None)
    yield
    sys.modules.pop(PLUGIN_MODULE, None)


# --------------------------------------------------------------------------- #
# (a) criteria -> engine composition goes through the REAL load_oracle.
# --------------------------------------------------------------------------- #
def test_build_oracles_resolves_mvp_names_via_entry_points():
    # The MVP names carry no ":" -> the loader's entry-point branch resolves
    # them from the pyproject ``cv_infra.oracles`` group (real metadata lookup).
    oracles = main.build_oracles(_request([REACHED_GOAL, NO_COLLISION]))
    assert [type(o) for o in oracles] == [ReachedGoalOracle, NoCollisionOracle]


def test_build_oracles_follows_criteria_order_and_selection():
    # Composition follows the request (no hardcoded pair): one criterion ->
    # one oracle, in criteria order.
    oracles = main.build_oracles(_request([NO_COLLISION, REACHED_GOAL]))
    assert [type(o) for o in oracles] == [NoCollisionOracle, ReachedGoalOracle]
    (only,) = main.build_oracles(_request([REACHED_GOAL]))
    assert type(only) is ReachedGoalOracle


def test_build_oracles_duplicate_criterion_loads_two_instances():
    # D-1: two criteria naming the same oracle = two loads/instances — the
    # deliberately-simple no-caching rule.
    oracles = main.build_oracles(_request([REACHED_GOAL, REACHED_GOAL]))
    assert [type(o) for o in oracles] == [ReachedGoalOracle, ReachedGoalOracle]
    assert oracles[0] is not oracles[1]


def test_build_oracles_without_request_composes_registered_builtins():
    # QA fixture-canonical-guard form: no request -> the entry-point registry
    # (still load_oracle-mediated — no hardcoded import list).
    oracles = main.build_oracles()
    assert {type(o) for o in oracles} == {ReachedGoalOracle, NoCollisionOracle}


# --------------------------------------------------------------------------- #
# (b) custom oracle via CV_ORACLE_PLUGIN_DIR -> loaded, evaluated, in verdict.
# --------------------------------------------------------------------------- #
def test_env_name_is_the_verbatim_cross_team_contract():
    # G-17 drift guard: M3 injects this exact name (D-1 (3)); one character of
    # drift silently disables custom oracles.
    assert main.ORACLE_PLUGIN_DIR_ENV == "CV_ORACLE_PLUGIN_DIR"


def test_custom_oracle_loads_from_plugin_dir_and_steers_verdict(plugin_import_state):
    request = _request(
        [REACHED_GOAL, {"oracle": CUSTOM_ORACLE, "params": {"custom_should_pass": False}}]
    )
    # Negative control (G-26 spirit): without the insertion the module must NOT
    # be importable — proving the sys.path insert is what turns the feature on.
    with pytest.raises(main.BadJobSpec):
        main.build_oracles(request)

    inserted = main.insert_oracle_plugin_dir({"CV_ORACLE_PLUGIN_DIR": str(PLUGIN_DIR)})
    assert inserted == str(PLUGIN_DIR)
    assert sys.path[0] == str(PLUGIN_DIR)

    verdict, outcomes = EvaluationEngine(main.build_oracles(request)).evaluate(
        _record(), main.criteria_view(request)
    )
    # reached_goal passes on this record, so the FAIL can only come from the
    # plugin oracle -> its outcome demonstrably reaches the verdict fold.
    assert verdict == "fail"
    assert [(o.name, o.passed) for o in outcomes] == [
        ("reached_goal", True),
        ("param_verdict", False),
    ]


def test_custom_oracle_pass_param_yields_pass_verdict(plugin_import_state):
    main.insert_oracle_plugin_dir({"CV_ORACLE_PLUGIN_DIR": str(PLUGIN_DIR)})
    request = _request(
        [REACHED_GOAL, {"oracle": CUSTOM_ORACLE, "params": {"custom_should_pass": True}}]
    )
    verdict, outcomes = EvaluationEngine(main.build_oracles(request)).evaluate(
        _record(), main.criteria_view(request)
    )
    assert verdict == "pass"
    assert outcomes[1].name == "param_verdict" and outcomes[1].passed is True


# --------------------------------------------------------------------------- #
# (c) load failure -> friendly error + exit 2, no traceback, no result.json.
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize(
    "oracle_ref",
    [
        "no_such_plugin_module:Nope",  # explicit-path form, module absent
        "definitely_not_registered",  # entry-point form, name not in the group
    ],
)
def test_unknown_oracle_is_friendly_exit_2(oracle_ref, tmp_path, capsys):
    # End-to-end through main(): composition is pre-sim, so a load failure is
    # exit 2 (usage) on CPU — no Isaac import/boot, no result.json (bad input
    # is not a Result), and the M1 friendly prose instead of a traceback.
    spec = _spec([REACHED_GOAL, {"oracle": oracle_ref, "params": {}}])
    env = {"JOB_SPEC": json.dumps(spec), "RESULT_OUT": str(tmp_path)}
    assert main.main(env) == main.EXIT_USAGE
    assert not (tmp_path / "result.json").exists()
    err = capsys.readouterr().err
    assert oracle_ref.partition(":")[0] in err  # names the offending reference
    assert "Traceback" not in err


# --------------------------------------------------------------------------- #
# (d) env unset -> sys.path untouched.
# --------------------------------------------------------------------------- #
def test_plugin_dir_env_unset_or_empty_leaves_syspath_untouched(plugin_import_state):
    before = list(sys.path)
    assert main.insert_oracle_plugin_dir({}) is None
    assert sys.path == before
    # "" counts as unset — inserting it would put the CWD on sys.path (G-26's
    # empty-env variant: silently importing from the wrong place).
    assert main.insert_oracle_plugin_dir({"CV_ORACLE_PLUGIN_DIR": ""}) is None
    assert sys.path == before


# --------------------------------------------------------------------------- #
# (e) D-1 2026-07-13 (b): validate_params runs pre-boot; a raise joins exit 2.
# --------------------------------------------------------------------------- #
class _BootReached(Exception):
    """Sentinel: constructing SimRuntime means the params gate failed to stop pre-boot."""


@pytest.fixture()
def boot_sentinel(monkeypatch):
    """Any attempt to build the sim runtime raises LOUDLY (pre-boot proof).

    ``run()`` imports SimRuntime from the module at call time, so patching the
    module attribute intercepts the construction — reaching it on the params-
    error path would surface as an unhandled ``_BootReached``, never a silent
    pass.
    """

    def _explode(*_args, **_kwargs):
        raise _BootReached

    monkeypatch.setattr(sim_runtime, "SimRuntime", _explode)


def test_builtin_params_error_joins_contract_error_path():
    # ① unit: the gate runs EVERY composed oracle against the merged view (the
    # first oracle passes, the SECOND raises) and joins the raise onto
    # BadJobSpec — the existing exit-2 exception — naming oracle + param.
    # Real built-ins, real validate_params, no mocks. (On the canonical wire
    # the M1 schema already requires chassis_path, so for built-ins this gate
    # is defence-in-depth; the free-form custom params below are its real bite.)
    oracles = [ReachedGoalOracle(), NoCollisionOracle()]
    view = {"goal_position": [3.0, 0.0, 0.0]}  # satisfies reached_goal, lacks chassis_path
    with pytest.raises(main.BadJobSpec) as excinfo:
        main.validate_oracle_params(oracles, view)
    msg = str(excinfo.value)
    assert "no_collision" in msg and "chassis_path" in msg


def test_params_error_rejected_pre_boot_exit_2(
    plugin_import_state, boot_sentinel, monkeypatch, tmp_path, capsys
):
    # ①+③ e2e through main(): a CUSTOM plugin oracle's validate_params (the
    # M1 schema cannot know its free-form params — CustomCriterion) rejects ->
    # friendly exit 2 BEFORE any sim construction (boot_sentinel would raise),
    # no result.json, no traceback. The recorded call proves the plugin's gate
    # runs on the SAME uniform path as the built-ins.
    calls = []

    def _reject(self, criteria):
        calls.append(criteria)
        raise ValueError("params must carry custom_should_pass")

    main.insert_oracle_plugin_dir({"CV_ORACLE_PLUGIN_DIR": str(PLUGIN_DIR)})
    plugin = importlib.import_module(PLUGIN_MODULE)
    monkeypatch.setattr(plugin.ParamVerdictOracle, "validate_params", _reject)

    spec = _spec([REACHED_GOAL, {"oracle": CUSTOM_ORACLE, "params": {}}])
    env = {
        "JOB_SPEC": json.dumps(spec),
        "RESULT_OUT": str(tmp_path),
        "CV_ORACLE_PLUGIN_DIR": str(PLUGIN_DIR),
    }
    assert main.main(env) == main.EXIT_USAGE
    assert len(calls) == 1  # the plugin's own gate WAS invoked (and rejected)
    assert not (tmp_path / "result.json").exists()  # bad input is not a Result
    err = capsys.readouterr().err
    assert "param_verdict" in err and "custom_should_pass" in err
    assert "Traceback" not in err


def test_valid_params_pass_the_gate_then_flow_reaches_boot(
    plugin_import_state, boot_sentinel, monkeypatch, tmp_path
):
    # ② positive control: with valid params the gate runs (the spy records the
    # merged criteria view it was handed) and RETURNS, and the flow proceeds to
    # sim construction — proving the exit 2 above is caused by validation
    # alone, not something upstream (G-07 spirit: non-vacuous negative).
    calls = []

    def _record_only(self, criteria):
        calls.append(criteria)

    main.insert_oracle_plugin_dir({"CV_ORACLE_PLUGIN_DIR": str(PLUGIN_DIR)})
    plugin = importlib.import_module(PLUGIN_MODULE)
    monkeypatch.setattr(plugin.ParamVerdictOracle, "validate_params", _record_only)

    spec = _spec(
        [
            REACHED_GOAL,
            NO_COLLISION,
            {"oracle": CUSTOM_ORACLE, "params": {"custom_should_pass": True}},
        ]
    )
    env = {
        "JOB_SPEC": json.dumps(spec),
        "RESULT_OUT": str(tmp_path),
        "CV_ORACLE_PLUGIN_DIR": str(PLUGIN_DIR),
    }
    with pytest.raises(_BootReached):
        main.main(env)
    (view,) = calls  # called exactly once, with the merged criteria view
    assert view["goal_position"] == [3.0, 0.0, 0.0]
    assert view["timeout_s"] == 120.0
