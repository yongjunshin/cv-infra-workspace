"""CPU unit tests for the M2 runner core (Isaac-independent surface only).

Covers JOB_SPEC parsing, RESULT_OUT resolution + exactly-one result.json write,
verdict/exit mapping, the metric math (path length, sim-time-to-goal, chassis
collision filtering D-E), the two MVP oracles, and the EULA/env guards. GPU wiring
(sim/bridge/telemetry acquisition/recording/DDS) is out of scope this cycle and is
covered on the workstation in cycles 2-6.
"""

import json
import math
import os
import subprocess
import sys

import pytest

from cv_infra.runner import evaluate, main, ros_bridge, sim_runtime, telemetry
from cv_infra.runner.telemetry import ContactEvent, PoseSample

CHASSIS = "/World/carter/chassis"


# --------------------------------------------------------------------------- #
# JOB_SPEC / RESULT_OUT I/O glue (D-2).
# --------------------------------------------------------------------------- #
def test_job_spec_inline_json():
    env = {"JOB_SPEC": '{"scene_ref": "s.usd", "acceptance_criteria": {}}'}
    assert main.resolve_job_spec_dict(env)["scene_ref"] == "s.usd"


def test_job_spec_from_file(tmp_path):
    p = tmp_path / "job.json"
    p.write_text(json.dumps({"scene_ref": "file.usd"}), encoding="utf-8")
    assert main.resolve_job_spec_dict({"JOB_SPEC": str(p)})["scene_ref"] == "file.usd"


def test_job_spec_inline_json_longer_than_name_max():
    # Measured (p3c3): a slash-free inline JSON > NAME_MAX (255 bytes) made
    # the file-or-inline probe's Path.is_file() raise ENAMETOOLONG instead of
    # returning False (earlier specs dodged it by luck — their "/" split the
    # string into short path components). Unstatable raw => inline, no crash.
    spec = {"scene_ref": "s.usd", "padding": "x" * 300}
    env = {"JOB_SPEC": json.dumps(spec)}
    assert main.resolve_job_spec_dict(env)["scene_ref"] == "s.usd"


def test_job_spec_missing_raises_usage():
    with pytest.raises(main.BadJobSpec):
        main.resolve_job_spec_dict({})


def test_job_spec_invalid_json_raises_usage():
    with pytest.raises(main.BadJobSpec):
        main.resolve_job_spec_dict({"JOB_SPEC": "{not json"})


def test_job_spec_non_object_raises_usage():
    with pytest.raises(main.BadJobSpec):
        main.resolve_job_spec_dict({"JOB_SPEC": "[1, 2, 3]"})


def test_result_path_dir_appends_result_json(tmp_path):
    assert main.resolve_result_path({"RESULT_OUT": str(tmp_path)}) == tmp_path / "result.json"


def test_result_path_explicit_json(tmp_path):
    target = tmp_path / "out" / "custom.json"
    assert main.resolve_result_path({"RESULT_OUT": str(target)}) == target


def test_result_path_missing_raises_usage():
    with pytest.raises(main.BadJobSpec):
        main.resolve_result_path({})


def test_require_job_id_returns_it():
    assert main.require_job_id({"job_id": "job-0001"}) == "job-0001"


def test_require_job_id_missing_or_empty_raises_usage():
    with pytest.raises(main.BadJobSpec):
        main.require_job_id({"scene_ref": "s.usd"})  # absent
    with pytest.raises(main.BadJobSpec):
        main.require_job_id({"job_id": ""})  # empty


def test_write_result_writes_exactly_one_file(tmp_path):
    payload = {"verdict": "pass", "metrics": {"path_len_m": 1.5}}
    out = main.write_result(payload, tmp_path / "result.json")
    assert out.exists()
    assert sorted(q.name for q in tmp_path.iterdir()) == ["result.json"]  # no .tmp leftover
    assert json.loads(out.read_text(encoding="utf-8")) == payload


# --------------------------------------------------------------------------- #
# Verdict / exit-code contract.
# --------------------------------------------------------------------------- #
def test_exit_code_mapping():
    assert main.exit_code_for_verdict("pass") == main.EXIT_PASS
    assert main.exit_code_for_verdict("fail") == main.EXIT_FAIL
    assert main.exit_code_for_verdict("timeout") == main.EXIT_FAIL
    assert main.exit_code_for_verdict("error") == main.EXIT_PLATFORM
    assert main.exit_code_for_verdict("weird") == main.EXIT_PLATFORM


def _outcome(name, passed, reason=""):
    return evaluate.OracleOutcome(name=name, passed=passed, reason=reason)


def test_fold_verdict_pass():
    assert evaluate.fold_verdict([_outcome("a", True), _outcome("b", True)]) == "pass"


def test_fold_verdict_fail():
    assert evaluate.fold_verdict([_outcome("a", True), _outcome("b", False)]) == "fail"


def test_fold_verdict_timeout_promotes():
    outs = [_outcome("reached_goal", False, "timeout"), _outcome("no_collision", True)]
    assert evaluate.fold_verdict(outs) == "timeout"


# --------------------------------------------------------------------------- #
# Metric math (REQ-EXEC-012).
# --------------------------------------------------------------------------- #
def _line(n, dt=1.0):
    return [
        PoseSample(
            sim_time_s=i * dt, position=(float(i), 0.0, 0.0), orientation_wxyz=(1.0, 0.0, 0.0, 0.0)
        )
        for i in range(n)
    ]


def test_path_length():
    assert telemetry.path_length_m(_line(4)) == pytest.approx(3.0)
    assert telemetry.path_length_m([]) == 0.0
    assert telemetry.path_length_m(_line(1)) == 0.0


def test_first_reach_index_and_time_to_goal():
    samples = _line(4)  # positions (0,0,0)..(3,0,0) at t=0..3
    assert telemetry.first_reach_index(samples, (3.0, 0.0, 0.0), 0.1) == 3
    assert telemetry.first_reach_index(samples, (9.0, 0.0, 0.0), 0.1) is None
    assert telemetry.time_to_goal_s(samples, (3.0, 0.0, 0.0), 0.1) == pytest.approx(3.0)
    assert telemetry.time_to_goal_s(samples, (9.0, 0.0, 0.0), 0.1) is None


def test_min_clearance_is_none_in_p2():
    # Name says "in_p2" but the None is PERMANENT: MVP-descoped by CEO 2026-08-04 D-3
    # (rationale in telemetry.min_clearance_m). Not a pending implementation.
    assert telemetry.min_clearance_m() is None


