#!/usr/bin/env python3
"""headless_smoke.py — Phase 1 in-container smoke for Isaac Sim 5.1.0 (M2).

Run INSIDE the isaac-sim:5.1.0 container via the bundled interpreter:

    /isaac-sim/python.sh headless_smoke.py --mode smoke     --out /cv/out
    /isaac-sim/python.sh headless_smoke.py --mode handshake --out /cv/out

Modes (dispatched by the run_smoke.sh / run_dds_handshake.sh host wrappers):
  smoke      DoD-P1-04 (REQ-EXEC-001): boot SimulationApp({"headless": True}),
             step PhysX (falling cube), capture >=1 off-screen render-product frame
             to a file and assert it is NON-black (pixel mean AND std > 0 — R19/D-A),
             log step-time/FPS (R5 diagnostics), clean close, exit 0.
  handshake  DoD-P1-05 (REQ-EXEC-004/REQ-ORCH-008 substrate, R8): enable
             isaacsim.ros2.bridge, publish /clock while playing, subscribe /cmd_vel
             and exit 0 once the expected Twist (sent from a separate ros:jazzy
             container across the docker bridge network) is observed in-process.

Hard rules honored here:
  * SimulationApp({"headless": True}) is instantiated BEFORE any omni.*/isaacsim.*
    import (LOCKED §7.7). All Isaac imports live inside functions, after boot.
  * EULA boot guard: refuses to start Isaac when the runtime-injected consent env
    is absent (decision 2026-07-03-p1-eula-runtime-consent; NEG-2). No acceptance
    literal is committed anywhere — the wrapper synthesizes it from operator input.
  * Exit codes follow the 0/1/2/3 contract slots: 0=pass, 1=assertion/test failure,
    2=bad usage (argparse), 3=platform/boot problem (EULA missing, bridge missing).

Markers (stable grep surface for the host wrappers):
  CV_SMOKE_APP_BOOTED / CV_SMOKE_PHYSX_OK / CV_SMOKE_RENDER_OK / CV_SMOKE_PERF /
  CV_SMOKE_PASS / CV_HANDSHAKE_ENV / CV_HANDSHAKE_BRIDGE_ENABLED /
  CV_HANDSHAKE_GRAPH_READY / CV_HANDSHAKE_CLOCK_TICKING / CV_HANDSHAKE_CMDVEL_RX /
  CV_HANDSHAKE_PASS / CV_HANDSHAKE_TIMEOUT
"""

# stdlib only before SimulationApp (LOCKED §7.7) — no omni.*/isaacsim.*/numpy here.
import argparse
import os
import sys
import time

EXIT_PASS = 0
EXIT_FAIL = 1
EXIT_PLATFORM = 3


def log(msg: str) -> None:
    print(f"[cv-smoke] {msg}", flush=True)


def eula_boot_guard() -> None:
    """Refuse to boot Isaac without runtime operator consent (NEG-2; LOCKED §8).

    The wrapper derives this env from CV_EULA_CONSENT at run time; nothing is baked
    into any committed file or image layer.
    """
    if not os.environ.get("ACCEPT_EULA"):
        log("ERROR: NVIDIA Isaac Sim EULA has not been accepted for this run.")
        log("Boot refused (NEG-2). Provide operator consent via the host wrapper:")
        log("    CV_EULA_CONSENT=yes ./run_smoke.sh")
        sys.exit(EXIT_PLATFORM)


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Isaac Sim 5.1.0 headless smoke / DDS handshake")
    p.add_argument("--mode", choices=("smoke", "handshake"), required=True)
    p.add_argument("--out", default="/cv/out", help="output dir (mounted volume)")
    p.add_argument("--steps", type=int, default=60, help="[smoke] timed physics steps")
    p.add_argument("--wait-s", type=float, default=180.0, help="[handshake] wall wait for /cmd_vel")
    p.add_argument(
        "--expect-linear-x",
        type=float,
        default=0.42,
        help="[handshake] Twist linear.x value the ros:jazzy peer publishes",
    )
    return p.parse_args()


def write_frame(arr, out_dir: str) -> str:
    """Persist the captured RGB frame. PNG via bundled PIL, PPM (P6) fallback."""
    rgb = arr[..., :3]
    try:
        from PIL import Image  # bundled with Isaac Sim

        path = os.path.join(out_dir, "frame_0001.png")
        Image.fromarray(rgb).save(path)
    except Exception as exc:  # pragma: no cover - fallback path
        log(f"PIL unavailable/failed ({exc}); writing binary PPM instead")
        path = os.path.join(out_dir, "frame_0001.ppm")
        h, w = rgb.shape[:2]
        with open(path, "wb") as f:
            f.write(f"P6\n{w} {h}\n255\n".encode("ascii"))
            f.write(rgb.tobytes())
    return path


