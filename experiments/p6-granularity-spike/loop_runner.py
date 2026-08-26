"""Arm B (C-2) — ONE Isaac process, N missions in series. p6c1 spike, THROWAWAY.

This is ``cv_infra/runner/main.py``'s ``run()`` COPIED AND MODIFIED (cycle-plan §2 /
task §1): the production module is not touched, not subclassed and not imported for
its ``run`` — everything below re-uses the SAME seams main uses (``SimRuntime``,
``Ros2Adapter``, ``PhysicsTelemetrySampler``, ``EvaluationEngine``, the recorders and
``build_result_dict``), so a per-iteration result is assembled by the same code that
assembles a per-job one.

WHAT IS DELIBERATELY DIFFERENT FROM main.run (this list IS the experiment):

1. boot + ROS-bridge + scene load + adapter wire happen ONCE; iterations reuse them.
2. per iteration i: re-pin seed -> ``world.stop()`` -> re-apply initial_pose /
   debug_obstacle / declared sensors -> re-bind telemetry -> ``world.reset()`` ->
   SUT nav-state realign over ROS (AMCL ``/initialpose`` + costmap clears) ->
   readiness barrier -> mission -> record -> evaluate -> ``results/<i>/result.json``.
3. exactly N result.json files come out of ONE process/container. That is precisely
   what today's contract forbids (REQ-EXEC-013 "one job -> exactly one result"), and
   demonstrating the collision is Q4 of the spike — nothing here proposes a contract.
4. an aggregate exit code is invented for the container boundary (see ``_worst``); it
   is a spike placeholder, NOT a contract proposal.

G-62 is honored: results/timings are flushed to disk as each iteration ends, and the
process is terminated by ``hard_exit`` (``os._exit``) — ``SimulationApp.close()`` is
never called, exactly as in production.

Env (all required unless marked):
  CV_SPIKE_SPECS   path to a JSON array of canonical JOB_SPEC dicts (>=1)
  RESULT_OUT       output root; iteration i writes <root>/results/<i>/
  ACCEPT_EULA      operator consent (the production boot guard, unchanged)
  CV_SPIKE_REALIGN_SETTLE_S   (optional, default 3.0) sim-seconds to settle after realign
  CV_SPIKE_ABLATE  (optional, p6c2) comma set of COMPONENT TOGGLES — see ABLATIONS
  CV_SPIKE_CLEANUP (optional, p6c2) comma set of TARGETED CLEANUPS — see CLEANUPS

p6c2 addition: both env sets default to EMPTY, i.e. the loop below is byte-for-byte
the p6c1 behaviour unless a toggle is named. Every toggle is a measurement arm, not
a proposal; the ones that survive Phase 2 are named in the report.
"""

from __future__ import annotations

import json
import os
import sys
import time
from pathlib import Path

from cv_infra.runner.evaluate import VERDICT_ERROR, EvaluationEngine, build_result_dict, read_field
from cv_infra.runner.main import (
    EXIT_FAIL,
    EXIT_PASS,
    EXIT_PLATFORM,
    EXIT_USAGE,
    READINESS_TIMEOUT_S,
    BadJobSpec,
    build_oracles,
    criteria_view,
    hard_exit,
    parse_request,
    require_job_id,
    sim_config_for,
    validate_oracle_params,
    write_result,
)
from cv_infra.runner.sim_runtime import EulaNotAcceptedError, resolve_initial_pose_target

SPECS_ENV = "CV_SPIKE_SPECS"
SETTLE_ENV = "CV_SPIKE_REALIGN_SETTLE_S"
DEFAULT_REALIGN_SETTLE_S = 3.0

# --------------------------------------------------------------------------- #
# p6c2 — ablation toggles (Phase 1) and targeted cleanups (Phase 2).
# --------------------------------------------------------------------------- #
ABLATE_ENV = "CV_SPIKE_ABLATE"
CLEANUP_ENV = "CV_SPIKE_CLEANUP"

#: Phase-1 component toggles. Each REMOVES one thing the p6c1 loop does per
#: iteration, so the VRAM slope of (base - variant) attributes the growth to that
#: component. Diagnostic arms are marked: they change what the run MEASURES and are
#: never candidates for production.
ABLATIONS = {
    "obstacle_move": "re-stage the debug obstacle by MOVING the existing prim "
    "instead of RemovePrim + FixedCuboid(); also a Phase-2 candidate",
    "no_obstacle": "skip the debug obstacle entirely (diagnostic)",
    "no_sensors": "skip the per-iteration enable_declared_sensors() call",
    "no_video": "skip the mp4 writer cycle (diagnostic)",
    "no_bag": "skip the rosbag2 session (diagnostic)",
    "no_mission": "skip drive_mission — re-initialise only (diagnostic)",
    "no_realign": "skip the SUT /initialpose + costmap realign (diagnostic)",
    "no_restage": "skip stop/author/reset entirely — keep driving the SAME world "
    "and telemetry binding to the next goal (diagnostic)",
    "soft_reset": "re-stage with World.reset(soft=True) — the SDK's own path that "
    "does NOT stop/replay the timeline, so the physics simulation views are never "
    "destroyed and re-created; PRODUCTION CANDIDATE (see report)",
}

