"""CPU unit tests for the runner's contract consumption: JOB_SPEC -> typed schema.

D-4' (P3 cycle-2): the parse chain REALLY calls ``contract.schema``
``model_validate`` (M1 pydantic canon — G-17: single shape definition) while the
JOB_SPEC *wire* stays byte-identical to the Phase-2 seam (job_id +
flattened sut_image_ref). Proves contract violations map to BadJobSpec/exit 2
pre-sim (no Isaac touched, no result.json emitted), that ``debug_obstacle`` is
consumed from its D-2' home (``scenario.debug_obstacle``, criteria ride-along
loud-rejected), and that the ros2 adapter's goal interface comes from the M1
adapter_schema (module DEFAULT_* constants removed).

Section (5) (p5c11) covers the two knobs whose consumers landed this cycle —
``scenario.initial_pose`` (REQ-EXEC-002 / CEO D-2) and
``execution_settings.fixed_dt`` (D-8) — including the mechanical
producer/consumer binds that keep the field names from drifting (G-17) and the
undeclared-path no-op that protects every pre-p5c11 scenario.
"""

import json

import pytest

from cv_infra.contract.adapter_schema import GoalInterface, Ros2AdapterConfig
from cv_infra.contract.schema import Goal, InitialPose, Scenario, VerificationRequest
from cv_infra.oracles.reached_goal import angle_diff, yaw_from_quat_wxyz
from cv_infra.runner import main, sim_runtime
from cv_infra.runner.adapter import ros2


def _valid_spec() -> dict:
    """A canonical JOB_SPEC dict (Phase-2 wire — frozen T1 seam, D-4')."""
    return {
        "job_id": "job-0001",
        "scenario": {
            "scene": "omniverse://assets/warehouse.usd",
            "robot": "omniverse://assets/nova_carter_ros.usd",
            "goal": {"x": 3.0, "y": -1.5, "yaw": 0.0},
            "seed": 7,
            "timeout_s": 120.0,
        },
        "sut_image_ref": "carter-sut:p2",
        "interface": {
            "type": "ros2",
            "adapter_config": {
                "odom_topics": ["/odom", "/chassis/odom"],
                "sensors": [
                    {
                        "topic": "/front_3d_lidar/lidar_points",
                        "type": "sensor_msgs/msg/PointCloud2",
                        "frame": "front_3d_lidar",
                    }
                ],
            },
        },
        "acceptance_criteria": [
            {"oracle": "reached_goal", "params": {"position_tolerance_m": 0.3}},
            {
                "oracle": "no_collision",
                "params": {
                    "chassis_path": "/World/carter/chassis",
                    "collision_excluded_paths": ["/World/ground"],
                },
            },
        ],
    }


# --------------------------------------------------------------------------- #
# (1) Valid JOB_SPEC -> real model_validate chain -> typed objects.
# --------------------------------------------------------------------------- #
def test_parse_request_builds_typed_schema_objects():
    request, cfg = main.parse_request(_valid_spec())
    assert isinstance(request, VerificationRequest)
    assert isinstance(request.scenario.goal, Goal)
    assert isinstance(cfg, Ros2AdapterConfig)
    assert cfg is request.interface.adapter_config  # single validation pass
    assert request.sut.image_ref == "carter-sut:p2"  # flattened wire -> sut.image_ref
    assert (request.scenario.goal.x, request.scenario.goal.y) == (3.0, -1.5)
    assert [c.oracle for c in request.acceptance_criteria] == ["reached_goal", "no_collision"]
    assert cfg.odom_topics == ["/odom", "/chassis/odom"]
    assert cfg.sensors[0].frame == "front_3d_lidar"


def test_parse_request_does_not_mutate_the_wire_dict():
    spec = _valid_spec()
    main.parse_request(spec)
    assert spec == _valid_spec()  # the JOB_SPEC wire (T1 seam) is read-only here


def test_criteria_view_flattens_scenario_and_params():
    request, _ = main.parse_request(_valid_spec())
    view = main.criteria_view(request)
    assert view["goal_position"] == [3.0, -1.5, 0.0]  # planar goal (z=0.0)
    assert view["timeout_s"] == 120.0  # sim-time budget (D-F)
    assert view["position_tolerance_m"] == 0.3  # from reached_goal params
    assert view["chassis_path"] == "/World/carter/chassis"  # from no_collision params
    assert view["collision_excluded_paths"] == ["/World/ground"]