def run_smoke(simulation_app, args) -> int:
    # Isaac imports are legal only after SimulationApp instantiation (LOCKED §7.7).
    import numpy as np
    import omni.replicator.core as rep
    import omni.usd
    from isaacsim.core.api import World
    from isaacsim.core.api.objects import DynamicCuboid
    from isaacsim.core.api.objects.ground_plane import GroundPlane
    from pxr import UsdLux

    os.makedirs(args.out, exist_ok=True)

    # Minimal local scene (no cloud asset dependency): lit ground plane + falling cube.
    world = World(stage_units_in_meters=1.0)
    stage = omni.usd.get_context().get_stage()
    UsdLux.DomeLight.Define(stage, "/World/DomeLight").CreateIntensityAttr(1000.0)
    UsdLux.DistantLight.Define(stage, "/World/DistantLight").CreateIntensityAttr(2500.0)
    GroundPlane(prim_path="/World/ground", size=10.0, color=np.array([0.35, 0.35, 0.40]))
    cube = DynamicCuboid(
        prim_path="/World/cube",
        position=np.array([0.0, 0.0, 1.5]),
        scale=np.array([0.5, 0.5, 0.5]),
        color=np.array([0.9, 0.15, 0.15]),
    )

    try:
        from isaacsim.core.utils.viewports import set_camera_view

        set_camera_view(eye=[4.0, 4.0, 3.0], target=[0.0, 0.0, 0.5])
    except Exception as exc:
        log(f"WARN set_camera_view failed ({exc}); keeping default camera")

    # Off-screen render product + RGB annotator (D-A / R19: prove RTX 'graphics'
    # capability with an actual rendered frame, not just nvidia-smi compute).
    render_product = rep.create.render_product("/OmniverseKit_Persp", (1280, 720))
    rgb_annot = rep.AnnotatorRegistry.get_annotator("rgb")
    rgb_annot.attach([render_product])

    world.reset()
    z_start = float(cube.get_world_pose()[0][2])
    physics_dt = float(world.get_physics_dt())
    log(f"CV_SMOKE_APP_BOOTED physics_dt={physics_dt:.6f} z_start={z_start:.3f}")

    # Warm-up (renderer + contact buffers are unreliable on the first steps).
    for _ in range(8):
        world.step(render=True)

    # Timed step loop — R5 diagnostics (FPS / step time; PhysX fallback warnings go
    # to this container log via carb logging and are grepped by the wrapper).
    t0 = time.monotonic()
    for _ in range(args.steps):
        world.step(render=True)
    wall = time.monotonic() - t0
    avg_ms = wall / args.steps * 1000.0
    fps = args.steps / wall
    log(f"CV_SMOKE_PERF steps={args.steps} wall_s={wall:.2f} step_ms={avg_ms:.1f} fps={fps:.1f}")

    # PhysX evidence: the dynamic cube must have fallen under gravity.
    z_end = float(cube.get_world_pose()[0][2])
    if not z_end < z_start - 0.2:
        log(f"ERROR PhysX check failed: cube did not fall (z {z_start:.3f} -> {z_end:.3f})")
        return EXIT_FAIL
    log(f"CV_SMOKE_PHYSX_OK z_start={z_start:.3f} z_end={z_end:.3f}")

    # Capture >=1 frame; step further until the annotator yields data.
    frame = rgb_annot.get_data()
    tries = 0
    while (frame is None or getattr(frame, "size", 0) == 0) and tries < 20:
        world.step(render=True)
        frame = rgb_annot.get_data()
        tries += 1
    if frame is None or getattr(frame, "size", 0) == 0:
        log("ERROR render product produced no frame data")
        return EXIT_FAIL

    arr = np.asarray(frame)
    mean = float(arr[..., :3].mean())
    std = float(arr[..., :3].std())
    path = write_frame(arr, args.out)
    if not (mean > 0.0 and std > 0.0):
        log(f"ERROR black-frame check failed: mean={mean:.4f} std={std:.4f} file={path}")
        return EXIT_FAIL
    log(f"CV_SMOKE_RENDER_OK mean={mean:.2f} std={std:.2f} shape={arr.shape} file={path}")

    log("CV_SMOKE_PASS")
    return EXIT_PASS


