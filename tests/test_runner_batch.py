"""CPU tests for the p6 batch carrier (``cv_infra.runner.batch``) — Isaac-free surface.

The carrier's GPU loop is a ``# pragma: no cover`` body proven on the workstation
(W1/W2). Everything it DECIDES before touching a GPU is here: the batch wire
(``JOB_SPEC_BATCH`` -> ``JobSpecBatch``), pre-boot admission (per-spec validation +
the uniformity the boot-once design depends on), the ``results/<i>`` layout that
IS the M1 wire invariant, the summary heartbeat, the carrier exit mapping, and the
re-exec argv — the one that made the first C-2 attempt die in 2.4 s by silently
re-execing into the SINGLE-job entrypoint.

Two structural guards close the loop on the parts a unit test cannot run:
``restage`` must not contain the hard-reset spelling it replaced, and ``run`` must
not close a vendor object on its terminal path (G-62).
"""

import ast
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from cv_infra.contract.job_batch import BATCH_SUMMARY_SCHEMA, JOB_SPEC_BATCH_ENV, JobSpecBatch
from cv_infra.runner import batch, ros_bridge, sim_runtime

CHASSIS = "/World/carter/chassis"


def _spec(index: int = 0, **scenario_overrides) -> dict:
    """One canonical JOB_SPEC — the materialized sample ``index`` of a request."""
    scenario = {
        "scene": "omniverse://assets/warehouse.usd",
        "robot": "omniverse://assets/nova_carter_ros.usd",
        "goal": {"x": 3.0, "y": -1.5, "yaw": 0.0},
        "initial_pose": {"x": 0.0, "y": 0.0, "yaw": 0.0},
        "debug_obstacle": {"x": 1.0, "y": 0.5},
        "seed": 7,
        "timeout_s": 120.0,
        # The platform's own materialization stamp: the runner must ACCEPT it
        # (the loader rejects a SUBMITTED one — that gate is on the admit plane).
        "derivation": {"version": "cv-derive/1", "index": index},
    }
    scenario.update(scenario_overrides)
    return {
        "job_id": f"req-0001:{index}",
        "scenario": scenario,
        "sut_image_ref": "carter-sut:p2",
        "interface": {"type": "ros2", "adapter_config": {}},
        "acceptance_criteria": [
            {"oracle": "reached_goal", "params": {"position_tolerance_m": 0.3}},
            {"oracle": "no_collision", "params": {"chassis_path": CHASSIS}},
        ],
    }


def _batch_doc(n: int = 2, request_id: str = "req-0001") -> dict:
    return {"specs": [_spec(i) for i in range(n)], "request_id": request_id}


def _batch_file(tmp_path: Path, doc: dict | list | str) -> Path:
    path = tmp_path / "job_spec_batch.json"
    path.write_text(doc if isinstance(doc, str) else json.dumps(doc), encoding="utf-8")
    return path


def _env(tmp_path: Path, doc: dict | None = None) -> dict:
    return {
        JOB_SPEC_BATCH_ENV: str(_batch_file(tmp_path, _batch_doc() if doc is None else doc)),
        "RESULT_OUT": str(tmp_path / "out"),
    }


