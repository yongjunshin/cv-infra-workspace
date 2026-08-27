"""Headless SimulationApp lifecycle (M2, REQ-EXEC-001/002/003/015, NFR-EXEC-001).

Boots ``SimulationApp({"headless": True})`` FIRST (before any ``omni.*`` / ``isaacsim.*``
import — LOCKED §7.7), opens the scene, moves the robot the scene asset already
placed to the DECLARED ``scenario.initial_pose`` when the scenario carries one
(REQ-EXEC-002 — nothing is ever *spawned*: the sample scene ships the robot, and
an undeclared pose leaves the asset's own placement alone), pins ``physics_dt`` /
``rendering_dt`` / seed for determinism AND reports what it applied
(``emit_sim_config`` — DoD-P2-06 ①), runs the step loop, and
closes cleanly to return VRAM/slots. Boot also pins the R4 texture-streaming
budget cap (see ``simulation_app_launch_config`` — the k>=2 OOM-trap guard).
All Isaac imports are deferred into ``boot()`` so this module imports on a CPU
host with no Isaac present.

Reuses (does NOT re-invent) the P1 ``scripts/isaac_smoke/headless_smoke.py`` pattern:
SimulationApp-first ordering + the EULA boot guard. That P1 file is a read-only
artifact and not an importable package, so the ~5-line guard is mirrored here (M5
§3.1 / decision 2026-07-03-p1-eula-runtime-consent). The GPU bring-up bodies below
are the seams filled in cycles 2-4.
"""

from __future__ import annotations

import math
import os
import random
import time
from dataclasses import dataclass

from cv_infra.runner.boot_trace import (
    PHASE_FIRST_RENDER_FRAME,
    PHASE_ROBOT_SPAWN,
    PHASE_SCENE_LOAD,
)


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
    """Deterministic sim settings (REQ-EXEC-002/003), built by ``main.sim_config_for``.

    ``initial_pose`` is the M1 ``Scenario.initial_pose`` block as a plain dict
    (``{"x", "y", "yaw"}`` — planar 3-DoF, metres / radians, scene world frame;
    the same hand-off shape ``debug_obstacle`` uses, so the sim layer stays free
    of contract models). **None means the runner applies NO pose at all** and the
    scene asset's own robot placement stands — that is what every pre-p5c11
    scenario gets, and it is why there is no ``(0, 0, 0)`` default here: a
    default would silently teleport those robots to the world origin. (This
    replaces the never-consumed ``initial_pose_xyz`` placeholder; the contract
    carries no ``z`` on purpose — floor contact owns it, see ``InitialPose``.)

    ``physics_dt`` / ``rendering_dt`` default to 1/60 and are overridden only by
    a DECLARED ``execution_settings.fixed_dt``.
    """

    scene_ref: str
    robot_usd_ref: str
    initial_pose: dict | None = None
    physics_dt: float = 1.0 / 60.0
    rendering_dt: float = 1.0 / 60.0
    seed: int = 0


# --------------------------------------------------------------------------- #
# R4: texture streaming budget cap (k>=2 prerequisite) — CPU-testable assembly.
# --------------------------------------------------------------------------- #
# By default every Isaac instance reserves 60% of TOTAL GPU memory for texture
# streaming — the #1 multi-instance OOM trap (implementation-plan/
# 06-risks-and-assumptions.md R4, "[OFFICIAL] Q5 gotchas"). The cap pins that
# budget EXPLICITLY at boot so per-instance VRAM stays deterministic under
# k-parallel scheduling instead of floating on an image default.
#
# Settings key [CANDIDATE — GPU-confirm at Wave 2 T4]: the Omniverse Kit RTX
# resource-manager settings tree names the texture-streaming budget
# ``/rtx-transient/resourcemanager/texturestreaming/memoryBudget`` (fraction of
# total GPU memory, documented default 0.6 — matching R4's "60% of total"
# description). No in-repo probe has dumped this carb subtree yet (no measured
# anchor to cite — G-28), so ``boot()`` logs a pre-set read (``at_boot=``) AND
# a post-set read-back (``readback=``): ``at_boot=none`` on the workstation
# means this candidate key does not exist in the 5.1.0 build and must be
# re-probed (surfaced assumption, task p4c4-T2 req 1 — non-blocking).
TEXTURE_BUDGET_SETTING = "/rtx-transient/resourcemanager/texturestreaming/memoryBudget"
# 0.6 is R4's PLAN POLICY value (60% cap), not a measured NFR (§2-4 untouched):
# pinning the suspected image default makes the budget observable and tunable at
# ONE constant when the P4-10 k-parallel VRAM measurement lands.
TEXTURE_BUDGET_FRACTION = 0.6
# Verbatim grep marker for T4/QA (G-26 prove-it-ran gate; pinned by CPU test).
TEXTURE_BUDGET_LOG_MARKER = "texture_budget_applied="


def simulation_app_launch_config() -> dict:
    """SimulationApp launch config — headless + R4 texture budget cap (CPU-testable).

    The cap rides the canonical kit CLI settings-override form (``--/path=value``
    — the exact form R4's verification column names) via the launcher's
    ``extra_args`` so it is present from renderer init, not patched in after.
    """
    return {
        "headless": True,
        "extra_args": [f"--{TEXTURE_BUDGET_SETTING}={TEXTURE_BUDGET_FRACTION}"],
    }