#: Phase-2 targeted cleanups. All are EXISTING Isaac 5.1.0 / stdlib calls invoked at
#: the iteration boundary — no cleanup framework, no re-implementation.
CLEANUPS = {
    "orphan_materials": "delete the /World/Looks/visual_material* and "
    "/World/Physics_Materials/physics_material* prims that each FixedCuboid() "
    "construction creates and nothing removes (isaacsim.core.utils.prims.delete_prim)",
    "gc": "gc.collect() at the iteration boundary (the SDK's own idiom in "
    "World.clear_instance / SimulationContext.clear_all_callbacks)",
    "mesh_cache": "omni.physx cooking interface release_local_mesh_cache()",
}


def _flag_set(environ: dict, key: str, known: dict) -> frozenset:
    """Parse a comma-separated toggle env; an unknown name is LOUD, never ignored."""
    raw = (environ.get(key) or "").strip()
    if not raw:
        return frozenset()
    names = {part.strip() for part in raw.split(",") if part.strip()}
    unknown = sorted(names - set(known))
    if unknown:
        raise BadJobSpec(f"{key}: unknown toggle(s) {unknown} — known: {sorted(known)}")
    return frozenset(names)


#: nav2 services used to drop stale occupancy between iterations (blackbox-safe: they
#: are the SUT's own published interfaces — nothing inside the SUT is modified).
COSTMAP_CLEAR_SERVICES = (
    "/global_costmap/clear_entirely_global_costmap",
    "/local_costmap/clear_entirely_local_costmap",
)
INITIALPOSE_TOPIC = "/initialpose"
DEBUG_OBSTACLE_PRIM = "/World/cv_debug_obstacle"


def _log(msg: str) -> None:
    print(f"[cv-spike] {msg}", flush=True)


class _Stopwatch:
    """Wall-clock stopwatch that records named spans into a dict."""

    def __init__(self) -> None:
        self.spans: dict[str, float] = {}
        self._t0: dict[str, float] = {}

    def begin(self, name: str) -> None:
        self._t0[name] = time.monotonic()

    def end(self, name: str) -> float:
        elapsed = time.monotonic() - self._t0.pop(name)
        self.spans[name] = round(elapsed, 4)
        return elapsed


def load_specs(env: dict | None = None) -> list[dict]:
    environ = os.environ if env is None else env
    raw = environ.get(SPECS_ENV)
    if not raw:
        raise BadJobSpec(f"{SPECS_ENV} is required (path to a JSON array of JOB_SPEC dicts)")
    path = Path(raw)
    if not path.is_file():
        raise BadJobSpec(f"{SPECS_ENV}={raw} is not a readable file")
    try:
        specs = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise BadJobSpec(f"{SPECS_ENV} is not valid JSON: {exc}") from exc
    if not isinstance(specs, list) or not specs:
        raise BadJobSpec(f"{SPECS_ENV} must decode to a NON-EMPTY JSON array of JOB_SPEC dicts")
    return specs


def resolve_out_root(env: dict | None = None) -> Path:
    environ = os.environ if env is None else env
    raw = environ.get("RESULT_OUT")
    if not raw:
        raise BadJobSpec("RESULT_OUT is required (output root dir for this spike run)")
    return Path(raw)


def _worst(codes: list[int]) -> int:
    """Aggregate N per-iteration exit codes into ONE container status.

    SPIKE PLACEHOLDER, not a proposal: platform (3) outranks usage (2) outranks
    fail (1) outranks pass (0). Q4's whole point is that a contract which says
    "one job -> one result -> one code" has no defined answer here.
    """
    for code in (EXIT_PLATFORM, EXIT_USAGE, EXIT_FAIL):
        if code in codes:
            return code
    return EXIT_PASS


