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

    Measured (p2c5 run1): the ContactReportAPI Applied to the chassis body — an
    ARTICULATION ROOT on the carter — aggregates contact reports of the WHOLE
    articulation, so wheel/caster<->ground pairs arrive too (7344 events on a
    clean run, neither actor the chassis). Those are not the D-E surface: an
    event counts only when the chassis IS one of the actors AND the other actor
    does not match any ``excluded_paths`` entry (ground plane / designated
    floor / robot self-bodies). Matching is by exact path or prim-subtree
    prefix so a whole floor/robot subtree excludes. Prevents the false FAIL
    where wheel-on-floor contact reads as a collision.
    """
    count = 0
    for e in events:
        actors = {e.actor0_path, e.actor1_path}
        if chassis_path not in actors:
            continue  # other-link contact (articulation aggregation artifact)
        others = actors - {chassis_path}
        other = next(iter(others)) if others else chassis_path
        if any(_matches(other, ex) for ex in excluded_paths):
            continue
        count += 1
    return count


def contact_partners(events: list[ContactEvent], chassis_path: str) -> list[str]:
    """Distinct non-chassis actor paths seen in contact events (debug surface).

    Bring-up aid (T3/T4): when no_collision counts unexpected contacts, this
    names the offending prims so the consumer can extend
    ``collision_excluded_paths`` with measured values instead of guessing (R7).
    """
    partners: set[str] = set()
    for e in events:
        others = {e.actor0_path, e.actor1_path} - {chassis_path}
        partners.add(next(iter(others)) if others else chassis_path)
    return sorted(partners)


def min_clearance_m() -> None:
    """Min distance to obstacles/walls along the GT trajectory (REQ-EXEC-012).

    The unconditional ``None`` is an INTENTIONAL STUB. The production mechanism is a
    distance query — a PhysX scene-query / raycast per GT sample (NOT contact-based,
    see ``implementation-plan/modules/M2-simulation-runner.md`` §"min-clearance 산출
    메커니즘") — so it is Isaac-only and cannot be produced on this CPU path.

    CEO decision 2026-07-09 (D-3): formally DEFERRED to Phase 4. DoD-P2-01 closes on
    3/4 metrics with ``min_clearance`` footnoted (needs a new PhysX scene-query +
    obstacle-prim set; adding it in the last P2 cycle would be over-engineering). See
    ``agent-comms/decisions/2026-07-09-p2-close-decisions.md`` §D-3.
    """
    return None


class PhysicsTelemetrySampler:
    """GPU-side telemetry acquisition (Isaac-only bodies; deferred imports).

    Registers a physics callback via ``world.add_physics_callback`` that reads
    the chassis rigid body's ``get_world_pose()`` (GT — never SUT ``/odom``,
    LOCKED §7) each physics step, and subscribes to
    ``omni.physx ... subscribe_contact_report_events`` with
    ``PhysxContactReportAPI`` Applied to the CHASSIS BODY ONLY (D-E).

    P2-13 false-FAIL-zero design point (explicit): collisions are filtered in
    TWO stages — (1) acquisition: the ContactReportAPI Apply is chassis-limited,
    so wheel-on-floor contacts of other bodies never even report; (2) reduction:
    ``count_real_collisions`` drops events whose other actor matches
    ``excluded_paths`` (ground plane / designated floor / robot self-bodies).
    Both the chassis prim and the exclusion list travel via adapter_config /
    criteria — never scene-path hardcoded (R7).

    Two-phase acquisition (measured p2c5 probe-02/03): the tensor-view wrapper
    (``SingleRigidPrim``) MUST be created BEFORE ``world.reset()`` — created
    after, the cached simulation view is already invalidated by the sample
    scene's reset-time prim churn ("Simulation view object is invalidated") and
    ``get_world_pose`` raises. So ``bind`` runs as a SimRuntime pre-reset hook
    (wrapper + ContactReportAPI Apply — the Apply lands before the physics
    parse), and ``attach`` (M2 §3.2 step 6, unchanged position) only registers
    the callbacks.
    """

    def __init__(self, chassis_path: str, excluded_paths: list[str]) -> None:
        self.chassis_path = chassis_path
        self.excluded_paths = list(excluded_paths)
        self.record = TelemetryRecord()
        self._world = None
        self._chassis_prim = None
        self._contact_sub = None  # keep-alive: dropping the ref unsubscribes

    _CALLBACK_NAME = "cv_infra_telemetry"

    def bind(self, world: object) -> None:  # pragma: no cover - GPU path (T3)
        """PRE-reset phase: validate the chassis prim, Apply the chassis-only
        ContactReportAPI, create the tensor-view wrapper (probe-03 recipe A)."""
        if not self.chassis_path:
            raise RuntimeError(
                "telemetry needs a chassis_path (no_collision criteria params / "
                "adapter_config) — scene-path hardcoding is forbidden (R7/D-E), so "
                "the consumer scenario must supply it"
            )

        import omni.usd  # noqa: PLC0415 (legal only after SimulationApp boot)
        from pxr import PhysxSchema  # noqa: PLC0415

        stage = omni.usd.get_context().get_stage()
        prim = stage.GetPrimAtPath(self.chassis_path)
        if not prim.IsValid():
            raise RuntimeError(
                f"chassis_path {self.chassis_path!r} not found in the stage — "
                "measure the chassis body prim on the workstation (R7) and fix the "
                "scenario criteria params"
            )
        # D-E: Apply to the chassis body ONLY (never globally); threshold 0 so
        # light touches report — filtering happens in the reduction, not by force.
        contact_api = PhysxSchema.PhysxContactReportAPI.Apply(prim)
        contact_api.CreateThresholdAttr().Set(0.0)

        from isaacsim.core.prims import SingleRigidPrim  # noqa: PLC0415

        self._chassis_prim = SingleRigidPrim(self.chassis_path)

    def attach(self, world: object) -> None:  # pragma: no cover - GPU path (T3)
        """Register the physics callback + chassis-only contact subscription."""
        if self._chassis_prim is None:
            raise RuntimeError(
                "bind(world) must run as a pre-reset hook before attach() — the "
                "tensor-view wrapper created post-reset is invalidated (probe-02)"
            )
        from omni.physx import get_physx_simulation_interface  # noqa: PLC0415
        from pxr import PhysicsSchemaTools  # noqa: PLC0415

        self._world = world

        def on_physics_step(step_size: float) -> None:
            position, orientation = self._chassis_prim.get_world_pose()
            self.record.gt_pose_samples.append(
                PoseSample(
                    sim_time_s=float(self._world.current_time),
                    position=tuple(float(v) for v in position),
                    orientation_wxyz=tuple(float(v) for v in orientation),
                )
            )

        def on_contact(contact_headers, contact_data) -> None:
            for header in contact_headers:
                self.record.contact_events.append(
                    ContactEvent(
                        sim_time_s=float(self._world.current_time),
                        actor0_path=str(PhysicsSchemaTools.intToSdfPath(header.actor0)),
                        actor1_path=str(PhysicsSchemaTools.intToSdfPath(header.actor1)),
                    )
                )

        world.add_physics_callback(self._CALLBACK_NAME, on_physics_step)
        self._contact_sub = get_physx_simulation_interface().subscribe_contact_report_events(
            on_contact
        )

    def detach(self) -> None:
        """Stop sampling (drop callback + contact subscription). CPU-safe no-op."""
        if self._world is not None:  # pragma: no cover - GPU path
            try:
                self._world.remove_physics_callback(self._CALLBACK_NAME)
            except Exception:
                pass
            self._world = None
        self._contact_sub = None  # releasing the ref ends the subscription
        self._chassis_prim = None
