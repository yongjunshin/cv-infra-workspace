"""CPU tests for the p6 batch carrier (``cv_infra.runner.batch``) — Isaac-free surface.

The carrier's GPU loop is a ``# pragma: no cover`` body proven on the workstation
(W1/W2). Everything it DECIDES before touching a GPU is here: the batch wire
(``JOB_SPEC_BATCH`` -> ``JobSpecBatch``), pre-boot admission (per-spec validation +
the uniformity the boot-once design depends on), the ``results/<i>`` layout that
IS the M1 wire invariant, the summary heartbeat, the carrier exit mapping, and the
re-exec argv — the one that made the first C-2 attempt die in 2.4 s by silently
re-execing into the SINGLE-job entrypoint.

Structural guards close the loop on the parts a unit test cannot run: the sample
boundary (``restage`` and, since p7, ``apply_obstacle_set``) must not contain the
authoring spellings it replaced, the p7 obstacle pool must be authored once at
boot and never inside the loop, ``run`` must not close a vendor object on its
terminal path (G-62), and the telemetry accumulator must be swapped where the
mission starts — after everything that steps the world, not before the restage
that teleports the robot (measured p6c3 T3 §4; the boundary's contact reports are
p7c1 W0 ⓓ).
"""

import ast
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from cv_infra.contract.job_batch import BATCH_SUMMARY_SCHEMA, JOB_SPEC_BATCH_ENV, JobSpecBatch
from cv_infra.runner import batch, main, ros_bridge, sim_runtime

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


def _with_obstacles(spec: dict, entries: list[dict] | None) -> dict:
    """Swap the legacy box for p7 obstacle groups (the schema forbids BOTH)."""
    spec["scenario"].pop("debug_obstacle", None)
    if entries:
        spec["scenario"]["obstacles"] = entries
    return spec


def test_admission_accepts_an_obstacle_count_that_differs_per_sample():
    """The axis p7 exists for — and the positive control for the guard above.

    Obstacles deliberately get NO uniformity row: "sample i places i chairs" is
    the CEO's stated case ("desk n={0..5}"), and the pool absorbs the variance by
    parking the surplus. If a future edit adds a uniformity row for obstacles,
    THIS is what goes red instead of a live batch silently rejecting its samples.
    """
    doc = _batch_doc(3)
    for index, spec in enumerate(doc["specs"]):
        _with_obstacles(spec, [{"asset": "chair", "x": 1.0 + index, "y": 2.0}] * index)
    parsed = batch.admit_specs(JobSpecBatch.model_validate(doc))
    assert [len(main.obstacle_specs(p.request)) for p in parsed] == [0, 1, 2]
    # ...and the pool the carrier would author is the per-sample MAXIMUM.
    plan = sim_runtime.obstacle_pool_plan([main.obstacle_specs(p.request) for p in parsed])
    assert plan == {("chair", None): 2}


def test_admit_specs_names_the_spec_whose_obstacle_asset_is_unknown():
    doc = _batch_doc(3)
    for spec in doc["specs"]:
        _with_obstacles(spec, [{"asset": "chair", "x": 1.0, "y": 2.0}])
    doc["specs"][2]["scenario"]["obstacles"][0]["asset"] = "chairr"
    with pytest.raises(batch.BadJobSpec, match=r"batch spec 2:.*obstacles\[0\]"):
        batch.admit_specs(JobSpecBatch.model_validate(doc))