# --------------------------------------------------------------------------- #
# SUT realign (ROS-only, blackbox-safe) — the "1순위" of the cycle plan.
# --------------------------------------------------------------------------- #
class SutRealigner:
    """Reset the SUT's nav state between iterations WITHOUT touching the SUT.

    (a) re-seed AMCL with the pose the sim just teleported the robot to
        (``/initialpose``, the same topic RViz's "2D Pose Estimate" publishes), and
    (b) clear both costmaps so occupancy from the previous mission cannot decide this
        one.

    The publisher/clients are created ONCE and kept: a publisher created and used in the
    same instant has not been DISCOVERED by the subscriber yet, so the message would go
    nowhere and the realign would read as done while doing nothing (G-26). ``realign``
    therefore also waits for a matched subscription, bounded, and reports what it saw.
    """

    def __init__(self, adapter) -> None:
        self.adapter = adapter
        self.node = adapter._node  # spike: the adapter exposes no node accessor
        self._pub = None
        self._clients: dict = {}

    def _publisher(self):
        if self._pub is None:
            from geometry_msgs.msg import PoseWithCovarianceStamped  # noqa: PLC0415

            self._pub = self.node.create_publisher(PoseWithCovarianceStamped, INITIALPOSE_TOPIC, 10)
        return self._pub

    def _client(self, service: str):
        if service not in self._clients:
            from nav2_msgs.srv import ClearEntireCostmap  # noqa: PLC0415

            self._clients[service] = self.node.create_client(ClearEntireCostmap, service)
        return self._clients[service]

    def realign(self, pose: dict | None, sim_time_s: float) -> dict:
        from geometry_msgs.msg import PoseWithCovarianceStamped  # noqa: PLC0415
        from nav2_msgs.srv import ClearEntireCostmap  # noqa: PLC0415

        from cv_infra.runner.adapter.ros2 import quat_z_w_from_yaw  # noqa: PLC0415

        step = self.adapter._step_and_spin
        observed: dict = {
            "initialpose_subscribers": None,
            "initialpose_published": 0,
            "costmaps_cleared": [],
            "missing": [],
        }
        if self.node is None:
            observed["missing"].append("rclpy node (adapter not wired)")
            return observed

        if pose is not None:
            pub = self._publisher()
            deadline = time.monotonic() + 10.0
            while pub.get_subscription_count() == 0 and time.monotonic() < deadline:
                step()
            observed["initialpose_subscribers"] = pub.get_subscription_count()
            msg = PoseWithCovarianceStamped()
            msg.header.frame_id = "map"
            msg.header.stamp.sec = int(sim_time_s)
            msg.header.stamp.nanosec = int((sim_time_s - int(sim_time_s)) * 1e9)
            msg.pose.pose.position.x = float(pose["x"])
            msg.pose.pose.position.y = float(pose["y"])
            qz, qw = quat_z_w_from_yaw(float(pose["yaw"]))
            msg.pose.pose.orientation.z = qz
            msg.pose.pose.orientation.w = qw
            # AMCL's default initial-pose covariance (x/y 0.25, yaw 0.0685) — the values
            # nav2 itself ships as ``initial_pose`` defaults.
            msg.pose.covariance[0] = 0.25
            msg.pose.covariance[7] = 0.25
            msg.pose.covariance[35] = 0.06853891945200942
            for _ in range(30):  # ~0.5 sim-seconds of repeats (volatile topic)
                pub.publish(msg)
                step()
            observed["initialpose_published"] = 30

        for service in COSTMAP_CLEAR_SERVICES:
            client = self._client(service)
            if not client.service_is_ready():
                ready_deadline = time.monotonic() + 5.0
                while not client.service_is_ready() and time.monotonic() < ready_deadline:
                    step()
            if not client.service_is_ready():
                observed["missing"].append(service)
                continue
            future = client.call_async(ClearEntireCostmap.Request())
            deadline = time.monotonic() + 10.0
            while not future.done() and time.monotonic() < deadline:
                step()
            observed["costmaps_cleared" if future.done() else "missing"].append(service)
        return observed


# --------------------------------------------------------------------------- #
# Per-iteration world staging.
# --------------------------------------------------------------------------- #
def _obstacle_world_xy(spec: dict) -> tuple[float, float, float]:
    """The world position ``SimRuntime.spawn_debug_obstacle`` would place — reused.

    The height default (0.15 m) and the ``z = height/2`` centring are the PRODUCTION
    module's, read from it rather than restated, so a "move" and a "respawn" put the
    cuboid in the same place by construction (the whole point of the arm).
    """
    height = float(spec.get("height", 0.15))
    return float(spec["x"]), float(spec["y"]), height / 2.0


def move_debug_obstacle(spec: dict) -> bool:  # pragma: no cover - GPU path
    """p6c2 A1: relocate the EXISTING cuboid prim; True if it was there to move.

    MEASURED in the 5.1.0 source (isaacsim/core/api/objects/cuboid.py): every
    ``FixedCuboid()`` on a fresh prim path also defines a NEW
    ``/World/Looks/visual_material_NN`` (VisualCuboid) and a NEW
    ``/World/Physics_Materials/physics_material_NN`` (FixedCuboid), via
    ``find_unique_string_name``. The p6c1 loop removes only ``/World/cv_debug_obstacle``,
    so a delete+respawn cycle leaves TWO orphan material prims behind per iteration.
    Moving the prim performs zero prim churn — no orphans, no re-cook, no new material.

    do-not-reinvent: the move goes through ``SingleXFormPrim``, the same Isaac
    world-pose wrapper production uses for the robot's declared initial pose.
    """
    import omni.usd  # noqa: PLC0415
    from isaacsim.core.prims import SingleXFormPrim  # noqa: PLC0415

    stage = omni.usd.get_context().get_stage()
    if not stage.GetPrimAtPath(DEBUG_OBSTACLE_PRIM).IsValid():
        return False
    SingleXFormPrim(DEBUG_OBSTACLE_PRIM).set_world_pose(position=_obstacle_world_xy(spec))
    return True


