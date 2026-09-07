#!/usr/bin/env python3
"""examples/selftest/sim.py — the smallest honest consumer of the verify contract.

A cube is dropped onto a ground plane and its height is recorded. That is the whole
simulation: it exercises the platform (image, GPU, mounts, seed, output collection,
oracle) without depending on any cloud asset, ROS or robot. `cv-infra selftest` runs
exactly this, so a runner that is broken says so here rather than in a consumer's PR.

It is also the reference for what a `sim_script` IS, and every line below that looks
incidental is actually the contract:

* **stdlib only before ``SimulationApp``.** Importing ``omni.*``/``isaacsim.*`` before
  the app is instantiated crashes the boot; all Isaac imports live inside ``simulate``,
  which only runs after. ``cv-infra`` cannot check this for you — it is measured by
  crashing.
* **The output path is relative to the CHECKOUT ROOT**, never to this file and never
  absolute. The container's working dir is the checkout mount, and the platform binds
  the case's own host directory over ``examples/selftest/out`` — so the same command
  writes to the same place locally (``./python.sh examples/selftest/sim.py`` from the
  repo root) and under CI, which is what makes a local reproduction meaningful.
* **The trajectory is written BEFORE ``simulation_app.close()``.** ``close()`` ends the
  process (status 0, always — G-62), so anything after it never runs. That same fact is
  why the exit code carries no verdict here: the ORACLE judges the file this writes.
* **``--gui`` defaults off.** The case container has no display, so a GUI boot hangs or
  dies; the flag exists for a developer at a workstation, and CI simply never passes it.
* **``CV_SEED``** is the platform's per-repeat seed. Consuming it is what makes repeats
  different runs rather than the same run measured twice.

Local parity (from the repository root, GUI optional):

    ./python.sh examples/selftest/sim.py --drop_height=1.5 --cube_scale=0.5 [--gui]
"""

# stdlib only down here — see the module docstring (LOCKED: no omni.*/isaacsim.* yet).
import argparse
import json
import os
import random
import sys
from pathlib import Path

#: Checkout-root-relative, matching the workflow's `sim_output_dir` — see the docstring.
OUT_PATH = Path("examples/selftest/out/trajectory.json")

#: Physics steps to record. 120 steps @ 60 Hz = 2 simulated seconds, enough for a 3 m
#: drop to land and settle, short enough that a case is dominated by Isaac's boot.
STEPS = 120

#: How far the seed may move the cube sideways. Small enough that the drop still lands
#: on the plane for every seed, large enough that two repeats are genuinely two runs.
JITTER_M = 0.05


def parse_args() -> argparse.Namespace:
    """The axes of ``param_space.pict`` arrive here as ``--<axis>=<value>``."""
    parser = argparse.ArgumentParser(description="cv-infra selftest: drop a cube, record its z")
    parser.add_argument("--drop_height", type=float, default=1.5, help="metres above the plane")
    parser.add_argument("--cube_scale", type=float, default=0.5, help="cube edge length, metres")
    parser.add_argument(
        "--gui",
        action="store_true",
        help="boot with a window — for a workstation with a display; CI never passes it",
    )
    return parser.parse_args()


def simulate(args: argparse.Namespace, seed: int) -> dict:
    """Drop the cube and return the record the oracle will judge.

    Isaac imports are legal only here: the caller has already instantiated
    ``SimulationApp``. ``render=False`` keeps this to the physics half — no render
    product, because the selftest asserts that the platform RUNS a simulation, and a
    rendered frame is a different (and much slower) claim.
    """
    import numpy as np
    from isaacsim.core.api import World
    from isaacsim.core.api.objects import DynamicCuboid
    from isaacsim.core.api.objects.ground_plane import GroundPlane

    rng = random.Random(seed)
    start_xy = [rng.uniform(-JITTER_M, JITTER_M), rng.uniform(-JITTER_M, JITTER_M)]

    world = World(stage_units_in_meters=1.0)
    GroundPlane(prim_path="/World/ground", size=10.0)
    cube = DynamicCuboid(
        prim_path="/World/cube",
        position=np.array([start_xy[0], start_xy[1], args.drop_height]),
        scale=np.array([args.cube_scale] * 3),
    )

    world.reset()
    z_samples = [round(float(cube.get_world_pose()[0][2]), 6)]
    for _ in range(STEPS):
        world.step(render=False)
        z_samples.append(round(float(cube.get_world_pose()[0][2]), 6))

    return {
        "seed": seed,
        "drop_height": args.drop_height,
        "cube_scale": args.cube_scale,
        "physics_dt": float(world.get_physics_dt()),
        "start_xy": [round(value, 6) for value in start_xy],
        "z": z_samples,
    }


def main() -> int:
    args = parse_args()
    # Absent CV_SEED means "run me by hand": a fixed seed keeps that run reproducible.
    seed = int(os.environ.get("CV_SEED") or 0)

    from isaacsim import SimulationApp

    simulation_app = SimulationApp({"headless": not args.gui})
    try:
        record = simulate(args, seed)
        OUT_PATH.write_text(json.dumps(record, indent=2) + "\n", encoding="utf-8")
        print(f"[selftest] wrote {OUT_PATH} ({len(record['z'])} samples, seed {seed})", flush=True)
    finally:
        # G-62: this ends the process with status 0 — nothing below runs, and the exit
        # code cannot carry a verdict. The oracle judges the file, not this status.
        simulation_app.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
