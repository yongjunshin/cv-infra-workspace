"""M1 P3 canonical adapter schema tests (contract/adapter_schema.py —
REQ-EXEC-004/005/006, FU-3 F2).

STANDALONE canon verification since D-4' (2026-07-10) retired the Phase-2
dataclass canon (cv_infra/adapter/) this file used to bind 1:1 against: the
field sets and the default tree are now pinned as EXPLICIT literals
(materialized from the retiring dataclass canon at base 1fd55e4 — never
regenerated from the model under test, G-25), and the canonical fixture's
measured adapter_config is driven through the pydantic canon. Plus the
REQ-EXEC-005 blackbox negative: the schema has NO field that reaches inside
the SUT container.
"""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml
from pydantic import ValidationError

from cv_infra.contract import adapter_schema as pyd_canon

FIXTURE = Path(__file__).parent / "fixtures" / "nova_carter_warehouse_goal.yaml"


def _fixture_adapter_config() -> dict:
    doc = yaml.safe_load(FIXTURE.read_text(encoding="utf-8"))
    return doc["interface"]["adapter_config"]


# --------------------------------------------------------------------------- #
# canon pins — explicit literals (ex-1:1 guards; the dataclass canon is gone)
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize(
    ("model", "expected"),
    [
        (pyd_canon.GoalInterface, {"kind", "name", "type"}),
        (pyd_canon.CmdVel, {"topic", "type"}),
        (pyd_canon.SensorInput, {"topic", "type", "frame"}),
        (pyd_canon.Frames, {"map", "odom", "base_link"}),
        (pyd_canon.Readiness, {"is_active_service"}),
        # Ros2AdapterConfig's exact top-level set = the blackbox negative below.
    ],
)
def test_field_sets_pin_the_wire_shape(model, expected):
    assert set(model.model_fields) == expected


def test_default_tree_is_the_phase2_canonical_literal():
    # Defaults carry the LOCKED pins (§7-1 jazzy/rmw_fastrtps_cpp), the measured
    # Nav2 names, and R7 (SUT-specific wiring defaults EMPTY).
    assert pyd_canon.Ros2AdapterConfig().model_dump() == {
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
        "odom_topics": [],
        "sensors": [],
        "frames": {"map": "map", "odom": "odom", "base_link": "base_link"},
        "readiness": {"is_active_service": "/lifecycle_manager_navigation/is_active"},
    }


def test_canonical_fixture_config_parses_with_the_measured_values():
    # The carter measured surface (T2 §4-6 / T3 §6-7 via the fixture copy) must
    # keep parsing losslessly through the canon (ex-both-stacks roundtrip).
    cfg = pyd_canon.Ros2AdapterConfig.model_validate(_fixture_adapter_config())
    assert (cfg.goal_interface.kind, cfg.goal_interface.name) == ("action", "/navigate_to_pose")
    assert (cfg.cmd_vel.topic, cfg.cmd_vel.type) == ("/cmd_vel", "geometry_msgs/msg/Twist")
    assert cfg.odom_topics == ["/odom", "/chassis/odom"]  # odom dualization — BOTH wired
    assert [s.topic for s in cfg.sensors] == [
        "/front_3d_lidar/lidar_points",
        "/front_2d_lidar/scan",
        "/back_2d_lidar/scan",
    ]
    assert cfg.sensors[0].frame == "front_3d_lidar"  # pointcloud_to_laserscan target_frame
    assert cfg.readiness.is_active_service == "/lifecycle_manager_navigation/is_active"


def test_fixture_config_dump_is_validation_stable():
    # model_dump materializes defaults; a second pass must be a fixed point.
    dumped = pyd_canon.Ros2AdapterConfig.model_validate(_fixture_adapter_config()).model_dump()
    assert pyd_canon.Ros2AdapterConfig.model_validate(dumped).model_dump() == dumped


# --------------------------------------------------------------------------- #
# loud-reject + interface discriminator
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize(
    "corrupt",
    [
        lambda d: d.update(bogus=1),
        lambda d: d["goal_interface"].update(bogus=1),
        lambda d: d["sensors"][0].update(bogus=1),
        lambda d: d["frames"].update(bogus=1),
        lambda d: d["readiness"].update(bogus=1),
    ],
)
def test_unknown_keys_reject_loudly_at_every_nesting_level(corrupt):
    cfg = _fixture_adapter_config()
    corrupt(cfg)
    with pytest.raises(ValidationError):
        pyd_canon.Ros2AdapterConfig.model_validate(cfg)


def test_sensor_topic_and_type_required():
    with pytest.raises(ValidationError):
        pyd_canon.SensorInput.model_validate({"topic": "/scan"})


def test_retired_draft_key_topic_map_is_rejected():
    # The cycle-1 empty-draft ``topic_map`` stays retired (replaced by cmd_vel /
    # clock_topic / odom_topics / sensors). Old DRAFT YAML must fail loudly
    # (ported from the retired dataclass-canon tests, D-4').
    with pytest.raises(ValidationError):
        pyd_canon.Ros2AdapterConfig.model_validate({"topic_map": {"cmd_vel": "/cmd_vel"}})


def test_goal_interface_topic_alternative_parses():
    # Measured alternative (T2 §4.1): /goal_pose [PoseStamped] as a topic goal.
    gi = pyd_canon.GoalInterface.model_validate(
        {"kind": "topic", "name": "/goal_pose", "type": "geometry_msgs/msg/PoseStamped"}
    )
    assert (gi.kind, gi.name, gi.type) == ("topic", "/goal_pose", "geometry_msgs/msg/PoseStamped")


def test_interface_accepts_ros2_only():
    iface = pyd_canon.Interface.model_validate(
        {"type": "ros2", "adapter_config": _fixture_adapter_config()}
    )
    assert iface.type == "ros2"
    with pytest.raises(ValidationError):
        pyd_canon.Interface.model_validate({"type": "grpc", "adapter_config": {}})


def test_interface_defaults_are_the_locked_pins():
    iface = pyd_canon.Interface()
    assert iface.adapter_config.ros_distro == "jazzy"  # LOCKED §7-1
    assert iface.adapter_config.rmw == "rmw_fastrtps_cpp"
    assert iface.adapter_config.odom_topics == []  # R7: no SUT-specific default


# --------------------------------------------------------------------------- #
# REQ-EXEC-005 blackbox negative — the ABSENCE is the contract
# --------------------------------------------------------------------------- #
def test_schema_has_no_sut_internal_mutation_field():
    names = set(pyd_canon.Ros2AdapterConfig.model_fields) | set(pyd_canon.Interface.model_fields)
    forbidden = {
        "sut_command",
        "sut_entrypoint",
        "sut_env",
        "sut_args",
        "command_override",
        "entrypoint_override",
        "patch",
        "inject",
    }
    assert names.isdisjoint(forbidden)
    # exact top-level field set: adding ANY field is a conscious contract change
    assert set(pyd_canon.Ros2AdapterConfig.model_fields) == {
        "ros_distro",
        "rmw",
        "use_sim_time",
        "goal_interface",
        "cmd_vel",
        "clock_topic",
        "odom_topics",
        "sensors",
        "frames",
        "readiness",
    }