def soft_restage(sim, request) -> None:  # pragma: no cover - GPU path
    """p6c2 A5: bring the world to the next start state WITHOUT stopping the timeline.

    MEASURED (p6c2 Phase 1): the whole per-iteration VRAM growth lives in the
    ``world.stop() ... world.reset()`` cycle — an arm that skips it (``no_restage``)
    is FLAT while every arm that keeps it leaks ~5 MiB/iteration regardless of what
    is authored in between (obstacle on/off, sensors on/off) or how much is driven.
    ``World.reset(soft=True)`` is the SDK's own path that skips ``stop()``/``play()``
    (isaacsim/core/api/world/world.py: ``if not soft: self.stop()``), so the physics
    simulation views ``SimulationManager._on_stop`` destroys and
    ``_create_simulation_view`` re-creates on every play are never cycled.

    DIAGNOSTIC SCOPE, stated: this arm answers "does avoiding stop/play remove the
    growth". It does NOT restore the robot to a declared spawn pose — the stage-level
    xform write production uses (``SimRuntime.apply_initial_pose``) only sticks
    pre-play. Repositioning a PLAYING articulation is a separate piece of work and is
    named as such in the report, not faked here.
    """
    obstacle = (
        None
        if request.scenario.debug_obstacle is None
        else request.scenario.debug_obstacle.model_dump(exclude_none=True)
    )
    if obstacle is not None:
        move_debug_obstacle(obstacle)
    sim.world.reset(soft=True)


def stage_world(  # noqa: PLR0913 - spike: one staging routine, explicitly parameterised
    sim,
    request,
    sensor_topics,
    chassis_path,
    excluded_paths,
    first: bool,
    ablate: frozenset = frozenset(),
):
    """Bring the world to iteration i's start state; return the fresh telemetry sampler.

    ``first`` reuses ``SimRuntime.load_scene``'s own pre-reset hook sequence (identical
    to production). Subsequent iterations reproduce that sequence by hand around an
    explicit ``stop() -> author -> reset()`` cycle, because ``load_scene`` would
    re-open the stage (the very cost the arm exists to avoid).
    """
    from cv_infra.runner.telemetry import PhysicsTelemetrySampler  # noqa: PLC0415

    pose = (
        None
        if request.scenario.initial_pose is None
        else request.scenario.initial_pose.model_dump()
    )
    sim.config.initial_pose = pose
    sim.config.seed = request.scenario.seed
    sampler = PhysicsTelemetrySampler(chassis_path, excluded_paths)

    obstacle = (
        None
        if request.scenario.debug_obstacle is None
        else request.scenario.debug_obstacle.model_dump(exclude_none=True)
    )
    if "no_obstacle" in ablate:
        obstacle = None

    if first:
        sim.pre_reset = [sampler.bind]
        if obstacle is not None:
            sim.pre_reset.append(lambda _w: sim.spawn_debug_obstacle(obstacle))
        if sensor_topics:
            sim.pre_reset.append(lambda _w: sim.enable_declared_sensors(sensor_topics))
        sim.load_scene(None)
        return sampler

    import random  # noqa: PLC0415

    import numpy as np  # noqa: PLC0415 (legal post-SimulationApp, D-C)
    import omni.usd  # noqa: PLC0415 (legal post-SimulationApp)

    # Same determinism pins load_scene applies, at the same point relative to the
    # World/physics re-init (REQ-EXEC-003) — iteration 2+ would otherwise inherit
    # iteration 1's PRNG stream, which is the opposite of an independent run.
    random.seed(sim.config.seed)
    np.random.seed(sim.config.seed & 0xFFFFFFFF)

    sim.world.stop()  # back to the authored (pre-play) stage state
    stage = omni.usd.get_context().get_stage()
    moved = False
    if obstacle is not None and "obstacle_move" in ablate:
        moved = move_debug_obstacle(obstacle)
    if not moved and stage.GetPrimAtPath(DEBUG_OBSTACLE_PRIM).IsValid():
        stage.RemovePrim(DEBUG_OBSTACLE_PRIM)
    target = resolve_initial_pose_target(pose, sim.robot_prim_path)
    if target is not None:
        sim.apply_initial_pose(target)
    sampler.bind(sim.world)
    if obstacle is not None and not moved:
        sim.spawn_debug_obstacle(obstacle)
    if sensor_topics and "no_sensors" not in ablate:
        sim.enable_declared_sensors(sensor_topics)
    sim.world.reset()
    return sampler


#: Prim roots the SDK's own cuboid constructors auto-populate (measured, see
#: ``move_debug_obstacle``). Deleting an ORPHAN under these is the p6c2 targeted
#: cleanup; the prims the SCENE ASSET ships under the same roots are protected by the
#: baseline snapshot taken right after boot (only paths that appear LATER are ours).
MATERIAL_ROOTS = ("/World/Looks", "/World/Physics_Materials")


