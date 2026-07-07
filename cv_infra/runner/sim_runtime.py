"""Headless SimulationApp lifecycle (M2, REQ-EXEC-001/002/003/015, NFR-EXEC-001).

Boots ``SimulationApp({"headless": True})`` FIRST (before any ``omni.*`` / ``isaacsim.*``
import — LOCKED §7.7), opens the scene, spawns the robot at a fixed pose, pins
``physics_dt`` / ``rendering_dt`` / seed for determinism, runs the step loop, and
closes cleanly to return VRAM/slots. All Isaac imports are deferred into ``boot()``
so this module imports on a CPU host with no Isaac present.

Reuses (does NOT re-invent) the P1 ``scripts/isaac_smoke/headless_smoke.py`` pattern:
SimulationApp-first ordering + the EULA boot guard. That P1 file is a read-only
artifact and not an importable package, so the ~5-line guard is mirrored here (M5
§3.1 / decision 2026-07-03-p1-eula-runtime-consent). The GPU bring-up bodies below
are the seams filled in cycles 2-4.
"""

from __future__ import annotations

import os
from dataclasses import dataclass


class EulaNotAcceptedError(RuntimeError):
    """Raised when the runtime operator consent env is absent (LOCKED §8, NEG-2)."""


def eula_boot_guard(env: dict | None = None) -> None:
    """Refuse to boot Isaac without runtime operator consent (mirrors headless_smoke).

    ``ACCEPT_EULA`` is injected by M3 only on explicit operator consent; it is never
    baked into any committed file or image layer. CPU-testable (pure env check).
    """
    environ = os.environ if env is None else env
    if not environ.get("ACCEPT_EULA"):
        raise EulaNotAcceptedError(
            "NVIDIA Isaac Sim EULA not accepted for this run — boot refused (NEG-2). "
            "M3 injects ACCEPT_EULA only on explicit operator consent."
        )


@dataclass
class SimConfig:
    """Deterministic sim settings (REQ-EXEC-002/003). P2: from JOB_SPEC; P3: Scenario."""

    scene_ref: str
    robot_usd_ref: str
    initial_pose_xyz: tuple[float, float, float] = (0.0, 0.0, 0.0)
    physics_dt: float = 1.0 / 60.0
    rendering_dt: float = 1.0 / 60.0
    seed: int = 0


class SimRuntime:
    """Wraps the SimulationApp / World lifecycle. GPU bodies wired in cycles 2-4."""

    def __init__(self, config: SimConfig) -> None:
        self.config = config
        self.simulation_app = None  # set by boot()
        self.world = None  # set by load_scene()

    def boot(self) -> object:
        """Instantiate SimulationApp FIRST, then it is legal to import omni/isaacsim."""
        eula_boot_guard()
        # LOCKED §7.7 — SimulationApp before any omni.*/isaacsim.* import.
        from isaacsim import SimulationApp  # noqa: PLC0415 (deferred by design)

        self.simulation_app = SimulationApp({"headless": True})
        return self.simulation_app

    def load_scene(self) -> None:  # pragma: no cover - GPU path (cycle 2-3)
        """open_stage(scene_ref) + spawn robot at fixed pose; pin dt/seed; world.reset()."""
        raise NotImplementedError("scene load / robot spawn wired in cycle 2-3")

    def step(self, render: bool = True) -> None:  # pragma: no cover - GPU path
        """One fixed-dt step (render=True: Nova Carter RTX lidar needs off-screen render)."""
        raise NotImplementedError("step loop wired in cycle 3-4")

    def close(self) -> None:
        """Clean shutdown — returns VRAM/slots (REQ-EXEC-015, NFR-EXEC-002/004)."""
        if self.simulation_app is not None:  # pragma: no cover - GPU path
            self.simulation_app.close()
            self.simulation_app = None
