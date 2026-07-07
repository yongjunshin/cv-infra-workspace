"""Adapter contract schema (M1) — Phase 2 minimal shape.

Versioned sub-schema for ``adapter_config`` when ``interface.type == "ros2"``
(REQ-EXEC-004/006). Phase 0 shipped an information-item guide; Phase 2 fills the
MINIMAL, NON-FROZEN fields the runner/adapter consumes. The pydantic
discriminated-union model (validators, JSON-schema) is finalized in Phase 3
(§1 deferral). Defaults track the LOCKED version pins (§7-1).

By design this schema has NO field that modifies the SUT container internals
(REQ-EXEC-005, blackbox SUT contract — the absence is itself the contract).

Boundary note: this lives in the ``cv_infra.adapter`` package (a layer ABOVE the
foundational ``cv_infra.contract`` per .importlinter). The contract carries the
adapter_config as a raw mapping; the consumer (M2) builds the typed view here via
``Ros2AdapterConfig.from_dict(interface.adapter_config)``.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any

# Standard Nav2 goal action (LOCKED §7-1 default). This is the well-known
# NavigateToPose action name, part of the goal contract — NOT a scene/sensor
# topic subject to R7 discovery (which applies to ``topic_map`` below).
_DEFAULT_GOAL_INTERFACE: dict[str, str] = {
    "kind": "action",
    "name": "/navigate_to_pose",
    "type": "nav2_msgs/action/NavigateToPose",
}


@dataclass
class Ros2AdapterConfig:
    """``adapter_config`` sub-schema for interface.type=ros2 (Phase 2 minimal).

    ``topic_map`` is an intentionally EMPTY draft: the real sim<->SUT topic names
    are discovered on the workstation during cycle-3 joint bring-up — no hardcoded
    topic names (R7). ``readiness`` is likewise a draft dict. ``use_sim_time`` is a
    SUT contract verified at readiness (via ``ros2 param get``), NOT forced (D-O).
    """

    ros_distro: str = "jazzy"
    rmw: str = "rmw_fastrtps_cpp"
    use_sim_time: bool = True
    topic_map: dict[str, str] = field(default_factory=dict)
    goal_interface: dict[str, str] = field(default_factory=lambda: dict(_DEFAULT_GOAL_INTERFACE))
    readiness: dict[str, Any] = field(default_factory=dict)

    @classmethod
    def from_dict(cls, d: dict[str, Any] | None) -> Ros2AdapterConfig:
        d = d or {}
        return cls(
            ros_distro=d.get("ros_distro", "jazzy"),
            rmw=d.get("rmw", "rmw_fastrtps_cpp"),
            use_sim_time=d.get("use_sim_time", True),
            topic_map=dict(d.get("topic_map") or {}),
            goal_interface=dict(d.get("goal_interface") or _DEFAULT_GOAL_INTERFACE),
            readiness=dict(d.get("readiness") or {}),
        )

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)