def material_prim_paths() -> set:  # pragma: no cover - GPU path
    """Current material prim paths under the two SDK-owned roots."""
    import omni.usd  # noqa: PLC0415

    stage = omni.usd.get_context().get_stage()
    found = set()
    for root in MATERIAL_ROOTS:
        prim = stage.GetPrimAtPath(root)
        if not prim.IsValid():
            continue
        found.update(str(child.GetPath()) for child in prim.GetChildren())
    return found


def run_cleanups(cleanup: frozenset, baseline: set) -> dict:  # pragma: no cover - GPU
    """Iteration-boundary cleanup: EXISTING SDK calls only, a few lines each."""
    done: dict = {}
    if "orphan_materials" in cleanup:
        from isaacsim.core.utils.prims import delete_prim  # noqa: PLC0415

        orphans = sorted(material_prim_paths() - baseline)
        for path in orphans:
            delete_prim(path)  # = omni.usd.commands.DeletePrimsCommand([path]).do()
        done["orphan_materials_deleted"] = len(orphans)
    if "mesh_cache" in cleanup:
        from omni.physx import get_physx_cooking_interface  # noqa: PLC0415

        get_physx_cooking_interface().release_local_mesh_cache()
        done["mesh_cache_released"] = True
    if "gc" in cleanup:
        import gc  # noqa: PLC0415

        done["gc_collected"] = gc.collect()
    return done