def test_criteria_view_omits_unset_known_key_params():
    # Known-key params left unset (None) must NOT shadow the oracle defaults:
    # the merge is exclude_none, so read_field's default still applies.
    request, _ = main.parse_request(_valid_spec())
    view = main.criteria_view(request)
    assert "yaw_tolerance_rad" not in view
    assert "goal_orientation_wxyz" not in view


def test_criteria_view_custom_criterion_params_merge_on_top():
    # The override path (e.g. a non-ground goal_position) survives for custom
    # oracles: their params stay a free mapping and merge as-is.
    spec = _valid_spec()
    spec["acceptance_criteria"].append(
        {"oracle": "my_pkg.checks:MyOracle", "params": {"goal_position": [3.0, -1.5, 0.4]}}
    )
    request, _ = main.parse_request(spec)
    assert main.criteria_view(request)["goal_position"] == [3.0, -1.5, 0.4]


# --------------------------------------------------------------------------- #
# (2) debug_obstacle — D-2' home = scenario; criteria ride-along is superseded.
# --------------------------------------------------------------------------- #
def test_debug_obstacle_parses_from_scenario_home():
    spec = _valid_spec()
    spec["scenario"]["debug_obstacle"] = {"x": -6.0, "y": 2.0, "height": 0.15}
    request, _ = main.parse_request(spec)
    obstacle = request.scenario.debug_obstacle
    assert (obstacle.x, obstacle.y, obstacle.height) == (-6.0, 2.0, 0.15)
    # None dimensions mean "runner default applies" — dropped from the spawn dict.
    assert obstacle.model_dump(exclude_none=True) == {"x": -6.0, "y": 2.0, "height": 0.15}


def test_debug_obstacle_absent_is_none():
    request, _ = main.parse_request(_valid_spec())
    assert request.scenario.debug_obstacle is None


def test_debug_obstacle_in_criteria_params_is_loud_rejected():
    # The P2 free-form ride-along home is superseded (D-2'): known-key MVP
    # params forbid it, so the old shape fails loudly instead of silently.
    spec = _valid_spec()
    spec["acceptance_criteria"][1]["params"]["debug_obstacle"] = {"x": -6.0, "y": 2.0}
    with pytest.raises(main.BadJobSpec):
        main.parse_request(spec)


# --------------------------------------------------------------------------- #
# (3) Contract violations -> BadJobSpec -> exit 2 (usage), pre-sim.
# --------------------------------------------------------------------------- #
def test_parse_request_missing_scenario_key_raises_usage():
    spec = _valid_spec()
    del spec["scenario"]["goal"]
    with pytest.raises(main.BadJobSpec):
        main.parse_request(spec)


def test_parse_request_unknown_adapter_config_key_raises_usage():
    spec = _valid_spec()
    spec["interface"]["adapter_config"]["topic_map"] = {}  # not a canonical key
    with pytest.raises(main.BadJobSpec):
        main.parse_request(spec)


def test_parse_request_unknown_nested_adapter_key_raises_usage():
    spec = _valid_spec()
    spec["interface"]["adapter_config"]["goal_interface"] = {"nmae": "/typo"}
    with pytest.raises(main.BadJobSpec):
        main.parse_request(spec)


def test_parse_request_unknown_criteria_param_key_raises_usage():
    """Runner-seam guard for the criteria params (p5c14 defect ③, measured LOUD).

    The contract layer forbids extras on ``ReachedGoalParams``; what matters for
    the runner is that its OWN path (parse_request -> oracles read a dict via
    read_field) can never let one through — an unread key would be applied as
    nothing at all and the run would silently judge with a different tolerance
    (the ``goal_tolerance_m`` lesson, G-25).
    """
    spec = _valid_spec()
    spec["acceptance_criteria"][0]["params"]["goal_tolerance_budget_m"] = 0.85
    with pytest.raises(main.BadJobSpec) as excinfo:
        main.parse_request(spec)
    assert "goal_tolerance_budget_m" in str(excinfo.value)


