"""CPU unit tests for the FU-13 (2)(3) wiring: JOB_SPEC -> typed contract objects.

Proves run()'s parse chain REALLY calls the canonical ``from_dict`` constructors
(``VerificationRequest`` / ``Ros2AdapterConfig`` — G-17: single shape definition),
that contract violations map to BadJobSpec/exit 2 pre-sim (no Isaac touched, no
result.json emitted), and that the ros2 adapter's goal interface comes from
adapter_config (module DEFAULT_* constants removed).
"""

import json

import pytest

from cv_infra.adapter.adapter_schema import GoalInterface, Ros2AdapterConfig
from cv_infra.contract.models import Goal, VerificationRequest
from cv_infra.runner import main
from cv_infra.runner.adapter import ros2


def _valid_spec() -> dict:
    """A canonical VerificationRequest dict (cycle-3 frozen shape)."""
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
# (1) Valid JOB_SPEC -> real from_dict chain -> typed objects.
# --------------------------------------------------------------------------- #
def test_parse_request_builds_typed_objects():
    request, cfg = main.parse_request(_valid_spec())
    assert isinstance(request, VerificationRequest)
    assert isinstance(request.scenario.goal, Goal)
    assert isinstance(cfg, Ros2AdapterConfig)
    assert request.job_id == "job-0001"
    assert request.sut_image_ref == "carter-sut:p2"
    assert (request.scenario.goal.x, request.scenario.goal.y) == (3.0, -1.5)
    assert [c.oracle for c in request.acceptance_criteria] == ["reached_goal", "no_collision"]
    assert cfg.odom_topics == ["/odom", "/chassis/odom"]
    assert cfg.sensors[0].frame == "front_3d_lidar"


def test_criteria_view_flattens_scenario_and_params():
    request, _ = main.parse_request(_valid_spec())
    view = main.criteria_view(request)
    assert view["goal_position"] == [3.0, -1.5, 0.0]  # planar goal (z=0.0)
    assert view["timeout_s"] == 120.0  # sim-time budget (D-F)
    assert view["position_tolerance_m"] == 0.3  # from reached_goal params
    assert view["chassis_path"] == "/World/carter/chassis"  # from no_collision params
    assert view["collision_excluded_paths"] == ["/World/ground"]


def test_criteria_view_explicit_goal_position_param_wins():
    spec = _valid_spec()
    spec["acceptance_criteria"][0]["params"]["goal_position"] = [3.0, -1.5, 0.4]
    request, _ = main.parse_request(spec)
    assert main.criteria_view(request)["goal_position"] == [3.0, -1.5, 0.4]


# --------------------------------------------------------------------------- #
# (2) Contract violations -> BadJobSpec -> exit 2 (usage), pre-sim.
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


def test_main_exits_2_on_bad_spec_and_emits_no_result(tmp_path):
    # End-to-end through main(): the parse is pre-sim, so exit 2 happens without
    # Isaac (CPU-safe) and WITHOUT a result.json (bad input is not a Result).
    spec = _valid_spec()
    spec["interface"]["adapter_config"]["topic_map"] = {}
    env = {"JOB_SPEC": json.dumps(spec), "RESULT_OUT": str(tmp_path)}
    assert main.main(env) == main.EXIT_USAGE
    assert not (tmp_path / "result.json").exists()


# --------------------------------------------------------------------------- #
# (3) Goal interface follows adapter_config — no module constants (FU-13 (3)).
# --------------------------------------------------------------------------- #
def test_goal_interface_follows_config():
    cfg = Ros2AdapterConfig.from_dict(
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