def texture_budget_log_line(at_boot: object, readback: object) -> str:
    """One structured boot line proving the R4 cap — Wave 2 T4/QA grep gate.

    ``at_boot`` = carb value right after SimulationApp boot (did the
    ``extra_args`` override land / does the key exist at all?); ``readback`` =
    value after the explicit belt-and-suspenders ``settings.set``. ``none``
    (either field) is the loud signal that the candidate key is wrong for this
    build — an observation, never an echo of intent (G-26).
    """

    def fmt(value: object) -> object:
        return "none" if value is None else value

    return (
        f"[cv-runner] {TEXTURE_BUDGET_LOG_MARKER}{TEXTURE_BUDGET_FRACTION} "
        f"at_boot={fmt(at_boot)} readback={fmt(readback)} key={TEXTURE_BUDGET_SETTING}"
    )


# --------------------------------------------------------------------------- #
# DoD-P2-06 ①: the settings this run ACTUALLY applied, emitted once — CPU-testable.
# --------------------------------------------------------------------------- #
# The gate was re-aimed on 2026-08-14 (CEO D-5) from "the results repeat" to "the
# PROCESSING repeats + the spread is not hidden". Its ① clause needs the applied
# execution settings to be OBSERVABLE per run; before this line they were applied
# and never reported, so N runs could not be compared at all.
#
# CROSS-TEAM WIRE (PM verbatim pin, cycle p5c15 — QA greps it): the prefix and the
# four field names are the contract; every VALUE is measured at run time (G-64 —
# a pinned shape never pins a number).
SIM_CONFIG_LOG_PREFIX = "[cv-runner] sim_config"


def sim_config_log_line(physics_dt, rendering_dt, seed, identity_key) -> str:
    """Format the one applied-settings line (see ``SimRuntime.emit_sim_config``).

    Floats are rendered at FULL round-trip precision (``repr``): a dt that differs
    in the 15th digit between two runs is exactly the kind of divergence this line
    exists to expose, and a ``%.6f`` would round it away. ``seed``/``identity_key``
    render as the literal string ``none`` when absent — an observation ("this run
    had no key"), never a fabricated value.
    """

    def opt(value: object) -> object:
        return "none" if value is None else value

    return (
        f"{SIM_CONFIG_LOG_PREFIX} physics_dt={float(physics_dt)!r} "
        f"rendering_dt={float(rendering_dt)!r} seed={opt(seed)} "
        f"identity_key={opt(identity_key)}"
    )


# --------------------------------------------------------------------------- #
# Scene mapping (scenario.scene name -> Isaac sample asset ref) — CPU-testable.
# --------------------------------------------------------------------------- #
@dataclass(frozen=True)
class SceneAsset:
    """One resolvable scene: sample USD (relative to the Isaac assets root) plus
    where the pre-wired robot lives in it.

    do-not-reinvent: Phase-2 scenes REUSE the official ``carter_warehouse_navigation``
    sample (warehouse + Nova Carter + ROS2 OmniGraph pre-wired — clock/TF/odom/
    sensors/cmd_vel action graphs come WITH the asset; we author no scene). The
    robot prim path inside the sample is NVIDIA's naming, so it is a candidate
    list resolved against the live stage (first existing wins) — a rename in a
    future asset rev degrades to a loud, listing error instead of a wrong pin.
    """

    scene_usd: str
    robot_prim_candidates: tuple[str, ...] = ()


SCENE_ASSETS: dict[str, SceneAsset] = {
    # cv-infra-user/scenarios/nova_carter_warehouse_goal.yaml: scene name (M1 Scenario).
    "nova_carter_warehouse": SceneAsset(
        scene_usd="/Isaac/Samples/ROS2/Scenario/carter_warehouse_navigation.usd",
        robot_prim_candidates=("/World/Nova_Carter_ROS", "/World/Carter_ROS"),
    ),
}


def resolve_scene(scene_ref: str) -> SceneAsset:
    """Map a scenario scene name to its asset ref; direct USD refs pass through.

    A ``.usd``-suffixed ref (omniverse://, http(s)://, or a mounted path) is used
    as-is with no robot-prim knowledge (P3 direction: consumer-supplied scenes).
    An unknown scene NAME is bad input -> loud ValueError listing known scenes
    (REQ-INTAKE-005 friendly-error direction).
    """
    if scene_ref in SCENE_ASSETS:
        return SCENE_ASSETS[scene_ref]
    if scene_ref.endswith((".usd", ".usda", ".usdz")):
        return SceneAsset(scene_usd=scene_ref)
    raise ValueError(
        f"unknown scenario.scene {scene_ref!r} — known scenes: {sorted(SCENE_ASSETS)} "
        "(or pass a direct .usd/.usda/.usdz reference)"
    )


def is_direct_usd_ref(scene_usd: str) -> bool:
    """True when the ref needs NO assets-root prefix (absolute URL or host path)."""
    return scene_usd.startswith(("omniverse://", "http://", "https://", "file://")) or (
        not scene_usd.startswith("/Isaac/")
    )


# --------------------------------------------------------------------------- #
# FU-17: declared-sensor render-product activation (pre-play) — CPU-testable.
# --------------------------------------------------------------------------- #
# The blocking node TYPE measured in the carter sample (p3c1 probe): the 2D lidar
# publish graph is complete but gated by ``IsaacCreateRenderProduct`` nodes with
# ``inputs:enabled=False``. The node NAMES (front/back_2d_lidar_render_product)
# are asset naming and deliberately NOT depended on — matching goes declared
# ``sensors[].topic`` -> publish node (``inputs:topicName``) -> upstream walk.
RENDER_PRODUCT_NODE_TYPE = "IsaacCreateRenderProduct"