def test_parse_request_unknown_key_inside_goal_tolerance_budget_raises_usage():
    """Same guard one level deeper — the budget block is where a plausible-looking
    extra term (an overshoot allowance) would be silently dropped from the sum."""
    spec = _valid_spec()
    spec["acceptance_criteria"][0]["params"] = {
        "goal_tolerance_budget": {
            "sut_xy_goal_tolerance_m": 0.25,
            "localization_budget_m": 0.60,
            "overshoot_m": 0.10,
        }
    }
    with pytest.raises(main.BadJobSpec) as excinfo:
        main.parse_request(spec)
    assert "goal_tolerance_budget.overshoot_m" in str(excinfo.value)


def test_parse_request_accepts_the_same_budget_without_the_extra_key():
    """Positive control (G-07): the two rejections above are caused by the extra
    key, not by the budget shape itself."""
    spec = _valid_spec()
    spec["acceptance_criteria"][0]["params"] = {
        "goal_tolerance_budget": {"sut_xy_goal_tolerance_m": 0.25, "localization_budget_m": 0.60}
    }
    request, _ = main.parse_request(spec)
    budget = request.acceptance_criteria[0].params.goal_tolerance_budget
    assert (budget.sut_xy_goal_tolerance_m, budget.localization_budget_m) == (0.25, 0.60)


def test_parse_request_non_ros2_interface_raises_usage():
    spec = _valid_spec()
    spec["interface"]["type"] = "grpc"
    with pytest.raises(main.BadJobSpec):
        main.parse_request(spec)


def test_parse_request_ambiguous_sut_pin_raises_usage():
    spec = _valid_spec()
    spec["sut"] = {"image_ref": "other:tag"}  # + the wire's sut_image_ref
    with pytest.raises(main.BadJobSpec):
        main.parse_request(spec)


def test_parse_request_error_carries_friendly_field_path():
    # The M1 friendly-error prose (contract.errors) names the violating field —
    # never a raw pydantic traceback dump (NFR-INTAKE-001 direction).
    spec = _valid_spec()
    del spec["scenario"]["goal"]
    with pytest.raises(main.BadJobSpec) as excinfo:
        main.parse_request(spec)
    assert "scenario.goal" in str(excinfo.value)


def test_main_exits_2_on_bad_spec_and_emits_no_result(tmp_path):
    # End-to-end through main(): the parse is pre-sim, so exit 2 happens without
    # Isaac (CPU-safe) and WITHOUT a result.json (bad input is not a Result).
    spec = _valid_spec()
    spec["interface"]["adapter_config"]["topic_map"] = {}
    env = {"JOB_SPEC": json.dumps(spec), "RESULT_OUT": str(tmp_path)}
    assert main.main(env) == main.EXIT_USAGE
    assert not (tmp_path / "result.json").exists()


# --------------------------------------------------------------------------- #
# (4) Goal interface follows adapter_config — no module constants (FU-13 (3)).
# --------------------------------------------------------------------------- #
def test_goal_interface_follows_config():
    cfg = Ros2AdapterConfig.model_validate(
        {
            "goal_interface": {
                "kind": "topic",
                "name": "/goal_pose",
                "type": "geometry_msgs/msg/PoseStamped",
            }
        }
    )
    adapter = ros2.Ros2Adapter(cfg)
    assert adapter.goal_interface.kind == "topic"
    assert adapter.goal_interface.name == "/goal_pose"
    assert adapter.goal_interface.type == "geometry_msgs/msg/PoseStamped"


def test_goal_interface_defaults_come_from_schema_not_module_constants():
    adapter = ros2.Ros2Adapter()  # config-less -> schema defaults (single definition)
    default = GoalInterface()
    got = adapter.goal_interface
    assert (got.kind, got.name, got.type) == (default.kind, default.name, default.type)
    assert not hasattr(ros2, "DEFAULT_GOAL_ACTION")
    assert not hasattr(ros2, "DEFAULT_GOAL_ACTION_TYPE")


# --------------------------------------------------------------------------- #
# (5) scenario.initial_pose + execution_settings.fixed_dt -> SimConfig
#     (p5c11 T4: REQ-EXEC-002 consumption / CEO D-2, and D-8's fixed_dt).
# --------------------------------------------------------------------------- #
DECLARED_POSE = {"x": -6.0, "y": -1.0, "yaw": 3.1416}


