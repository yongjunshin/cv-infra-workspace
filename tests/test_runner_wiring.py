"""CPU unit tests for the runner's contract consumption: JOB_SPEC -> typed schema.

D-4' (P3 cycle-2): the parse chain REALLY calls ``contract.schema``
``model_validate`` (M1 pydantic canon — G-17: single shape definition) while the
JOB_SPEC *wire* stays byte-identical to the Phase-2 seam (job_id +
flattened sut_image_ref). Proves contract violations map to BadJobSpec/exit 2
pre-sim (no Isaac touched, no result.json emitted), that ``debug_obstacle`` is
consumed from its D-2' home (``scenario.debug_obstacle``, criteria ride-along
loud-rejected), and that the ros2 adapter's goal interface comes from the M1
adapter_schema (module DEFAULT_* constants removed).
"""

import json

import pytest

from cv_infra.contract.adapter_schema import GoalInterface, Ros2AdapterConfig
from cv_infra.contract.schema import Goal, VerificationRequest
from cv_infra.runner import main
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
