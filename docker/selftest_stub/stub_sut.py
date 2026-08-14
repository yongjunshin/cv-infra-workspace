#!/usr/bin/env python3
"""Built-in stub SUT for the deployment self-test (M7 §3.5 **option B**).

WHAT THIS IS: the platform-internal container that plays the SUT side of ONE
self-test job, so `cv-infra selftest` proves a fresh deployment is alive with
**zero external SUT** (`NFR-SELFTEST-001`, `REQ-SELFTEST-001`). It is spawned by
M3 onto the same per-job bridge network / `ROS_DOMAIN_ID` as the runner, exactly
like a consumer SUT (`cv_infra/orchestrator/supervisor.py`: no command override,
no env beyond `ROS_DOMAIN_ID`, no docker.sock, no GPU) — that identity is the
whole point of option B: the self-test exercises the real **container-boundary
DDS + per-job isolation substrate**, not an in-process shortcut (§3.5 D-O).

WHAT THIS IS NOT — it does not drive the robot. It publishes no `/cmd_vel`, runs
no planner, and holds no map. The self-test scenario spawns the robot AT its goal
(`cv_infra/orchestrator/selftest.py`), so "the robot is at the goal" is true from
frame 0 *by construction* and needs no motion. The verdict is NOT taken from what
this node answers: the reached_goal oracle re-derives it from Isaac **GT pose**
(`cv_infra/oracles/reached_goal.py`), which this container cannot influence.
A stub that lied would therefore fail, not pass — the oracle is not weakened.

THE CONTRACT IT MUST SATISFY (read out of `cv_infra/runner/adapter/ros2.py`,
never guessed — the runner is not modified to fit the stub, the stub fits the
runner):

  1. ``/clock`` FLOW               -> supplied by the RUNNER (Isaac), not here.
  2. ``<is_active_service>``       -> ``std_srvs/Trigger`` answering success=True
                                      (``Ros2Adapter._poll_is_active``).
  3. ``<node>/get_parameters``     -> ``use_sim_time`` = **true**, on the node that
                                      OWNS (2). The adapter derives that service
                                      name with ``get_parameters_service_for()``
                                      (rsplit of the Trigger name), so this node
                                      is NAMED after the Trigger service's parent
                                      segment and rclpy's own parameter services
                                      answer it. ``use_sim_time`` false =
                                      readiness FAILS by design (G-19: a sim-time
                                      SUT without ``/clock`` freezes).
  4. mission                       -> ``nav2_msgs/action/NavigateToPose`` accepts
                                      the goal and reaches terminal
                                      ``GoalStatus`` (``drive_mission``).

Every name above comes from the M1 adapter schema defaults
(`cv_infra/contract/adapter_schema.py`), so the built-in stub request — which
declares no ``interface`` block and therefore takes those defaults — matches this
node with zero configuration. The env knobs below exist so the SAME image can be
pointed at a differently-wired scenario without a code change (FU-13: names are
configuration, not constants); note that the production spawn passes ONLY
``ROS_DOMAIN_ID``, so overriding them is a build-time/manual-run affair.

Dependencies: rclpy + std_srvs + rcl_interfaces ship in the pinned ``ros:jazzy``
base; ``nav2_msgs`` is the one apt add (see the Dockerfile). No cv_infra import —
this container is a blackbox SUT like any other (REQ-EXEC-005).
"""

from __future__ import annotations

import os
import sys
import time

import rclpy
from nav2_msgs.action import NavigateToPose
from rclpy.action import ActionServer, GoalResponse
from rclpy.node import Node
from rclpy.parameter import Parameter
from std_srvs.srv import Trigger

#: Wiring defaults — 1:1 with the M1 adapter schema defaults so the built-in stub
#: request (which declares no ``interface`` block) matches without configuration.
#: Changing either side alone breaks readiness LOUDLY at the barrier, never silently.
DEFAULT_IS_ACTIVE_SERVICE = "/lifecycle_manager_navigation/is_active"  # Readiness
DEFAULT_GOAL_ACTION = "/navigate_to_pose"  # GoalInterface.name

#: Sim-time contract of the SUT side (Ros2AdapterConfig.use_sim_time). The adapter
#: VERIFIES this, never forces it (REQ-EXEC-005), so the stub must declare it.
DEFAULT_USE_SIM_TIME = True

#: Wall-seconds to hold an accepted goal before succeeding. **0 = succeed as soon
#: as the goal arrives**, which is the honest default: the robot is already at the
#: goal, so there is nothing to wait for and no measured duration to claim
#: (M7 §8 open item 2 stays open rather than being filled with an invented number
#: — CLAUDE §2-4 / G-64).
#:
#: WHEN TO RAISE IT (the measurement, for whoever runs the live round-trip): the
#: reached_goal oracle needs at least one GT pose sample, and samples accrue one
#: per sim step DURING the mission — i.e. only while this goal is outstanding. If
#: a live self-test returns ``reason=no_telemetry`` or a suspiciously small sample
#: count in ``result.json``, raise this knob and record the measured value here.
#: It is wall-time, not sim-time: the sim runs as fast as the runner steps it, so
#: this knob buys steps, not seconds of simulated mission.
DEFAULT_GOAL_HOLD_S = 0.0


def _env_str(name: str, default: str) -> str:
    """Env override, empty-string-safe (set-but-empty never means "unset", G-26)."""
    return (os.environ.get(name) or "").strip() or default


def _env_bool(name: str, default: bool) -> bool:
    raw = _env_str(name, "true" if default else "false").lower()
    if raw in ("1", "true", "yes", "y", "on"):
        return True
    if raw in ("0", "false", "no", "n", "off"):
        return False
    raise SystemExit(f"{name}={raw!r} is not a boolean (use true/false)")


