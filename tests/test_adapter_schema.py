"""SEAM-2 canonical Ros2AdapterConfig tests (M1 / p2-seam-alignment).

Pins the adapter_config canonical fixed from the MEASURED carter surface
(reports T2 carter-topic-inventory §4-6 / T3 dds-encounter §7):
  (1) canonical round-trip — ``from_dict`` -> ``to_dict`` lossless;
  (2) unknown keys LOUD-REJECT with the key path — pins the cycle-1 ``frames``
      silent-drop regression (a key the schema does not know must raise, never
      vanish);
  (3) the carter measured fixture (values verbatim from the T2/T3 evidence)
      parses losslessly.
Stdlib + pytest only — friendly error prose is Phase 3 pydantic.
"""

from __future__ import annotations

import pytest

from cv_infra.adapter.adapter_schema import (
    CmdVel,
    Frames,
    GoalInterface,
    Readiness,
    Ros2AdapterConfig,
    SensorInput,
)

# ---------------------------------------------------------------------------
# carter measured fixture — values verbatim from T2 §4-6 / T3 §6-7 evidence
# ---------------------------------------------------------------------------

CARTER_ADAPTER_CONFIG = {
    "ros_distro": "jazzy",
    "rmw": "rmw_fastrtps_cpp",
    "use_sim_time": True,
    "goal_interface": {
        "kind": "action",
        "name": "/navigate_to_pose",
        "type": "nav2_msgs/action/NavigateToPose",
    },
    "cmd_vel": {"topic": "/cmd_vel", "type": "geometry_msgs/msg/Twist"},
    "clock_topic": "/clock",
    # odom dualization (T2 §6-1, T3 matched 1/1): BOTH topics must be wired.
    "odom_topics": ["/odom", "/chassis/odom"],
    "sensors": [
        {
            "topic": "/front_3d_lidar/lidar_points",
            "type": "sensor_msgs/msg/PointCloud2",
            "frame": "front_3d_lidar",  # measured pointcloud_to_laserscan target_frame
        },
        # 2D lidar frames were NOT measured — header-carried (frame: None).
        {"topic": "/front_2d_lidar/scan", "type": "sensor_msgs/msg/LaserScan", "frame": None},
        {"topic": "/back_2d_lidar/scan", "type": "sensor_msgs/msg/LaserScan", "frame": None},
    ],
    "frames": {"map": "map", "odom": "odom", "base_link": "base_link"},
    "readiness": {"is_active_service": "/lifecycle_manager_navigation/is_active"},
}


# ---------------------------------------------------------------------------
# (1) canonical round-trip
# ---------------------------------------------------------------------------


def test_default_config_round_trips():
    d = Ros2AdapterConfig().to_dict()
    assert Ros2AdapterConfig.from_dict(d).to_dict() == d


def test_object_round_trips_to_equal_object():
    cfg = Ros2AdapterConfig.from_dict(CARTER_ADAPTER_CONFIG)
    assert Ros2AdapterConfig.from_dict(cfg.to_dict()) == cfg


def test_none_and_empty_dict_yield_defaults():
    assert Ros2AdapterConfig.from_dict(None) == Ros2AdapterConfig()
    assert Ros2AdapterConfig.from_dict({}) == Ros2AdapterConfig()


def test_partial_dict_fills_defaults():
    cfg = Ros2AdapterConfig.from_dict({"odom_topics": ["/odom", "/chassis/odom"]})
    assert cfg.odom_topics == ["/odom", "/chassis/odom"]
    assert cfg.clock_topic == "/clock"
    assert cfg.goal_interface == GoalInterface()
    assert cfg.frames == Frames()
    assert cfg.readiness == Readiness()


# ---------------------------------------------------------------------------
# (2) unknown keys LOUD-REJECT (silent-drop regression pin)
# ---------------------------------------------------------------------------


def test_unknown_top_level_key_is_rejected_not_silently_dropped():
    # Cycle-1 regression: the DRAFT from_dict silently dropped keys it did not
    # know (the consumer's ``frames`` block vanished). Canonical policy: raise.
    with pytest.raises(ValueError, match=r"adapter_config: unknown key\(s\).*framez"):
        Ros2AdapterConfig.from_dict({"framez": {"map": "map"}})


def test_retired_draft_key_topic_map_is_rejected():
    # The cycle-1 empty-draft ``topic_map`` is retired (replaced by cmd_vel /
    # clock_topic / odom_topics / sensors). Old DRAFT YAML must fail loudly.
    with pytest.raises(ValueError, match=r"adapter_config: unknown key\(s\).*topic_map"):
        Ros2AdapterConfig.from_dict({"topic_map": {"cmd_vel": "/cmd_vel"}})