def test_collision_filter_excludes_ground_and_self_counts_obstacle():
    excluded = ["/World/ground", "/World/carter/wheels"]
    events = [
        ContactEvent(0.1, CHASSIS, "/World/ground"),  # ground -> excluded
        ContactEvent(0.2, "/World/carter/wheels/left", CHASSIS),  # self subtree -> excluded
        ContactEvent(0.3, CHASSIS, "/World/obstacle_box"),  # obstacle -> counted
    ]
    assert telemetry.count_real_collisions(events, CHASSIS, excluded) == 1


def test_collision_filter_empty_is_zero():
    assert telemetry.count_real_collisions([], CHASSIS, ["/World/ground"]) == 0


def test_collision_filter_keeps_carter_meaning_when_the_scenario_declares_its_exclusions():
    # Measured p2c5 run1: ContactReportAPI on the chassis (an articulation root)
    # aggregates WHOLE-robot reports — wheel<->ground pairs arrived 7344x on a
    # clean run. AR-12 widened the reduction from "the chassis prim" to "the
    # robot subtree", so what keeps those off the count is now the DECLARED
    # exclusion list — which every carter scenario ships (measured, e.g.
    # cv-infra-user/scenarios/nova_carter_warehouse_goal.yaml: the robot subtree
    # + the warehouse ground plane).
    excluded = ["/World/carter", "/World/GroundPlane"]
    events = [
        ContactEvent(0.1, "/World/carter/wheel_left", "/World/GroundPlane/collisionPlane"),
        ContactEvent(0.2, "/World/GroundPlane/collisionPlane", "/World/carter/caster_wheel_right"),
        ContactEvent(0.3, CHASSIS, "/World/obstacle_box"),  # real chassis hit still counts
    ]
    assert telemetry.count_real_collisions(events, CHASSIS, excluded) == 1


def test_ar12_counts_a_leg_hitting_an_obstacle_the_chassis_never_touched():
    # C1 MEASURED the defect this closes: a go2 dropped onto a 0.10 m box logged
    # 263 leg<->box contacts and ended upside down (roll 3.14), and the
    # pre-AR-12 reduction reported collision_count == 0 — every actor was a foot
    # or a calf, never ``/World/Go2/base``. The grounds are excluded exactly as
    # C1's measured canon declares them (two floor prims, differing case).
    grounds = [
        "/World/GroundPlane/collisionPlane",
        "/World/Warehouse_Empty_small_realtime/GroundPlane/CollisionPlane",
    ]
    events = [
        ContactEvent(0.1, "/World/Go2/FL_foot", grounds[0]),  # walking -> excluded
        ContactEvent(0.2, grounds[1], "/World/Go2/RR_calf"),  # walking -> excluded
        ContactEvent(0.3, "/World/Go2/FL_calf", "/World/cv_obstacles/box_0"),  # counted
        ContactEvent(0.4, "/World/Go2/base", "/World/cv_obstacles/box_0"),  # counted
    ]
    assert telemetry.count_real_collisions(events, "/World/Go2/base", grounds) == 2


def test_the_robot_subtree_is_the_chassis_parent_and_never_the_whole_world():
    # The widening is ONE level (the chassis prim's parent). A chassis declared
    # directly under the stage root keeps the exact-prim meaning — widening it
    # would make every warehouse shelf part of the robot.
    assert telemetry.robot_subtree("/World/Go2/base") == "/World/Go2"
    carter = "/World/Nova_Carter_ROS"
    assert telemetry.robot_subtree(f"{carter}/chassis_link") == carter
    assert telemetry.robot_subtree("/World/Go2") == "/World/Go2"
    assert telemetry.robot_subtree("/Go2") == "/Go2"
    shelf_hit = [ContactEvent(0.1, "/World/shelf_a", "/World/forklift")]
    assert telemetry.count_real_collisions(shelf_hit, "/World/Go2", []) == 0


# --------------------------------------------------------------------------- #
# p6 §0-5: an UNMATERIALIZED distribution must never reach the execution plane.
# --------------------------------------------------------------------------- #
def _randomizable_spec(**scenario_overrides) -> dict:
    scenario = {
        "scene": "omniverse://assets/warehouse.usd",
        "robot": "omniverse://assets/nova_carter_ros.usd",
        "goal": {"x": 3.0, "y": -1.5, "yaw": 0.0},
        "seed": 7,
        "timeout_s": 120.0,
    }
    scenario.update(scenario_overrides)
    return {
        "job_id": "job-0001",
        "scenario": scenario,
        "sut_image_ref": "carter-sut:p2",
        "interface": {"type": "ros2", "adapter_config": {}},
        "acceptance_criteria": [{"oracle": "reached_goal"}],
    }


def test_parse_request_accepts_a_materialized_sample():
    """Positive control: a CONCRETE sample — stamp included — still parses.

    The runner must accept ``scenario.derivation`` (the platform stamps it when
    it materializes a sample); it is the LOADER that rejects a submitted one.
    """
    spec = _randomizable_spec(derivation={"version": "cv-derive/1", "index": 3})
    request, _config = main.parse_request(spec)
    assert request.scenario.goal.x == 3.0
    assert request.scenario.derivation.index == 3


@pytest.mark.parametrize(
    ("block", "field", "value"),
    [
        ("goal", "x", {"uniform": [-6.5, -5.5]}),
        ("goal", "yaw", {"choice": [0.0, 1.57]}),
        ("initial_pose", "y", {"uniform": [0.0, 1.0]}),
        ("debug_obstacle", "x", {"choice": [1.0]}),
    ],
)
def test_parse_request_rejects_a_leaked_distribution(block, field, value):
    """The union extension means ``extra="forbid"`` no longer stops a
    distribution from reaching the runner — this check is what does.

    Drawing the sample HERE would fork the provenance: the result would carry
    values that no ``derivation`` stamp explains and no identity key predicts.
    It is a platform bug, so it is bad input -> exit 2, never a pose.
    """
    scenario_block = (
        {"x": 1.0, "y": 0.5} if block == "debug_obstacle" else {"x": 1.0, "y": 0.5, "yaw": 0.0}
    )
    scenario_block[field] = value
    spec = _randomizable_spec(**{block: scenario_block})
    with pytest.raises(main.BadJobSpec) as excinfo:
        main.parse_request(spec)
    message = str(excinfo.value)
    assert f"scenario.{block}.{field}" in message  # names the field to materialize
    assert "materialize" in message