def _env_float(name: str, default: float) -> float:
    raw = _env_str(name, str(default))
    try:
        value = float(raw)
    except ValueError:
        raise SystemExit(f"{name}={raw!r} is not a number") from None
    if value < 0.0:
        raise SystemExit(f"{name}={raw!r} must be >= 0")
    return value


def node_identity(is_active_service: str) -> tuple[str, str]:
    """(node_name, namespace) that makes rclpy answer the adapter's derived service.

    The adapter asks ``<parent>/get_parameters`` where ``<parent>`` is the Trigger
    service path minus its last segment (``get_parameters_service_for``). rclpy
    creates a node's parameter services under ``<namespace>/<node_name>/``, so
    naming the node after that parent segment makes the derivation land on THIS
    node — no second node, no manual parameter service.

      ``/lifecycle_manager_navigation/is_active`` -> ("lifecycle_manager_navigation", "/")
      ``/nav/lifecycle_manager/is_active``        -> ("lifecycle_manager", "/nav")
    """
    parent, _, leaf = is_active_service.rpartition("/")
    namespace, _, node_name = parent.rpartition("/")
    if not leaf or not node_name:
        raise SystemExit(
            f"is_active service {is_active_service!r} must be an absolute path with a "
            "parent segment, e.g. /lifecycle_manager_navigation/is_active"
        )
    return node_name, namespace or "/"


class StubSut(Node):
    """One node that owns all three surfaces the readiness barrier touches."""

    def __init__(
        self,
        *,
        node_name: str,
        namespace: str,
        is_active_service: str,
        goal_action: str,
        use_sim_time: bool,
        goal_hold_s: float,
    ) -> None:
        # start_parameter_services defaults to True and is LOAD-BEARING here: it is
        # what publishes <node>/get_parameters, i.e. contract step 3. The override
        # (not a set_parameters call) is what makes use_sim_time true from the very
        # first answer — a node that flips it later would race the barrier.
        super().__init__(
            node_name,
            namespace=namespace,
            parameter_overrides=[Parameter("use_sim_time", Parameter.Type.BOOL, use_sim_time)],
        )
        self._goal_hold_s = goal_hold_s
        self._is_active_calls = 0
        self._goals = 0
        self._service = self.create_service(Trigger, is_active_service, self._on_is_active)
        # goal_callback is spelled out although ACCEPT is rclpy's default: "the goal
        # was ACCEPTED" is a contract point the adapter checks (`handle.accepted`),
        # and a contract point should be visible in the code that promises it.
        self._action = ActionServer(
            self,
            NavigateToPose,
            goal_action,
            execute_callback=self._on_goal,
            goal_callback=lambda _goal: GoalResponse.ACCEPT,
        )
        self.get_logger().info(
            f"stub SUT up: is_active={is_active_service} action={goal_action} "
            f"use_sim_time={self.get_parameter('use_sim_time').value} "
            f"goal_hold_s={goal_hold_s} ros_domain_id={os.environ.get('ROS_DOMAIN_ID', '<unset>')}"
        )

    def _on_is_active(self, _request: Trigger.Request, response: Trigger.Response):
        """Readiness gate: always active. This stub has no lifecycle to bring up."""
        self._is_active_calls += 1
        response.success = True
        response.message = "stub SUT active"
        self.get_logger().info(f"is_active -> success (call #{self._is_active_calls})")
        return response

    def _on_goal(self, goal_handle):
        """Accept the nav goal and finish it — WITHOUT moving anything.

        Legitimate for this scenario only: the self-test spawns the robot at its
        goal, so "arrived" is true before the goal is sent. The verdict is taken
        from Isaac GT by the oracle regardless of what this returns.
        """
        self._goals += 1
        pose = goal_handle.request.pose.pose.position
        self.get_logger().info(f"goal #{self._goals} accepted: x={pose.x:.3f} y={pose.y:.3f}")
        if self._goal_hold_s > 0.0:
            # Blocking on purpose: by the time a goal exists the barrier is long
            # past, and a single-threaded executor is one less moving part than a
            # callback-group dance nothing needs.
            deadline = time.monotonic() + self._goal_hold_s
            while time.monotonic() < deadline:
                time.sleep(0.05)
        goal_handle.succeed()
        self.get_logger().info(f"goal #{self._goals} -> succeeded")
        return NavigateToPose.Result()


def main() -> int:
    is_active_service = _env_str("CV_STUB_IS_ACTIVE_SERVICE", DEFAULT_IS_ACTIVE_SERVICE)
    goal_action = _env_str("CV_STUB_GOAL_ACTION", DEFAULT_GOAL_ACTION)
    use_sim_time = _env_bool("CV_STUB_USE_SIM_TIME", DEFAULT_USE_SIM_TIME)
    goal_hold_s = _env_float("CV_STUB_GOAL_HOLD_S", DEFAULT_GOAL_HOLD_S)
    node_name, namespace = node_identity(is_active_service)

    rclpy.init()
    node = StubSut(
        node_name=node_name,
        namespace=namespace,
        is_active_service=is_active_service,
        goal_action=goal_action,
        use_sim_time=use_sim_time,
        goal_hold_s=goal_hold_s,
    )
    try:
        rclpy.spin(node)
    except KeyboardInterrupt:
        pass
    finally:
        node.destroy_node()
        if rclpy.ok():
            rclpy.shutdown()
    return 0


if __name__ == "__main__":
    sys.exit(main())
