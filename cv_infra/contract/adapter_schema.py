"""Adapter contract schema (M1 §3.5) — Phase 3 CANONICAL location (FU-3 F2).

pydantic v2 formalization of the ``interface`` block: ``interface.type``
selects the adapter and the ``adapter_config`` sub-schema branches on it
(REQ-EXEC-004/006). MVP carries exactly ONE member (``ros2`` — NFR-EXEC-003:
non-ROS2 is post-MVP); the union is discriminated by the ``type`` Literal, so
a second adapter type lands as ``Annotated[Union[Ros2Interface, XInterface],
Field(discriminator="type")]`` with ZERO wire change to existing scenarios.

Field sets, defaults and measured provenance are 1:1 with the Phase-2 canon at
``cv_infra/adapter/adapter_schema.py`` (kept unmodified this cycle — consumers
still import it; cycle-2 migrates them here). The 1:1 equivalence is bound
MECHANICALLY by tests/test_contract_adapter_schema.py (G-25: a copy without a
guard drifts silently) — see that file for the dataclass<->model dump-equality
guard. Measured provenance prose (carter topic inventory, clock-first supply
order, nav2 lifecycle window) lives with the Phase-2 canon and is not
duplicated here.

By design this schema has NO field that modifies the SUT container internals
(REQ-EXEC-005, blackbox SUT contract — the absence IS the contract, asserted
as a negative test). Every nesting level rejects unknown keys loudly
(``extra="forbid"`` — no silent drop; the friendly prose comes from
``errors.from_validation_error``).
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field


class _ForbidExtra(BaseModel):
    """Shared config: every nesting level loud-rejects unknown keys."""

    model_config = ConfigDict(extra="forbid")


class GoalInterface(_ForbidExtra):
    """Mission goal binding (REQ-EXEC-007). Defaults track the LOCKED Nav2 pin (§7-1)."""

    kind: str = "action"  # "action" | "topic"
    name: str = "/navigate_to_pose"
    type: str = "nav2_msgs/action/NavigateToPose"


class CmdVel(_ForbidExtra):
    """SUT -> sim actuation stream (REQ-EXEC-006). Measured: Twist, 2 publishers."""

    topic: str = "/cmd_vel"
    type: str = "geometry_msgs/msg/Twist"


class SensorInput(_ForbidExtra):
    """One sim -> SUT sensor stream (REQ-EXEC-006). ``topic``/``type`` required."""

    topic: str = Field(examples=["/front_3d_lidar/lidar_points"])
    type: str = Field(examples=["sensor_msgs/msg/PointCloud2"])
    frame: str | None = None


class Frames(_ForbidExtra):
    """Canonical TF frame ids (SUT publishes map->odom only; odom->base_link = sim)."""

    map: str = "map"
    odom: str = "odom"
    base_link: str = "base_link"


class Readiness(_ForbidExtra):
    """SUT readiness gate — the Nav2 lifecycle-manager Trigger service (G-19)."""

    is_active_service: str = "/lifecycle_manager_navigation/is_active"


class Ros2AdapterConfig(_ForbidExtra):
    """``adapter_config`` sub-schema for ``interface.type == "ros2"``.

    Defaults are ROS/Nav2-generic conventions only (LOCKED pins §7-1);
    SUT/scene-specific wiring (``odom_topics``, ``sensors``) has NO hardcoded
    default (R7) — the consumer scenario supplies it.
    """

    ros_distro: str = "jazzy"
    rmw: str = "rmw_fastrtps_cpp"
    use_sim_time: bool = True
    goal_interface: GoalInterface = Field(default_factory=GoalInterface)
    cmd_vel: CmdVel = Field(default_factory=CmdVel)
    clock_topic: str = "/clock"
    odom_topics: list[str] = Field(default_factory=list)
    sensors: list[SensorInput] = Field(default_factory=list)
    frames: Frames = Field(default_factory=Frames)
    readiness: Readiness = Field(default_factory=Readiness)


class Ros2Interface(_ForbidExtra):
    """``interface`` block, ros2 member (currently the only one — NFR-EXEC-003).

    ``type`` is the discriminator Literal: adding a non-ROS2 adapter type means
    adding a sibling model and forming the discriminated union (module
    docstring) — existing ``type: ros2`` documents are untouched.
    """

    type: Literal["ros2"] = "ros2"
    adapter_config: Ros2AdapterConfig = Field(default_factory=Ros2AdapterConfig)


# MVP alias: the request schema types its ``interface`` field with this name so
# the cycle-2+ union swap is a one-line change here, not a wire change.
Interface = Ros2Interface