def test_parse_request_accepts_a_materialized_obstacle_list():
    """Positive control for the two rejections below: a CONCRETE group parses, and
    ``obstacle_specs`` hands the sim layer plain dicts with the unset dimensions
    ABSENT (so the runner's own defaults apply, never ``float(None)``)."""
    spec = _randomizable_spec(
        obstacles=[{"asset": "chair", "count": 1, "x": 1.0, "y": 2.0, "yaw": 0.5}]
    )
    request, _config = main.parse_request(spec)
    assert main.obstacle_specs(request) == [
        {"asset": "chair", "count": 1, "x": 1.0, "y": 2.0, "yaw": 0.5}
    ]
    # No obstacles declared -> [] (the contract says None; this is the one adapter).
    assert main.obstacle_specs(main.parse_request(_randomizable_spec())[0]) == []


def test_parse_request_rejects_an_unexpanded_obstacle_group():
    """p7's widening of the §0-5 leak check: ``count: 3`` carries NO distribution.

    Without it the runner places ONE box where the document declared three and
    judges the sample as if that were the world — the silent-ignore failure (G-25)
    with a verdict attached to it.
    """
    spec = _randomizable_spec(obstacles=[{"asset": "box", "count": 3, "x": 1.0, "y": 2.0}])
    with pytest.raises(main.BadJobSpec) as excinfo:
        main.parse_request(spec)
    message = str(excinfo.value)
    assert "scenario.obstacles[0].count" in message and "materialize" in message


def test_parse_request_rejects_a_distribution_inside_an_obstacle_group():
    spec = _randomizable_spec(obstacles=[{"asset": "box", "x": {"uniform": [-1.0, 1.0]}, "y": 2.0}])
    with pytest.raises(main.BadJobSpec) as excinfo:
        main.parse_request(spec)
    assert "scenario.obstacles[0].x" in str(excinfo.value)


def test_parse_request_rejects_an_obstacle_asset_the_runner_cannot_resolve():
    """Pre-boot (exit 2) and named by GROUP INDEX — the alternative is a 404 at
    reference time, mid-boot, after the GPU was already paid."""
    spec = _randomizable_spec(
        obstacles=[
            {"asset": "box", "x": 1.0, "y": 2.0},
            {"asset": "chairr", "x": 1.0, "y": 2.0},
        ]
    )
    with pytest.raises(main.BadJobSpec) as excinfo:
        main.parse_request(spec)
    message = str(excinfo.value)
    assert "scenario.obstacles[1]" in message and "chairr" in message


# --------------------------------------------------------------------------- #
# p7 obstacle sets — the pure layer (asset resolution / pool / placement).
# --------------------------------------------------------------------------- #
def _obstacle(asset: str = "box", **overrides) -> dict:
    """One CONCRETE obstacle entry, the shape ``obstacle_specs`` hands over."""
    return {"asset": asset, "count": 1, "x": 1.0, "y": 2.0, "yaw": 0.0, **overrides}


def test_resolve_obstacle_asset_covers_box_registry_and_direct_refs():
    # "box" is NOT an asset: None is the branch that says "author a cuboid".
    assert sim_runtime.resolve_obstacle_asset(sim_runtime.BOX_ASSET_REF) is None
    chair = sim_runtime.resolve_obstacle_asset("chair")
    assert chair.usd_path.endswith(".usd") and chair.usd_path.startswith("/Isaac/")
    # A direct ref passes through with NO registry knowledge (resolve_scene's grammar).
    direct = sim_runtime.resolve_obstacle_asset("omniverse://host/props/crate.usd")
    assert direct.usd_path == "omniverse://host/props/crate.usd" and direct.z_offset == 0.0


def test_resolve_obstacle_asset_is_loud_about_an_unknown_name():
    # REQ-INTAKE-005: a typo must name what IS available, never resolve to a 404
    # at reference time (which would surface mid-boot, after the GPU was paid).
    with pytest.raises(ValueError) as excinfo:
        sim_runtime.resolve_obstacle_asset("chairr")
    message = str(excinfo.value)
    assert "chairr" in message and "chair" in message and "box" in message


def test_every_registered_asset_carries_a_measured_offset_and_an_isaac_path():
    # The registry is the one place a REMEMBERED path would hide (G-25/G-28): a
    # relative /Isaac/... path is what gets joined onto the live assets root.
    assert set(sim_runtime.OBSTACLE_ASSETS) == {"chair", "desk", "forklift", "person"}
    for name, asset in sim_runtime.OBSTACLE_ASSETS.items():
        assert asset.usd_path.startswith("/Isaac/"), name
        assert asset.usd_path.endswith((".usd", ".usda", ".usdz")), name
        assert asset.z_offset >= 0.0, name
    # go2 C1: the patrol target entered by the same measured rule. Its offset is
    # NOT 0 — the character's origin sits 0.1248 m above its own feet (C0 probe
    # A13), and a 0 here would bury the feet in the floor.
    assert sim_runtime.OBSTACLE_ASSETS["person"].z_offset == 0.1248


def test_obstacle_pool_key_buckets_boxes_by_size_and_assets_verbatim():
    # A box's dimensions are CONSTRUCTION-time on a FixedCuboid, so each distinct
    # size is its own bucket; an undeclared dimension resolves to the runner default.
    assert sim_runtime.obstacle_pool_key(_obstacle()) == (
        "box",
        (
            sim_runtime.DEBUG_OBSTACLE_DEFAULT_WIDTH,
            sim_runtime.DEBUG_OBSTACLE_DEFAULT_DEPTH,
            sim_runtime.DEBUG_OBSTACLE_DEFAULT_HEIGHT,
        ),
    )
    assert sim_runtime.obstacle_pool_key(_obstacle(height=0.5)) != sim_runtime.obstacle_pool_key(
        _obstacle()
    )
    # An asset keys on the DESIGNATOR, not on the resolved path.
    assert sim_runtime.obstacle_pool_key(_obstacle("chair")) == ("chair", None)