def test_admit_specs_rejects_a_batch_whose_pool_would_not_fit():
    """The one obstacle question a per-spec parse cannot answer: the pool is the
    union over EVERY spec, so only admission sees the total (0 GPU seconds)."""
    doc = _batch_doc(2)
    for spec in doc["specs"]:
        _with_obstacles(
            spec,
            [{"asset": "chair", "x": 1.0, "y": 2.0}] * (sim_runtime.OBSTACLE_POOL_MAX + 1),
        )
    with pytest.raises(batch.BadJobSpec, match="batch obstacles"):
        batch.admit_specs(JobSpecBatch.model_validate(doc))


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
# The seams the carrier loop was decomposed into (p8c1) — the pre-boot half of
# ``run`` is CPU-reachable now, so it is tested rather than only AST-guarded.
# --------------------------------------------------------------------------- #
def test_admit_reads_the_wire_admits_every_spec_and_beats_before_any_gpu_second(tmp_path, capsys):
    admitted = batch._admit(_env(tmp_path))
    assert [p.index for p in admitted.specs] == [0, 1]
    assert admitted.out_root == tmp_path / "out"
    assert admitted.settle_s == batch.DEFAULT_REALIGN_SETTLE_S
    assert admitted.identity_key is None  # absent stays absent, never invented
    assert "batch admitted: 2 sample(s)" in capsys.readouterr().out
    # The heartbeat is on disk BEFORE Isaac is touched (P5-13: M3 can tell "the
    # container started and admitted n" from "it never got that far").
    doc = json.loads((tmp_path / "out" / batch.BATCH_SUMMARY_FILENAME).read_text(encoding="utf-8"))
    assert doc["schema"] == BATCH_SUMMARY_SCHEMA and doc["n"] == 2
    assert doc["iterations"] == []


def test_admit_carries_the_injected_identity_key_and_settle_override(tmp_path):
    env = dict(_env(tmp_path))
    env[main.REQUEST_IDENTITY_KEY_ENV] = "sha256:cb39be9a"
    env[batch.REALIGN_SETTLE_ENV] = "1.25"
    admitted = batch._admit(env)
    assert admitted.identity_key == "sha256:cb39be9a"  # VERBATIM (M4 owns the key)
    assert admitted.settle_s == 1.25


def test_admit_rejects_a_bad_wire_before_it_admits_anything(tmp_path):
    with pytest.raises(batch.BadJobSpec, match="RESULT_OUT"):
        batch._admit({JOB_SPEC_BATCH_ENV: str(_batch_file(tmp_path, _batch_doc()))})


def test_plan_staging_pools_the_union_over_every_spec_and_stages_sample_zero():
    doc = _batch_doc(3)
    for index, spec in enumerate(doc["specs"]):
        _with_obstacles(spec, [{"asset": "chair", "x": 1.0 + index, "y": 2.0}] * index)
    for spec in doc["specs"]:  # the wiring is done ONCE, so every spec declares it
        spec["interface"]["adapter_config"] = {
            "sensors": [{"topic": "/front_2d_lidar/scan", "type": "sensor_msgs/msg/LaserScan"}]
        }
    specs = batch.admit_specs(JobSpecBatch.model_validate(doc))
    staging = batch._plan_staging(specs, specs[0].adapter_config)
    # The pool is the per-sample MAXIMUM over EVERY spec (sample 2 wants 2), not
    # spec 0's (which wants none) — a spec-0-sized pool leaves sample 2 nothing.
    assert staging.pool_plan == {("chair", None): 2} and staging.pool_total == 2
    assert staging.head_obstacles == []  # sample 0 declares none: it parks the pool
    assert staging.sensor_topics == ["/front_2d_lidar/scan"]
    assert staging.debug_obstacle is None  # _with_obstacles dropped the legacy box


def test_plan_staging_dumps_sample_zeros_legacy_box_and_leaves_the_pool_empty():
    specs = batch.admit_specs(JobSpecBatch.model_validate(_batch_doc(2)))
    staging = batch._plan_staging(specs, specs[0].adapter_config)
    assert staging.debug_obstacle == {"x": 1.0, "y": 0.5}  # exclude_none: no invented dims
    assert staging.pool_plan == {} and staging.pool_total == 0
    assert staging.sensor_topics == []


def test_dumped_keeps_an_undeclared_block_absent():
    from cv_infra.contract.schema import DebugObstacle, InitialPose

    assert batch._dumped(None) is None
    assert batch._dumped(InitialPose(x=1.0, y=2.0, yaw=0.5)) == {"x": 1.0, "y": 2.0, "yaw": 0.5}
    # exclude_none is what lets the RUNNER's own dimension defaults apply — a
    # dumped ``height: None`` would arrive as a declared value (float(None)).
    assert batch._dumped(DebugObstacle(x=1.0, y=0.5), exclude_none=True) == {"x": 1.0, "y": 0.5}