def run_handshake(simulation_app, args) -> int:
    import omni.graph.core as og
    import omni.timeline
    from isaacsim.core.utils.extensions import enable_extension

    # Surface the env the wrapper injected (R8/R16 diagnostics + [VERIFY] record).
    for key in (
        "ROS_DISTRO",
        "RMW_IMPLEMENTATION",
        "ROS_DOMAIN_ID",
        "FASTRTPS_DEFAULT_PROFILES_FILE",
    ):
        log(f"CV_HANDSHAKE_ENV {key}={os.environ.get(key)}")
    jazzy_on_path = "isaacsim.ros2.bridge/jazzy/lib" in os.environ.get("LD_LIBRARY_PATH", "")
    log(f"CV_HANDSHAKE_ENV jazzy_in_ld_library_path={jazzy_on_path}")

    if not enable_extension("isaacsim.ros2.bridge"):
        log("ERROR could not enable isaacsim.ros2.bridge")
        return EXIT_PLATFORM
    simulation_app.update()
    log("CV_HANDSHAKE_BRIDGE_ENABLED")

    # Minimal OmniGraph: /clock publisher (forward) + /cmd_vel subscriber (reverse).
    keys = og.Controller.Keys
    try:
        og.Controller.edit(
            {"graph_path": "/CVHandshakeGraph", "evaluator_name": "execution"},
            {
                keys.CREATE_NODES: [
                    ("tick", "omni.graph.action.OnPlaybackTick"),
                    ("sim_time", "isaacsim.core.nodes.IsaacReadSimulationTime"),
                    ("pub_clock", "isaacsim.ros2.bridge.ROS2PublishClock"),
                    ("sub_twist", "isaacsim.ros2.bridge.ROS2SubscribeTwist"),
                ],
                keys.SET_VALUES: [
                    ("pub_clock.inputs:topicName", "clock"),
                    ("sub_twist.inputs:topicName", "cmd_vel"),
                ],
                keys.CONNECT: [
                    ("tick.outputs:tick", "pub_clock.inputs:execIn"),
                    ("tick.outputs:tick", "sub_twist.inputs:execIn"),
                    ("sim_time.outputs:simulationTime", "pub_clock.inputs:timeStamp"),
                ],
            },
        )
    except Exception as exc:
        log(f"ERROR OmniGraph wiring failed: {exc}")
        try:
            ros2_nodes = [n for n in og.get_registered_nodes() if "ros2" in n.lower()]
            log(f"registered ros2 node types: {ros2_nodes}")
        except Exception:
            pass
        return EXIT_PLATFORM
    log("CV_HANDSHAKE_GRAPH_READY topics=/clock(pub),/cmd_vel(sub)")

    timeline = omni.timeline.get_timeline_interface()
    timeline.play()

    lin_attr = og.Controller.attribute("/CVHandshakeGraph/sub_twist.outputs:linearVelocity")
    expect = args.expect_linear_x
    deadline = time.monotonic() + args.wait_s
    last_beat = 0.0
    received = None
    while time.monotonic() < deadline:
        simulation_app.update()
        now = time.monotonic()
        if now - last_beat >= 2.0:
            sim_t = timeline.get_current_time()
            log(f"CV_HANDSHAKE_CLOCK_TICKING sim_time={sim_t:.2f}")
            last_beat = now
        try:
            value = og.Controller.get(lin_attr)
            lx = float(value[0])
        except Exception:
            continue
        if abs(lx - expect) < 1e-3:
            received = lx
            break

    if received is None:
        log(f"CV_HANDSHAKE_TIMEOUT no /cmd_vel with linear.x={expect} within {args.wait_s}s")
        timeline.stop()
        return EXIT_FAIL

    log(f"CV_HANDSHAKE_CMDVEL_RX linear_x={received:.3f} (expected {expect})")
    # Keep /clock flowing briefly so the peer's still-running probes stay green.
    settle = time.monotonic() + 5.0
    while time.monotonic() < settle:
        simulation_app.update()
    timeline.stop()
    log("CV_HANDSHAKE_PASS")
    return EXIT_PASS


def main() -> int:
    eula_boot_guard()
    args = parse_args()

    # LOCKED §7.7: instantiate SimulationApp FIRST — before any omni.*/isaacsim.* import.
    from isaacsim import SimulationApp

    boot_t0 = time.monotonic()
    simulation_app = SimulationApp({"headless": True})
    log(f"SimulationApp headless boot took {time.monotonic() - boot_t0:.1f}s wall")

    try:
        if args.mode == "smoke":
            rc = run_smoke(simulation_app, args)
        else:
            rc = run_handshake(simulation_app, args)
    except Exception as exc:
        log(f"ERROR unhandled exception: {exc!r}")
        rc = EXIT_PLATFORM
    finally:
        simulation_app.close()
    return rc


if __name__ == "__main__":
    sys.exit(main())