def test_obstacle_pool_plan_is_the_per_sample_maximum_never_the_sum():
    """The whole reason a pool exists: 12 samples x 2 chairs is 2 chairs, not 24.

    The sum would grow the stage with the batch size and re-introduce exactly the
    prim growth the boot-once design removed.
    """
    per_sample = [
        [_obstacle("chair")] + [_obstacle("desk")] * 5,
        [_obstacle("chair")] * 2,
        [],
    ]
    assert sim_runtime.obstacle_pool_plan(per_sample) == {
        ("chair", None): 2,
        ("desk", None): 5,
    }
    assert sim_runtime.obstacle_pool_plan([[], []]) == {}


def test_obstacle_pool_plan_rejects_a_pool_over_the_cap_before_any_gpu_second():
    over = [[_obstacle("chair")] * (sim_runtime.OBSTACLE_POOL_MAX + 1)]
    with pytest.raises(ValueError, match=str(sim_runtime.OBSTACLE_POOL_MAX)):
        sim_runtime.obstacle_pool_plan(over)
    at_cap = [[_obstacle("chair")] * sim_runtime.OBSTACLE_POOL_MAX]
    assert sum(sim_runtime.obstacle_pool_plan(at_cap).values()) == sim_runtime.OBSTACLE_POOL_MAX


def test_obstacle_pool_paths_are_derived_ordered_and_under_one_scope():
    plan = {("chair", None): 1, ("desk", None): 2}
    pool = sim_runtime.obstacle_pool_paths(plan)
    members = sim_runtime.obstacle_pool_members(pool)
    assert len(members) == 3 and len(set(members)) == 3
    assert all(path.startswith(sim_runtime.OBSTACLE_POOL_ROOT + "/") for path in members)
    assert pool[("desk", None)][1].endswith("_1")  # <slug>_<j>, j ascending
    # The flat order is the PARKING index — one derivation, two call sites.
    assert members == tuple(path for paths in pool.values() for path in paths)


def test_obstacle_slug_separates_buckets_that_differ_in_the_fourth_decimal():
    """A dimension-spelled slug would collapse these two into one pool."""
    a = sim_runtime.obstacle_slug(("box", (1.2, 0.4, 0.15)))
    b = sim_runtime.obstacle_slug(("box", (1.2, 0.4, 0.1501)))
    assert a != b and a.startswith("box_") and b.startswith("box_")
    assert sim_runtime.obstacle_slug(("chair", None)).startswith("chair_")
    # A direct ref is not a USD-safe name, so the readable half degrades to "usd".
    assert sim_runtime.obstacle_slug(("/mnt/props/crate.usd", None)).startswith("usd_")


def test_obstacle_park_position_is_a_countable_column_under_the_floor():
    first = sim_runtime.obstacle_park_position(0)
    second = sim_runtime.obstacle_park_position(1)
    assert first == (0.0, 0.0, sim_runtime.OBSTACLE_PARK_Z)
    # DEPTH, not distance: a horizontal 2D-lidar ray cannot reach it at ANY range.
    assert second[2] < first[2] < 0.0
    assert first[2] - second[2] == sim_runtime.OBSTACLE_PARK_PITCH


def test_obstacle_place_transform_lets_the_floor_own_z():
    box_position, box_orientation = sim_runtime.obstacle_place_transform(
        _obstacle(height=0.5, yaw=0.0), None
    )
    # box: the LEGACY height/2 centring, reused rather than re-derived.
    assert box_position == sim_runtime.debug_obstacle_position(_obstacle(height=0.5))
    assert box_orientation == (1.0, 0.0, 0.0, 0.0)
    asset = sim_runtime.ObstacleAsset(usd_path="/Isaac/x.usd", z_offset=0.25)
    position, orientation = sim_runtime.obstacle_place_transform(
        _obstacle("chair", yaw=math.pi / 2), asset
    )
    assert position == (1.0, 2.0, 0.25)  # the MEASURED offset, never a consumer input
    assert orientation == pytest.approx(sim_runtime.yaw_to_quat_wxyz(math.pi / 2))


def test_yaw_to_quat_wxyz_is_the_one_home_the_initial_pose_also_uses():
    pose = {"x": 1.0, "y": 2.0, "yaw": 3.1416}
    _position, orientation = sim_runtime.initial_pose_world_transform(pose, (0.0, 0.0, 0.37))
    assert orientation == sim_runtime.yaw_to_quat_wxyz(3.1416)
    w, x, y, z = sim_runtime.yaw_to_quat_wxyz(0.7)
    assert (x, y) == (0.0, 0.0) and w**2 + z**2 == pytest.approx(1.0)


def test_obstacle_placement_plan_assigns_by_declared_order_and_parks_the_surplus():
    pool = sim_runtime.obstacle_pool_paths({("chair", None): 1, ("desk", None): 2})
    entries = [_obstacle("desk", x=9.0), _obstacle("chair")]
    placed, parked = sim_runtime.obstacle_placement_plan(entries, pool)
    # j-th desk of the sample is always pool member desk_<hash>_j (stable mapping:
    # a contact event's prim path reads back to the declaration that put it there).
    assert [path for path, _ in placed] == [
        pool[("desk", None)][0],
        pool[("chair", None)][0],
    ]
    assert [entry["x"] for _path, entry in placed] == [9.0, 1.0]
    assert parked == [pool[("desk", None)][1]]  # the surplus is computed HERE
    assert set(parked).isdisjoint({path for path, _ in placed})


def test_an_empty_sample_parks_the_whole_pool():
    """``[]`` is NOT "nothing to do" — it is "this sample places nothing".

    Folding it into the no-pool branch would leave a 0-obstacle sample standing on
    the PREVIOUS sample's placement, judged against obstacles it never declared.
    """
    pool = sim_runtime.obstacle_pool_paths({("chair", None): 2})
    placed, parked = sim_runtime.obstacle_placement_plan([], pool)
    assert placed == []
    assert parked == list(sim_runtime.obstacle_pool_members(pool))


def test_obstacle_placement_plan_is_loud_when_the_pool_cannot_serve_it():
    pool = sim_runtime.obstacle_pool_paths({("chair", None): 1})
    with pytest.raises(ValueError, match="chair"):
        sim_runtime.obstacle_placement_plan([_obstacle("chair")] * 2, pool)
    with pytest.raises(ValueError, match="desk"):
        sim_runtime.obstacle_placement_plan([_obstacle("desk")], pool)