def _node_type(prim) -> str:
    """OmniGraph node type id (``node:type`` attr) or "" — USD-generic access."""
    attr = prim.GetAttribute("node:type")
    value = attr.Get() if attr else None
    return str(value) if value else ""


def _upstream_prims(stage, start):
    """BFS the OmniGraph upstream of ``start`` via ``inputs:*`` connections.

    Pure USD traversal (attribute connections), no omni.graph runtime API — the
    same walk works on a fake stage in CPU tests and on the live stage on GPU.
    """
    seen = {str(start.GetPath())}
    queue = [start]
    while queue:
        prim = queue.pop()
        for attr in prim.GetAttributes():
            if not attr.GetName().startswith("inputs:"):
                continue
            for source in attr.GetConnections():
                src = stage.GetPrimAtPath(source.GetPrimPath())
                if not src or str(src.GetPath()) in seen:
                    continue
                seen.add(str(src.GetPath()))
                queue.append(src)
                yield src


def _normalized_topic(name) -> str:
    """Topic name in slashless-canonical form for MATCHING only.

    Measured (T4 p3c2 L1 + p3c1 probe runA/inventory-og-hits.txt): the scenario
    declares ROS-absolute names (``/front_2d_lidar/scan``) while the carter
    asset's OmniGraph ``inputs:topicName`` values carry NO leading slash
    (``front_2d_lidar/scan``) — a literal comparison 0-matched every declared
    topic. Both sides strip the leading ``/`` before comparing; reporting/log
    strings keep the DECLARED spelling untouched.
    """
    return str(name).lstrip("/")


def enable_sensor_render_products(stage, topics) -> tuple[list[str], list[str]]:
    """FU-17: enable the render products feeding the DECLARED sensor topics.

    For each publish node whose ``inputs:topicName`` names a declared topic
    (slash-normalized comparison — see ``_normalized_topic``), walk its
    upstream graph and set ``inputs:enabled=true`` on every
    ``IsaacCreateRenderProduct`` node still False. In-memory attribute set only
    (never a stage save — the asset stays unmodified); idempotent (already-enabled
    nodes are left untouched, so a second call is a no-op). Returns
    ``(newly_enabled_node_paths, declared_topics_with_no_publish_node)`` — the
    second list (declared spelling, as-written) is the original FU-17 bug class
    (declared but publisher-less) and is surfaced loudly by the caller.
    """
    # normalized form -> declared as-written (reporting stays in declared form)
    wanted = {_normalized_topic(t): t for t in topics if t}
    if not wanted:
        return [], []
    enabled: list[str] = []
    matched: set[str] = set()
    for prim in stage.Traverse():
        topic_attr = prim.GetAttribute("inputs:topicName")
        topic = topic_attr.Get() if topic_attr else None
        if topic is None or _normalized_topic(topic) not in wanted:
            continue
        matched.add(_normalized_topic(topic))
        for node in _upstream_prims(stage, prim):
            if not _node_type(node).endswith(RENDER_PRODUCT_NODE_TYPE):
                continue
            enabled_attr = node.GetAttribute("inputs:enabled")
            if enabled_attr and not enabled_attr.Get():
                enabled_attr.Set(True)
                enabled.append(str(node.GetPath()))
    return sorted(set(enabled)), sorted(wanted[k] for k in set(wanted) - matched)


# --------------------------------------------------------------------------- #
# P2-04 debug obstacle placement — ONE home for the prim path + defaults.
# --------------------------------------------------------------------------- #
#: Stage path of the runner-authored debug obstacle. It is a CONSTANT because two
#: call sites must name the same prim: ``spawn_debug_obstacle`` (once, pre-reset)
#: and ``move_debug_obstacle`` (every batch iteration). A drift between them would
#: not fail — it would silently author a SECOND box, and every ``FixedCuboid()``
#: on a fresh path also defines two material prims nothing removes (measured
#: p6c2 §2.1: 2 -> 48 material prims over 24 delete+respawn iterations).
DEBUG_OBSTACLE_PRIM = "/World/cv_debug_obstacle"

#: Runner-owned dimension defaults (M1 schema None = "the runner default
#: applies"). The LOW default height keeps the box below the 2D-lidar scan plane,
#: so it stays invisible to the blackbox nav's costmaps and deterministically
#: meets the chassis.
DEBUG_OBSTACLE_DEFAULT_HEIGHT = 0.15
DEBUG_OBSTACLE_DEFAULT_WIDTH = 1.2
DEBUG_OBSTACLE_DEFAULT_DEPTH = 0.4


def debug_obstacle_position(spec: dict) -> tuple[float, float, float]:
    """Declared ``{x, y[, height]}`` -> the world position that CENTRES the box.

    Spawn and move share this one function on purpose: the ``z = height/2``
    centring and the height default are what put the cuboid on the floor, so a
    moved box and a respawned box must land in the same place by construction
    (G-25 — the spike carried a second copy of this arithmetic). Pure, CPU-tested.
    """
    height = float(spec.get("height", DEBUG_OBSTACLE_DEFAULT_HEIGHT))
    return float(spec["x"]), float(spec["y"]), height / 2.0