def test_boot_total_s_sums_the_span_keys_only():
    boot = {"bootstrap_s": 0.0054, "simulation_app_init_s": 20.5, "robot_prim": "/World/Carter"}
    assert batch.boot_total_s(boot) == 20.5054


def test_summary_verdicts_report_every_sample_side_by_side(tmp_path):
    summary = batch.BatchSummary(tmp_path, "req-0001", 3)
    assert summary.verdicts() == []
    for index, verdict in enumerate(("pass", "fail", "timeout")):
        summary.add_iteration({"index": index, "verdict": verdict})
    # A list, never a fold: the carrier does not collapse n verdicts into one.
    assert summary.verdicts() == ["pass", "fail", "timeout"]


class _SettleAdapter:
    """Minimal stand-in for the ROS adapter's pump: stepping advances sim time."""

    def __init__(self, dt: float) -> None:
        self.sim_time_s = 0.0
        self.dt = dt
        self.steps = 0

    def step_and_spin(self) -> None:
        self.steps += 1
        self.sim_time_s += self.dt


def test_settle_world_pumps_until_the_sim_budget_is_spent():
    adapter = _SettleAdapter(dt=0.1)
    assert batch._settle_world(adapter, 0.5) == pytest.approx(0.5)
    assert adapter.steps == 5  # sim-time budget (D-F), not a wall sleep


def test_settle_world_gives_up_on_a_stopped_clock_instead_of_hanging(monkeypatch):
    """The wall cap exists for a sim whose /clock stopped: the loop must END.

    Without it the carrier hangs here forever; with it the readiness barrier
    below reports the dead sim normally and the batch exits 3.
    """
    monkeypatch.setattr(batch, "SETTLE_WALL_BUDGET_S", 0.05)
    adapter = _SettleAdapter(dt=0.0)  # /clock is not moving
    assert batch._settle_world(adapter, 3.0) == 0.0
    assert adapter.steps > 0  # it really did try to pump the world


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


@pytest.mark.parametrize("method", ["restage", "apply_obstacle_set"])
@pytest.mark.parametrize(
    "forbidden",
    [
        "stop",
        "RemovePrim",
        "FixedCuboid",
        "close",
        # p7: the authoring/deleting spellings a prim POOL makes reachable.
        "add_reference_to_stage",
        "DefinePrim",
        "delete_prim",
        # ...and the helper names themselves, so the guard cannot be walked around
        # by calling the boot-only spawn from inside the iteration.
        "spawn_obstacle_pool",
        "spawn_debug_obstacle",
    ],
)
def test_the_iteration_boundary_never_authors_a_prim(method, forbidden):
    """The two methods the sample boundary runs are pinned to CREATE NOTHING.

    ``stop()``/``RemovePrim``/``FixedCuboid`` ARE the hard path: they cycle the
    physics simulation views (+4.96 MiB/iteration, flat only once removed) and
    leave two orphan material prims per respawn (p6c2 §2.1: 2 -> 48 over 24
    iterations). ``close()`` would end the process with status 0 mid-batch
    (G-62). ``add_reference_to_stage``/``DefinePrim``/``delete_prim`` are the same
    class one asset-referencing pool later. And the two SPAWN helpers are
    forbidden by NAME: the measurement they encode is "author once at boot", so a
    call to them from the boundary would satisfy every other guard here while
    undoing the whole point (p7 §2-7).
    """
    source = Path(sim_runtime.__file__).read_text(encoding="utf-8")
    assert forbidden not in _called_names(_method(source, "SimRuntime", method))


def test_restage_keeps_the_soft_reset_and_its_three_neighbours():
    """Positive control: the guard above passes trivially on an EMPTY body."""
    source = Path(sim_runtime.__file__).read_text(encoding="utf-8")
    called = _called_names(_method(source, "SimRuntime", "restage"))
    assert {"reset", "move_debug_obstacle", "apply_obstacle_set", "repose_robot"} <= set(called)
    reset_call = [
        node
        for node in ast.walk(_method(source, "SimRuntime", "restage"))
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr == "reset"
    ]
    assert [ast.unparse(k) for k in reset_call[0].keywords] == ["soft=True"]