def test_obstacle_set_log_line_carries_the_marker_and_both_sides():
    pool = sim_runtime.obstacle_pool_paths({("chair", None): 2})
    placed, parked = sim_runtime.obstacle_placement_plan([_obstacle("chair", yaw=0.5)], pool)
    line = sim_runtime.obstacle_set_log_line(placed, parked)
    # The grep marker is the G-26 prove-it-ran handle NEG-6 gate 5 counts.
    assert f"{sim_runtime.OBSTACLE_SET_LOG_MARKER}1 parked=1 pool=2" in line
    assert placed[0][0] in line and parked[0] in line
    assert "(1.0, 2.0, 0.5)" in line  # what was DECLARED, next to where it went


def test_obstacle_physics_log_line_states_the_census_it_took():
    line = sim_runtime.obstacle_physics_log_line("/World/cv_obstacles/chair_0", "applied(C3)", 0, 0)
    assert sim_runtime.OBSTACLE_PHYSICS_LOG_MARKER in line
    assert "collider=applied(C3) rigid_body=0 articulation=0" in line


# --------------------------------------------------------------------------- #
# MVP oracles (REQ-EXEC-011).
# --------------------------------------------------------------------------- #
def _record(samples=None, events=None):
    return telemetry.TelemetryRecord(gt_pose_samples=samples or [], contact_events=events or [])


def test_reached_goal_pass():
    from cv_infra.oracles.reached_goal import ReachedGoalOracle

    rec = _record(samples=_line(4))
    criteria = {"goal_position": [3.0, 0.0, 0.0], "position_tolerance_m": 0.1, "timeout_s": 10}
    assert ReachedGoalOracle().evaluate(rec, criteria).passed is True


def test_reached_goal_not_reached_is_fail():
    from cv_infra.oracles.reached_goal import ReachedGoalOracle

    rec = _record(samples=_line(4))
    criteria = {"goal_position": [9.0, 0.0, 0.0], "position_tolerance_m": 0.1, "timeout_s": 10}
    out = ReachedGoalOracle().evaluate(rec, criteria)
    assert out.passed is False and out.reason == "not_reached"


def test_reached_goal_timeout_when_budget_exceeded():
    from cv_infra.oracles.reached_goal import ReachedGoalOracle

    rec = _record(samples=_line(4))  # reaches at sim_time 3
    criteria = {"goal_position": [3.0, 0.0, 0.0], "position_tolerance_m": 0.1, "timeout_s": 1}
    out = ReachedGoalOracle().evaluate(rec, criteria)
    assert out.passed is False and out.reason == "timeout"


def test_reached_goal_validate_requires_goal():
    from cv_infra.oracles.reached_goal import ReachedGoalOracle

    with pytest.raises(ValueError):
        ReachedGoalOracle().validate_params({})


def test_no_collision_negative_normal_drive_passes():
    from cv_infra.oracles.no_collision import NoCollisionOracle

    rec = _record(events=[ContactEvent(0.1, CHASSIS, "/World/ground")])
    criteria = {"chassis_path": CHASSIS, "collision_excluded_paths": ["/World/ground"]}
    assert NoCollisionOracle().evaluate(rec, criteria).passed is True


def test_no_collision_positive_obstacle_fails():
    from cv_infra.oracles.no_collision import NoCollisionOracle

    rec = _record(events=[ContactEvent(0.3, CHASSIS, "/World/obstacle")])
    criteria = {"chassis_path": CHASSIS, "collision_excluded_paths": ["/World/ground"]}
    out = NoCollisionOracle().evaluate(rec, criteria)
    assert out.passed is False and out.reason == "collision"


def test_reached_goal_orientation_gate_is_only_armed_by_a_declared_yaw():
    """The declared-orientation arm of the verdict ladder (p8c1 ``_yaw_out_of_tolerance``).

    Both directions matter: an UNDECLARED goal orientation must read as "not
    judged" (position-only goals are the norm — reading it as a failure would
    fail every shipped scenario), and a declared one must actually be able to
    fail a run that arrived at the right place facing the wrong way.
    """
    from cv_infra.oracles.reached_goal import ReachedGoalOracle, _yaw_out_of_tolerance

    rec = _record(samples=_line(4))  # every sample faces +X (yaw 0)
    criteria = {"goal_position": [3.0, 0.0, 0.0], "position_tolerance_m": 0.1, "timeout_s": 10}
    assert _yaw_out_of_tolerance(rec.gt_pose_samples[-1], criteria) is False

    quarter_turn = (math.cos(math.pi / 4), 0.0, 0.0, math.sin(math.pi / 4))  # 90deg about +Z
    out = ReachedGoalOracle().evaluate(rec, dict(criteria, goal_orientation_wxyz=quarter_turn))
    assert out.passed is False and out.reason == "orientation"
    # ...and a yaw tolerance wide enough to cover it passes the same trajectory.
    wide = dict(criteria, goal_orientation_wxyz=quarter_turn, yaw_tolerance_rad=math.pi)
    assert ReachedGoalOracle().evaluate(rec, wide).passed is True


def test_reached_goal_yaw_helpers():
    from cv_infra.oracles.reached_goal import angle_diff, yaw_from_quat_wxyz

    assert yaw_from_quat_wxyz((1.0, 0.0, 0.0, 0.0)) == pytest.approx(0.0)
    # 90deg about +Z -> quat (cos45, 0, 0, sin45)
    assert yaw_from_quat_wxyz(
        (math.cos(math.pi / 4), 0.0, 0.0, math.sin(math.pi / 4))
    ) == pytest.approx(math.pi / 2)
    assert angle_diff(math.pi + 0.1, -math.pi + 0.1) == pytest.approx(0.0, abs=1e-9)


# --------------------------------------------------------------------------- #
# Result assembly + guards.
# --------------------------------------------------------------------------- #
def test_build_result_dict_is_canonical_m1_shape():
    # SEAM-1 (G-17): canonical keys are job_id / criteria_results / oracle / artifacts.
    outs = [_outcome("reached_goal", True), _outcome("no_collision", True)]
    metrics = {"time_to_goal_s": 3.0, "collision_count": 0, "path_len_m": 3.0}
    result = evaluate.build_result_dict("job-0001", "pass", outs, metrics)
    assert result["job_id"] == "job-0001"
    assert result["verdict"] == "pass"
    assert result["metrics"]["min_clearance_m"] is None  # optional in P2
    assert {c["oracle"] for c in result["criteria_results"]} == {"reached_goal", "no_collision"}
    assert result["artifacts"] == {"mcap": None, "mp4": None}  # fields always present


