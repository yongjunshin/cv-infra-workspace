"""MVP oracle: reached_goal (M2 impl of M1 OracleBase — REQ-EXEC-011).

Passes iff the robot's ground-truth trajectory comes within the position (and,
if declared, yaw) tolerance of the goal within the sim-time budget (D-F). Uses GT
pose samples only (``get_world_pose()``), never the SUT ``/odom`` (LOCKED §7).
Decision math is stdlib-only and unit-tested on CPU.

Position tolerance has ONE home here (``resolve_position_tolerance``): both this
oracle's verdict and the ``time_to_goal_s`` metric in ``runner.main`` take it
from that function, so the two decision surfaces cannot drift apart (G-25).
"""

from __future__ import annotations

import math
from dataclasses import dataclass

from cv_infra.oracles.base import OracleBase
from cv_infra.runner.evaluate import OracleOutcome, read_field
from cv_infra.runner.telemetry import TelemetryRecord, first_reach_index

# Fallback tolerances if the criteria omit them (P2 hardcoded; contract'd in P3).
DEFAULT_POS_TOL_M = 0.25
DEFAULT_YAW_TOL_RAD = 0.20

# Where the applied position tolerance came from (audit tag on the outcome).
TOL_FROM_BUDGET = "derived_from_budget"
TOL_FROM_CONSTANT = "scenario_constant"
TOL_FROM_DEFAULT = "oracle_default"

# Both terms of ``goal_tolerance_budget`` are required — half a budget is not a
# budget (a missing term must not be read as 0 and silently tighten the verdict).
BUDGET_TERMS = ("sut_xy_goal_tolerance_m", "localization_budget_m")


@dataclass(frozen=True)
class ToleranceDecision:
    """The position tolerance a verdict was taken with, and where it came from.

    ``audit`` is the human-readable provenance carried into the outcome detail
    (and hence into ``result.json``'s ``criteria_results[].detail``), so a run
    can be audited after the fact for WHICH number judged it — a derived
    tolerance is worthless as evidence if nobody can see the value it took.
    """

    value_m: float
    source: str
    audit: str


def _budget_term(budget: object, name: str) -> float:
    """One ``goal_tolerance_budget`` term as a positive float, else raise LOUD."""
    raw = read_field(budget, name)
    if raw is None:
        raise ValueError(
            f"goal_tolerance_budget is missing '{name}' — both of "
            f"{list(BUDGET_TERMS)} are required (half a budget is not a budget)"
        )
    try:
        value = float(raw)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"goal_tolerance_budget.{name} is not a number: {raw!r}") from exc
    if value <= 0.0:
        raise ValueError(f"goal_tolerance_budget.{name} must be > 0 (got {value})")
    return value


def resolve_position_tolerance(criteria: object) -> ToleranceDecision:
    """Decide the position tolerance from the criteria — the ONLY place it is decided.

    Three paths, in this order:

    * ``goal_tolerance_budget`` declared -> the tolerance is DERIVED from what the
      SUT declares it achieves (CEO D-6, p5c11): the planner stops when its own
      pose ESTIMATE is within ``sut_xy_goal_tolerance_m`` of the goal, and the
      estimate itself is off by at most the declared ``localization_budget_m``,
      so by the triangle inequality the GT residual at that instant is bounded by
      their SUM. The oracle's predicate is over the closest approach, so no
      overshoot term is needed (overshoot can only move the FINAL pose, never the
      minimum). Assumptions surfaced, not hidden: the bound holds only while the
      consumer's declared localization budget holds, and it ignores the GT sample
      spacing (v*dt/2 ~ 4 mm at 0.5 m/s and 1/60 s).
    * ``position_tolerance_m`` declared -> the pre-existing constant path, kept for
      SUTs that declare no budget (behaviour unchanged, byte for byte).
    * neither -> ``DEFAULT_POS_TOL_M``.

    Declaring BOTH is a LOUD contract violation (one home per field) — never a
    silent precedence rule. Raises ``ValueError``; on the runner path that lands
    pre-boot through ``validate_params`` -> exit 2 (usage), not a mid-mission crash.
    """
    budget = read_field(criteria, "goal_tolerance_budget")
    constant = read_field(criteria, "position_tolerance_m")
    if budget is not None and constant is not None:
        raise ValueError(
            "reached_goal declares BOTH 'goal_tolerance_budget' and "
            "'position_tolerance_m' — the position tolerance has exactly one home. "
            "Keep the budget and delete 'position_tolerance_m' (the tolerance is "
            "then derived), or keep the constant and delete 'goal_tolerance_budget'."
        )
    if budget is not None:
        sut_tol, loc_budget = (_budget_term(budget, name) for name in BUDGET_TERMS)
        value = sut_tol + loc_budget
        return ToleranceDecision(
            value_m=value,
            source=TOL_FROM_BUDGET,
            audit=(
                f"tolerance {value:.3f} m [{TOL_FROM_BUDGET}: "
                f"{BUDGET_TERMS[0]} {sut_tol:.3f} + {BUDGET_TERMS[1]} {loc_budget:.3f}]"
            ),
        )
    if constant is not None:
        value = float(constant)
        return ToleranceDecision(
            value_m=value,
            source=TOL_FROM_CONSTANT,
            audit=f"tolerance {value:.3f} m [{TOL_FROM_CONSTANT}: position_tolerance_m]",
        )
    return ToleranceDecision(
        value_m=DEFAULT_POS_TOL_M,
        source=TOL_FROM_DEFAULT,
        audit=f"tolerance {DEFAULT_POS_TOL_M:.3f} m [{TOL_FROM_DEFAULT}]",
    )