def test_both_obstacle_writes_precede_the_soft_reset():
    """Order pin: an obstacle written AFTER the reset is not in the state the
    reset published to physics.

    The legacy box already carried this constraint ("obstacle move FIRST"); the
    p7 set is the same authored-transform claim on n more prims, so it rides the
    same window. A reviewer who appends the new call at the end of the method
    would produce a body that runs, logs, and stages the obstacles one reset too
    late.
    """
    source = Path(sim_runtime.__file__).read_text(encoding="utf-8")
    restage = _method(source, "SimRuntime", "restage")
    lines: dict[str, list[int]] = {}
    for node in ast.walk(restage):
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute):
            lines.setdefault(node.func.attr, []).append(node.lineno)
    (move,) = lines["move_debug_obstacle"]
    (apply_set,) = lines["apply_obstacle_set"]
    (reset,) = lines["reset"]
    assert move < reset, "the legacy obstacle move slipped past the soft reset"
    assert apply_set < reset, "the obstacle SET is written after the reset published the state"


@pytest.mark.parametrize(
    ("method", "required"),
    [
        ("spawn_obstacle_pool", {"obstacle_pool_paths", "obstacle_park_position"}),
        (
            "apply_obstacle_set",
            {"obstacle_placement_plan", "obstacle_place_transform", "obstacle_park_position"},
        ),
    ],
)
def test_the_obstacle_gpu_wrappers_go_through_the_pure_functions(method, required):
    """Single-home pin (G-25), the sibling of ``spawn``/``move`` sharing
    ``debug_obstacle_position``.

    Where a prim is PARKED, where it is PLACED and which member serves which
    declaration are pure, CPU-tested decisions. A GPU wrapper that inlines any of
    them puts the arithmetic somewhere no unit test can see it — and the parking
    sweep in particular has to agree with the spawn on the member ORDER, or a
    parked prim lands on another parked prim's slot.
    """
    source = Path(sim_runtime.__file__).read_text(encoding="utf-8")
    assert required <= set(_called_names(_method(source, "SimRuntime", method)))


def test_the_pool_is_spawned_once_at_boot_and_never_inside_the_sample_loop():
    """Structural pin: the pool is authored by a pre-reset hook registered OUTSIDE
    the sample loop, exactly once.

    The name-based guard above stops the boundary from CALLING the spawn; this
    stops the carrier from REGISTERING it per sample, which would author a second
    pool on top of the first (the prim census that NEG-6 gate 5 watches would
    climb, and the placement sweep would keep re-posing the members of the first).
    """
    run = _batch_run_function()
    loop = next(
        node
        for node in ast.walk(run)
        if isinstance(node, ast.For) and ast.unparse(node.iter) == "enumerate(specs)"
    )
    spawns = [
        node
        for node in ast.walk(run)
        if isinstance(node, ast.Attribute) and node.attr == "spawn_obstacle_pool"
    ]
    assert len(spawns) == 1, f"spawn_obstacle_pool named {len(spawns)}x in batch.run"
    assert not (
        loop.lineno <= spawns[0].lineno <= loop.end_lineno
    ), "the pool spawn was registered from INSIDE the sample loop"
    hooks = [
        node
        for node in ast.walk(run)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr == "append"
        and ast.unparse(node.func.value) == "sim.pre_reset"
        and "spawn_obstacle_pool" in ast.unparse(node)
    ]
    assert len(hooks) == 1, "the pool must be authored by exactly one pre_reset hook"


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


def _batch_run_function() -> ast.FunctionDef:
    """``batch.run``'s AST — the GPU loop no unit test can execute."""
    tree = ast.parse(Path(batch.__file__).read_text(encoding="utf-8"))
    return next(n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name == "run")


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