def test_unknown_nested_key_error_carries_the_path():
    # Consumer-DRAFT spellings must be rejected WITH their nested path.
    with pytest.raises(
        ValueError, match=r"adapter_config\.goal_interface: unknown key\(s\).*action_name"
    ):
        Ros2AdapterConfig.from_dict({"goal_interface": {"action_name": "/navigate_to_pose"}})
    with pytest.raises(ValueError, match=r"adapter_config\.frames: unknown key\(s\).*'base'"):
        Ros2AdapterConfig.from_dict({"frames": {"base": "base_link"}})
    with pytest.raises(
        ValueError, match=r"adapter_config\.readiness: unknown key\(s\).*action_server_available"
    ):
        Ros2AdapterConfig.from_dict({"readiness": {"action_server_available": "/navigate_to_pose"}})
    with pytest.raises(ValueError, match=r"adapter_config\.cmd_vel: unknown key\(s\).*qos"):
        Ros2AdapterConfig.from_dict({"cmd_vel": {"topic": "/cmd_vel", "qos": "reliable"}})


def test_unknown_sensor_key_error_carries_the_list_index():
    sensors = [
        {"topic": "/front_3d_lidar/lidar_points", "type": "sensor_msgs/msg/PointCloud2"},
        {"topic": "/front_2d_lidar/scan", "type": "sensor_msgs/msg/LaserScan", "rate_hz": 30},
    ]
    with pytest.raises(
        ValueError, match=r"adapter_config\.sensors\[1\]: unknown key\(s\).*rate_hz"
    ):
        Ros2AdapterConfig.from_dict({"sensors": sensors})


def test_sensor_missing_required_keys_is_loud():
    with pytest.raises(
        ValueError, match=r"adapter_config\.sensors\[0\]: missing required key\(s\).*type"
    ):
        Ros2AdapterConfig.from_dict({"sensors": [{"topic": "/front_2d_lidar/scan"}]})


def test_error_message_lists_allowed_keys():
    # Key-path + allowed-key vocabulary is the Phase-2 self-correction aid
    # (full expected-value/example prose is Phase 3, NFR-INTAKE-001).
    with pytest.raises(ValueError, match=r"allowed keys.*odom_topics"):
        Ros2AdapterConfig.from_dict({"odom": "/odom"})


# ---------------------------------------------------------------------------
# (3) carter measured fixture parses losslessly
# ---------------------------------------------------------------------------


def test_carter_fixture_round_trips_exactly():
    assert Ros2AdapterConfig.from_dict(CARTER_ADAPTER_CONFIG).to_dict() == CARTER_ADAPTER_CONFIG


def test_carter_fixture_builds_typed_tree_with_measured_values():
    cfg = Ros2AdapterConfig.from_dict(CARTER_ADAPTER_CONFIG)
    # goal (T2 §4.1)
    assert isinstance(cfg.goal_interface, GoalInterface)
    assert (cfg.goal_interface.kind, cfg.goal_interface.name) == ("action", "/navigate_to_pose")
    assert cfg.goal_interface.type == "nav2_msgs/action/NavigateToPose"
    # command stream (T2 §4.2 — Twist, not TwistStamped)
    assert isinstance(cfg.cmd_vel, CmdVel)
    assert (cfg.cmd_vel.topic, cfg.cmd_vel.type) == ("/cmd_vel", "geometry_msgs/msg/Twist")
    # odom dualization — both wired (T2 §6-1 / T3 §6)
    assert cfg.odom_topics == ["/odom", "/chassis/odom"]
    # 3 sensor inputs (T2 §4.3), 3D lidar frame measured
    assert [s.topic for s in cfg.sensors] == [
        "/front_3d_lidar/lidar_points",
        "/front_2d_lidar/scan",
        "/back_2d_lidar/scan",
    ]
    assert isinstance(cfg.sensors[0], SensorInput)
    assert cfg.sensors[0].frame == "front_3d_lidar"
    assert cfg.sensors[1].frame is None
    # frames + readiness (T2 §4.3 / §6-3)
    assert isinstance(cfg.frames, Frames)
    assert (cfg.frames.map, cfg.frames.odom, cfg.frames.base_link) == ("map", "odom", "base_link")
    assert isinstance(cfg.readiness, Readiness)
    assert cfg.readiness.is_active_service == "/lifecycle_manager_navigation/is_active"


def test_goal_interface_topic_alternative_parses():
    # Measured alternative (T2 §4.1): /goal_pose [PoseStamped] as a topic goal.
    gi = GoalInterface.from_dict(
        {"kind": "topic", "name": "/goal_pose", "type": "geometry_msgs/msg/PoseStamped"}
    )
    assert (gi.kind, gi.name, gi.type) == ("topic", "/goal_pose", "geometry_msgs/msg/PoseStamped")