def yaw_from_quat_wxyz(q: tuple[float, float, float, float]) -> float:
    """Yaw (rad) about +Z from a (w, x, y, z) quaternion — stdlib only."""
    w, x, y, z = q
    return math.atan2(2.0 * (w * z + x * y), 1.0 - 2.0 * (y * y + z * z))


def angle_diff(a: float, b: float) -> float:
    """Smallest signed difference a-b wrapped to [-pi, pi]."""
    return math.atan2(math.sin(a - b), math.cos(a - b))


def _yaw_out_of_tolerance(sample, criteria: object) -> bool:
    """Did the reached sample miss a DECLARED goal orientation?

    False when no ``goal_orientation_wxyz`` is declared — position-only goals are
    the norm, and "not declared" must read as "not judged", never as a failure.
    Split out of the verdict ladder so the quaternion math sits next to its own
    name instead of inside the last decision of ``evaluate``.
    """
    goal_orient = read_field(criteria, "goal_orientation_wxyz")
    if goal_orient is None:
        return False
    yaw_tol = float(read_field(criteria, "yaw_tolerance_rad", DEFAULT_YAW_TOL_RAD))
    got_yaw = yaw_from_quat_wxyz(sample.orientation_wxyz)
    want_yaw = yaw_from_quat_wxyz(tuple(float(v) for v in goal_orient))
    return abs(angle_diff(got_yaw, want_yaw)) > yaw_tol


class ReachedGoalOracle(OracleBase):
    name = "reached_goal"
    version = "0.1.0"

    def validate_params(self, criteria: object) -> None:
        if read_field(criteria, "goal_position") is None:
            raise ValueError("reached_goal criteria require a goal_position [x, y, z]")
        # Pre-boot (exit 2, D-1 2026-07-13): a both-declared / half budget must be
        # rejected before the GPU spends a mission, not at evaluation time.
        resolve_position_tolerance(criteria)

    def evaluate(self, telemetry: TelemetryRecord, criteria: object) -> OracleOutcome:
        samples = telemetry.gt_pose_samples
        goal = read_field(criteria, "goal_position")
        tolerance = resolve_position_tolerance(criteria)
        pos_tol = tolerance.value_m
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
            closest = min(math.dist(s.position, goal_xyz) for s in samples)
            return OracleOutcome(
                self.name,
                passed=False,
                reason="timeout" if timed_out else "not_reached",
                detail=(
                    f"closest approach {closest:.3f} m never within {tolerance.audit} "
                    f"(elapsed_sim={elapsed:.2f}s)"
                ),
            )

        reach_time = samples[idx].sim_time_s - samples[0].sim_time_s
        if timeout_s is not None and reach_time > float(timeout_s):
            return OracleOutcome(
                self.name,
                passed=False,
                reason="timeout",
                detail=f"reached at {reach_time:.2f}s > budget {timeout_s}s ({tolerance.audit})",
            )

        if _yaw_out_of_tolerance(samples[idx], criteria):
            return OracleOutcome(
                self.name,
                passed=False,
                reason="orientation",
                detail="reached position but yaw out of tolerance",
            )

        return OracleOutcome(
            self.name, passed=True, detail=f"reached at {reach_time:.2f}s within {tolerance.audit}"
        )