def test_eula_boot_guard_blocks_without_consent():
    with pytest.raises(sim_runtime.EulaNotAcceptedError):
        sim_runtime.eula_boot_guard({})


def test_eula_boot_guard_allows_with_consent():
    # opaque token: truthiness check; no consent literal (2026-07-03 decision)
    sim_runtime.eula_boot_guard({"ACCEPT_EULA": "operator-consent-token"})  # no raise


def test_honored_env_reads_injected_isolation_env():
    env = {
        "ROS_DOMAIN_ID": "42",
        "RMW_IMPLEMENTATION": "rmw_fastrtps_cpp",
        "LD_LIBRARY_PATH": "/opt/isaacsim.ros2.bridge/jazzy/lib:/x",
    }
    got = ros_bridge.honored_env(env)
    assert got.ros_domain_id == "42"
    assert got.rmw_implementation == "rmw_fastrtps_cpp"
    assert got.jazzy_on_ld_path is True


# --------------------------------------------------------------------------- #
# contact_partners (p2c5 bring-up debug surface for excluded_paths measurement).
# --------------------------------------------------------------------------- #
def test_contact_partners_names_distinct_non_chassis_actors():
    from cv_infra.runner.telemetry import ContactEvent, contact_partners

    chassis = "/World/Robot/chassis_link"
    events = [
        ContactEvent(0.1, chassis, "/World/wall_a"),
        ContactEvent(0.2, "/World/wall_a", chassis),  # order-insensitive
        ContactEvent(0.3, chassis, "/World/floor"),
        ContactEvent(0.4, chassis, chassis),  # degenerate self-pair kept visible
    ]
    assert contact_partners(events, chassis) == [
        "/World/Robot/chassis_link",
        "/World/floor",
        "/World/wall_a",
    ]


# --------------------------------------------------------------------------- #
# G-72 — the INSTRUMENT'S OWN determinism. The p5c15 observation ("a stopped
# robot, one seed, yet the contact-partner set moves run to run") was not the
# engine: contact_partners picked one of two actors by set-iteration (hash)
# order. Measure the instrument before attributing spread to the system.
# --------------------------------------------------------------------------- #
#: Articulation-aggregated pairs: NEITHER actor is the chassis (wheel/caster
#: <-> ground), which is the shape that leaves TWO paths in the "others" set —
#: the case the pre-existing test above never fed in (G-70 input diversity).
_AGGREGATED_EVENTS = [
    ContactEvent(0.1, "/World/warehouse/GroundPlane", "/World/carter/wheel_left"),
    ContactEvent(0.2, "/World/warehouse/GroundPlane", "/World/carter/wheel_right"),
    ContactEvent(0.3, "/World/warehouse/GroundPlane", "/World/carter/caster"),
]


def test_contact_partners_names_both_actors_when_neither_is_the_chassis():
    # The documented contract is "distinct non-chassis actor paths seen in
    # contact events" — with the chassis absent from the pair, BOTH actors are
    # such paths. Naming one of them dropped a real partner from the debug
    # surface AND made the choice hash-order dependent.
    assert telemetry.contact_partners(_AGGREGATED_EVENTS, CHASSIS) == [
        "/World/carter/caster",
        "/World/carter/wheel_left",
        "/World/carter/wheel_right",
        "/World/warehouse/GroundPlane",
    ]


def test_contact_partners_output_is_hash_seed_independent():
    """Same input into the instrument N times -> the same value out (G-72).

    Child processes: ``PYTHONHASHSEED`` is read at interpreter startup only, so
    an in-process ``os.environ`` poke would measure nothing. The seeds are
    PINNED here (not left to pytest's ambient randomization) so the lock is
    deterministic rather than flaky. Positive control (p5c16 T3, measured): with
    ``next(iter(others))`` restored, these same ten seeds returned SIX different
    answers and this test goes red.
    """
    pairs = [(e.actor0_path, e.actor1_path) for e in _AGGREGATED_EVENTS]
    code = (
        "from cv_infra.runner.telemetry import ContactEvent, contact_partners\n"
        f"events = [ContactEvent(0.0, a, b) for a, b in {pairs!r}]\n"
        f"print(contact_partners(events, {CHASSIS!r}))\n"
    )
    # PYTHONPATH stripped like the other child-process tests here: this host's
    # shell leaks a ROS overlay into it (G-41), and the child must import the
    # venv's cv_infra, not a py3.10 overlay.
    base_env = {k: v for k, v in os.environ.items() if k != "PYTHONPATH"}
    by_output: dict[str, list[str]] = {}
    for seed in (str(i) for i in range(10)):
        proc = subprocess.run(
            [sys.executable, "-c", code],
            env=dict(base_env, PYTHONHASHSEED=seed),
            capture_output=True,
            text=True,
            timeout=60,
        )
        assert proc.returncode == 0, proc.stderr
        by_output.setdefault(proc.stdout.strip(), []).append(seed)
    assert len(by_output) == 1, (
        "contact_partners output depends on PYTHONHASHSEED — the instrument is "
        f"non-deterministic (G-72): {by_output}"
    )


# --------------------------------------------------------------------------- #
# Seams both entrypoints share (p8c1 — extracted out of the two GPU ``run``
# bodies, so the CPU tests that could not reach them there live here).
# --------------------------------------------------------------------------- #
def test_artifact_paths_renders_present_and_absent_recordings():
    from pathlib import Path as _Path

    assert main.artifact_paths(_Path("/cv/out/bag/bag_0.mcap"), _Path("/cv/out/run.mp4")) == {
        "mcap": "/cv/out/bag/bag_0.mcap",
        "mp4": "/cv/out/run.mp4",
    }
    # P2-02 honest degradation: a recorder that produced nothing leaves null,
    # never "" — the field is read by M3/M8 as "there is no artifact".
    assert main.artifact_paths(None, None) == {"mcap": None, "mp4": None}
    assert main.artifact_paths(None, _Path("/cv/out/run.mp4"))["mcap"] is None


def test_abort_recorders_aborts_what_exists_and_skips_what_does_not():
    class _Rec:
        def __init__(self):
            self.aborted = 0

        def abort(self):
            self.aborted += 1

    bag, video = _Rec(), _Rec()
    main._abort_recorders(bag, None, video)  # the None arm is the failure path
    assert (bag.aborted, video.aborted) == (1, 1)