def test_telemetry_accumulator_is_swapped_at_mission_start_not_at_restage():
    """WHERE the record is replaced decides what the sample's first GT sample is.

    ``World.reset(soft=True)`` "will do one step internally regardless" (vendor
    docstring), and ``repose_robot`` runs AFTER that reset — so a record swapped
    before the restage collects its first GT pose at the PREVIOUS sample's pose.
    Measured (p6c3 T3 §4, 12 samples): all 11 teleported samples reported
    ``time_to_goal_s = 0.0`` (the previous sample ended near the goal, so
    ``reached_goal`` read t=0 as arrived) and ``path_len_m`` carried the teleport
    distance (i=3: 12.564 m, of which 6.354 m was the jump). The swap therefore
    belongs where ``main.run`` attaches the sampler: after readiness, at mission
    start. Guarded on the AST rather than the text so comments cannot fake it.
    """
    run = _batch_run_function()
    swaps = [
        node.lineno
        for node in ast.walk(run)
        if isinstance(node, ast.Assign)
        and ast.unparse(node.targets[0]) == "sampler.record"
        and ast.unparse(node.value) == "TelemetryRecord()"
    ]
    assert len(swaps) == 1, f"the sample boundary must be exactly ONE swap, found {swaps}"
    called: dict[str, list[int]] = {}
    for node in ast.walk(run):
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute):
            called.setdefault(node.func.attr, []).append(node.lineno)
    (restage,) = called["restage"]
    (realign,) = called["realign"]
    (readiness,) = called["await_ready"]
    (mission,) = called["drive_mission"]
    assert restage < realign < readiness < swaps[0] < mission, (
        f"record swap at line {swaps[0]} is not between readiness ({readiness}) "
        f"and the mission ({mission})"
    )


#: Calls inside the sample loop that ADVANCE sim time (and therefore let PhysX
#: deliver contact reports). ``drive_mission`` is deliberately absent: it IS the
#: boundary the guard below measures against.
#:
#: ``_settle_world`` joined at p8c1: the settle pump used to be an inline
#: ``while ... adapter.step_and_spin()`` in the loop and was covered by
#: ``step_and_spin``; as a named helper its call site spells neither name.
#: MEASURED before adding it — with the pump moved below the record swap this
#: guard stayed GREEN, i.e. the extraction had put the very regression it exists
#: to catch outside its field of view (§3-4 / G-106). Any future extraction of a
#: sim-advancing wait belongs in this tuple for the same reason.
_SIM_ADVANCING_CALLS = (
    "step",
    "step_and_spin",
    "restage",
    "realign",
    "await_ready",
    "_settle_world",
)


def test_nothing_advances_the_sim_between_the_record_swap_and_the_mission():
    """The sample BOUNDARY's contact reports must not land in the next sample's record.

    Measured (p7c1 W0 gate ⓓ): PhysX delivers the contact-LOST reports for
    whatever the chassis was touching at the end of sample i-1 in the first steps
    AFTER the restage — a transition window, not the previous sample's window.
    In this carrier every one of those steps (the soft reset's own internal step,
    the realign, the settle pump, the readiness barrier) runs BEFORE
    ``sampler.record`` is replaced, so those reports append to the RETIRED
    record: sample i-1's, whose metrics were already computed and whose
    result.json is already on disk. They are dropped, and sample i's collision
    count starts at its own mission.

    That holds only while NOTHING between the swap and the mission advances the
    sim. A settle pump moved below the swap — or a "let the world breathe two
    steps before recording" line — would charge sample i with the collision
    sample i-1 was in when it was teleported away, and the verdict would be a
    real FAIL for an event in another sample's world. The sibling guard
    ``test_telemetry_accumulator_is_swapped_at_mission_start_not_at_restage``
    holds the other direction (the swap must not move UP, which was measured to
    inflate path_len and zero time_to_goal).
    """
    loop = next(
        node
        for node in ast.walk(_batch_run_function())
        if isinstance(node, ast.For) and ast.unparse(node.iter) == "enumerate(specs)"
    )
    calls: dict[str, list[int]] = {}
    for node in ast.walk(loop):
        if isinstance(node, ast.Call):
            name = (
                node.func.attr if isinstance(node.func, ast.Attribute) else ast.unparse(node.func)
            )
            calls.setdefault(name, []).append(node.lineno)
    (swap,) = [
        node.lineno
        for node in ast.walk(loop)
        if isinstance(node, ast.Assign)
        and ast.unparse(node.targets[0]) == "sampler.record"
        and ast.unparse(node.value) == "TelemetryRecord()"
    ]
    (mission,) = calls["drive_mission"]
    advancing = sorted(
        (line, name) for name in _SIM_ADVANCING_CALLS for line in calls.get(name, [])
    )
    # Positive control (G-07): the extraction must actually see the pre-mission
    # stepping, or "nothing between the swap and the mission" is vacuously true.
    assert [name for line, name in advancing if line < swap], (
        f"no sim-advancing call found before the record swap — {_SIM_ADVANCING_CALLS} "
        "no longer names how this loop steps the world, so this guard sees nothing"
    )
    between = [(line, name) for line, name in advancing if swap < line < mission]
    assert not between, (
        f"{between} advance the sim between the record swap (line {swap}) and the mission "
        f"(line {mission}) — the boundary's contact-LOST reports would be charged to THIS "
        "sample"
    )


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


