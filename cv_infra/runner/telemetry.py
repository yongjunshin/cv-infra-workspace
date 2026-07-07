"""GT-pose + PhysX-contact telemetry (M2, REQ-EXEC-008) + CPU-testable metric math.

Two planes in ONE OS process (M2 §3.1): the sim plane steps PhysX; this
*verification* plane samples ground-truth pose via ``Articulation.get_world_pose()``
(the Isaac GT pose, NOT the SUT ``/odom`` — LOCKED §7) inside a physics callback and
accumulates PhysX contact events filtered to the robot chassis (D-E, REQ-EXEC-011).

The GPU acquisition itself (physics-callback registration, ``get_world_pose()``,
``subscribe_contact_report_events``) is Isaac-only and lands in cycle 3-4 — see
``PhysicsTelemetrySampler`` below (stub). The pure reduction math (path length,
sim-time-to-goal, chassis collision filtering) is Isaac-independent and is the unit
test surface for this cycle. All math here is stdlib-only (no numpy on the runner's
bundle-independent code path; numpy is legal only after ``SimulationApp`` boots).
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field

# 3-vector / quaternion aliases keep the pure math readable without numpy.
Vec3 = tuple[float, float, float]
QuatWXYZ = tuple[float, float, float, float]


@dataclass(frozen=True)
class PoseSample:
    """One ground-truth pose sample, keyed by sim-time (D-F: sim-time, not wall)."""

    sim_time_s: float
    position: Vec3
    orientation_wxyz: QuatWXYZ


@dataclass(frozen=True)
class ContactEvent:
    """One PhysX contact report between two actor prim-paths, at a sim-time.

    ``PhysxContactReportAPI`` is Applied to the chassis body only (D-E), so one of
    the two actors is (normally) the chassis. Which prim is the chassis and which
    paths are ground/self is NOT hardcoded — it comes from adapter_config / measured
    at runtime (R7 discipline).
    """

    sim_time_s: float
    actor0_path: str
    actor1_path: str


@dataclass
class TelemetryRecord:
    """Accumulated verification-plane telemetry for one job (Isaac-independent)."""

    gt_pose_samples: list[PoseSample] = field(default_factory=list)
    contact_events: list[ContactEvent] = field(default_factory=list)


# --------------------------------------------------------------------------- #
# Pure metric math (REQ-EXEC-012) — CPU unit-test surface.
# --------------------------------------------------------------------------- #
def path_length_m(samples: list[PoseSample]) -> float:
    """Total travelled distance = sum of 3D distances between consecutive samples."""
    total = 0.0
    for prev, cur in zip(samples, samples[1:]):
        total += math.dist(prev.position, cur.position)
    return total


def first_reach_index(samples: list[PoseSample], goal_xyz: Vec3, pos_tol_m: float) -> int | None:
    """Index of the first sample within ``pos_tol_m`` of the goal, else ``None``.

    Shared by the ``reached_goal`` oracle and ``time_to_goal_s`` so goal-reach is
    defined once (position tolerance only; orientation tolerance is the oracle's).
    """
    for i, s in enumerate(samples):
        if math.dist(s.position, goal_xyz) <= pos_tol_m:
            return i
    return None


def time_to_goal_s(samples: list[PoseSample], goal_xyz: Vec3, pos_tol_m: float) -> float | None:
    """Sim-time elapsed from first sample to first goal-reach (D-F), else ``None``."""
    if not samples:
        return None
    idx = first_reach_index(samples, goal_xyz, pos_tol_m)
    if idx is None:
        return None
    return samples[idx].sim_time_s - samples[0].sim_time_s


def _matches(actor_path: str, excluded: str) -> bool:
    """True if ``actor_path`` equals ``excluded`` or is a descendant prim of it."""
    return actor_path == excluded or actor_path.startswith(excluded.rstrip("/") + "/")


def count_real_collisions(
    events: list[ContactEvent], chassis_path: str, excluded_paths: list[str]
) -> int:
    """Chassis-only collision count with ground/self filtered out (D-E).

    A contact counts as a *real* collision only when the non-chassis actor is an
    external body — i.e. it does NOT match any ``excluded_paths`` entry (ground
    plane / designated floor / robot self-bodies such as wheels). Matching is by
    exact path or prim-subtree prefix so a whole floor/robot subtree excludes.
    Prevents the false FAIL where wheel-on-floor contact reads as a collision.
    """
    count = 0
    for e in events:
        others = {e.actor0_path, e.actor1_path} - {chassis_path}
        other = next(iter(others)) if others else chassis_path
        if any(_matches(other, ex) for ex in excluded_paths):
            continue
        count += 1
    return count


def min_clearance_m() -> None:
    """Min distance to obstacles/walls along the GT trajectory (REQ-EXEC-012).

    Mechanism is a PhysX scene-query / raycast per GT sample (NOT contact-based),
    so it is Isaac-only and cannot be produced on this CPU path. Returns ``None`` in
    Phase 2 (metrics.min_clearance_m is optional/None until measured — see task
    data contract). Wired in cycle 3-4 alongside the physics callback.
    """
    return None


class PhysicsTelemetrySampler:
    """GPU-side telemetry acquisition (Isaac-only — wired cycle 3-4).

    Registers a physics callback via ``world.add_physics_callback`` that, each
    substep, reads ``Articulation.get_world_pose()`` (GT) and subscribes to
    ``omni.physx ... subscribe_contact_report_events`` for the chassis body (D-E).
    Kept as an interface so ``main`` can compose it without importing Isaac on CPU.
    """

    def __init__(self, chassis_path: str, excluded_paths: list[str]) -> None:
        self.chassis_path = chassis_path
        self.excluded_paths = list(excluded_paths)
        self.record = TelemetryRecord()

    def attach(self, world: object) -> None:  # pragma: no cover - GPU path
        """Register the physics callback + contact subscription (cycle 3-4)."""
        raise NotImplementedError("GPU telemetry acquisition is wired in cycle 3-4")
