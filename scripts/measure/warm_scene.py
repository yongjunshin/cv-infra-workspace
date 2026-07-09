#!/usr/bin/env python3
"""warm_scene.py — M5 cache-warming scene loader (scripts/measure).

Runs INSIDE the runner image via the bundled interpreter (host wrapper overrides the
ENTRYPOINT with ``--entrypoint /isaac-sim/python.sh`` — G-14). Boots SimulationApp
headless FIRST (LOCKED §7.7), then opens the scene at ``get_assets_root_path() +
scene_rel`` and pumps ``app.update()`` until the container's network goes idle. That
forces ``omni.client`` to download the FULL dependency closure into the mounted disk
cache (``.cache/ov/client`` for assets; the Kit/ComputeCache mounts for the GPU-derived
shader/compute caches). One load, then exit — no physics/oracle/ROS/telemetry.

This is the committed port of the T0 probe's PROVEN ``openstage`` warming path
(cycle p2c6, ``reports/deployment-2026-07-09-fu16-probe.md``) — cache warming is M5's
charter responsibility, distinct from the M2 runner runtime (``cv_infra.runner``).

The scene ref is a measurement INPUT (``--scene-rel`` / ``CV_MEASURE_SCENE_REL``), never
a product hardcode (R7). The EULA boot guard only READS the operator-injected env — no
acceptance literal is baked here (NEG-2).
"""

# stdlib only before SimulationApp (LOCKED §7.7) — no omni.*/isaacsim.* here.
import argparse
import os
import sys
import time

EXIT_PASS = 0
EXIT_FAIL = 1
EXIT_PLATFORM = 3

# Default = the P2 canonical carter warehouse nav scene (relative to the S3 asset root).
# Overridable so the warmed closure can be matched to the scenario under test.
DEFAULT_SCENE_REL = "/Isaac/Samples/ROS2/Scenario/carter_warehouse_navigation.usd"


def log(msg: str) -> None:
    print(f"[cv-warm] {msg}", flush=True)


def rx_bytes() -> int:
    """Sum received bytes across all non-loopback interfaces in this net namespace."""
    total = 0
    with open("/proc/net/dev") as f:
        for line in f.readlines()[2:]:
            name, rest = line.split(":", 1)
            if name.strip() == "lo":
                continue
            total += int(rest.split()[0])
    return total


def sample_until_idle(app, out_dir: str, idle_s: float, max_s: float) -> tuple[int, float]:
    """Pump app.update() and record rx bytes each loop; stop when the net goes idle.

    The idle gap (no new received bytes for ``idle_s``) is how we know the dependency
    closure finished downloading. Raw per-loop samples are preserved to CSV (G-18).
    """
    csv_path = os.path.join(out_dir, "warm_rx_samples.csv")
    rx0 = rx_bytes()
    t0 = time.monotonic()
    last_rx = rx0
    last_change = t0
    prev_t = t0
    prev_rx = rx0
    rows = ["t_s,rx_delta_bytes,rx_rate_Bps"]
    while True:
        try:
            app.update()
        except Exception as exc:  # noqa: BLE001 — a warm loop must not abort on one bad frame
            log(f"app.update() raised: {exc!r}")
        now = time.monotonic()
        rx = rx_bytes()
        dt = max(now - prev_t, 1e-6)
        rows.append(f"{now - t0:.2f},{rx - rx0},{(rx - prev_rx) / dt:.0f}")
        if rx != last_rx:
            last_rx = rx
            last_change = now
        prev_t, prev_rx = now, rx
        if now - last_change > idle_s:
            log(f"network idle {idle_s}s -> closure download complete")
            break
        if now - t0 > max_s:
            log(f"max window {max_s}s reached (closure may be incomplete)")
            break
        time.sleep(0.5)
    with open(csv_path, "w") as f:
        f.write("\n".join(rows) + "\n")
    total = last_rx - rx0
    wall = last_change - t0
    log(f"CV_WARM_RX_TOTAL_BYTES={total} CV_WARM_LOAD_WALL_S={wall:.1f} samples={len(rows) - 1}")
    return total, wall


def main() -> int:
    p = argparse.ArgumentParser(description="M5 cache-warming scene loader")
    p.add_argument("--scene-rel", default=os.environ.get("CV_MEASURE_SCENE_REL", DEFAULT_SCENE_REL))
    p.add_argument("--out", default="/cv/measure-out", help="evidence dir (mounted volume)")
    p.add_argument("--idle-s", type=float, default=30.0, help="net-idle gap that ends the load")
    p.add_argument("--max-s", type=float, default=1700.0, help="hard cap on the warm window")
    args = p.parse_args()
    os.makedirs(args.out, exist_ok=True)

    # EULA boot guard (operator env injected at run time; no literal baked here — NEG-2).
    if not os.environ.get("ACCEPT_EULA"):
        log("ERROR: ACCEPT_EULA not present — refusing to boot (NEG-2).")
        return EXIT_PLATFORM

    from isaacsim import SimulationApp  # noqa: PLC0415 — legal only before omni.* import

    boot_t0 = time.monotonic()
    app = SimulationApp({"headless": True})
    log(f"CV_WARM_BOOT_WALL_S={time.monotonic() - boot_t0:.1f}")

    rc = EXIT_PASS
    try:
        import omni.usd  # noqa: PLC0415 — legal only post-SimulationApp
        from isaacsim.storage.native import get_assets_root_path  # noqa: PLC0415

        root = get_assets_root_path()
        log(f"CV_WARM_ASSET_ROOT={root!r}")
        if not root:
            log("ERROR asset root is None/empty — cannot build scene path")
            return EXIT_FAIL

        scene_path = root + args.scene_rel
        t0 = time.monotonic()
        ok = omni.usd.get_context().open_stage(scene_path)
        open_wall = time.monotonic() - t0
        log(f"CV_WARM_OPEN_OK={ok} CV_WARM_OPEN_WALL_S={open_wall:.2f} scene={scene_path!r}")
        if not ok:
            log("ERROR open_stage returned False")
            return EXIT_FAIL

        sample_until_idle(app, args.out, args.idle_s, args.max_s)

        stage = omni.usd.get_context().get_stage()
        n = sum(1 for _ in stage.Traverse()) if stage is not None else 0
        log(f"CV_WARM_PRIM_COUNT={n}")
        log("CV_WARM_DONE")
    except Exception as exc:  # noqa: BLE001 — surface any boot/load failure as platform exit
        log(f"ERROR unhandled: {exc!r}")
        import traceback

        traceback.print_exc()
        rc = EXIT_PLATFORM
    finally:
        app.close()
    return rc


if __name__ == "__main__":
    sys.exit(main())
