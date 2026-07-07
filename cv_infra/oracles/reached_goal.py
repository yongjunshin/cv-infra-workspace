"""MVP oracle: reached_goal (M2 impl of M1 OracleBase — REQ-EXEC-011).

Passes iff the robot's ground-truth trajectory comes within the position (and,
if declared, yaw) tolerance of the goal within the sim-time budget (D-F). Uses GT
pose samples only (``get_world_pose()``), never the SUT ``/odom`` (LOCKED §7).
Decision math is stdlib-only and unit-tested on CPU.
"""

from __future__ import annotations

import math

from cv_infra.oracles.base import OracleBase
from cv_infra.runner.evaluate import OracleOutcome, read_field
from cv_infra.runner.telemetry import TelemetryRecord, first_reach_index

# Fallback tolerances if the criteria omit them (P2 hardcoded; contract'd in P3).
DEFAULT_POS_TOL_M = 0.25
DEFAULT_YAW_TOL_RAD = 0.20


def yaw_from_quat_wxyz(q: tuple[float, float, float, float]) -> float:
    """Yaw (rad) about +Z from a (w, x, y, z) quaternion — stdlib only."""
    w, x, y, z = q
    return math.atan2(2.0 * (w * z + x * y), 1.0 - 2.0 * (y * y + z * z))


def angle_diff(a: float, b: float) -> float:
    """Smallest signed difference a-b wrapped to [-pi, pi]."""
    return math.atan2(math.sin(a - b), math.cos(a - b))


class ReachedGoalOracle(OracleBase):
    name = "reached_goal"
    version = "0.1.0"

    def validate_params(self, criteria: object) -> None:
        if read_field(criteria, "goal_position") is None:
            raise ValueError("reached_goal criteria require a goal_position [x, y, z]")

    def evaluate(self, telemetry: TelemetryRecord, criteria: object) -> OracleOutcome:
        samples = telemetry.gt_pose_samples
        goal = read_field(criteria, "goal_position")
        pos_tol = float(read_field(criteria, "position_tolerance_m", DEFAULT_POS_TOL_M))
        timeout_s = read_field(criteria, "timeout_s")

        if goal is None:
            return OracleOutcome(
                self.name, passed=False, reason="bad_criteria", detail="missing goal_position"
            )
        if not samples:
            return OracleOutcome(
                self.name, passed=False, reason="no_telemetry", detail="no GT pose samples"
            )

        goal_xyz = (float(goal[0]), float(goal[1]), float(goal[2]))
        idx = first_reach_index(samples, goal_xyz, pos_tol)
        elapsed = samples[-1].sim_time_s - samples[0].sim_time_s

        if idx is None:
            timed_out = timeout_s is not None and elapsed >= float(timeout_s)
            return OracleOutcome(
                self.name,
                passed=False,
                reason="timeout" if timed_out else "not_reached",
                detail=f"closest within tol never reached (elapsed_sim={elapsed:.2f}s)",
            )

        reach_time = samples[idx].sim_time_s - samples[0].sim_time_s
        if timeout_s is not None and reach_time > float(timeout_s):
            return OracleOutcome(
                self.name,
                passed=False,
                reason="timeout",
                detail=f"reached at {reach_time:.2f}s > budget {timeout_s}s",
            )

        goal_orient = read_field(criteria, "goal_orientation_wxyz")
        if goal_orient is not None:
            yaw_tol = float(read_field(criteria, "yaw_tolerance_rad", DEFAULT_YAW_TOL_RAD))
            got_yaw = yaw_from_quat_wxyz(samples[idx].orientation_wxyz)
            want_yaw = yaw_from_quat_wxyz(tuple(float(v) for v in goal_orient))
            if abs(angle_diff(got_yaw, want_yaw)) > yaw_tol:
                return OracleOutcome(
                    self.name,
                    passed=False,
                    reason="orientation",
                    detail="reached position but yaw out of tolerance",
                )

        return OracleOutcome(self.name, passed=True, detail=f"reached at {reach_time:.2f}s")