def run(env: dict | None = None) -> int:  # noqa: PLR0915 - spike: one linear sequence
    """Arm B: boot once, run every spec in series, write one result per iteration."""
    from cv_infra.contract.schema import Artifacts  # noqa: PLC0415
    from cv_infra.oracles.reached_goal import resolve_position_tolerance  # noqa: PLC0415
    from cv_infra.runner.adapter.ros2 import Ros2Adapter  # noqa: PLC0415
    from cv_infra.runner.boot_trace import BootTrace, emit_cache_probe, observe  # noqa: PLC0415
    from cv_infra.runner.recording import RosbagRecorder, plan_artifacts  # noqa: PLC0415
    from cv_infra.runner.ros_bridge import (  # noqa: PLC0415
        bootstrap_bridge_env,
        enable_bridge,
        honored_env,
        reexec_for_bridge_lib,
    )
    from cv_infra.runner.sim_runtime import SimRuntime  # noqa: PLC0415
    from cv_infra.runner.telemetry import (  # noqa: PLC0415
        contact_partners,
        count_real_collisions,
        min_clearance_m,
        path_length_m,
        time_to_goal_s,
    )

    environ = os.environ if env is None else env
    out_root = resolve_out_root(env)
    specs = load_specs(env)
    settle_s = float(environ.get(SETTLE_ENV) or DEFAULT_REALIGN_SETTLE_S)
    ablate = _flag_set(environ, ABLATE_ENV, ABLATIONS)
    cleanup = _flag_set(environ, CLEANUP_ENV, CLEANUPS)
    if ablate or cleanup:
        _log(f"p6c2 arms: ablate={sorted(ablate) or '-'} cleanup={sorted(cleanup) or '-'}")

    # Pre-boot validation of EVERY spec (main.run does this for its one job): a bad
    # spec must cost 0 GPU seconds, and in this arm a spec 8 failing after 7 missions
    # would be the worst possible time to find out.
    parsed = []
    for index, spec in enumerate(specs, start=1):
        job_id = require_job_id(spec)
        request, adapter_config = parse_request(spec)
        criteria = criteria_view(request)
        oracles = build_oracles(request)
        validate_oracle_params(oracles, criteria)
        parsed.append((index, job_id, request, adapter_config, criteria, oracles))
    _log(f"{len(parsed)} spec(s) admitted pre-boot; out_root={out_root}")

    # The adapter wiring is boot-once, so every spec must agree on it (the spike's
    # samples differ only in scenario values — assert rather than assume).
    base_config = parsed[0][3]
    for index, _jid, _req, cfg, _crit, _orc in parsed[1:]:
        if cfg != base_config:
            raise BadJobSpec(
                f"spec {index} carries a different interface.adapter_config than spec 1 — "
                "this arm wires the ROS side exactly once (that is the experiment)"
            )

    timings: dict = {
        "arm": "B",
        "started_at": time.time(),
        "n": len(parsed),
        "ablate": sorted(ablate),
        "cleanup": sorted(cleanup),
        "boot": {},
        "iterations": [],
    }
    timings_path = out_root / "timings.json"

    def flush_timings() -> None:
        out_root.mkdir(parents=True, exist_ok=True)
        tmp = timings_path.with_name(timings_path.name + ".tmp")
        tmp.write_text(json.dumps(timings, indent=2, sort_keys=True) + "\n", encoding="utf-8")
        os.replace(tmp, timings_path)

    watch = _Stopwatch()
    trace = BootTrace()
    sim = SimRuntime(sim_config_for(parsed[0][2]), trace=trace)
    adapter = Ros2Adapter(base_config, stepper=sim.step)
    exit_codes: list[int] = []
    sampler = None
    try:
        watch.begin("bootstrap")
        bootstrap = bootstrap_bridge_env(base_config.ros_distro, base_config.rmw)
        _log(f"bridge bootstrap: {bootstrap}")
        # MEASURED HERE (p6c1, first Arm B attempt — runner exited 2 in 2.4 s): the
        # production boot glue re-execs the interpreter with a HARDCODED entrypoint,
        # ``[sys.executable, "-m", "cv_infra.runner.main"]``. A second entrypoint —
        # which is exactly what "1 job, n repeats" needs — is therefore replaced by
        # main.py mid-boot and dies on the JOB_SPEC main.py expects. The parameter
        # exists (``argv``), so the fix is to pass our own; the finding is that the
        # runner image today assumes ONE process = ONE entrypoint = ONE job.
        reexec_for_bridge_lib(bootstrap, argv=[sys.executable, "-m", "p6spike.loop_runner"])
        observe("cache probe", emit_cache_probe)
        watch.end("bootstrap")

        watch.begin("simulation_app_init")
        sim.boot()
        timings["boot"]["simulation_app_init_s"] = round(watch.end("simulation_app_init"), 4)

        _ = honored_env()
        watch.begin("ros_bridge_ready")
        enable_bridge(sim.simulation_app)
        timings["boot"]["ros_bridge_ready_s"] = round(watch.end("ros_bridge_ready"), 4)

        first_criteria = parsed[0][4]
        chassis_path = read_field(first_criteria, "chassis_path", "")
        excluded_paths = read_field(first_criteria, "collision_excluded_paths", []) or []
        sensor_topics = [s.topic for s in base_config.sensors]

        watch.begin("scene_load")
        sampler = stage_world(
            sim,
            parsed[0][2],
            sensor_topics,
            chassis_path,
            excluded_paths,
            first=True,
            ablate=ablate,
        )
        timings["boot"]["scene_load_and_spawn_s"] = round(watch.end("scene_load"), 4)

        watch.begin("adapter_wire")
        adapter.wire(sim.simulation_app, base_config)
        timings["boot"]["adapter_wire_s"] = round(watch.end("adapter_wire"), 4)
        timings["boot"]["total_s"] = round(
            sum(v for k, v in timings["boot"].items() if k.endswith("_s")), 4
        )
        flush_timings()
        _log(f"boot done: {timings['boot']}")

        # ONE render product for the whole process (creating a new one per iteration
        # would be a VRAM growth term the spike would then have to explain away).
        video = _LoopVideoRecorder()
        if "no_video" not in ablate:
            video.open_render_product()
            sim.on_step.append(video.capture_frame)
        # ONE realigner for the whole process (its publisher/clients must OUTLIVE an
        # iteration to be discovered — see SutRealigner).
        realigner = SutRealigner(adapter)
        # p6c2: the material prims the SCENE ASSET ships, snapshotted once. Anything
        # that appears under those roots LATER was created by this loop (measured:
        # every FixedCuboid() defines one visual + one physics material), so the
        # cleanup arm can delete only its own orphans and never the asset's.
        material_baseline = material_prim_paths()
        _log(f"material baseline: {len(material_baseline)} prim(s) under {MATERIAL_ROOTS}")

        for position, (index, job_id, request, _cfg, criteria, oracles) in enumerate(parsed):
            first = position == 0
            iter_dir = out_root / "results" / str(index)
            iter_dir.mkdir(parents=True, exist_ok=True)
            iter_watch = _Stopwatch()
            iter_watch.begin("iteration")
            record: dict = {"index": index, "job_id": job_id, "first": first}
            # Wall-clock marks: the 0.5 s per-PID VRAM sampler runs OUTSIDE the
            # container, so attributing a MiB step to a phase needs the phase's own
            # epoch boundaries in this file (p6c2 — otherwise "which phase grew" is
            # unanswerable without a second instrument).
            record["epoch"] = {"iteration_begin": time.time()}
            _log(f"=== iteration {index}/{len(parsed)} job_id={job_id} ===")

            iter_watch.begin("restage")
            keeps_binding = bool(ablate & {"no_restage", "soft_reset"})
            if not first and not keeps_binding:
                sampler.detach()
                sampler = stage_world(
                    sim,
                    request,
                    sensor_topics,
                    chassis_path,
                    excluded_paths,
                    first=False,
                    ablate=ablate,
                )
                sim.emit_sim_config(None)  # DoD-P2-06 ① line, per iteration
                sampler.attach(sim.world)
            elif first:
                sampler.attach(sim.world)
            else:
                # no_restage / soft_reset: the timeline keeps running and the
                # telemetry binding survives, so only the ACCUMULATOR is cycled —
                # replacing ``record`` is enough because the physics callback reads
                # the attribute on every step (it does not close over the list).
                from cv_infra.runner.telemetry import TelemetryRecord  # noqa: PLC0415

                if "soft_reset" in ablate:
                    soft_restage(sim, request)
                sampler.record = TelemetryRecord()
            record["epoch"]["restage_end"] = time.time()
            iter_watch.end("restage")

            iter_watch.begin("sut_realign")
            pose = (
                None
                if request.scenario.initial_pose is None
                else request.scenario.initial_pose.model_dump()
            )
            # Pump first: ``world.reset()`` restarts the timeline, so the adapter's
            # cached ``sim_time_s`` still holds the PREVIOUS iteration's (larger) value
            # until the first post-reset /clock arrives. Realigning or arming the
            # mission budget on that stale value would stamp AMCL in the future and
            # give drive_mission a deadline the restarted clock can never reach.
            sim_before = adapter.sim_time_s
            if not first and not keeps_binding:
                pump_deadline = time.monotonic() + 60.0
                for _ in range(600):
                    adapter._step_and_spin()
                    if adapter.sim_time_s < sim_before or time.monotonic() > pump_deadline:
                        break
            record["sim_time_before_reset_s"] = sim_before
            record["sim_time_after_reset_s"] = adapter.sim_time_s
            if "no_realign" in ablate:
                realign = {"skipped": "no_realign ablation"}
            else:
                realign = realigner.realign(pose, adapter.sim_time_s)
            settle_until = adapter.sim_time_s + settle_s
            settle_deadline = time.monotonic() + 60.0
            while adapter.sim_time_s < settle_until and time.monotonic() < settle_deadline:
                adapter._step_and_spin()
            record["epoch"]["realign_end"] = time.time()
            iter_watch.end("sut_realign")
            record["sut_realign"] = realign
            _log(f"sut realign: {realign}")

            iter_watch.begin("readiness")
            ready = adapter.await_ready(timeout_s=READINESS_TIMEOUT_S)
            iter_watch.end("readiness")
            record["readiness_ok"] = ready
            record["readiness_phase"] = adapter.readiness_phase
            record["clock_count_at_readiness"] = adapter.clock_count

            plan = plan_artifacts(iter_dir)
            rosbag = None
            iter_watch.begin("record_start")
            if ready and "no_bag" not in ablate:
                rosbag = RosbagRecorder(plan, base_config)
                try:
                    rosbag.start()
                except Exception as exc:  # loud, non-fatal (P2-02 stance)
                    print(f"[cv-spike] recorder unavailable: {exc}", file=sys.stderr, flush=True)
                    rosbag = None
            if ready and "no_video" not in ablate:
                video.begin_iteration(plan.video_mp4)
            iter_watch.end("record_start")

            iter_watch.begin("mission")
            if ready and "no_mission" not in ablate:
                outcome = adapter.drive_mission(
                    request.scenario.goal, timeout_s=request.scenario.timeout_s
                )
            else:
                outcome = None
            iter_watch.end("mission")
            record["epoch"]["mission_end"] = time.time()
            record["mission_status"] = None if outcome is None else outcome.status
            record["mission_sim_time_s"] = None if outcome is None else outcome.sim_time_elapsed_s
            _log(f"mission outcome: {outcome}")

            iter_watch.begin("record_stop")
            mcap_path = None
            if rosbag is not None:
                try:
                    mcap_path = rosbag.stop()
                except Exception as exc:
                    print(f"[cv-spike] no mcap: {exc}", file=sys.stderr, flush=True)
            mp4_path = video.end_iteration()
            iter_watch.end("record_stop")

            iter_watch.begin("evaluate")
            if not keeps_binding:
                sampler.detach()
            telemetry = sampler.record
            goal = read_field(criteria, "goal_position")
            tolerance = resolve_position_tolerance(criteria)
            goal_xyz = (float(goal[0]), float(goal[1]), float(goal[2]))
            metrics = {
                "time_to_goal_s": time_to_goal_s(
                    telemetry.gt_pose_samples, goal_xyz, tolerance.value_m
                ),
                "min_clearance_m": min_clearance_m(),
                "collision_count": count_real_collisions(
                    telemetry.contact_events, chassis_path, excluded_paths
                ),
                "path_len_m": path_length_m(telemetry.gt_pose_samples),
            }
            if ready:
                verdict, outcomes = EvaluationEngine(oracles).evaluate(telemetry, criteria)
            else:
                verdict, outcomes = VERDICT_ERROR, []
            result = build_result_dict(
                job_id,
                verdict,
                outcomes,
                metrics,
                artifacts=Artifacts(
                    mcap=str(mcap_path) if mcap_path is not None else None,
                    mp4=str(mp4_path) if mp4_path is not None else None,
                ),
                request_identity_key=None,
            )
            write_result(result, iter_dir / "result.json")
            iter_watch.end("evaluate")

            record["verdict"] = verdict
            record["metrics"] = metrics
            record["gt_pose_samples"] = len(telemetry.gt_pose_samples)
            record["contact_events"] = len(telemetry.contact_events)
            if telemetry.contact_events:
                record["contact_partners"] = contact_partners(
                    telemetry.contact_events, chassis_path
                )[:10]
            if telemetry.gt_pose_samples:
                last = telemetry.gt_pose_samples[-1]
                record["final_position"] = list(last.position)
                record["final_orientation_wxyz"] = list(last.orientation_wxyz)
                record["first_position"] = list(telemetry.gt_pose_samples[0].position)
            record["video_frames"] = video.last_frame_count

            # p6c2 targeted cleanup, at the ITERATION BOUNDARY (after the result is on
            # disk, before the next re-stage) — the only place a per-iteration leak can
            # be returned without touching a live mission.
            iter_watch.begin("cleanup")
            record["cleanup"] = run_cleanups(cleanup, material_baseline) if cleanup else {}
            iter_watch.end("cleanup")
            record["material_prims"] = len(material_prim_paths())

            iter_watch.end("iteration")
            record["epoch"]["iteration_end"] = time.time()
            record["timings_s"] = iter_watch.spans
            record["sim_time_at_end_s"] = adapter.sim_time_s
            record["clock_count_at_end"] = adapter.clock_count
            timings["iterations"].append(record)
            flush_timings()
            exit_codes.append(
                {"pass": EXIT_PASS, "fail": EXIT_FAIL, "timeout": EXIT_FAIL}.get(
                    verdict, EXIT_PLATFORM
                )
            )
            _log(f"iteration {index} verdict={verdict} metrics={metrics}")

        timings["finished_at"] = time.time()
        timings["wall_total_s"] = round(timings["finished_at"] - timings["started_at"], 4)
        timings["verdicts"] = [r.get("verdict") for r in timings["iterations"]]
        flush_timings()
        return _worst(exit_codes)
    except EulaNotAcceptedError:
        raise
    except Exception as exc:
        print(f"[cv-spike] loop runner error: {exc!r}", file=sys.stderr, flush=True)
        import traceback  # noqa: PLC0415

        traceback.print_exc()
        timings["error"] = repr(exc)
        timings["finished_at"] = time.time()
        timings["wall_total_s"] = round(timings["finished_at"] - timings["started_at"], 4)
        try:
            flush_timings()
        except Exception:
            pass
        return EXIT_PLATFORM
    finally:
        observe("boot summary", trace.emit_summary)
        if sampler is not None:
            sampler.detach()
        adapter.teardown()
        # sim.close() is NOT called (G-62) — main.hard_exit delivers the status.


