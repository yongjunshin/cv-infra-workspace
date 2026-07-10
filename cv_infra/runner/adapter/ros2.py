"""ROS 2 adapter (M2, REQ-EXEC-004/005/006/007) — DDS-only, no docker.sock.

Consumes ``Ros2AdapterConfig`` (M1 wire schema) and wires the runner to an
already-running SUT over DDS: clock-flow monitor, odom fan-out, Nav2 goal
interface, and the readiness barrier. It does NOT spawn the SUT and holds NO
docker.sock (M3 co-spawns SUT+runner on the shared per-job network/domain —
D-2/D-D). ``use_sim_time`` is a SUT contract set in the SUT launch (M3-injected);
this adapter guarantees ``/clock`` and *verifies* ``use_sim_time`` at readiness —
it never forces it (REQ-EXEC-005, M2 §3.2 step4).

rclpy is the bundled internal Jazzy site (cycle-3 measured: rclpy 7.1.5 +
nav2_msgs, put on ``sys.path`` by ``ros_bridge.bootstrap_bridge_env``) and is
imported lazily inside the ROS bodies (R16). The pure sequencing/math surface
(``readiness_sequence`` / ``quat_z_w_from_yaw`` / ``nav_status_str``) is
Isaac/rclpy-free and CPU unit-tested.

Wiring stance (do-not-reinvent): the carter sample scene's OmniGraphs already
publish /clock, TF, odom, sensors and subscribe /cmd_vel — ``wire`` REUSES them
and only supplements the measured gap (odom dualization: the SAME Odometry
stream must flow on every ``odom_topics[]`` entry; the sim graph publishes one
of them, so a thin rclpy relay fans it out to the publisher-less rest).
"""

from __future__ import annotations

import math
import sys
import time
from collections.abc import Callable
from dataclasses import dataclass

from cv_infra.contract.adapter_schema import GoalInterface, Ros2AdapterConfig
from cv_infra.runner.adapter.base import SimAdapter

# /clock FLOW threshold for the readiness barrier (G-19): flow is claimed from a
# RECEIVED-message count only — endpoint existence is never accepted as evidence.
CLOCK_FLOW_MIN_MSGS = 2

# Bounded per-poll budget for the lifecycle Trigger service (measured cycle-3:
# the service does NOT respond during activation — every poll MUST carry a
# timeout or the barrier hangs inside a single call).
IS_ACTIVE_POLL_TIMEOUT_S = 2.0


# --------------------------------------------------------------------------- #
# Pure sequencing/math surface — CPU unit-test target (no rclpy/Isaac).
# --------------------------------------------------------------------------- #
def readiness_sequence(
    clock_flowing: Callable[[], bool],
    sut_active: Callable[[], bool],
    now: Callable[[], float],
    deadline: float,
    wait: Callable[[], None],
) -> tuple[bool, str]:
    """Readiness barrier order (REQ-EXEC-007, G-19): clock flow FIRST, then gate.

    A use_sim_time SUT is fully frozen without /clock (measured cycle-3 T3), so
    probing the lifecycle gate before clock flow would just burn the barrier
    budget. ``wait`` is the caller's step-and-spin (the sim must keep stepping —
    it IS the /clock source). Returns ``(ok, phase)`` where phase names the stage
    reached: "clock" / "active" (timed out there) or "ready".
    """
    while not clock_flowing():
        if now() >= deadline:
            return False, "clock"
        wait()
    while not sut_active():
        if now() >= deadline:
            return False, "active"
        wait()
    return True, "ready"


def quat_z_w_from_yaw(yaw: float) -> tuple[float, float]:
    """Planar yaw -> quaternion (z, w) for a Nav2 goal pose — stdlib only."""
    return math.sin(yaw / 2.0), math.cos(yaw / 2.0)


# action_msgs/msg/GoalStatus terminal codes (values are the ROS 2 wire contract).
_NAV_STATUS = {4: "succeeded", 5: "canceled", 6: "aborted"}


def nav_status_str(code: int | None) -> str:
    """Map a GoalStatus terminal code to a mission status tag."""
    if code is None:
        return "unknown"
    return _NAV_STATUS.get(code, f"status_{code}")