# --------------------------------------------------------------------------- #
# Isaac-free import (admission must cost 0 GPU seconds — it cannot if importing
# the module already needs the bundled interpreter).
# --------------------------------------------------------------------------- #
def test_batch_module_imports_with_no_isaac_ros_or_cv2_present():
    """A fresh interpreter importing the carrier pulls in NO vendor runtime.

    Child process on purpose: this module is already imported in the test
    session, so an in-process check would measure nothing.
    """
    code = (
        "import sys; import cv_infra.runner.batch\n"
        "roots = {'isaacsim', 'omni', 'rclpy', 'carb', 'pxr', 'cv2', 'numpy', "
        "'geometry_msgs', 'nav2_msgs'}\n"
        "print(sorted(m for m in sys.modules if m.split('.')[0] in roots))\n"
    )
    proc = subprocess.run(
        [sys.executable, "-c", code],
        env={k: v for k, v in os.environ.items() if k != "PYTHONPATH"},
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert proc.returncode == 0, proc.stderr
    assert proc.stdout.strip() == "[]"


def test_batch_module_literal_matches_the_real_import_path():
    """``BATCH_MODULE`` cannot be derived from ``__name__`` (it is ``__main__``
    under ``-m``), so the literal is pinned to the module a rename would move."""
    assert batch.BATCH_MODULE == batch.__name__
    assert batch.reexec_argv() == [sys.executable, "-m", "cv_infra.runner.batch"]


# --------------------------------------------------------------------------- #
# JOB_SPEC_BATCH / RESULT_OUT wire.
# --------------------------------------------------------------------------- #
def test_load_batch_reads_the_wrapper_document(tmp_path):
    loaded = batch.load_batch({JOB_SPEC_BATCH_ENV: str(_batch_file(tmp_path, _batch_doc(3)))})
    assert isinstance(loaded, JobSpecBatch)
    assert loaded.request_id == "req-0001"
    assert [s["job_id"] for s in loaded.specs] == ["req-0001:0", "req-0001:1", "req-0001:2"]


@pytest.mark.parametrize("value", [None, ""])
def test_load_batch_requires_the_env(value):
    env = {} if value is None else {JOB_SPEC_BATCH_ENV: value}
    with pytest.raises(batch.BadJobSpec, match=JOB_SPEC_BATCH_ENV):
        batch.load_batch(env)


def test_load_batch_rejects_a_path_that_is_not_a_file(tmp_path):
    with pytest.raises(batch.BadJobSpec, match="not a readable file"):
        batch.load_batch({JOB_SPEC_BATCH_ENV: str(tmp_path / "nope.json")})


def test_load_batch_rejects_inline_json_because_the_wire_is_a_path(tmp_path):
    # A path, never inline JSON: n specs do not fit in an env value.
    with pytest.raises(batch.BadJobSpec, match="not a readable file"):
        batch.load_batch({JOB_SPEC_BATCH_ENV: json.dumps(_batch_doc(1))})


def test_load_batch_rejects_invalid_json(tmp_path):
    with pytest.raises(batch.BadJobSpec, match="not valid JSON"):
        batch.load_batch({JOB_SPEC_BATCH_ENV: str(_batch_file(tmp_path, "{not json"))})


def test_load_batch_rejects_a_bare_array(tmp_path):
    """The wire is a WRAPPER document — a bare array is the overload M1 rejected."""
    path = _batch_file(tmp_path, [_spec(0)])
    with pytest.raises(batch.BadJobSpec, match="must decode to a JSON object"):
        batch.load_batch({JOB_SPEC_BATCH_ENV: str(path)})


def test_load_batch_rejects_an_empty_batch(tmp_path):
    """Booting Isaac to run nothing is the most expensive way to find a producer bug."""
    path = _batch_file(tmp_path, {"specs": []})
    with pytest.raises(batch.BadJobSpec, match="specs"):
        batch.load_batch({JOB_SPEC_BATCH_ENV: str(path)})


def test_load_batch_rejects_an_unknown_wrapper_key(tmp_path):
    path = _batch_file(tmp_path, {"specs": [_spec(0)], "repeats": 3})
    with pytest.raises(batch.BadJobSpec) as excinfo:
        batch.load_batch({JOB_SPEC_BATCH_ENV: str(path)})
    assert "repeats" in str(excinfo.value)


def test_load_batch_rejects_non_object_specs(tmp_path):
    path = _batch_file(tmp_path, {"specs": ["job-1"]})
    with pytest.raises(batch.BadJobSpec):
        batch.load_batch({JOB_SPEC_BATCH_ENV: str(path)})


def test_out_root_is_a_directory(tmp_path):
    assert batch.resolve_out_root({"RESULT_OUT": str(tmp_path)}) == tmp_path


def test_out_root_requires_the_env():
    with pytest.raises(batch.BadJobSpec, match="RESULT_OUT"):
        batch.resolve_out_root({})


def test_out_root_rejects_a_result_json_path(tmp_path):
    """The single-job seam accepts an explicit result.json; a carrier cannot —
    it writes n results plus a summary, so a file path is a producer bug."""
    with pytest.raises(batch.BadJobSpec, match="output DIRECTORY"):
        batch.resolve_out_root({"RESULT_OUT": str(tmp_path / "result.json")})


def test_iteration_paths_render_the_wire_invariant(tmp_path):
    # specs[i] <-> results/<i>/ <-> repeat_index i (contract.job_batch).
    assert batch.iteration_dir(tmp_path, 3) == tmp_path / "results" / "3"
    assert batch.iteration_result_path(tmp_path, 0) == tmp_path / "results" / "0" / "result.json"


def test_settle_budget_defaults_and_overrides():
    assert batch.realign_settle_s({}) == batch.DEFAULT_REALIGN_SETTLE_S
    assert batch.realign_settle_s({batch.REALIGN_SETTLE_ENV: "5.5"}) == 5.5
    with pytest.raises(batch.BadJobSpec, match="sim-seconds"):
        batch.realign_settle_s({batch.REALIGN_SETTLE_ENV: "soon"})


# --------------------------------------------------------------------------- #
# Pre-boot admission (0 GPU seconds).
# --------------------------------------------------------------------------- #
def test_admit_specs_returns_one_parsed_spec_per_index():
    parsed = batch.admit_specs(JobSpecBatch.model_validate(_batch_doc(3)))
    assert [p.index for p in parsed] == [0, 1, 2]
    assert [p.job_id for p in parsed] == ["req-0001:0", "req-0001:1", "req-0001:2"]
    assert parsed[0].criteria["goal_position"] == [3.0, -1.5, 0.0]
    names = [type(o).__name__ for o in parsed[0].oracles]
    assert names == ["ReachedGoalOracle", "NoCollisionOracle"]


def test_admission_imports_no_vendor_runtime():
    """The 0-GPU-seconds promise, measured rather than asserted in prose."""
    code = (
        "import json, sys\n"
        "from cv_infra.contract.job_batch import JobSpecBatch\n"
        "from cv_infra.runner.batch import admit_specs\n"
        f"admit_specs(JobSpecBatch.model_validate({_batch_doc(3)!r}))\n"
        "roots = {'isaacsim', 'omni', 'rclpy', 'carb', 'pxr', 'cv2', 'numpy'}\n"
        "print(sorted(m for m in sys.modules if m.split('.')[0] in roots))\n"
    )
    proc = subprocess.run(
        [sys.executable, "-c", code],
        env={k: v for k, v in os.environ.items() if k != "PYTHONPATH"},
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert proc.returncode == 0, proc.stderr
    assert proc.stdout.strip() == "[]"


def test_admit_specs_names_the_failing_index():
    """A spec 8 that only fails after 7 missions is the worst time to find out —
    and when it fails the message must say WHICH sample (M3's only handle)."""
    doc = _batch_doc(3)
    doc["specs"][2]["scenario"].pop("goal")
    with pytest.raises(batch.BadJobSpec, match=r"batch spec 2:"):
        batch.admit_specs(JobSpecBatch.model_validate(doc))


def test_admit_specs_requires_a_job_id_per_spec():
    doc = _batch_doc(2)
    doc["specs"][1].pop("job_id")
    with pytest.raises(batch.BadJobSpec, match=r"batch spec 1:.*job_id"):
        batch.admit_specs(JobSpecBatch.model_validate(doc))


def test_admit_specs_rejects_a_duplicate_job_id():
    """Two samples sharing an id would overwrite each other in M3's store."""
    doc = _batch_doc(3)
    doc["specs"][2]["job_id"] = doc["specs"][0]["job_id"]
    with pytest.raises(batch.BadJobSpec, match=r"batch spec 2:.*already used by spec 0"):
        batch.admit_specs(JobSpecBatch.model_validate(doc))


def test_admit_specs_rejects_an_unmaterialized_distribution():
    """The p6 §0-5 leak check reaches the carrier too (one parse_request for both
    entrypoints): a distribution here means the platform dispatched a template."""
    doc = _batch_doc(2)
    doc["specs"][1]["scenario"]["goal"]["x"] = {"uniform": [-6.5, -5.5]}
    with pytest.raises(batch.BadJobSpec, match=r"batch spec 1:.*scenario.goal.x"):
        batch.admit_specs(JobSpecBatch.model_validate(doc))


@pytest.mark.parametrize(
    ("label", "mutate"),
    [
        (
            "interface.adapter_config",
            lambda s: s["interface"].__setitem__("adapter_config", {"ros_domain_id": 7}),
        ),
        ("sut.image_ref", lambda s: s.__setitem__("sut_image_ref", "other-sut:p2")),
        ("scenario.scene", lambda s: s["scenario"].__setitem__("scene", "omniverse://other.usd")),
        ("scenario.robot", lambda s: s["scenario"].__setitem__("robot", "omniverse://other.usd")),
        (
            "execution_settings.fixed_dt",
            lambda s: s.__setitem__("execution_settings", {"fixed_dt": 0.02}),
        ),
        ("scenario.debug_obstacle declared", lambda s: s["scenario"].pop("debug_obstacle")),
        (
            "criteria.chassis_path",
            lambda s: s["acceptance_criteria"][1]["params"].__setitem__(
                "chassis_path", "/World/other/chassis"
            ),
        ),
    ],
)
def test_admit_specs_rejects_a_batch_that_disagrees_on_what_the_carrier_does_once(label, mutate):
    """One carrier boots ONE world and wires the ROS side ONCE.

    A spec that disagrees would be run against sample 0's world and judged as if
    it were its own — a silently WRONG verdict, which is why this is a pre-boot
    rejection (0 GPU seconds) and not a runtime surprise.
    """
    doc = _batch_doc(3)
    mutate(doc["specs"][1])
    with pytest.raises(batch.BadJobSpec) as excinfo:
        batch.admit_specs(JobSpecBatch.model_validate(doc))
    message = str(excinfo.value)
    assert "batch spec 1:" in message and label in message


def test_admit_specs_accepts_the_axes_that_are_SUPPOSED_to_vary():
    """Positive control for the guard above: the randomized axes must NOT trip it
    (a uniformity check that rejects everything would also pass every test above)."""
    doc = _batch_doc(3)
    for i, spec in enumerate(doc["specs"]):
        spec["scenario"]["goal"] = {"x": 3.0 + i, "y": -1.5 - i, "yaw": 0.1 * i}
        spec["scenario"]["initial_pose"] = {"x": 0.1 * i, "y": 0.2 * i, "yaw": 0.3 * i}
        spec["scenario"]["debug_obstacle"] = {"x": 1.0 + i, "y": 0.5 + i}
    parsed = batch.admit_specs(JobSpecBatch.model_validate(doc))
    assert [p.criteria["goal_position"][0] for p in parsed] == [3.0, 4.0, 5.0]


# --------------------------------------------------------------------------- #
# Sim-time monotonicity across the iteration boundary (the soft-restage property).
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize(
    ("previous_end", "start", "expected"),
    [
        (None, 3.6, True),  # first sample: nothing to compare against
        (3.6, 3.6, True),  # a boundary that costs no sim time is still forward
        (3.6, 85.7, True),  # soft restage: the timeline never stopped
        (85.7, 0.05, False),  # hard reset rewound the clock (p6c2 measured 23/23)
    ],
)
def test_sim_time_advanced(previous_end, start, expected):
    assert batch.sim_time_advanced(previous_end, start) is expected


# --------------------------------------------------------------------------- #
# batch_summary.json — the carrier's heartbeat.
# --------------------------------------------------------------------------- #
def test_summary_starts_with_the_schema_handle_and_the_batch_size(tmp_path):
    summary = batch.BatchSummary(tmp_path, "req-0001", 5, started_at=1000.0)
    path = summary.flush()
    doc = json.loads(path.read_text(encoding="utf-8"))
    assert path == tmp_path / "batch_summary.json"
    assert doc["schema"] == BATCH_SUMMARY_SCHEMA
    assert doc["request_id"] == "req-0001"
    assert doc["n"] == 5
    assert doc["started_at"] == 1000.0
    assert doc["iterations"] == [] and doc["finished_at"] is None and doc["error"] is None


def test_summary_is_flushed_after_every_iteration(tmp_path):
    """ "Did sample i run?" must be answerable when the carrier dies at sample i+1,
    so the file on disk is re-read after each add — not at the end."""
    summary = batch.BatchSummary(tmp_path, "req-0001", 3)
    for index in range(3):
        summary.add_iteration({"index": index, "job_id": f"req-0001:{index}", "verdict": "pass"})
        on_disk = json.loads((tmp_path / "batch_summary.json").read_text(encoding="utf-8"))
        assert [i["index"] for i in on_disk["iterations"]] == list(range(index + 1))


def test_summary_flush_leaves_no_temp_file_behind(tmp_path):
    """Atomic by REUSE (``main.write_result``) — one definition of 'atomic write'."""
    summary = batch.BatchSummary(tmp_path, None, 1)
    summary.add_iteration({"index": 0})
    summary.finish()
    assert sorted(p.name for p in tmp_path.iterdir()) == ["batch_summary.json"]


def test_summary_finish_records_the_reason_a_batch_stopped(tmp_path):
    summary = batch.BatchSummary(tmp_path, "req-0001", 4)
    summary.add_iteration({"index": 0, "verdict": "pass"})
    summary.finish(error="SUT readiness barrier timed out at sample 1", finished_at=2000.0)
    doc = json.loads((tmp_path / "batch_summary.json").read_text(encoding="utf-8"))
    assert doc["finished_at"] == 2000.0
    assert "readiness" in doc["error"]
    # The completed sample keeps its verdict — a later death cannot retract it.
    assert doc["iterations"] == [{"index": 0, "verdict": "pass"}]


# --------------------------------------------------------------------------- #
# The re-exec argv — the p6c1 killer.
# --------------------------------------------------------------------------- #
def test_reexec_uses_the_batch_entrypoint_not_mains():
    calls: list = []
    bootstrap = ros_bridge.BridgeBootstrap(
        jazzy_root="/isaac-sim/exts/isaacsim.ros2.bridge/jazzy",
        ros_distro_defaulted=False,
        rmw_defaulted=False,
        ld_path_prepended=True,
        rclpy_site_added=True,
    )
    ros_bridge.reexec_for_bridge_lib(
        bootstrap, argv=batch.reexec_argv(), execv=lambda path, args: calls.append((path, args))
    )
    assert calls == [(sys.executable, [sys.executable, "-m", "cv_infra.runner.batch"])]


# --------------------------------------------------------------------------- #
# The carrier's own exit codes (real process boundary where it is Isaac-free).
# --------------------------------------------------------------------------- #
def _fake_isaac_root(tmp_path: Path) -> Path:
    """An ISAAC_PATH whose jazzy ext exists — enough to make bootstrap PREPEND
    LD_LIBRARY_PATH, which is the only condition that triggers the re-exec."""
    jazzy = tmp_path / "isaac-sim" / "exts" / "isaacsim.ros2.bridge" / "jazzy"
    (jazzy / "lib").mkdir(parents=True)
    (jazzy / "rclpy").mkdir()
    return tmp_path / "isaac-sim"


def _run_entrypoint(tmp_path: Path, env_extra: dict) -> subprocess.CompletedProcess:
    env = {
        k: v
        for k, v in os.environ.items()
        if k not in ("PYTHONPATH", "ACCEPT_EULA", "ISAAC_PATH", "LD_LIBRARY_PATH")
    }
    env.update(env_extra)
    return subprocess.run(
        [sys.executable, "-m", "cv_infra.runner.batch"],
        env=env,
        capture_output=True,
        text=True,
        timeout=120,
    )


def test_entrypoint_bad_batch_exits_2(tmp_path):
    proc = _run_entrypoint(
        tmp_path,
        {
            JOB_SPEC_BATCH_ENV: str(_batch_file(tmp_path, "{not json")),
            "RESULT_OUT": str(tmp_path / "out"),
        },
    )
    assert proc.returncode == batch.EXIT_USAGE
    assert "bad batch spec" in proc.stderr


def test_entrypoint_without_eula_consent_exits_3(tmp_path):
    """Positive control for the arm above: a VALID batch gets past admission and
    dies at the boot guard (NEG-2), i.e. the parse is not what stopped it."""
    proc = _run_entrypoint(tmp_path, _env(tmp_path))
    assert proc.returncode == batch.EXIT_PLATFORM
    assert "EULA not accepted" in proc.stderr
    assert "batch admitted: 2 sample(s)" in proc.stdout


def test_entrypoint_reexec_lands_back_in_the_batch_module(tmp_path):
    """The re-exec argv, measured at the process boundary.

    With a jazzy ext present the boot glue prepends LD_LIBRARY_PATH and re-execs
    ONCE. If it re-exec'd into ``cv_infra.runner.main`` (the parameter default),
    that process would find no ``JOB_SPEC`` and exit 2 — so a 3 here, with the
    EULA message, is proof the carrier re-entered ITSELF and reached its own boot.
    """
    env = _env(tmp_path)
    env["ISAAC_PATH"] = str(_fake_isaac_root(tmp_path))
    proc = _run_entrypoint(tmp_path, env)
    assert proc.returncode == batch.EXIT_PLATFORM, proc.stderr
    assert "EULA not accepted" in proc.stderr
    assert "JOB_SPEC is required" not in proc.stderr  # = the single-job entrypoint's death
    assert proc.stdout.count("batch admitted:") == 2  # pre-exec + post-exec process


def test_main_maps_a_bad_batch_to_usage_in_process():
    assert batch.main({}) == batch.EXIT_USAGE


def test_summary_is_written_before_isaac_is_touched(tmp_path):
    """The heartbeat exists even for a carrier that dies at the EULA guard: M3 can
    tell "the container started and admitted n" from "it never got that far"."""
    _run_entrypoint(tmp_path, _env(tmp_path))
    doc = json.loads((tmp_path / "out" / "batch_summary.json").read_text(encoding="utf-8"))
    assert doc["schema"] == BATCH_SUMMARY_SCHEMA and doc["n"] == 2
    assert doc["iterations"] == []  # nothing ran -> M3 charges every slot (P5-13)


# --------------------------------------------------------------------------- #
# Structure: the two things a unit test cannot run must still be guarded.
# --------------------------------------------------------------------------- #
def _method(source: str, class_name: str, method_name: str) -> ast.FunctionDef:
    tree = ast.parse(source)
    for node in ast.walk(tree):
        if isinstance(node, ast.ClassDef) and node.name == class_name:
            for child in node.body:
                if isinstance(child, ast.FunctionDef) and child.name == method_name:
                    return child
    raise AssertionError(f"{class_name}.{method_name} not found")


def _called_names(node: ast.AST) -> list[str]:
    names = []
    for call in ast.walk(node):
        if isinstance(call, ast.Call):
            names.append(
                call.func.attr if isinstance(call.func, ast.Attribute) else ast.unparse(call.func)
            )
    return names


@pytest.mark.parametrize("forbidden", ["stop", "RemovePrim", "FixedCuboid", "close"])
def test_restage_never_falls_back_to_the_hard_reset_spelling(forbidden):
    """``restage`` is the p6c2 measurement's whole product, so its body is pinned.

    ``stop()``/``RemovePrim``/``FixedCuboid`` ARE the hard path: they cycle the
    physics simulation views (+4.96 MiB/iteration, flat only once removed) and
    leave two orphan material prims per respawn. ``close()`` would end the
    process with status 0 mid-batch (G-62). None of them may reappear here — a
    reviewer adding "just one" would not see the measurement they undo.
    """
    source = Path(sim_runtime.__file__).read_text(encoding="utf-8")
    assert forbidden not in _called_names(_method(source, "SimRuntime", "restage"))


def test_restage_keeps_the_soft_reset_and_its_two_neighbours():
    """Positive control: the guard above passes trivially on an EMPTY body."""
    source = Path(sim_runtime.__file__).read_text(encoding="utf-8")
    called = _called_names(_method(source, "SimRuntime", "restage"))
    assert {"reset", "move_debug_obstacle", "repose_robot"} <= set(called)
    reset_call = [
        node
        for node in ast.walk(_method(source, "SimRuntime", "restage"))
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr == "reset"
    ]
    assert [ast.unparse(k) for k in reset_call[0].keywords] == ["soft=True"]


def test_spawn_and_move_share_one_obstacle_position_home():
    """The moved box and the spawned box must land in the same place BY CONSTRUCTION.

    The batch loop MOVES the prim the first sample spawned, so the two placements
    are the same physical claim written twice — the p6 spike carried the ``z =
    height/2`` centring in two copies. Both call sites must go through the one
    pure home; the read-set guard in test_contract_schema_p3 sees their UNION and
    would therefore NOT notice one of them re-inlining the arithmetic (measured:
    that mutation stays green).
    """
    source = Path(sim_runtime.__file__).read_text(encoding="utf-8")
    for method in ("spawn_debug_obstacle", "move_debug_obstacle"):
        called = _called_names(_method(source, "SimRuntime", method))
        assert "debug_obstacle_position" in called, f"{method} places the box on its own"


def test_batch_run_closes_no_vendor_object_on_its_terminal_path():
    """Same G-62 invariant ``main.run`` carries: a vendor ``close()`` ends the
    process with status 0 and erases the carrier's exit code."""
    tree = ast.parse(Path(batch.__file__).read_text(encoding="utf-8"))
    run = next(n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name == "run")
    closes = [
        ast.unparse(node.func)
        for node in ast.walk(run)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr == "close"
    ]
    assert not closes, f"batch.run calls {closes} (G-62)"


def test_batch_module_entrypoint_delivers_the_code_with_hard_exit():
    tree = ast.parse(Path(batch.__file__).read_text(encoding="utf-8"))
    guard = tree.body[-1]
    assert isinstance(guard, ast.If) and ast.unparse(guard.test) == "__name__ == '__main__'"
    assert "hard_exit" in ast.unparse(guard) and "sys.exit" not in ast.unparse(guard)


def test_carrier_never_uses_exit_code_1():
    """1 is deliberately unused: "some sample failed" is not a carrier property."""
    source = Path(batch.__file__).read_text(encoding="utf-8")
    assert "EXIT_FAIL" not in source
    assert {batch.EXIT_PASS, batch.EXIT_USAGE, batch.EXIT_PLATFORM} == {0, 2, 3}