# --------------------------------------------------------------------------- #
# p8c2 — the per-iteration stopwatch and the carrier's platform-exit mapping.
# --------------------------------------------------------------------------- #
def test_stopwatch_records_a_named_span_and_forgets_it_after_the_end():
    """The W2 per-iteration anchors: every span is named, so a summary reader can
    tell "restage was slow" from "the mission was slow". ``end`` pops its start —
    a span left armed would make the NEXT sample's identically-named span measure
    from the wrong t0, quietly inflating every later iteration."""
    watch = batch._Stopwatch()
    assert watch.spans == {}

    watch.begin("restage")
    elapsed = watch.end("restage")

    assert elapsed >= 0.0
    assert watch.spans == {"restage": round(elapsed, 4)}  # rounded once, at the source
    with pytest.raises(KeyError):  # the start was consumed, not left behind
        watch.end("restage")


def test_stopwatch_keeps_concurrent_spans_apart():
    """Iterations nest spans (an outer per-sample one around inner phases), so two
    open names must not share a t0."""
    watch = batch._Stopwatch()
    watch.begin("sample")
    watch.begin("mission")
    watch.end("mission")
    watch.end("sample")
    assert sorted(watch.spans) == ["mission", "sample"]
    assert watch.spans["sample"] >= watch.spans["mission"]


def test_carrier_main_maps_a_refused_eula_to_the_platform_exit_code(monkeypatch, capsys):
    """The carrier's half of NEG-2. ``run`` re-raises ``EulaNotAcceptedError`` past
    its own error handling on purpose, so this outer handler owns the exit code —
    and it must be 3 (platform), never 2 (the operator's spec was fine)."""

    def _refuse(_env):
        raise sim_runtime.EulaNotAcceptedError("Isaac Sim EULA not accepted — boot refused")

    monkeypatch.setattr(batch, "run", _refuse)
    assert batch.main({}) == batch.EXIT_PLATFORM == 3
    assert "boot refused" in capsys.readouterr().err


# --------------------------------------------------------------------------- #
# C5 — the quadruped repose + the carrier parity a unit test cannot execute.
# --------------------------------------------------------------------------- #
def test_repose_restores_the_stance_the_registry_row_declares():
    """The legged half of the iteration boundary (D-5).

    A quadruped ends a mission with its legs wherever the last gait cycle left
    them, so a teleport alone starts sample i+1 mid-stride. The stance VALUES
    must come from the scene row (which took them from the measured training
    contract), never from a literal here: a second copy of the 12 numbers is a
    plant that drifts away from the offset the policy adds its actions to.
    """
    source = Path(sim_runtime.__file__).read_text(encoding="utf-8")
    called = _called_names(_method(source, "SimRuntime", "repose_robot"))
    assert "set_joint_positions" in called, "the repose leaves a quadruped mid-stride"
    assert "scene_row" in called, "the stance was written from somewhere other than the row"
    assert "repose_height" in called, "sample i+1 inherits sample i's end base height"


