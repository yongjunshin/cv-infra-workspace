"""ROS 2 adapter (M2, REQ-EXEC-004/005/006) — DDS-only, no docker.sock.

Consumes ``Ros2AdapterConfig`` (M1 wire schema) and wires the runner to an
already-running SUT over DDS: topic_map remap, QoS overrides, Nav2 goal interface,
and the readiness barrier. It does NOT spawn the SUT and holds NO docker.sock (M3
co-spawns SUT+runner on the shared per-job network/domain — D-2/D-D). ``use_sim_time``
is a SUT contract set in the SUT launch (M3-injected); this adapter guarantees
``/clock`` and *verifies* ``use_sim_time`` at readiness — it never forces it
(REQ-EXEC-005, M2 §3.2 step4).

rclpy is the bundled internal Jazzy interpreter and is imported lazily inside the
GPU/ROS bodies (R16 — in-process rclpy preferred, availability verified at runtime).
"""

from __future__ import annotations

from cv_infra.adapter.adapter_schema import Ros2AdapterConfig
from cv_infra.runner.adapter.base import SimAdapter

# MVP defaults (P2 hardcoded; from adapter_config in P3). Real topic names are
# discovered on the workstation in cycle 3 (R7) and fed via adapter_config, never
# baked into oracle/telemetry code paths.
DEFAULT_GOAL_ACTION = "/navigate_to_pose"
DEFAULT_GOAL_ACTION_TYPE = "nav2_msgs/action/NavigateToPose"


class Ros2Adapter(SimAdapter):
    """SimAdapter over ROS 2 / Fast DDS. Interfaces + wiring seams (cycle 3-4)."""

    interface_type = "ros2"

    def __init__(self, config: Ros2AdapterConfig | None = None) -> None:
        self.config = config
        self._node = None  # internal Jazzy rclpy node (set in wire())

    def wire(self, simulation_app: object, adapter_config: object) -> None:  # pragma: no cover
        """Join the SUT DDS domain, remap topics/QoS, bind the Nav2 goal interface."""
        raise NotImplementedError("ros2 DDS wiring (topic_map/QoS/goal) lands in cycle 3")

    def await_ready(self, timeout_s: float) -> bool:  # pragma: no cover - GPU/ROS path
        """Readiness barrier (REQ-EXEC-007): action server available + /amcl_pose +
        lifecycle active + /clock flow, and verify (not force) SUT use_sim_time."""
        raise NotImplementedError("readiness barrier lands in cycle 3")

    def drive_mission(self, goal: object) -> None:  # pragma: no cover - GPU/ROS path
        """Send NavigateToPose goal; monitor result/timeout on sim-time (/clock)."""
        raise NotImplementedError("goal driver lands in cycle 3-4")

    def teardown(self) -> None:  # pragma: no cover - GPU/ROS path
        """Destroy the rclpy node / leave the DDS domain."""
        if self._node is not None:
            self._node = None