# --------------------------------------------------------------------------- #
# REQ-EXEC-002: the DECLARED spawn pose (scenario.initial_pose) — CPU-testable.
# --------------------------------------------------------------------------- #
def resolve_initial_pose_target(pose: dict | None, robot_prim_path: str | None) -> str | None:
    """Which prim the declared spawn pose applies to — or None for "do nothing".

    This is the branch that carries the regression risk, so it lives OFF the GPU
    path and is unit-tested: ``pose is None`` (every pre-p5c11 scenario) MUST be
    a no-op, because the scene asset has already placed the robot and moving it
    would change behaviour nobody asked to change (CEO D-2).

    A pose declared for a scene whose robot prim never resolved (a direct
    ``.usd`` ref carries no ``robot_prim_candidates``) is LOUD, never silently
    dropped — a field the runner accepts and ignores is the ``goal_tolerance_m``
    silent-ignore pattern (G-25).
    """
    if pose is None:
        return None
    if not robot_prim_path:
        raise RuntimeError(
            "scenario.initial_pose was declared but this run has no known robot prim, "
            "so the runner cannot honour it: mapped scenes resolve the robot via "
            "SCENE_ASSETS[...].robot_prim_candidates, a direct .usd/.usda/.usdz ref "
            "carries none. Drop initial_pose or use a mapped scene."
        )
    return robot_prim_path


def initial_pose_world_transform(
    pose: dict, current_position
) -> tuple[tuple[float, float, float], tuple[float, float, float, float]]:
    """``{"x", "y", "yaw"}`` + the asset's own height -> (position, ``(w,x,y,z)``).

    The contract is planar 3-DoF (M1 ``InitialPose``): ``z`` is deliberately NOT
    a consumer input — floor contact owns it — so the robot keeps the height the
    scene asset placed it at, and the orientation is a pure right-handed +Z
    rotation (roll/pitch dropped for the same reason). Stdlib-only (no numpy) so
    the math is unit-tested on CPU; the GPU caller only moves the numbers.
    """
    half_yaw = float(pose["yaw"]) / 2.0
    position = (float(pose["x"]), float(pose["y"]), float(current_position[2]))
    return position, (math.cos(half_yaw), 0.0, 0.0, math.sin(half_yaw))


# --------------------------------------------------------------------------- #
# p6 batch loop: the iteration boundary's own evidence line — CPU-testable.
# --------------------------------------------------------------------------- #
#: Verbatim grep marker (G-26 prove-it-ran gate, pinned by CPU test + NEG-6): a
#: repose that silently did nothing and a repose that never ran read the same in
#: a log, and the batch loop's whole correctness rests on sample i+1 starting
#: where it declared it would.
REPOSE_LOG_MARKER = "repose_applied="


def repose_log_line(prim_path: str, declared: dict, position, orientation_wxyz) -> str:
    """One structured line per repose — what was DECLARED and what was WRITTEN.

    Both sides are printed because they are different objects: the declared block
    is planar (x/y/yaw) and the written position carries the asset's own z. The
    trailing note names what this repose does NOT touch, so a W1 reading of
    "the robot is in the right place but the wheels are spinning" has the code's
    own statement of scope next to it.
    """
    return (
        f"[cv-runner] {REPOSE_LOG_MARKER}{prim_path} declared={declared} "
        f"position={position} orientation_wxyz={orientation_wxyz} "
        "(z from the scene asset; lin/ang + joint velocities zeroed, "
        "joint POSITIONS untouched)"
    )