def test_the_policy_episode_state_is_dropped_before_the_restage_not_after():
    """Order pin (C5): ``World.reset(soft=True)`` "will do one step internally
    regardless" (vendor docstring — the same property the record swap is placed
    around), and that step runs the physics callbacks. A policy reset AFTER the
    restage therefore lets sample i's last gait torque be applied to sample i+1's
    robot once, before the repose has even written the stance.

    Reset first and the two agree by construction: the loop's post-reset joint
    target IS ``go2_constants.DEFAULT_JOINT_POS``, which is the stance the row
    declares and the repose writes.
    """
    run = _batch_run_function()
    lines: dict[str, list[int]] = {}
    for node in ast.walk(run):
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute):
            lines.setdefault(node.func.attr, []).append(node.lineno)
    (reset,) = lines["reset"]  # policy.reset() — the carrier's only bare reset
    (restage,) = lines["restage"]
    assert reset < restage, "the policy carried sample i's episode state into the reset step"


def test_the_carrier_publishes_the_same_sensor_suite_a_single_job_does():
    """A COMPOSED scene (go2) ships no vendor ROS graph — not even /clock — so a
    carrier that only ran the FU-17 graph walk would publish NOTHING and every
    sample would fail the readiness barrier waiting on a clock nobody sources.

    Pinned by NAME on the same three seams ``main.run`` uses, because "the two
    entrypoints stage the same world" is the property the batch exists to keep
    (the sample IS the job — p6 §0).
    """
    run = _batch_run_function()
    called = _called_names(run)
    for seam in ("build_sensor_suite", "register_sensor_hooks", "_attach_optional_streams"):
        assert seam in called, f"the carrier never calls {seam}"
    # ``detach`` by its RECEIVER: the telemetry sampler spells that name too, so a
    # bare-attribute check would pass on a carrier with no sensor suite at all
    # (G-106: a guard its own bug satisfies). ``attach`` moved into the helper,
    # which the two tests below drive directly.
    spelled = {
        ast.unparse(node.func)
        for node in ast.walk(run)
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
    }
    assert "sensors.detach" in spelled
    # ...and the graph-only spelling it replaced must be gone: leaving it next to
    # the suite would enable render products for topics the RUNNER publishes and
    # warn about every one of them.
    assert "enable_declared_sensors" not in called


class _FakeSensors:
    """A ``Go2SensorSuite``-shaped stand-in (attach/detach return report lines)."""

    def __init__(self) -> None:
        self.attached: tuple | None = None

    def attach(self, node, on_step) -> list[str]:
        self.attached = (node, on_step)
        return ["[cv-runner] go2_sensors inventory=8"]


def test_a_composed_worlds_streams_are_attached_to_the_carriers_one_node(monkeypatch, capsys):
    """Both optional attachments, on the shape a go2 carrier has."""
    from types import SimpleNamespace

    from cv_infra.contract.adapter_schema import Ros2AdapterConfig
    from cv_infra.runner import go2_wiring

    subscribed: list = []
    monkeypatch.setattr(
        go2_wiring,
        "subscribe_cmd_vel",
        lambda node, cmd_vel, on_command: subscribed.append((node, cmd_vel.topic, on_command)),
    )
    node, on_step = object(), []
    adapter = SimpleNamespace(node=node)
    sim = SimpleNamespace(on_step=on_step)
    sensors, policy = _FakeSensors(), SimpleNamespace(set_command=lambda *a: None)

    batch._attach_optional_streams(adapter, sim, Ros2AdapterConfig(), sensors, policy)

    assert sensors.attached == (node, on_step)  # the suite publishes on the STEP hook
    assert "go2_sensors inventory=8" in capsys.readouterr().out  # ...and says so (G-26)
    assert subscribed == [(node, "/cmd_vel", policy.set_command)]  # ONE rclpy node


def test_a_carter_carrier_attaches_neither_stream(monkeypatch, capsys):
    """Positive control for the pair above: the pre-wired scene's carrier must
    behave byte-identically to its pre-go2 self — no publishers, no subscription,
    no extra line in the boot log."""
    from types import SimpleNamespace

    from cv_infra.contract.adapter_schema import Ros2AdapterConfig
    from cv_infra.runner import go2_wiring

    monkeypatch.setattr(
        go2_wiring,
        "subscribe_cmd_vel",
        lambda *a, **k: pytest.fail("a carter carrier subscribed to /cmd_vel"),
    )
    adapter = SimpleNamespace(node=object())
    batch._attach_optional_streams(
        adapter, SimpleNamespace(on_step=[]), Ros2AdapterConfig(), None, None
    )
    assert capsys.readouterr().out == ""