class _LoopVideoRecorder:
    """Per-iteration mp4 over ONE long-lived render product (spike-local).

    ``recording.VideoRecorder`` creates a render product in ``start()``; calling that 8
    times in one process would add a VRAM growth term that Q3 would then have to
    disentangle from a real leak. So the render product/annotator are created once and
    only the cv2 writer is cycled. Frame policy (camera, resolution, fps, stride) is
    IMPORTED from the production module — no second definition.
    """

    def __init__(self) -> None:
        self._annotator = None
        self._writer = None
        self._step_count = 0
        self.last_frame_count = 0
        from cv_infra.runner.recording import DEFAULT_VIDEO_FPS, capture_stride  # noqa: PLC0415

        self._stride = capture_stride(60.0, DEFAULT_VIDEO_FPS)
        self._fps = DEFAULT_VIDEO_FPS

    def open_render_product(self) -> None:
        import omni.replicator.core as rep  # noqa: PLC0415

        from cv_infra.runner.recording import (  # noqa: PLC0415
            DEFAULT_CAMERA_PATH,
            DEFAULT_RESOLUTION,
        )

        self._resolution = DEFAULT_RESOLUTION
        render_product = rep.create.render_product(DEFAULT_CAMERA_PATH, DEFAULT_RESOLUTION)
        self._annotator = rep.AnnotatorRegistry.get_annotator("rgb")
        self._annotator.attach(render_product)

    def begin_iteration(self, path: Path) -> None:
        import cv2  # noqa: PLC0415

        path.parent.mkdir(parents=True, exist_ok=True)
        self._writer = cv2.VideoWriter(
            str(path), cv2.VideoWriter_fourcc(*"mp4v"), self._fps, self._resolution
        )
        self._path = path
        self._step_count = 0
        self.last_frame_count = 0

    def capture_frame(self) -> None:
        if self._writer is None:
            return
        self._step_count += 1
        if self._step_count % self._stride:
            return
        import numpy as np  # noqa: PLC0415

        data = self._annotator.get_data()
        if data is None or getattr(data, "size", 0) == 0:
            return
        frame = np.asarray(data)
        if frame.ndim != 3 or frame.shape[2] < 3:
            return
        self._writer.write(np.ascontiguousarray(frame[:, :, 2::-1]))
        self.last_frame_count += 1

    def end_iteration(self):
        if self._writer is None:
            return None
        self._writer.release()
        self._writer = None
        return self._path if self.last_frame_count else None


def main(env: dict | None = None) -> int:
    try:
        return run(env)
    except BadJobSpec as exc:
        print(f"[cv-spike] bad spec: {exc}", file=sys.stderr, flush=True)
        return EXIT_USAGE
    except EulaNotAcceptedError as exc:
        print(f"[cv-spike] {exc}", file=sys.stderr, flush=True)
        return EXIT_PLATFORM


if __name__ == "__main__":
    hard_exit(main())