class SimRuntime:
    """Wraps the SimulationApp / World lifecycle. Isaac bodies are deferred-import."""

    def __init__(self, config: SimConfig, trace: object | None = None) -> None:
        self.config = config
        self.simulation_app = None  # set by boot()
        self.world = None  # set by load_scene()
        self.robot_prim_path: str | None = None  # set by load_scene()
        # Callbacks invoked after every step (e.g. video frame capture) — kept
        # runner-side so the adapter's step loop stays recorder-agnostic.
        self.on_step: list = []
        # Hooks called with the World JUST BEFORE world.reset() (measured p2c5
        # probe-03: tensor-view wrappers — telemetry's SingleRigidPrim — must be
        # created pre-reset or the cached simulation view is already invalid).
        self.pre_reset: list = []
        # p4c5 T1: optional boot_trace.BootTrace — the scene-load / robot-spawn /
        # first-render-frame phase boundaries only this class can see. None = no
        # instrumentation (behavior identical; the runner works untraced).
        self.trace = trace
        self._first_step_traced = False
        # p6 batch loop: the SingleArticulation wrapper ``repose_robot`` writes
        # through, created once and kept — building it per iteration would re-run
        # the physics-view handshake n times for no gain (and the wrapper is only
        # valid while the timeline plays, which soft restaging never interrupts).
        self._articulation = None

    def _phase_begin(self, phase: str, **fields) -> None:
        if self.trace is not None:
            self.trace.begin(phase, **fields)

    def _phase_end(self, phase: str, **fields) -> None:
        if self.trace is not None:
            self.trace.end(phase, **fields)

    def boot(self) -> object:
        """Instantiate SimulationApp FIRST, then it is legal to import omni/isaacsim."""
        eula_boot_guard()
        # LOCKED §7.7 — SimulationApp before any omni.*/isaacsim.* import.
        from isaacsim import SimulationApp  # noqa: PLC0415 (deferred by design)

        self.simulation_app = SimulationApp(simulation_app_launch_config())
        self._apply_texture_budget()
        return self.simulation_app

    def _apply_texture_budget(self) -> None:  # pragma: no cover - GPU path (T4 observes)
        """R4 belt-and-suspenders: explicit carb set + read-back, one loud line.

        The launch config already injects the cap as a boot-time ``--/...``
        settings override (canonical form; covers init-time snapshot semantics).
        The explicit set below covers an ``extra_args`` key being ignored by the
        5.1.0 launcher; the pre-set read + post-set read-back make the log line
        an observation (see ``texture_budget_log_line``).
        """
        import carb  # noqa: PLC0415 (legal only after SimulationApp)

        settings = carb.settings.get_settings()
        at_boot = settings.get(TEXTURE_BUDGET_SETTING)
        settings.set(TEXTURE_BUDGET_SETTING, TEXTURE_BUDGET_FRACTION)
        readback = settings.get(TEXTURE_BUDGET_SETTING)
        print(texture_budget_log_line(at_boot, readback), flush=True)

    def _applied_dt(self, getter_name: str, requested: float) -> tuple[float, bool]:
        """Ask the World what dt it is really running with; (value, observed?).

        The read-back accessor is NOT assumed to exist: a vendor API is measured,
        not trusted (G-62 ④). Missing/raising -> the REQUESTED value with
        ``observed=False``, which ``emit_sim_config`` says out loud rather than
        passing an echo of intent off as an observation (G-26).
        """
        getter = getattr(self.world, getter_name, None)
        if getter is None:
            return requested, False
        try:
            return float(getter()), True
        except Exception:  # a diagnostic read-back must never cost the job
            return requested, False

    def emit_sim_config(self, identity_key: str | None = None) -> str:
        """Emit the applied-settings line (DoD-P2-06 ①) + a loud note if it diverges.

        Called by ``load_scene`` the moment the World holds the settings, so a job
        that dies later has ALREADY left this line in its log.

        ``identity_key`` is a parameter, not something this runner derives: the key
        is M4's (``report/regression.identity_key`` over the request wire dump).
        Since p5c18 T4/T5 it IS handed in — the supervisor injects
        ``CV_REQUEST_IDENTITY_KEY``, ``main.run`` reads it once and passes it here
        (measured live: 5/5 jobs carried the report plane's own key). ``None``
        still renders ``none``: that is the honest report for an entry point that
        does not supply one (``cv-infra run`` today), never a value to invent.
        """
        physics_dt, physics_ok = self._applied_dt("get_physics_dt", self.config.physics_dt)
        rendering_dt, rendering_ok = self._applied_dt("get_rendering_dt", self.config.rendering_dt)
        line = sim_config_log_line(physics_dt, rendering_dt, self.config.seed, identity_key)
        print(line, flush=True)
        notes = []
        for field, requested, applied, observed in (
            ("physics_dt", self.config.physics_dt, physics_dt, physics_ok),
            ("rendering_dt", self.config.rendering_dt, rendering_dt, rendering_ok),
        ):
            if not observed:
                notes.append(f"{field} not readable from the World (line carries the REQUEST)")
            elif applied != requested:
                notes.append(f"{field} requested={requested!r} applied={applied!r}")
        if notes:
            print(f"[cv-runner] WARNING: sim_config {'; '.join(notes)}", flush=True)
        return line

    def pin_determinism_seeds(self) -> None:  # pragma: no cover - GPU path (numpy)
        """Pin the PRNG streams for the sample about to run (REQ-EXEC-003).

        ONE home for the pins (G-25): ``load_scene`` calls it BEFORE the World is
        constructed (seed before physics init), and the batch loop calls it again
        at every iteration boundary — sample i+1 must not inherit sample i's
        already-advanced stream, which is the opposite of an independent run.
        numpy is legal here (post-SimulationApp, D-C) and is why this body is off
        the CPU path: the bundled interpreter has it, a CPU host need not.
        """
        import numpy as np  # noqa: PLC0415

        random.seed(self.config.seed)
        np.random.seed(self.config.seed & 0xFFFFFFFF)

    def load_scene(self, identity_key: str | None = None) -> None:  # pragma: no cover - GPU
        """open_stage(sample scene) + locate pre-wired robot; pin dt/seed; reset.

        REUSE (do-not-reinvent): the sample scene ships the Nova Carter robot with
        its ROS2 action graphs pre-wired — no robot spawn/graph authoring here for
        mapped scenes; we only locate the robot prim (candidates -> loud error) and,
        when the scenario DECLARED ``initial_pose``, move that already-placed robot
        to it before reset (REQ-EXEC-002). No declaration = the asset's placement
        stands untouched.
        """
        if self.simulation_app is None:
            raise RuntimeError("boot() must run before load_scene() (M2 §3.2 order)")

        import omni.usd  # noqa: PLC0415 (legal only after SimulationApp)
        from isaacsim.core.api import World  # noqa: PLC0415

        asset = resolve_scene(self.config.scene_ref)
        scene_path = asset.scene_usd
        if not is_direct_usd_ref(scene_path):
            from isaacsim.storage.native import get_assets_root_path  # noqa: PLC0415

            root = get_assets_root_path()
            if root is None:
                raise RuntimeError(
                    "Isaac assets root unreachable (cloud assets / cache) — cannot "
                    f"resolve sample scene {scene_path!r}; check network or asset cache "
                    "mounts (M5 cache seam)"
                )
            scene_path = root + scene_path

        self._phase_begin(PHASE_SCENE_LOAD)
        t0 = time.monotonic()
        if not omni.usd.get_context().open_stage(scene_path):
            raise RuntimeError(f"open_stage failed for {scene_path!r}")
        self.simulation_app.update()
        self._phase_end(PHASE_SCENE_LOAD)
        # P2-09 cold/warm attribution: report how long the runner actually spent
        # loading the scene (open_stage + first app pump). Lets the cold penalty be
        # split into asset-download vs shader/compute-compile terms. The resolved
        # path/URL is logged so cold/warm & local/remote can be told apart post-hoc.
        print(
            f"[cv-runner] scene load: {scene_path} took "
            f"{time.monotonic() - t0:.2f}s (open_stage+update)",
            flush=True,
        )

        # robot_spawn (p4c5 T1) = World ctor + seed pins + robot-prim resolve +
        # declared initial pose + pre_reset hooks + world.reset() — i.e. everything
        # that materializes the robot/physics scene. A block INSIDE reset()
        # (pipeline/PhysX warm-up) lands here; a block in the first stepped frame
        # lands in first_render_frame.
        self._phase_begin(PHASE_ROBOT_SPAWN)
        # Determinism pins (REQ-EXEC-003, LOCKED §6): seed before physics init;
        # fixed dt on the World.
        self.pin_determinism_seeds()
        self.world = World(
            physics_dt=self.config.physics_dt,
            rendering_dt=self.config.rendering_dt,
            stage_units_in_meters=1.0,
        )
        # DoD-P2-06 ①: report what was applied, HERE — the seeds are pinned just
        # above and the World now holds the dt, and everything after this point
        # (robot resolve, initial pose, reset) can fail. Emitting later would lose
        # exactly the runs whose settings a reader most wants to compare.
        # ``identity_key`` is M3's (CV_REQUEST_IDENTITY_KEY, read by main.run) —
        # passed through VERBATIM; None keeps the honest ``identity_key=none``.
        self.emit_sim_config(identity_key)

        if asset.robot_prim_candidates:
            stage = omni.usd.get_context().get_stage()
            for path in asset.robot_prim_candidates:
                if stage.GetPrimAtPath(path).IsValid():
                    self.robot_prim_path = path
                    break
            if self.robot_prim_path is None:
                children = [str(p.GetPath()) for p in stage.GetPseudoRoot().GetAllChildren()]
                raise RuntimeError(
                    f"robot prim not found in {scene_path!r} — tried "
                    f"{list(asset.robot_prim_candidates)}; stage roots: {children} "
                    "(sample asset naming changed? update SCENE_ASSETS)"
                )

        # REQ-EXEC-002: honour a DECLARED spawn pose before anything binds to the
        # robot (the telemetry tensor view is a pre_reset hook) — and before
        # reset(), while the robot is still a plain USD xform.
        target = resolve_initial_pose_target(self.config.initial_pose, self.robot_prim_path)
        if target is not None:
            self.apply_initial_pose(target)

        for hook in self.pre_reset:
            hook(self.world)
        self.world.reset()
        self._phase_end(PHASE_ROBOT_SPAWN, robot_prim=self.robot_prim_path)

    def apply_initial_pose(self, prim_path: str) -> None:  # pragma: no cover - GPU path
        """Move the asset-placed robot to the declared spawn pose (REQ-EXEC-002).

        Called only when ``config.initial_pose`` is declared — the branch itself
        is ``resolve_initial_pose_target`` (CPU-tested) — and only BEFORE
        ``world.reset()``: at that point the robot is still a plain USD xform, so
        a stage-level world-pose write is what sticks (after play it would have
        to go through the articulation view). do-not-reinvent: ``SingleXFormPrim``
        is Isaac's own world-pose wrapper, the singular sibling of telemetry's
        ``SingleRigidPrim``; we author no transform ops ourselves. The declared
        pose is planar, so the asset's own z is read back and kept.

        API anchor (NOT yet GPU-confirmed by us — p5c11 was a CPU cycle):
        ``docs/research/nova-carter-nav2-verification.md`` records
        ``isaacsim.core.prims.SingleXFormPrim.get_world_pose() -> (pos(3),
        quat(4) wxyz)`` from the 5.0.0 API docs, which is where the ``wxyz``
        ordering and the "z lives at index 2" assumption below come from. First
        live run must confirm the pose actually MOVES the robot (a wrong wrapper
        here fails loudly at import/attr, not silently).
        """
        import numpy as np  # noqa: PLC0415 (legal post-SimulationApp, D-C)
        from isaacsim.core.prims import SingleXFormPrim  # noqa: PLC0415

        prim = SingleXFormPrim(prim_path)
        current_position, _ = prim.get_world_pose()
        position, orientation = initial_pose_world_transform(
            self.config.initial_pose, current_position
        )
        prim.set_world_pose(position=np.array(position), orientation=np.array(orientation))
        print(
            f"[cv-runner] initial pose applied: {prim_path} -> "
            f"declared={self.config.initial_pose} position={position} "
            f"(z kept from the scene asset) orientation_wxyz={orientation}",
            flush=True,
        )

    def enable_declared_sensors(self, topics) -> list[str]:  # pragma: no cover - GPU path
        """FU-17 GPU wrapper: pre_reset hook body over the LIVE stage.

        Runs the pure ``enable_sensor_render_products`` walk inside a
        session-layer edit context — the exact p3c1 probe recipe (in-memory,
        asset-unmodified, never saved). Must run BEFORE ``world.reset()``
        (measured: the render-product exec chain is one-shot; mid-play toggling
        publishes nothing), which the pre_reset seam guarantees.
        """
        import omni.usd  # noqa: PLC0415 (legal only after SimulationApp)
        from pxr import Usd  # noqa: PLC0415

        stage = omni.usd.get_context().get_stage()
        with Usd.EditContext(stage, stage.GetSessionLayer()):
            enabled, unmatched = enable_sensor_render_products(stage, topics)
        print(
            f"[cv-runner] sensor render products enabled: {enabled or '(none needed)'}",
            flush=True,
        )
        if unmatched:  # declared but publisher-less — the original FU-17 bug class
            print(
                f"[cv-runner] WARNING: declared sensor topic(s) with no publish "
                f"graph node in the scene: {unmatched}",
                flush=True,
            )
        return enabled

    def spawn_debug_obstacle(self, spec: dict) -> None:  # pragma: no cover - GPU path
        """P2-04 FAIL-injection (bring-up): drop a fixed cuboid into the stage.

        Runner-side scene mutation. The spec travels via the M1 known-key field
        ``scenario.debug_obstacle {x, y, height, width, depth}`` (D-2' — an
        obstacle is WORLD state, not a judging criterion; supersedes the P2
        criteria-params ride-along). Dimension defaults stay RUNNER-owned (the
        ``DEBUG_OBSTACLE_DEFAULT_*`` constants; schema None = "runner default
        applies"). A LOW box stays below the 2D-lidar scan plane and is therefore
        invisible to the blackbox nav's costmaps, so it deterministically meets
        the chassis. Called as a pre-reset hook so the physics parse includes the
        collider — ONCE per process: the batch loop RE-POSITIONS this prim
        (``move_debug_obstacle``) instead of respawning it.
        """
        import numpy as np  # noqa: PLC0415 (legal post-SimulationApp, D-C)
        from isaacsim.core.api.objects import FixedCuboid  # noqa: PLC0415

        height = float(spec.get("height", DEBUG_OBSTACLE_DEFAULT_HEIGHT))
        FixedCuboid(
            prim_path=DEBUG_OBSTACLE_PRIM,
            name=DEBUG_OBSTACLE_PRIM.rsplit("/", 1)[-1],
            position=np.array(debug_obstacle_position(spec)),
            scale=np.array(
                [
                    float(spec.get("width", DEBUG_OBSTACLE_DEFAULT_WIDTH)),
                    float(spec.get("depth", DEBUG_OBSTACLE_DEFAULT_DEPTH)),
                    height,
                ]
            ),
        )

    def move_debug_obstacle(self, spec: dict) -> None:  # pragma: no cover - GPU path (W2)
        """Relocate the EXISTING debug-obstacle prim to this sample's position.

        The batch loop's obstacle seam. Delete+respawn is FORBIDDEN here, and this
        is why: measured in the installed 5.1.0 source
        (``isaacsim/core/api/objects/cuboid.py``), every ``FixedCuboid()`` on a
        fresh prim path also defines a NEW ``/World/Looks/visual_material_NN`` and
        a NEW ``/World/Physics_Materials/physics_material_NN`` via
        ``find_unique_string_name``, and nothing removes them (p6c2 §2.1 measured
        2 -> 48 material prims over 24 delete+respawn iterations). Moving performs
        zero prim churn: no orphans, no re-cook, no new material.

        do-not-reinvent: the write goes through ``SingleXFormPrim``, the same
        Isaac world-pose wrapper the declared initial pose uses.

        A MISSING prim is a loud ``RuntimeError``, never a shrug: this runs only
        when the sample DECLARES an obstacle, so "nothing to move" means the
        pre-reset spawn hook never ran and the sample would silently be judged on
        an obstacle-free world — exactly the FAIL-injection that must not vanish.
        """
        import omni.usd  # noqa: PLC0415 (legal post-SimulationApp)
        from isaacsim.core.prims import SingleXFormPrim  # noqa: PLC0415

        stage = omni.usd.get_context().get_stage()
        if not stage.GetPrimAtPath(DEBUG_OBSTACLE_PRIM).IsValid():
            raise RuntimeError(
                f"debug obstacle prim {DEBUG_OBSTACLE_PRIM!r} is not on the stage — this "
                "sample declares scenario.debug_obstacle, so the pre-reset spawn hook must "
                "have run once at scene load (a missing box would silently turn a "
                "FAIL-injection sample into an obstacle-free one)"
            )
        position = debug_obstacle_position(spec)
        SingleXFormPrim(DEBUG_OBSTACLE_PRIM).set_world_pose(position=position)
        print(f"[cv-runner] debug obstacle moved: {DEBUG_OBSTACLE_PRIM} -> {position}", flush=True)

    def repose_robot(self, pose: dict) -> None:  # pragma: no cover - GPU path (W1 measured)
        """Put the PLAYING robot back at a declared pose (batch iteration step 3).

        ``apply_initial_pose`` cannot be reused: its stage-level xform write only
        sticks BEFORE play (its own docstring says so), and the batch loop never
        stops the timeline. The post-play equivalent is the articulation view, and
        do-not-reinvent these are all existing 5.1.0 calls
        (``SingleArticulation.set_world_pose`` = "teleport the prim pose
        immediately" / ``set_linear_velocity`` / ``set_angular_velocity`` /
        ``set_joint_velocities``). The planar-to-world math is the SAME
        ``initial_pose_world_transform`` the pre-play path uses, so the two
        spellings of "put the robot at the declared pose" agree by construction.

        Velocities are zeroed because a teleport does not zero them: carrying
        sample i's momentum into sample i+1 is a hidden coupling between samples,
        and it would surface as unexplained variance in exactly the comparison
        this platform exists to make.

        SURFACED ASSUMPTION (v1 — W1 measures it): joint POSITIONS are left alone.
        The MVP robot is a differential-drive base whose joint positions are wheel
        angles, physically irrelevant to where a mission starts. If W1's settling
        gate says otherwise, ``set_joint_positions`` is the one-line addition; the
        log line says out loud that this run did not touch them.
        """
        import numpy as np  # noqa: PLC0415 (legal post-SimulationApp, D-C)
        from isaacsim.core.prims import SingleArticulation  # noqa: PLC0415

        # Same "declared but no known robot prim" rejection the pre-play path
        # raises — ONE definition of that branch (CPU-tested there).
        target = resolve_initial_pose_target(pose, self.robot_prim_path)
        if self._articulation is None:
            self._articulation = SingleArticulation(target)
            self._articulation.initialize()
        robot = self._articulation
        current_position, _ = robot.get_world_pose()
        position, orientation = initial_pose_world_transform(pose, current_position)
        robot.set_world_pose(position=np.array(position), orientation=np.array(orientation))
        robot.set_linear_velocity(np.zeros(3))
        robot.set_angular_velocity(np.zeros(3))
        dof = robot.num_dof
        if dof:
            robot.set_joint_velocities(np.zeros(dof))
        print(repose_log_line(target, pose, position, orientation), flush=True)

    def restage(
        self, pose: dict | None = None, obstacle: dict | None = None
    ) -> None:  # pragma: no cover - GPU path (W1/W2/W3 measured)
        """Bring the world to the NEXT sample's start state (p6 batch iteration seam).

        NOT ``load_scene``: that re-opens the stage, and avoiding that cost is the
        entire reason the batch carrier exists. The three steps and THEIR ORDER
        are the contract:

        1. **obstacle move FIRST** — it is an authored (kinematic) prim, so the new
           transform must be on the stage before the reset publishes the start
           state to physics.
        2. **``world.reset(soft=True)``** — the SDK's own path that skips
           ``stop()``/``play()`` (``isaacsim/core/api/world/world.py``: ``if not
           soft: self.stop()``), so the physics simulation views are never
           destroyed and re-created. Measured (p6c2): this is the ONLY intervention
           that flattens the per-iteration VRAM slope (+4.96 -> +0.00 MiB/iteration,
           flat to n=60); it also shortens the iteration by ~0.9 s and removes the
           ``/clock`` rewind entirely (23/23 rewinds -> 0) because the timeline
           never stops. Every post-hoc cleanup measured INEFFECTIVE (§5).
        3. **repose LAST** — soft reset restores the articulation's DEFAULT state,
           so a pose written before it would simply be overwritten. ``pose=None``
           needs no step at all: that default restore IS the declared answer (and
           is what every scenario declaring no initial_pose expects).

        The hard-reset fallback (stop/author/reset + an ``n`` cap) is a CONTRACT
        RESERVATION, not code: it is pre-approved for the case W1's gate fails, and
        writing it now would ship a branch nobody has measured.
        """
        if self.world is None:
            raise RuntimeError("load_scene() must run before restage() (M2 §3.2 order)")
        if obstacle is not None:
            self.move_debug_obstacle(obstacle)
        self.world.reset(soft=True)
        if pose is not None:
            self.repose_robot(pose)

    def step(self, render: bool = True) -> None:
        """One fixed-dt step (render=True: Nova Carter RTX lidar needs off-screen render).

        The FIRST step is traced (p4c5 T1): p4c4 measured a ~190 s block before the
        first frame at k=4 while the GPU idled, and the begin/end pair around this
        one call is what tells "the first rendered frame blocked" apart from "the
        SUT never came up" — the barrier's step-and-spin pumps this very call, so
        the phase nests inside ``sut_readiness_wait`` by construction. Steady-state
        cost of the instrumentation = one bool test per step.
        """
        if self.world is None:
            raise RuntimeError("load_scene() must run before step()")
        first = not self._first_step_traced
        if first:
            self._first_step_traced = True
            self._phase_begin(PHASE_FIRST_RENDER_FRAME, render=render)
        self.world.step(render=render)
        if first:
            self._phase_end(PHASE_FIRST_RENDER_FRAME)
        for callback in self.on_step:
            callback()

    def close(self) -> None:
        """Clean shutdown — returns VRAM/slots (REQ-EXEC-015, NFR-EXEC-002/004).

        NOT on the runner's terminal path since p5c14: the vendor ``close()`` does
        not return, it ends the process with status 0 and takes the job's exit code
        with it (G-62), so ``main.run``'s ``finally`` hands the shutdown to process
        death (``main.hard_exit``) instead. The p5c14 live ``closeprobe`` arm closed
        the "restore it if the probe says it is survivable" branch: close() raises
        nothing catchable, the process ends inside it. So this stays a seam for a
        caller that INTENDS to end here (probes/tools), never a shutdown to return
        from — and a caller that must OUTLIVE the sim cannot use it at all.
        """
        if self.simulation_app is not None:  # pragma: no cover - GPU path
            self.simulation_app.close()
            self.simulation_app = None