def test_announce_plugin_dir_prints_only_when_one_was_injected(tmp_path, capsys, monkeypatch):
    monkeypatch.setattr(sys, "path", list(sys.path))  # insertion is in-place
    assert main.announce_oracle_plugin_dir({}) is None
    assert capsys.readouterr().out == ""
    assert main.announce_oracle_plugin_dir({"CV_ORACLE_PLUGIN_DIR": str(tmp_path)}) == str(tmp_path)
    assert capsys.readouterr().out == f"[cv-runner] oracle plugin dir on sys.path: {tmp_path}\n"
    assert sys.path[0] == str(tmp_path)


def test_plan_obstacle_pool_plans_one_jobs_pool_and_rejects_an_over_cap_one():
    obstacles = [{"asset": "box", "count": 1, "x": 1.0, "y": 2.0}] * 3
    assert sum(main.plan_obstacle_pool(obstacles).values()) == 3
    # Over-cap is BAD INPUT (exit 2), not a platform failure after a paid boot.
    with pytest.raises(main.BadJobSpec) as excinfo:
        main.plan_obstacle_pool([{"asset": "box", "count": 1, "x": 0.0, "y": 0.0}] * 999)
    assert "scenario.obstacles" in str(excinfo.value)


def test_contact_partner_line_is_emitted_verbatim_and_only_when_there_was_contact(capsys):
    """The bring-up line both entrypoints print — byte-exact (§3-2 log syntax)."""
    quiet = telemetry.TelemetryRecord()
    main._print_contact_partners(quiet, CHASSIS)
    assert capsys.readouterr().out == ""

    record = telemetry.TelemetryRecord(
        contact_events=[
            ContactEvent(sim_time_s=1.0, actor0_path=CHASSIS, actor1_path="/World/box"),
            ContactEvent(sim_time_s=1.1, actor0_path="/World/box", actor1_path=CHASSIS),
        ]
    )
    main._print_contact_partners(record, CHASSIS)
    assert capsys.readouterr().out == (
        "[cv-runner] contact events: 2 with 1 distinct partner prim(s): ['/World/box']\n"
    )


def test_closest_approach_line_is_emitted_verbatim_and_stays_silent_without_data(capsys):
    samples = [
        PoseSample(sim_time_s=0.0, position=(0.0, 0.0, 0.0), orientation_wxyz=(1.0, 0.0, 0.0, 0.0)),
        PoseSample(sim_time_s=1.0, position=(2.4, 0.0, 0.0), orientation_wxyz=(1.0, 0.0, 0.0, 0.0)),
    ]
    record = telemetry.TelemetryRecord(gt_pose_samples=samples)
    main._print_closest_approach(record, [3.0, 0.0, 0.0])
    assert capsys.readouterr().out == "[cv-runner] GT closest-approach to goal: 0.600 m\n"
    # No trajectory / no goal -> no line at all (an absent debug line, never a
    # fake one: a "0.000 m" here would read as "it arrived").
    main._print_closest_approach(telemetry.TelemetryRecord(), [3.0, 0.0, 0.0])
    main._print_closest_approach(record, None)
    assert capsys.readouterr().out == ""


# --------------------------------------------------------------------------- #
# p8c2 — the surfaces the branch census found unmeasured. Each one is a path the
# runner takes in production but no CPU test had ever driven.
# --------------------------------------------------------------------------- #
def test_read_field_reads_an_attribute_object_the_same_way_it_reads_a_dict():
    """``read_field`` is the ONE accessor both oracles use, and criteria reach it
    in two shapes: a plain dict (batch/JOB_SPEC path) and an M1 pydantic model
    (``AcceptanceCriterion.params`` after model_validate). If the attribute arm
    ever diverged, an oracle would silently read its default off a typed
    criteria object and judge the mission against the wrong tolerance."""

    class _TypedCriteria:  # the attribute shape an M1 model presents
        goal_position = [1.0, 2.0, 3.0]
        timeout_s = 30.0

    typed = _TypedCriteria()
    assert evaluate.read_field(typed, "goal_position") == [1.0, 2.0, 3.0]
    assert evaluate.read_field(typed, "timeout_s") == 30.0
    assert evaluate.read_field(typed, "absent") is None
    assert evaluate.read_field(typed, "absent", 0.25) == 0.25
    # ...and identically off the dict form, which is what makes them one accessor.
    as_dict = {"goal_position": [1.0, 2.0, 3.0], "timeout_s": 30.0}
    assert evaluate.read_field(as_dict, "goal_position") == [1.0, 2.0, 3.0]
    assert evaluate.read_field(as_dict, "absent", 0.25) == 0.25


def test_time_to_goal_is_none_when_the_mission_produced_no_samples():
    """An empty trajectory means "we never observed the robot", which must read as
    "no time-to-goal" — NOT as 0.0 s, the value a bare ``samples[0]`` index error
    would have to be papered over with and the one a report renders as instant."""
    assert telemetry.time_to_goal_s([], (3.0, 0.0, 0.0), 0.1) is None


def test_latest_pose_reports_the_newest_gt_sample_and_none_before_any():
    """The batch carrier's realign seed (AR-19) reads this right after its settle
    pump: the NEWEST sample is where the robot came to rest, and it is the value
    ``/initialpose`` must carry. ``None`` before anything was sampled is what makes
    ``realign_seed`` fall back to the declared pose instead of seeding the origin.

    Owned here (G-110): this accessor's two arms belong to the telemetry suite, not
    to whatever other test happens to build a sampler.
    """
    sampler = telemetry.PhysicsTelemetrySampler(CHASSIS, ["/World/ground"])
    assert sampler.latest_pose() is None  # nothing sampled yet

    sampler.record.gt_pose_samples.extend(_line(3))
    assert sampler.latest_pose() == _line(3)[-1]

    # The accessor follows ``self.record``, which the carrier SWAPS per sample —
    # a fresh accumulator must read as "nothing sampled", never as the retired
    # sample's last pose.
    sampler.record = telemetry.TelemetryRecord()
    assert sampler.latest_pose() is None


def test_telemetry_detach_is_cpu_safe_and_idempotent_before_any_bind():
    """``main.run``'s ``finally`` calls ``detach()`` on EVERY path, including the
    ones that died before ``bind`` ever ran on GPU. A raise here would replace the
    real failure with a teardown traceback (REQ-EXEC-015 clean shutdown)."""
    sampler = telemetry.PhysicsTelemetrySampler(CHASSIS, ["/World/ground"])
    sampler.detach()
    sampler.detach()  # idempotent: the batch carrier detaches once per sample
    assert sampler._contact_sub is None  # the subscription ref is what unsubscribes
    assert sampler._chassis_prim is None