def _request(initial_pose: dict | None = None, fixed_dt: float | None = None):
    """The canonical spec as the ADMITTED ``VerificationRequest`` (producer side)."""
    spec = _valid_spec()
    doc = {
        "scenario": dict(spec["scenario"]),
        "sut": {"image_ref": spec["sut_image_ref"]},
        "interface": spec["interface"],
        "acceptance_criteria": spec["acceptance_criteria"],
    }
    if initial_pose is not None:
        doc["scenario"]["initial_pose"] = initial_pose
    if fixed_dt is not None:
        doc["execution_settings"] = {"fixed_dt": fixed_dt}
    return VerificationRequest.model_validate(doc)


def test_undeclared_initial_pose_is_a_no_op_all_the_way_to_the_prim():
    # ★ The biggest regression risk of the whole wiring (CEO D-2): a scenario
    # that says nothing about the spawn pose must leave the scene asset's own
    # robot placement ALONE. A (0, 0, 0) default would teleport every pre-p5c11
    # robot to the world origin. Proven at each hop, not just at the config.
    request, _ = main.parse_request(_valid_spec())
    assert request.scenario.initial_pose is None  # contract hop
    config = main.sim_config_for(request)  # runner-config hop
    assert config.initial_pose is None
    # apply hop: with no pose there is no target prim, so apply_initial_pose is
    # never called even though the robot prim resolved fine.
    assert (
        sim_runtime.resolve_initial_pose_target(config.initial_pose, "/World/Nova_Carter_ROS")
        is None
    )


def test_declared_initial_pose_reaches_sim_config_verbatim():
    request, _ = main.parse_request(
        {**_valid_spec(), "scenario": {**_valid_spec()["scenario"], "initial_pose": DECLARED_POSE}}
    )
    config = main.sim_config_for(request)
    assert config.initial_pose == DECLARED_POSE  # x/y/yaw, names unchanged
    assert sim_runtime.resolve_initial_pose_target(config.initial_pose, "/World/X") == "/World/X"


def test_declared_initial_pose_without_a_resolved_robot_prim_is_loud():
    # A pose the runner cannot honour must NOT be silently dropped (G-25's
    # goal_tolerance_m pattern): direct .usd scene refs carry no robot prim.
    with pytest.raises(RuntimeError, match="initial_pose"):
        sim_runtime.resolve_initial_pose_target(DECLARED_POSE, None)


def test_initial_pose_transform_keeps_the_assets_height_and_yaws_about_z():
    position, orientation = sim_runtime.initial_pose_world_transform(
        DECLARED_POSE, current_position=(1.0, 2.0, 0.37)
    )
    assert position == (-6.0, -1.0, 0.37)  # z comes from the asset, never the consumer
    # Round-trip through the oracle's OWN quaternion->yaw reader, compared wrapped:
    # the contract's example 3.1416 sits a hair PAST pi, so atan2 returns the
    # equivalent negative angle. Same rotation, and angle_diff is how the rest of
    # the runner already compares headings.
    assert angle_diff(yaw_from_quat_wxyz(orientation), DECLARED_POSE["yaw"]) == pytest.approx(
        0.0, abs=1e-9
    )
    w, x, y, z = orientation
    assert (x, y) == (0.0, 0.0)  # pure +Z rotation: no roll/pitch smuggled in
    assert w**2 + z**2 == pytest.approx(1.0)


def test_undeclared_fixed_dt_leaves_the_step_at_one_sixtieth():
    # T3's wire contract: an undeclared knob leaves behaviour unchanged. NOT a
    # determinism claim (D-8) — 1/60 was already the fixed step.
    config = main.sim_config_for(main.parse_request(_valid_spec())[0])
    assert config.physics_dt == pytest.approx(1.0 / 60.0)
    assert config.rendering_dt == pytest.approx(1.0 / 60.0)


def test_declared_fixed_dt_drives_both_physics_and_rendering_dt():
    spec = {**_valid_spec(), "execution_settings": {"fixed_dt": 0.02}}
    config = main.sim_config_for(main.parse_request(spec)[0])
    # render_interval 1 (every scene but go2) reproduces the pre-B-5 coupling.
    assert (config.physics_dt, config.rendering_dt) == (0.02, 0.02)