def get_parameters_service_for(is_active_service: str) -> str:
    """Derive the owning node's get_parameters service from the Trigger service.

    e.g. ``/lifecycle_manager_navigation/is_active`` ->
    ``/lifecycle_manager_navigation/get_parameters`` (used to VERIFY — never
    force — the SUT ``use_sim_time`` contract at readiness).
    """
    return is_active_service.rsplit("/", 1)[0] + "/get_parameters"


@dataclass(frozen=True)
class MissionOutcome:
    """Terminal state of one drive_mission call (log surface; verdict stays with
    the oracles — a timeout here simply ends telemetry at the sim-time budget,
    which the reached_goal oracle folds to verdict=timeout, D-F)."""

    status: str  # succeeded | aborted | canceled | rejected | timeout | status_<n>
    nav_status_code: int | None
    sim_time_elapsed_s: float


class Ros2Adapter(SimAdapter):
    """SimAdapter over ROS 2 / Fast DDS (bundled internal Jazzy rclpy in-process)."""

    interface_type = "ros2"

    def __init__(
        self,
        config: Ros2AdapterConfig | None = None,
        stepper: Callable[[], None] | None = None,
    ) -> None:
        # FU-13 (3): all wiring names come from adapter_config — no module DEFAULT_*
        # constants. Defaults live in the M1 adapter_schema (single definition), so a
        # config-less adapter gets the schema defaults (LOCKED Nav2 goal action pin).
        self.config = config if config is not None else Ros2AdapterConfig()
        # step-and-spin dependency: the sim must keep stepping while we spin rclpy
        # (the sim IS the /clock source — G-19); main passes SimRuntime.step.
        self._stepper = stepper
        self._rclpy = None  # bundled-jazzy rclpy module (set in wire())
        self._node = None  # internal Jazzy rclpy node (set in wire())
        self._nav_client = None
        self._is_active_client = None
        self._clock_count = 0
        self._clock_time_s = 0.0
        self._odom_fanout_done = False
        self._odom_relay = None  # (subscription, publishers) keep-alive

    @property
    def goal_interface(self) -> GoalInterface:
        """Mission goal binding (kind/name/type) — always from adapter_config.

        Real topic names are measured on the workstation (R7) and travel via
        adapter_config, never baked into code paths.
        """
        return self.config.goal_interface

    @property
    def clock_count(self) -> int:
        """Received /clock message count (the ONLY admissible flow evidence, G-19)."""
        return self._clock_count

    @property
    def sim_time_s(self) -> float:
        """Latest sim-time seen on /clock (mission clock domain, D-F)."""
        return self._clock_time_s

    # ------------------------------------------------------------------ #
    # SimAdapter interface — ROS bodies (bundled rclpy; T3 proves on GPU).
    # ------------------------------------------------------------------ #
    def wire(self, simulation_app: object, adapter_config: object) -> None:  # pragma: no cover
        """Join the SUT DDS domain: rclpy node + clock monitor + goal client.

        The sim-side topics (clock/TF/odom/sensors/cmd_vel) are the carter
        sample's pre-wired OmniGraphs (REUSE); the odom fan-out supplement is
        deferred to the readiness loop when publishers are discoverable.
        """
        if isinstance(adapter_config, Ros2AdapterConfig):
            self.config = adapter_config

        import rclpy  # noqa: PLC0415 (bundled jazzy site — bootstrap_bridge_env)
        from rclpy.qos import (  # noqa: PLC0415
            QoSProfile,
            ReliabilityPolicy,
        )
        from rosgraph_msgs.msg import Clock  # noqa: PLC0415

        rclpy.init()  # ROS_DOMAIN_ID / RMW honored from env (M3-injected, LOCKED §5)
        self._rclpy = rclpy
        self._node = rclpy.create_node("cv_infra_runner")

        def on_clock(msg: Clock) -> None:
            self._clock_count += 1
            self._clock_time_s = msg.clock.sec + msg.clock.nanosec * 1e-9

        # BEST_EFFORT matches both reliable and best-effort publishers (QoS-safe).
        self._node.create_subscription(
            Clock,
            self.config.clock_topic,
            on_clock,
            QoSProfile(depth=10, reliability=ReliabilityPolicy.BEST_EFFORT),
        )

        if self.config.goal_interface.kind == "action":
            from nav2_msgs.action import NavigateToPose  # noqa: PLC0415
            from rclpy.action import ActionClient  # noqa: PLC0415

            self._nav_client = ActionClient(
                self._node, NavigateToPose, self.config.goal_interface.name
            )

        from std_srvs.srv import Trigger  # noqa: PLC0415

        self._is_active_client = self._node.create_client(
            Trigger, self.config.readiness.is_active_service
        )

    def await_ready(self, timeout_s: float) -> bool:  # pragma: no cover - ROS path
        """Readiness barrier (REQ-EXEC-007): /clock FLOW -> lifecycle gate ->
        use_sim_time verify. Mission/timeout clocks must not start before this."""
        if self._node is None:
            raise RuntimeError("wire() must run before await_ready()")
        deadline = time.monotonic() + timeout_s

        def clock_flowing() -> bool:
            return self._clock_count >= CLOCK_FLOW_MIN_MSGS

        def sut_active() -> bool:
            self._ensure_odom_fanout()
            return self._poll_is_active()

        ok, phase = readiness_sequence(
            clock_flowing, sut_active, time.monotonic, deadline, self._step_and_spin
        )
        if not ok:
            print(
                f"[cv-runner] readiness barrier timed out at phase {phase!r} "
                f"(clock_count={self._clock_count}, "
                f"service={self.config.readiness.is_active_service})",
                file=sys.stderr,
                flush=True,
            )
            return False

        verified = self._verify_use_sim_time()
        if verified is False:
            print(
                "[cv-runner] SUT use_sim_time=false — sim-time mission budget "
                "cannot hold (SUT launch must set use_sim_time:=true; verified, "
                "not forced — REQ-EXEC-005)",
                file=sys.stderr,
                flush=True,
            )
            return False
        return True

    def drive_mission(
        self, goal: object, *, timeout_s: float | None = None
    ) -> MissionOutcome:  # pragma: no cover - ROS path
        """Send the Nav2 goal and monitor to terminal status on sim-time (D-F).

        On sim-time budget expiry the goal is cancelled (best-effort) and the
        loop exits — telemetry then ends at the budget, and the reached_goal
        oracle folds that to verdict=timeout (no separate verdict plumbing).
        """
        if self._node is None:
            raise RuntimeError("wire() must run before drive_mission()")
        if self.config.goal_interface.kind != "action":
            raise RuntimeError(
                f"goal_interface.kind={self.config.goal_interface.kind!r} is not "
                "wired in Phase 2 (action only; measured topic alternative lands "
                "with the P3 adapter formalization)"
            )

        from nav2_msgs.action import NavigateToPose  # noqa: PLC0415

        goal_msg = NavigateToPose.Goal()
        goal_msg.pose.header.frame_id = getattr(goal, "frame", "map")
        goal_msg.pose.pose.position.x = float(goal.x)
        goal_msg.pose.pose.position.y = float(goal.y)
        goal_msg.pose.pose.position.z = 0.0  # planar goal (same z=0 as criteria_view)
        qz, qw = quat_z_w_from_yaw(float(goal.yaw))
        goal_msg.pose.pose.orientation.z = qz
        goal_msg.pose.pose.orientation.w = qw

        start_sim = self._clock_time_s
        deadline_sim = None if timeout_s is None else start_sim + float(timeout_s)

        def sim_timed_out() -> bool:
            return deadline_sim is not None and self._clock_time_s > deadline_sim

        send_future = self._nav_client.send_goal_async(goal_msg)
        while not send_future.done():
            if sim_timed_out():
                return MissionOutcome("timeout", None, self._clock_time_s - start_sim)
            self._step_and_spin()
        handle = send_future.result()
        if not handle.accepted:
            return MissionOutcome("rejected", None, self._clock_time_s - start_sim)

        result_future = handle.get_result_async()
        while not result_future.done():
            if sim_timed_out():
                cancel_future = handle.cancel_goal_async()
                cancel_deadline = time.monotonic() + 5.0
                while not cancel_future.done() and time.monotonic() < cancel_deadline:
                    self._step_and_spin()
                return MissionOutcome("timeout", None, self._clock_time_s - start_sim)
            self._step_and_spin()

        status = result_future.result().status
        return MissionOutcome(nav_status_str(status), status, self._clock_time_s - start_sim)

    def teardown(self) -> None:
        """Destroy the rclpy node / leave the DDS domain (CPU-safe when unwired)."""
        node, self._node = self._node, None
        rclpy, self._rclpy = self._rclpy, None
        self._nav_client = None
        self._is_active_client = None
        self._odom_relay = None
        if node is not None:  # pragma: no cover - ROS path
            try:
                node.destroy_node()
            except Exception:
                pass
        if rclpy is not None:  # pragma: no cover - ROS path
            try:
                if rclpy.ok():
                    rclpy.shutdown()
            except Exception:
                pass

    # ------------------------------------------------------------------ #
    # ROS-body helpers (bundled rclpy; not CPU-reachable).
    # ------------------------------------------------------------------ #
    def _step_and_spin(self) -> None:  # pragma: no cover - ROS path
        """One sim step (the /clock source) + one non-blocking rclpy spin."""
        if self._stepper is not None:
            self._stepper()
        self._rclpy.spin_once(self._node, timeout_sec=0.0)

    def _poll_is_active(self) -> bool:  # pragma: no cover - ROS path
        """One bounded poll of the Nav2 lifecycle Trigger gate (never blocks open-
        ended: measured — no response during activation, success=False after an
        aborted bringup)."""
        from std_srvs.srv import Trigger  # noqa: PLC0415

        if not self._is_active_client.service_is_ready():
            self._step_and_spin()
            return False
        future = self._is_active_client.call_async(Trigger.Request())
        poll_deadline = time.monotonic() + IS_ACTIVE_POLL_TIMEOUT_S
        while not future.done() and time.monotonic() < poll_deadline:
            self._step_and_spin()
        if not future.done():
            future.cancel()
            return False
        response = future.result()
        return bool(response is not None and response.success)

    def _ensure_odom_fanout(self) -> None:  # pragma: no cover - ROS path
        """Supplement (not replace) the sample graph: relay the ONE sim-published
        Odometry stream to every publisher-less ``odom_topics[]`` entry (measured
        odom dualization — controller_server=/odom AND bt_navigator=/chassis/odom
        must both flow). No-op once wired or when the graph already covers all."""
        if self._odom_fanout_done or len(self.config.odom_topics) < 2:
            return
        from nav_msgs.msg import Odometry  # noqa: PLC0415

        infos = {t: self._node.get_publishers_info_by_topic(t) for t in self.config.odom_topics}
        sources = [t for t, pubs in infos.items() if len(pubs) > 0]
        targets = [t for t, pubs in infos.items() if len(pubs) == 0]
        if not sources:
            return  # sim graph not publishing yet — retry next poll
        if not targets:
            self._odom_fanout_done = True  # graph already covers every entry
            return
        publishers = [self._node.create_publisher(Odometry, t, 10) for t in targets]

        def relay(msg: Odometry) -> None:
            for pub in publishers:
                pub.publish(msg)

        subscription = self._node.create_subscription(Odometry, sources[0], relay, 10)
        self._odom_relay = (subscription, publishers)
        self._odom_fanout_done = True

    def _verify_use_sim_time(self) -> bool | None:  # pragma: no cover - ROS path
        """VERIFY the SUT use_sim_time contract (never force — REQ-EXEC-005).

        Asks the lifecycle-gate node for its ``use_sim_time`` parameter. Returns
        True/False when answered, None when unknown (no param service response —
        proceed with a warning; the D-F budget then rests on the SUT contract).
        """
        from rcl_interfaces.msg import ParameterType  # noqa: PLC0415
        from rcl_interfaces.srv import GetParameters  # noqa: PLC0415

        service = get_parameters_service_for(self.config.readiness.is_active_service)
        client = self._node.create_client(GetParameters, service)
        try:
            if not client.wait_for_service(timeout_sec=2.0):
                return None
            future = client.call_async(GetParameters.Request(names=["use_sim_time"]))
            poll_deadline = time.monotonic() + IS_ACTIVE_POLL_TIMEOUT_S
            while not future.done() and time.monotonic() < poll_deadline:
                self._step_and_spin()
            if not future.done():
                future.cancel()
                return None
            values = future.result().values
            if not values or values[0].type != ParameterType.PARAMETER_BOOL:
                return None
            return bool(values[0].bool_value)
        finally:
            self._node.destroy_client(client)