def test_no_collision_without_a_chassis_path_reports_bad_criteria_instead_of_passing():
    """D-E/R7: the filter is meaningless without the chassis prim, so the oracle
    must FAIL LOUD. Passing would be the worst outcome — an unfilterable run would
    read as "no collisions" and ship a green verdict for an unjudged mission."""
    from cv_infra.oracles.no_collision import NoCollisionOracle

    rec = _record(events=[ContactEvent(0.3, CHASSIS, "/World/obstacle")])
    out = NoCollisionOracle().evaluate(rec, {"collision_excluded_paths": ["/World/ground"]})
    assert out.passed is False
    assert out.reason == "bad_criteria"
    assert "chassis_path" in out.detail


def test_reached_goal_separates_bad_criteria_from_an_empty_trajectory():
    """Two DIFFERENT unjudgeable states, and the reason tag is what tells them
    apart in the result: no goal = the request was wrong; no samples = the mission
    produced nothing (telemetry never bound / the sim died). Both must fail — a
    pass here would be a verdict about a mission nobody observed."""
    from cv_infra.oracles.reached_goal import ReachedGoalOracle

    no_goal = ReachedGoalOracle().evaluate(_record(samples=_line(4)), {})
    assert (no_goal.passed, no_goal.reason) == (False, "bad_criteria")

    no_samples = ReachedGoalOracle().evaluate(
        _record(samples=[]), {"goal_position": [3.0, 0.0, 0.0], "position_tolerance_m": 0.1}
    )
    assert (no_samples.passed, no_samples.reason) == (False, "no_telemetry")


def test_parse_request_accepts_the_nested_sut_block_not_only_the_flat_pin():
    """The JOB_SPEC wire allows BOTH spellings: the flattened ``sut_image_ref``
    (T1 seam, what M3 dispatches) and the canonical nested ``sut`` block (what a
    scenario document carries). The flat one is adapted INTO the nested one, so
    the nested path must survive untouched — and declaring both stays ambiguous."""
    spec = _randomizable_spec()
    del spec["sut_image_ref"]
    spec["sut"] = {"image_ref": "sut:nested"}
    request, _adapter = main.parse_request(spec)
    assert request.sut.image_ref == "sut:nested"

    with pytest.raises(main.BadJobSpec, match="ambiguous SUT pin"):
        main.parse_request({**spec, "sut_image_ref": "sut:flat"})


def test_main_maps_a_refused_eula_to_the_platform_exit_code(monkeypatch, capsys):
    """``run`` RE-RAISES ``EulaNotAcceptedError`` (it must not be folded into the
    generic error result), so this outer handler is the production owner of the
    NEG-2 exit code. The subprocess proof lives in tests/negative/test_eula_gate.py;
    this pins the mapping itself, in-process, without a GPU-shaped boot."""

    def _refuse(_env):
        raise sim_runtime.EulaNotAcceptedError("Isaac Sim EULA not accepted — boot refused")

    monkeypatch.setattr(main, "run", _refuse)
    assert main.main({}) == main.EXIT_PLATFORM == 3
    assert "boot refused" in capsys.readouterr().err


def test_step_before_load_scene_is_a_loud_order_violation():
    """M2 §3.2 order: ``world`` exists only after ``load_scene``. Stepping before
    it must name the missing step — an ``AttributeError`` on ``None.step`` would
    reach M3 as an anonymous platform error."""
    sim = sim_runtime.SimRuntime(sim_runtime.SimConfig(scene_ref="s.usd", robot_usd_ref="r.usd"))
    with pytest.raises(RuntimeError, match="load_scene"):
        sim.step()


def test_repose_log_line_states_both_the_declared_pose_and_the_written_one():
    """G-26 prove-it-ran: a repose that silently did nothing and one that never ran
    read the same in a log. The line carries the marker NEG-6 greps plus BOTH
    sides — declared (planar x/y/yaw) and written (the asset's own z) — because
    they are different objects and only the pair shows the repose took effect."""
    line = sim_runtime.repose_log_line(
        "/World/Nova_Carter",
        {"x": -6.0, "y": -1.5, "yaw": 1.57},
        (-6.0, -1.5, 0.24),
        (0.707, 0.0, 0.0, 0.707),
    )
    assert line.startswith(f"[cv-runner] {sim_runtime.REPOSE_LOG_MARKER}/World/Nova_Carter ")
    assert "declared={'x': -6.0, 'y': -1.5, 'yaw': 1.57}" in line
    assert "position=(-6.0, -1.5, 0.24)" in line
    assert "orientation_wxyz=(0.707, 0.0, 0.0, 0.707)" in line
    assert "joint POSITIONS untouched" in line  # the scope this repose does NOT claim


def test_repose_puts_a_composed_robot_back_at_its_own_drop_height():
    """C5 measured the coupling this closes: sample 0 starts at the row's drop
    height (go2: 0.32) but sample 1's repose read the LIVE base z — 0.1795 m,
    wherever the last gait cycle left it — so every sample would get a different
    drop. A row that composes no robot keeps the asset's own z, i.e. carter's
    pre-C5 behaviour."""
    go2 = sim_runtime.SCENE_ASSETS["go2_warehouse"]
    carter = sim_runtime.SCENE_ASSETS["nova_carter_warehouse"]
    assert sim_runtime.repose_height(0.1795, go2) == go2.robot_spawn_z == 0.32
    assert sim_runtime.repose_height(0.24, carter) == 0.24  # nothing declared -> unchanged


def test_repose_log_line_says_when_it_restored_a_stance():
    """C5: "restored 12 joints" and "left the joints alone" are BOTH correct
    answers depending on the robot, and a reader of a legged sample's log has no
    other way to tell which one this repose did (G-26)."""
    stance = sim_runtime.SCENE_ASSETS["go2_warehouse"].default_joint_pos
    line = sim_runtime.repose_log_line(
        "/World/Go2", {"x": -6.0, "y": -1.0, "yaw": 0.0}, (-6.0, -1.0, 0.32), (1.0, 0, 0, 0), stance
    )
    assert "joint POSITIONS restored to the declared stance (12 dof)" in line
    assert "untouched" not in line  # the carter sentence must not survive next to it
