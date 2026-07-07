"""Adapter contract schema (M1) — Phase 2 canonical (SEAM-2).

Versioned sub-schema for ``adapter_config`` when ``interface.type == "ros2"``
(REQ-EXEC-004/006). Cycle-1 shipped a DRAFT with an empty ``topic_map``; this
revision fixes the CANONICAL Phase-2 field set from the measured carter surface
(cycle p2-seam-alignment: reports T2 carter-topic-inventory §4-6 and T3
dds-encounter §7 are the authoritative evidence). The pydantic discriminated-union
model (validators, JSON-schema, friendly error prose) is still Phase 3 (§1
deferral) — here every nesting level LOUD-REJECTS unknown keys with a stdlib
``ValueError`` carrying the key path. No silent drop: the cycle-1 ``frames``
silent-drop regression is pinned by tests/test_adapter_schema.py.

By design this schema has NO field that modifies the SUT container internals
(REQ-EXEC-005, blackbox SUT contract — the absence is itself the contract).

Measured runtime knowledge documented here but deliberately NOT schema fields
(it is M2/M3 sequencing/policy, not consumer configuration):

* Supply ORDER is ``/clock`` first: without ``/clock`` a use_sim_time SUT's data
  plane freezes entirely (sim-time stuck at 0, no timers fire — T3 §5). Then TF
  ``odom->base_link`` + odometry (unblocked Nav2 costmap/controller activation
  within ~1s — T3 §6), then sensors (the 3D-lidar->scan->amcl path gates full
  bringup — T3 §7).
* Nav2 lifecycle bringup has a 60s activation window and ``Aborting bringup`` is
  terminal (no auto-retry — T3 §6). SUT start-order/restart policy is M3's.

Boundary note: this lives in the ``cv_infra.adapter`` package (a layer ABOVE the
foundational ``cv_infra.contract`` per .importlinter). The contract carries the
adapter_config as a raw mapping; the consumer (M2) builds the typed view here via
``Ros2AdapterConfig.from_dict(interface.adapter_config)``.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field, fields
from typing import Any


def _reject_unknown_keys(d: dict[str, Any], cls: type, where: str) -> None:
    """LOUD-REJECT unknown keys with their path (no silent drop).

    Friendly error prose (expected values, examples) is Phase 3 pydantic; the
    Phase 2 contract is only that unknown keys never disappear silently.
    """
    allowed = {f.name for f in fields(cls)}
    unknown = sorted(set(d) - allowed)
    if unknown:
        raise ValueError(f"{where}: unknown key(s) {unknown}; allowed keys: {sorted(allowed)}")


@dataclass
class GoalInterface:
    """Mission goal binding (REQ-EXEC-007). Defaults track the LOCKED Nav2 pin (§7-1).

    Measured (T2 §4.1): action server ``/navigate_to_pose``
    [nav2_msgs/action/NavigateToPose] on bt_navigator. The measured topic
    alternative is ``kind="topic"``, ``name="/goal_pose"``,
    ``type="geometry_msgs/msg/PoseStamped"``.
    """

    kind: str = "action"  # "action" | "topic"
    name: str = "/navigate_to_pose"
    type: str = "nav2_msgs/action/NavigateToPose"

    @classmethod
    def from_dict(
        cls, d: dict[str, Any] | None, where: str = "adapter_config.goal_interface"
    ) -> GoalInterface:
        d = d or {}
        _reject_unknown_keys(d, cls, where)
        return cls(**d)


@dataclass
class CmdVel:
    """SUT -> sim actuation stream (REQ-EXEC-006).

    Measured (T2 §4.2): ``/cmd_vel`` [geometry_msgs/msg/Twist — NOT TwistStamped]
    with TWO publishers (collision_monitor + docking_server) and zero subscribers
    SUT-side. The sim subscribes; the adapter must NOT assume a single publisher.
    """

    topic: str = "/cmd_vel"
    type: str = "geometry_msgs/msg/Twist"

    @classmethod
    def from_dict(cls, d: dict[str, Any] | None, where: str = "adapter_config.cmd_vel") -> CmdVel:
        d = d or {}
        _reject_unknown_keys(d, cls, where)
        return cls(**d)


@dataclass
class SensorInput:
    """One sim -> SUT sensor stream (REQ-EXEC-006). ``topic``/``type`` required.

    ``frame`` is the TF frame the sim must supply for this sensor (measured for
    the 3D lidar: ``front_3d_lidar`` = pointcloud_to_laserscan target_frame, T2
    §4.3); ``None`` = frame id travels in the message header only.

    Measured carter set (T2 §4.2/4.3, T3 §6-7): ``/front_3d_lidar/lidar_points``
    [PointCloud2] feeds the scan->amcl path = the FULL-BRINGUP GATE;
    ``/front_2d_lidar/scan`` + ``/back_2d_lidar/scan`` [LaserScan] are costmap
    observation only — NOT activation gates.
    """

    topic: str
    type: str
    frame: str | None = None

    @classmethod
    def from_dict(cls, d: dict[str, Any], where: str = "adapter_config.sensors[]") -> SensorInput:
        _reject_unknown_keys(d, cls, where)
        missing = sorted({"topic", "type"} - set(d))
        if missing:
            raise ValueError(f"{where}: missing required key(s) {missing}")
        return cls(**d)


@dataclass
class Frames:
    """Canonical TF frame ids (measured T2 §4.3: map / odom / base_link).

    TF ownership split (measured, T2 §6-8): the SUT (amcl) publishes ONLY
    ``map->odom``; the sim must publish ``odom->base_link`` (+ each sensor frame,
    see ``SensorInput.frame``) or Nav2 activation blocks forever.
    """

    map: str = "map"
    odom: str = "odom"
    base_link: str = "base_link"

    @classmethod
    def from_dict(cls, d: dict[str, Any] | None, where: str = "adapter_config.frames") -> Frames:
        d = d or {}
        _reject_unknown_keys(d, cls, where)
        return cls(**d)


@dataclass
class Readiness:
    """SUT readiness gate (REQ-EXEC-007 surface; measured T2 §5-6, T3 §6-7).

    ``action_server_available`` alone is INSUFFICIENT (dropped from the schema):
    the goal action server is DDS-discoverable while bt_navigator is still
    inactive (measured). The necessary-but-insufficient action-server precheck
    derives its name from ``goal_interface`` — no duplicate field here. The
    authoritative gate is the Nav2 lifecycle-manager Trigger service below.

    Measured call semantics (M2 runtime knowledge, not schema fields): the
    service does NOT respond during activation — every poll MUST carry a
    timeout — and answers ``success=False`` after an aborted bringup (= not
    ready).
    """

    is_active_service: str = "/lifecycle_manager_navigation/is_active"  # std_srvs/srv/Trigger

    @classmethod
    def from_dict(
        cls, d: dict[str, Any] | None, where: str = "adapter_config.readiness"
    ) -> Readiness:
        d = d or {}
        _reject_unknown_keys(d, cls, where)
        return cls(**d)


@dataclass
class Ros2AdapterConfig:
    """``adapter_config`` sub-schema for interface.type=ros2 (Phase 2 canonical).

    Defaults are ROS/Nav2-generic conventions only (LOCKED pins §7-1 + measured
    Nav2 names); SUT/scene-specific wiring (``odom_topics``, ``sensors``) has NO
    hardcoded default (R7) — the consumer scenario supplies it.

    ``odom_topics``: wire the SAME Odometry stream to EVERY listed topic. The
    measured carter surface is dual (T2 §6-1, re-confirmed T3 §6 matched 1/1):
    controller_server subscribes ``/odom`` (un-overridden nav2 default) AND
    bt_navigator subscribes ``/chassis/odom`` — both must flow.

    ``clock_topic``: ``/clock`` is dominant — absent ``/clock`` the whole SUT
    data plane freezes (T3 §5). Supply order (clock -> TF/odom -> sensors) is
    M2/M3 runtime knowledge (module docstring), not a schema field.

    ``use_sim_time`` is a SUT contract verified at readiness (via ``ros2 param
    get``), NOT forced (D-O, REQ-EXEC-005).
    """

    ros_distro: str = "jazzy"
    rmw: str = "rmw_fastrtps_cpp"
    use_sim_time: bool = True
    goal_interface: GoalInterface = field(default_factory=GoalInterface)
    cmd_vel: CmdVel = field(default_factory=CmdVel)
    clock_topic: str = "/clock"
    odom_topics: list[str] = field(default_factory=list)
    sensors: list[SensorInput] = field(default_factory=list)
    frames: Frames = field(default_factory=Frames)
    readiness: Readiness = field(default_factory=Readiness)

    @classmethod
    def from_dict(cls, d: dict[str, Any] | None) -> Ros2AdapterConfig:
        d = d or {}
        _reject_unknown_keys(d, cls, "adapter_config")
        return cls(
            ros_distro=d.get("ros_distro", "jazzy"),
            rmw=d.get("rmw", "rmw_fastrtps_cpp"),
            use_sim_time=d.get("use_sim_time", True),
            goal_interface=GoalInterface.from_dict(d.get("goal_interface")),
            cmd_vel=CmdVel.from_dict(d.get("cmd_vel")),
            clock_topic=d.get("clock_topic", "/clock"),
            odom_topics=list(d.get("odom_topics") or []),
            sensors=[
                SensorInput.from_dict(s, f"adapter_config.sensors[{i}]")
                for i, s in enumerate(d.get("sensors") or [])
            ],
            frames=Frames.from_dict(d.get("frames")),
            readiness=Readiness.from_dict(d.get("readiness")),
        )

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)