def test_a_go2_scene_decimates_the_render_and_leaves_the_physics_step_alone():
    """B-5/AR-17: the render interval is the SCENE's property (the training cfg's
    own ``sim.render_interval`` = 4), not a consumer knob.

    Measured motivation: with the two coupled, ``fixed_dt: 0.005`` renders at 200
    Hz and the go2 job runs at RTF 0.318 with its sensors on (C3 §4-1); C2b
    measured the same scene at 1.72 with rendering_dt 0.02. The physics step —
    the plant the policy was trained in — must NOT move with it."""
    spec = {
        **_valid_spec(),
        "scenario": {**_valid_spec()["scenario"], "scene": "go2_warehouse", "robot": "go2"},
        "execution_settings": {"fixed_dt": 0.005},
    }
    config = main.sim_config_for(main.parse_request(spec)[0])
    assert config.physics_dt == 0.005  # 200 Hz plant, untouched
    assert config.rendering_dt == pytest.approx(0.02)  # 50 Hz render = 4 physics steps


def test_an_undeclared_dt_on_a_decimated_scene_still_decimates():
    """The interval multiplies whatever the physics step ends up being — a go2
    document that forgets ``fixed_dt`` is a different (wrong) plant, but its
    render decimation is still the row's, and ``emit_sim_config`` prints both."""
    spec = {
        **_valid_spec(),
        "scenario": {**_valid_spec()["scenario"], "scene": "go2_warehouse", "robot": "go2"},
    }
    config = main.sim_config_for(main.parse_request(spec)[0])
    assert (config.physics_dt, config.rendering_dt) == pytest.approx((1 / 60.0, 4 / 60.0))


def test_the_declared_knobs_reach_sim_config_through_the_real_job_spec_wire():
    # Full round-trip over the PRODUCTION producer (M3 REST twin, G-25-anchored
    # to the M8 CLI twin) -> runner parse -> SimConfig: the value has to survive
    # every hop under its contract name (G-17 is exactly this seam).
    from cv_infra.orchestrator.api import _job_spec_for

    spec = _job_spec_for(_request(initial_pose=DECLARED_POSE, fixed_dt=0.02), "job-0001")
    assert spec["scenario"]["initial_pose"] == DECLARED_POSE
    assert spec["execution_settings"] == {"fixed_dt": 0.02}  # repeats stays M3's axis
    config = main.sim_config_for(main.parse_request(spec)[0])
    assert config.initial_pose == DECLARED_POSE
    assert (config.physics_dt, config.rendering_dt) == (0.02, 0.02)


def test_the_undeclared_knobs_keep_the_wire_and_sim_config_at_pre_p5c11_behaviour():
    from cv_infra.orchestrator.api import _job_spec_for

    spec = _job_spec_for(_request(), "job-0001")
    assert "initial_pose" not in spec["scenario"]  # exclude_none: off the wire
    assert "execution_settings" not in spec  # nothing survived the knob filter
    config = main.sim_config_for(main.parse_request(spec)[0])
    assert config.initial_pose is None
    assert (config.physics_dt, config.rendering_dt) == (1.0 / 60.0, 1.0 / 60.0)


def test_initial_pose_keys_match_the_runner_read_set():
    # ★ G-17 guard, same mechanical form as
    # test_debug_obstacle_keys_match_the_runner_read_set: the contract's known
    # keys ARE the keys the runner actually reads (``pose["k"]`` call sites),
    # never a hand-kept list. M1 could not write this in T2 — until this cycle
    # there was no consumer, so the read-set was empty and the test vacuous.
    import inspect
    import re

    src = inspect.getsource(sim_runtime.initial_pose_world_transform)
    reads = set(re.findall(r"""pose(?:\.get\(|\[)\s*["'](\w+)["']""", src))
    assert reads, "read-set extraction went empty (positive control, G-07)"
    assert set(InitialPose.model_fields) == reads


def test_runner_reads_the_spawn_pose_under_its_contract_field_name():
    # The other half of the G-17 bind: the read-set above pins the SUB-keys, this
    # pins the FIELD name. Renaming Scenario.initial_pose (or deleting the
    # runner's read) breaks one of the two assertions. Note for the next editor:
    # a ``request.scenario.<method>()`` call would also be captured here — if one
    # is ever added, exclude it explicitly rather than weakening the field check.
    import inspect
    import re

    reads = set(re.findall(r"request\.scenario\.(\w+)", inspect.getsource(main)))
    assert "initial_pose" in reads, "the runner stopped reading scenario.initial_pose"
    assert reads <= set(Scenario.model_fields), f"runner reads non-contract fields: {reads}"
