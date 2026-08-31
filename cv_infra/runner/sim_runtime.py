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

import hashlib
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
    where the robot lives in it — either pre-wired by the asset, or COMPOSED here.

    do-not-reinvent: Phase-2 scenes REUSE the official ``carter_warehouse_navigation``
    sample (warehouse + Nova Carter + ROS2 OmniGraph pre-wired — clock/TF/odom/
    sensors/cmd_vel action graphs come WITH the asset; we author no scene). The
    robot prim path inside the sample is NVIDIA's naming, so it is a candidate
    list resolved against the live stage (first existing wins) — a rename in a
    future asset rev degrades to a loud, listing error instead of a wrong pin.

    The go2 rows (D-1) add the second shape: a ROBOT-FREE environment plus the
    references that turn it into a world. Every field below defaults to "this
    scene composes nothing", so the carter row keeps its exact meaning:

    * ``extra_scene_usds`` — scene layers referenced at IDENTITY next to
      ``scene_usd``. MEASURED reason (C0 probe A5/§4): the carter sample is
      itself ``warehouse_with_forklifts.usd`` + ``Stage/warehouse_extras.usd``,
      both at identity, so composing the SAME two layers keeps the carter
      occupancy map (origin, resolution, extent) valid for a different robot.
      Dropping the extras layer would leave the props out and silently skew the
      map by exactly those props.
    * ``robot_usd`` / ``robot_spawn_prim`` — the robot to reference and the prim
      path it lands on, for a scene whose asset ships no robot. ``None`` means
      the asset already placed one (carter).
    * ``robot_spawn_z`` — drop height of that composed robot, MEASURED per robot
      (see the go2 row). It exists because a referenced robot arrives at its own
      origin, which for a legged asset is the standing base height, not the
      floor; ``initial_pose`` cannot supply it (the contract is planar on
      purpose — floor contact owns z).
    * ``firmware_slots`` — DECLARATION ONLY (D-3): the named artifact slots this
      robot runs onboard, e.g. go2's locomotion policy. The platform infers no
      meaning from a slot here; it only lets the request's ``sut`` block be
      checked against what the robot declares (consumed in C2, not in C1).
    """

    scene_usd: str
    robot_prim_candidates: tuple[str, ...] = ()
    extra_scene_usds: tuple[str, ...] = ()
    robot_usd: str | None = None
    robot_spawn_prim: str | None = None
    robot_spawn_z: float = 0.0
    firmware_slots: tuple[str, ...] = ()


SCENE_ASSETS: dict[str, SceneAsset] = {
    # cv-infra-user/scenarios/nova_carter_warehouse_goal.yaml: scene name (M1 Scenario).
    "nova_carter_warehouse": SceneAsset(
        scene_usd="/Isaac/Samples/ROS2/Scenario/carter_warehouse_navigation.usd",
        robot_prim_candidates=("/World/Nova_Carter_ROS", "/World/Carter_ROS"),
    ),
    # go2 (D-1). Every path here is C0-probe MEASURED on the live 5.1.0 asset root
    # (probe §4, all URLs HTTP 200) — never typed from memory, because a
    # remembered path is a 404 at reference time, i.e. a boot failure after the
    # GPU was already paid (G-28).
    #
    # ``scene_usd`` is the SAME warehouse the carter sample references, opened
    # directly, and ``extra_scene_usds`` is the SAME extras layer: identity +
    # identity, so the carter occupancy map transfers (probe A5).
    #
    # ``robot_usd`` is the IsaacLab-flavoured go2 — deliberately not the
    # ``/Isaac/Robots/Unitree/Go2/go2.usd`` sibling: this is the asset the
    # locomotion policy was TRAINED against (12 dof in the same order, 19 bodies),
    # and the policy contract is what makes one of the two right (probe §3/§5).
    "go2_warehouse": SceneAsset(
        scene_usd="/Isaac/Environments/Simple_Warehouse/warehouse_with_forklifts.usd",
        extra_scene_usds=("/Isaac/Environments/Simple_Warehouse/Stage/warehouse_extras.usd",),
        robot_usd="/Isaac/IsaacLab/Robots/Unitree/Go2/go2.usd",
        robot_spawn_prim="/World/Go2",
        robot_prim_candidates=("/World/Go2",),
        # C1 MEASURED (this cycle, workstation, 3 s stance-hold settle from a
        # standing drop, same seed/dt, one variable): the drop height decides how
        # far the robot SLIDES before it is standing still, and that slide is
        # error on every initial_pose the scenario declares.
        #   z=0.40 (the training init height) -> slide 0.1170 m, pitch -0.069 rad
        #   z=0.32                            -> slide 0.0197 m, pitch +0.013 rad
        #   z=0.25                            -> slide 0.3373 m, pitch +0.378 rad
        # 0.32 wins by 5.9x and is ADOPTED. It is not "lower is better": 0.25
        # starts the feet already through their stance and the robot pitches.
        # Settled base height is ~0.28 either way (C0 A7 measured 0.279~0.288).
        robot_spawn_z=0.32,
        # D-3: go2 runs its locomotion policy onboard -> one slot. carter = none.
        firmware_slots=("locomotion_policy",),
    ),
}


#: Scope every composed scene layer / robot lands under. It is ``/World`` because
#: that is where the carter sample puts its own two warehouse layers (probe §4),
#: and matching it is what keeps a composed stage readable next to the official one.
SCENE_COMPOSE_ROOT = "/World"


def extra_scene_prim_path(usd_path: str) -> str:
    """``.../warehouse_extras.usd`` -> ``/World/warehouse_extras``. Pure.

    The registry declares WHAT to compose, not where it lands: the prim name is
    derived from the file stem so a composed stage reads like the official carter
    sample, whose extras layer sits at exactly ``/World/warehouse_extras`` with an
    identity xform. The stem is used VERBATIM — these names come from the registry
    (a consumer's direct ``.usd`` ref composes nothing), so a stem that is not a
    legal prim name fails loudly at reference time instead of being mangled into a
    second, differently-named prim that no exclusion path would ever match.
    """
    return f"{SCENE_COMPOSE_ROOT}/{usd_path.rsplit('/', 1)[-1].rsplit('.', 1)[0]}"


def robot_spawn_target(asset: SceneAsset) -> str:
    """Prim path a DECLARED ``robot_usd`` is referenced onto — loud when incoherent.

    Two registry-drift rejections, both pure and both here rather than on the GPU
    path, because each one degrades into a confusing failure hours later:

    * ``robot_usd`` without ``robot_spawn_prim`` — nothing to reference onto.
    * a spawn prim absent from ``robot_prim_candidates`` — the composition would
      succeed and then the robot RESOLVE (which only consults the candidates)
      would fail with "sample asset naming changed?", pointing the reader at the
      vendor asset instead of at the two registry fields that disagree.
    """
    if not asset.robot_spawn_prim:
        raise ValueError(
            f"scene registry row declares robot_usd={asset.robot_usd!r} but no "
            "robot_spawn_prim — there is nowhere to reference the robot onto"
        )
    if asset.robot_spawn_prim not in asset.robot_prim_candidates:
        raise ValueError(
            f"scene registry row spawns the robot at {asset.robot_spawn_prim!r} but its "
            f"robot_prim_candidates are {list(asset.robot_prim_candidates)} — the runner "
            "would compose a robot it then refuses to find"
        )
    return asset.robot_spawn_prim


#: Verbatim grep marker (G-26 prove-it-ran gate; pinned by a CPU test): a scene the
#: runner ASSEMBLES has to say what it put on the stage. A missing extras layer is
#: invisible in a screenshot and only surfaces as a map that no longer matches the
#: world, which reads as "the SUT's localisation is bad" — the wrong suspect.
SCENE_COMPOSE_LOG_MARKER = "scene_composed="


def scene_compose_log_line(
    extras: list[tuple[str, str]], robot: tuple[str, str, float] | None
) -> str:
    """One structured line naming every reference this run composed. Pure."""
    return (
        f"[cv-runner] {SCENE_COMPOSE_LOG_MARKER}{len(extras) + (robot is not None)} "
        f"extras={extras} robot={robot}"
    )


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


def _asset_url(usd_path: str, what: str) -> str:  # pragma: no cover - GPU path
    """Registry path -> assets-root-prefixed URL; a direct ref passes through.

    ONE home for the join (p8c1). ``load_scene`` spelled it for the SCENE and
    ``spawn_obstacle_pool`` for a prop, so the unreachable-root failure — the one
    an operator actually meets when the cache mount is wrong — was authored
    twice. ``what`` names the thing in that message, which is the only part that
    ever differed between the two copies: with ``what="sample scene"`` this
    raises the scene message character for character (asserted before the swap,
    G-17). The direct-ref arm stays CPU-testable; the join needs isaacsim.
    """
    if is_direct_usd_ref(usd_path):
        return usd_path
    from isaacsim.storage.native import get_assets_root_path  # noqa: PLC0415

    root = get_assets_root_path()
    if root is None:
        raise RuntimeError(
            "Isaac assets root unreachable (cloud assets / cache) — cannot "
            f"resolve {what} {usd_path!r}; check network or asset cache "
            "mounts (M5 cache seam)"
        )
    return root + usd_path


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


def _enable_upstream_render_products(stage, publish_node) -> list[str]:
    """Turn ON every still-disabled render-product node upstream of ONE publish node.

    In-memory attribute set only (never a stage save — the asset stays
    unmodified) and idempotent: an already-enabled node is left untouched, which
    is what makes a second call a no-op. Returns the node paths this call flipped.
    """
    flipped: list[str] = []
    for node in _upstream_prims(stage, publish_node):
        if not _node_type(node).endswith(RENDER_PRODUCT_NODE_TYPE):
            continue
        enabled_attr = node.GetAttribute("inputs:enabled")
        if enabled_attr and not enabled_attr.Get():
            enabled_attr.Set(True)
            flipped.append(str(node.GetPath()))
    return flipped


def enable_sensor_render_products(stage, topics) -> tuple[list[str], list[str]]:
    """FU-17: enable the render products feeding the DECLARED sensor topics.

    This half answers "WHICH publish nodes are ours" (a declared topic names them,
    slash-normalized — see ``_normalized_topic``); ``_enable_upstream_render_products``
    answers "which nodes upstream of one of them to flip". Returns
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
        enabled.extend(_enable_upstream_render_products(stage, prim))
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
#: applies"). The box is low so it deterministically meets the chassis.
#:
#: CORRECTION (p7c3 T5, 2026-08-29 — this comment used to claim the default height
#: keeps the box BELOW the 2D-lidar scan plane and therefore invisible to the
#: blackbox nav; that was never measured and is FALSE for the carter SUT). The
#: measured band is z in [0.1256, 2.0256] m (`XT_32` prim at world z 0.5256 minus
#: `pointcloud_to_laserscan` min_height 0.4), so a 0.15 m box reaches 2.4 cm INTO
#: the band and IS seen: 17 % of scans detect it, and the SUT's AMCL then loses
#: localisation (end-of-mission belief error up to 5.66 m vs 0.73 m at 0.10 m).
#: The default is NOT changed — shipped documents that omit `height` must keep
#: their bytes and their behaviour. A scenario that wants an invisible probe
#: declares `height: 0.10` explicitly (measured: 0/241 scan detections, 5/5 pass).
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
# Planar +Z rotation — ONE home for the quaternion the runner writes INTO the sim.
# --------------------------------------------------------------------------- #
def yaw_to_quat_wxyz(yaw: float) -> tuple[float, float, float, float]:
    """Planar +Z rotation as an Isaac ``(w, x, y, z)`` quaternion (pure, stdlib).

    ONE home for the half-angle formula on the SIM-facing side: the declared
    initial pose (pre-play and repose) wrote it, and the p7 obstacle set would
    have made it a second copy. The ROS spelling
    (``adapter/ros2.quat_z_w_from_yaw``, ``(z, w)`` order) deliberately stays
    where it is — that one is SUT-facing surface in ROS message field order, so
    folding the two would trade a real duplicate for a confusing one.
    """
    half = float(yaw) / 2.0
    return (math.cos(half), 0.0, 0.0, math.sin(half))


# --------------------------------------------------------------------------- #
# p7 obstacle sets: asset resolution -> pool -> placement (pure, CPU-testable).
# --------------------------------------------------------------------------- #
#: The literal that means "the runner's own FixedCuboid", not an asset at all.
#: M1 spells the same word for its dimensions validator
#: (``contract.schema.BUILTIN_BOX_ASSET``); this layer imports no contract, so a
#: CPU test holds the two equal (the BATCH_RUNNER_COMMAND <-> Dockerfile precedent).
BOX_ASSET_REF = "box"


@dataclass(frozen=True)
class ObstacleAsset:
    """One resolvable obstacle prop: the USD to reference plus how it sits on the floor.

    Mirrors ``SceneAsset``: ``usd_path`` is relative to the Isaac assets root
    (``is_direct_usd_ref`` decides whether the root prefix applies — ONE
    definition of that branch, shared with the scene path).

    ``z_offset`` is the asset-origin correction MEASURED per asset (W0 gate ⓐ):
    0.0 means the prop's own origin sits on its footprint, which is the Isaac
    prop convention but NOT a promise — the same W0 sweep measured
    ``table.usd`` at ``bbox_min_z = -1.0398`` (a live counterexample; it is
    therefore not in the registry). An asset whose offset has never been
    measured does not belong here: a guessed offset either floats the prop or
    sinks it through the floor, and both read as "the obstacle did nothing".
    """

    usd_path: str
    z_offset: float = 0.0


#: The v1 curated registry. Every row is W0-MEASURED (p7c1 gate ⓐ/ⓑ; raw evidence
#: ``etri6000:~/cv-infra-p7c1-evidence/w0/`` + its ``tables.md``), and the three
#: rows are the set CEO ruling ② adopted
#: (``agent-comms/decisions/2026-08-28-p7c1-obstacle-gate1-rulings.md``).
#: Admission rule (CLAUDE §2-4 measured-then-written, G-25): a name enters this
#: table only after its bbox/z_offset, collider candidate and dynamic-physics
#: census were measured on the live 5.1.0 asset root — never typed from memory,
#: because a remembered path resolves to a 404 at reference time, i.e. a boot
#: failure at sample 0 after the batch already paid the Isaac boot (G-28).
OBSTACLE_ASSETS: dict[str, ObstacleAsset] = {
    # _source W0 ⓐ/ⓑ: 0.600 x 0.599 x 0.877 m, bbox_min_z ~ 0 -> z_offset 0.0,
    # collider C3, RigidBodyAPI 0.
    "chair": ObstacleAsset(usd_path="/Isaac/Environments/Office/Props/SM_Chair.usd"),
    # _source W0 ⓐ/ⓑ: 0.800 x 2.800 x 0.750 m, bbox_min_z ~ 0 -> z_offset 0.0,
    # collider C3, RigidBodyAPI 0.
    "desk": ObstacleAsset(usd_path="/Isaac/Environments/Office/Props/SM_SecretaryDeskA.usd"),
    # _source W0 ⓐ/ⓑ: 1.214 x 3.495 x 2.155 m, z_offset 0.001907 (the only non-zero
    # one of the three), collider C3, RigidBodyAPI 0. It stands in for the CEO's
    # "car": the 5.1.0 asset root ships no passenger-car visual asset at all
    # (ruling ①, 5,529 non-thumbnail usd enumerated).
    "forklift": ObstacleAsset(usd_path="/Isaac/Props/Forklift/forklift.usd", z_offset=0.001907),
    # _source C0 probe A13 (2026-09-01) — the go2 patrol TARGET, admitted by the
    # same measured rule as the three above: bbox 1.765 x 0.441 x 1.732 m,
    # bbox_min_z -0.1248 -> z_offset 0.1248 (adding it puts the feet exactly on
    # 0.0, verified), collider C3 applies (5 Gprim / 5 Mesh), RigidBodyAPI and
    # ArticulationRootAPI 0 -> the static-obstacle path accepts it. Live proof it
    # is not a ghost: a dropped cube came to rest on its head at z=1.757 and a
    # chest-height raycast hit.
    # ⚠ It spawns in BIND POSE (arms out), so it is 1.76 m WIDE — a scenario
    # placing one in an aisle must budget that, not a person's shoulder width.
    # ⚠ Isaac People directory names and file names differ elsewhere in that tree
    # (probe §6-8); this row is the pair that was actually fetched (HTTP 200).
    "person": ObstacleAsset(
        usd_path="/Isaac/People/Characters/F_Business_02/F_Business_02.usd",
        z_offset=0.1248,
    ),
}


def resolve_obstacle_asset(asset_ref: str) -> ObstacleAsset | None:
    """Map an obstacle asset designator to the USD to reference — ``"box"`` -> None.

    ``None`` is the runner's built-in ``FixedCuboid`` body (the same one the
    legacy debug obstacle uses), and that is why the sentinel is None rather than
    an ``ObstacleAsset(usd_path="")``: ``asset is None`` is the ONE branch that
    tells "author a cuboid" from "reference a prop", and an empty path would be a
    landmine the moment it is joined onto the assets root. A registry NAME
    resolves through ``OBSTACLE_ASSETS``; a ``.usd/.usda/.usdz`` ref passes
    through with no registry knowledge (same grammar as ``resolve_scene``); an
    unknown NAME is bad input -> loud ValueError listing the registry
    (REQ-INTAKE-005).
    """
    if asset_ref == BOX_ASSET_REF:
        return None
    if asset_ref in OBSTACLE_ASSETS:
        return OBSTACLE_ASSETS[asset_ref]
    if asset_ref.endswith((".usd", ".usda", ".usdz")):
        return ObstacleAsset(usd_path=asset_ref)
    raise ValueError(
        f"unknown obstacle asset {asset_ref!r} — known assets: {sorted(OBSTACLE_ASSETS)} "
        f"(or {BOX_ASSET_REF!r} for the built-in cuboid, or a direct "
        ".usd/.usda/.usdz reference)"
    )


#: Root scope of the runner-authored obstacle POOL. One flat namespace under one
#: scope, so "how many obstacle prims exist?" is a single subtree count and the
#: parking check has exactly one place to look. Also the path a consumer names in
#: ``collision_excluded_paths`` when it wants obstacle contacts off the verdict.
OBSTACLE_POOL_ROOT = "/World/cv_obstacles"

#: Parking pose of an UNUSED pool member. DEPTH, not distance: a 2D-lidar ray is
#: horizontal, so a prim 50 m under the floor cannot be hit by one at ANY range,
#: while a far-XY park stays in the scan PLANE and leans on a max-range the runner
#: does not own. W0 gate ⓓ measured the parked column's contribution to /scan at
#: 8.70e-05 m against a 0.0657 m self-consistency noise floor, and 0/12 parked
#: contacts.
OBSTACLE_PARK_Z = -50.0
#: Vertical pitch between parked members: the parked set must be a COUNTABLE
#: column, not n prims stacked at one point.
OBSTACLE_PARK_PITCH = 5.0

#: Fail-loud cap on the TOTAL pool (all buckets). Not a measurement (CLAUDE §2-4):
#: the CEO's stated worst case is chair 1 + desk 0..5 + forklift 2 = 8 prims, and
#: 32 is 4x that. Its job is to turn "a template expanded to 500 obstacles per
#: sample" into a pre-boot rejection (exit 2, 0 GPU seconds) instead of an OOM
#: after the boot is paid. Raising it is backwards compatible.
OBSTACLE_POOL_MAX = 32


def obstacle_pool_key(spec: dict) -> tuple[str, tuple[float, float, float] | None]:
    """The pool BUCKET one declared obstacle belongs to (pure, CPU-tested).

    * box   -> ``("box", (width, depth, height))`` with the runner defaults
      resolved. The dimensions are CONSTRUCTION-time on a ``FixedCuboid``, so each
      distinct size is its own bucket — re-scaling a cooked collider mid-play is
      an authoring op the soft reset does not republish to physics.
    * asset -> ``(asset_ref, None)`` VERBATIM (registry name or direct ref). Not
      the RESOLVED path: two aliases for one asset then cost two prims, which is
      wasteful but not wrong, whereas keying on the resolution would move the
      "unknown name" rejection out of admission and into the boot.

    Float keys carry no epsilon and need none: the pool plan and the per-sample
    lookup call THIS function on the SAME parsed values, so equality holds by
    construction.
    """
    asset_ref = str(spec["asset"])
    if asset_ref != BOX_ASSET_REF:
        return (asset_ref, None)
    return (
        BOX_ASSET_REF,
        (
            float(spec.get("width", DEBUG_OBSTACLE_DEFAULT_WIDTH)),
            float(spec.get("depth", DEBUG_OBSTACLE_DEFAULT_DEPTH)),
            float(spec.get("height", DEBUG_OBSTACLE_DEFAULT_HEIGHT)),
        ),
    )


def obstacle_pool_plan(per_sample: list[list[dict]]) -> dict[tuple, int]:
    """Every sample's obstacle list -> how many prims of each bucket to spawn ONCE.

    The pool is the per-sample MAXIMUM multiplicity, never the sum: sample i uses
    k_i members of a bucket and PARKS the rest, so a batch of 12 samples that each
    place 2 chairs needs 2 chairs, not 24. Raises ValueError when the total
    exceeds ``OBSTACLE_POOL_MAX`` (the caller turns it into a pre-boot exit 2).

    Pure — it runs in admission at 0 GPU seconds, and its result is exactly what
    the pre-reset spawn hook authors, so "what admission approved" and "what the
    boot creates" cannot be two different numbers.
    """
    plan: dict[tuple, int] = {}
    for entries in per_sample:
        multiplicity: dict[tuple, int] = {}
        for spec in entries:
            key = obstacle_pool_key(spec)
            multiplicity[key] = multiplicity.get(key, 0) + 1
        for key, needed in multiplicity.items():
            plan[key] = max(plan.get(key, 0), needed)
    total = sum(plan.values())
    if total > OBSTACLE_POOL_MAX:
        raise ValueError(
            f"the obstacle pool would need {total} prim(s) across {len(plan)} bucket(s), "
            f"over the {OBSTACLE_POOL_MAX} cap — the pool is the per-sample MAXIMUM "
            f"multiplicity, so this is one sample asking for that many at once "
            f"(buckets: {dict(sorted(plan.items(), key=lambda kv: -kv[1]))})"
        )
    return plan


def obstacle_slug(key: tuple[str, tuple[float, float, float] | None]) -> str:
    """USD-name-safe, collision-free slug for one bucket (pure).

    ``"<readable>_<8 hex of sha256(repr(key))>"``: the readable half is ``box`` /
    the registry name / ``usd`` for a direct ref, and the hash half is what makes
    it injective. A dimension-spelled slug (``box_1p20x0p40x0p15``) reads better
    and is WRONG: two buckets differing in the 4th decimal would render to the
    same prim name and silently collapse into one pool.
    """
    designator = key[0]
    if designator == BOX_ASSET_REF:
        readable = BOX_ASSET_REF
    elif designator in OBSTACLE_ASSETS:
        readable = designator
    else:
        readable = "usd"  # a direct ref: its path is not a USD-safe name
    digest = hashlib.sha256(repr(key).encode()).hexdigest()[:8]
    return f"{readable}_{digest}"


def obstacle_pool_paths(plan: dict[tuple, int]) -> dict[tuple, tuple[str, ...]]:
    """Bucket -> its pool members' prim paths, ``/World/cv_obstacles/<slug>_<j>``.

    Ordered and derived, never hand-listed: the spawn hook, the placement plan and
    the parking sweep must name the same prims; a drift would author a SECOND set
    — the exact failure ``DEBUG_OBSTACLE_PRIM``'s comment describes.
    """
    return {
        key: tuple(f"{OBSTACLE_POOL_ROOT}/{obstacle_slug(key)}_{j}" for j in range(count))
        for key, count in plan.items()
    }


def obstacle_pool_members(pool: dict[tuple, tuple[str, ...]]) -> tuple[str, ...]:
    """Every pool member's prim path in ONE canonical order (pure).

    That order IS the parking index (``obstacle_park_position``), so the spawn
    hook (which parks at birth) and the per-sample parking sweep must agree on
    it. Deriving it here is what makes them agree by construction instead of by
    two identical comprehensions that can drift apart.
    """
    return tuple(path for paths in pool.values() for path in paths)


def obstacle_park_position(pool_index: int) -> tuple[float, float, float]:
    """Where pool member ``pool_index`` waits while it is not placed (pure)."""
    return (0.0, 0.0, OBSTACLE_PARK_Z - pool_index * OBSTACLE_PARK_PITCH)


def obstacle_place_transform(
    spec: dict, asset: ObstacleAsset | None
) -> tuple[tuple[float, float, float], tuple[float, float, float, float]]:
    """One declared obstacle -> (world position, ``(w,x,y,z)`` orientation). Pure.

    ``z`` is NEVER a consumer input — floor contact owns it (the reason
    ``InitialPose`` has no z either):

    * box (``asset is None``) -> ``debug_obstacle_position(spec)``, the legacy
      ``height/2`` centring REUSED rather than re-derived;
    * asset -> ``asset.z_offset``, 0.0 for a prop whose own origin is on its
      footprint and non-zero only where W0 MEASURED otherwise.
    """
    if asset is None:
        position = debug_obstacle_position(spec)
    else:
        position = (float(spec["x"]), float(spec["y"]), float(asset.z_offset))
    return position, yaw_to_quat_wxyz(float(spec["yaw"]))


def obstacle_placement_plan(
    entries: list[dict], pool: dict[tuple, tuple[str, ...]]
) -> tuple[list[tuple[str, dict]], list[str]]:
    """(prim_path, declared entry) pairs to PLACE + prim paths to PARK. Pure.

    Assignment is by DECLARED ORDER within a bucket, so sample i's j-th chair is
    always pool member ``chair_<hash>_j`` — a stable mapping is what lets a
    contact event's prim path be read back to the declaration that put it there.

    BOTH lists are returned, and the parked one is computed HERE as pool minus
    placed, so "forgot to park the surplus" is not expressible by the caller. A
    bucket present in ``entries`` but absent from (or exhausted in) ``pool`` is a
    loud ValueError: it means the pool was planned from a different sample set,
    and the quiet alternative is a sample judged with fewer obstacles than it
    declared.
    """
    placed: list[tuple[str, dict]] = []
    used: dict[tuple, int] = {}
    for spec in entries:
        key = obstacle_pool_key(spec)
        members = pool.get(key, ())
        index = used.get(key, 0)
        if index >= len(members):
            raise ValueError(
                f"obstacle bucket {key!r} needs pool member #{index} but the pool holds "
                f"{len(members)} — the pool was planned from a different sample set "
                f"(pool buckets: {sorted(pool)})"
            )
        used[key] = index + 1
        placed.append((members[index], spec))
    taken = {path for path, _ in placed}
    parked = [path for path in obstacle_pool_members(pool) if path not in taken]
    return placed, parked


#: Verbatim grep marker (G-26 prove-it-ran gate; pinned by a CPU test + NEG-6 gate 5).
OBSTACLE_SET_LOG_MARKER = "obstacle_set_applied="

#: Verbatim grep marker of the BOOT-time per-member physics audit. A collider that
#: was never applied is a GHOST obstacle — it changes nothing and the verdict
#: still says pass (W0 measured exactly that on a RigidBody-carrying prop), so
#: every pool member states what it got.
OBSTACLE_PHYSICS_LOG_MARKER = "obstacle physics: "


def obstacle_set_log_line(placed: list[tuple[str, dict]], parked: list[str]) -> str:
    """One structured line per sample — what was PLACED and what was PARKED."""
    rendered = [
        (path, e["asset"], (float(e["x"]), float(e["y"]), float(e["yaw"]))) for path, e in placed
    ]
    return (
        f"[cv-runner] {OBSTACLE_SET_LOG_MARKER}{len(placed)} parked={len(parked)} "
        f"pool={len(placed) + len(parked)} placed={rendered} parked_paths={parked}"
    )


def obstacle_physics_log_line(
    prim_path: str, collider: str, rigid_body: int, articulation: int
) -> str:
    """One audit line per pool member — what physics the runner gave it, and what
    the asset brought with it.

    The two counts are 0 on every line the runner ever prints (a non-zero census
    is refused before this is reached — see ``_make_obstacle_static``); they are
    printed anyway because "we looked and found none" and "we never looked" are
    the same silence otherwise.
    """
    return (
        f"[cv-runner] {OBSTACLE_PHYSICS_LOG_MARKER}{prim_path} collider={collider} "
        f"rigid_body={rigid_body} articulation={articulation}"
    )


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
    position = (float(pose["x"]), float(pose["y"]), float(current_position[2]))
    return position, yaw_to_quat_wxyz(float(pose["yaw"]))


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
        # p7: bucket -> pool member prim paths, set ONCE by spawn_obstacle_pool.
        # ``apply_obstacle_set`` reads it rather than re-deriving the paths, so the
        # placement sweep can only ever name prims the boot actually authored.
        self._obstacle_pool: dict | None = None
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
        # LOCKED §7.7 — SimulationApp before any omni.*/isaacsim.* import (deferred
        # by design; the AST guard in tests/negative/test_eula_gate.py pins the order).
        # Everything below this line IS the Isaac kit: no ``isaacsim`` module exists on
        # a CPU host, so it can only ever run on the workstation — hence the per-line
        # pragmas. The guard above stays measured: it is the one CPU-reachable
        # statement here, and NEG-2 drives it on every host.
        from isaacsim import SimulationApp  # noqa: PLC0415  # pragma: no cover - GPU path

        self.simulation_app = SimulationApp(  # pragma: no cover - GPU path
            simulation_app_launch_config()
        )
        self._apply_texture_budget()  # pragma: no cover - GPU path
        return self.simulation_app  # pragma: no cover - GPU path

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
        scene_path = _asset_url(asset.scene_usd, "sample scene")

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

        # D-1: a robot-free environment becomes a world HERE — after the World
        # exists (the window the p7 obstacle pool's identical recipe is measured
        # in) and before the robot resolve below, which is what has to find it.
        # A registry row that composes nothing (carter) makes this a no-op.
        self._compose_scene(asset)

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

    def _compose_scene(self, asset: SceneAsset) -> None:  # pragma: no cover - GPU path
        """Reference the DECLARED extra scene layers + robot onto the open stage (D-1).

        do-not-reinvent, and not a new recipe either: this is the SAME
        ``add_reference_to_stage`` + ``simulation_app.update()`` pump +
        ``SingleXFormPrim.set_world_pose`` sequence ``spawn_obstacle_pool`` runs
        (W0-measured — the pump is what makes the reference COMPOSE before
        anything traverses it). Only the payload differs: a scene layer and a
        robot instead of a prop.

        The extras layers are written at NO transform on purpose. Identity is the
        whole point (probe A5): the carter sample composes these same layers at
        identity, so identity here is what keeps that occupancy map valid. The
        robot is the one thing that gets a pose, and only its ``z`` — x/y/yaw
        belong to ``initial_pose`` (planar contract), which runs right after this
        and reads back exactly the z written here.
        """
        if not asset.extra_scene_usds and not asset.robot_usd:
            return

        import numpy as np  # noqa: PLC0415 (legal post-SimulationApp, D-C)
        from isaacsim.core.prims import SingleXFormPrim  # noqa: PLC0415
        from isaacsim.core.utils.stage import add_reference_to_stage  # noqa: PLC0415

        extras: list[tuple[str, str]] = []
        for usd_path in asset.extra_scene_usds:
            prim_path = extra_scene_prim_path(usd_path)
            add_reference_to_stage(
                usd_path=_asset_url(usd_path, "extra scene layer"), prim_path=prim_path
            )
            self.simulation_app.update()
            extras.append((prim_path, usd_path))

        robot = None
        if asset.robot_usd:
            prim_path = robot_spawn_target(asset)
            add_reference_to_stage(
                usd_path=_asset_url(asset.robot_usd, "robot asset"), prim_path=prim_path
            )
            self.simulation_app.update()
            SingleXFormPrim(prim_path).set_world_pose(
                position=np.array((0.0, 0.0, float(asset.robot_spawn_z)))
            )
            robot = (prim_path, asset.robot_usd, float(asset.robot_spawn_z))

        print(scene_compose_log_line(extras, robot), flush=True)

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

    def robot_articulation(self) -> object:  # pragma: no cover - GPU path (C2b measured)
        """The ``SingleArticulation`` view over the resolved robot prim, created ONCE.

        Creation and initialization are SPLIT on purpose (C2b): the wrapper must
        be constructed BEFORE ``world.reset()`` — the same probe-02/03 constraint
        the telemetry tensor view lives under — while the physics handshake
        (``initialize()``) only answers once the timeline plays. So this is a
        ``pre_reset`` hook body for the policy path, and the CALLER initializes
        after the reset.

        Shared with ``repose_robot`` (which creates+initializes it when it is the
        first user): one view per robot, because two wrappers over one
        articulation would each re-run the physics-view handshake for nothing.
        """
        if self._articulation is None:
            from isaacsim.core.prims import SingleArticulation  # noqa: PLC0415

            self._articulation = SingleArticulation(self.robot_prim_path)
        return self._articulation

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

    def _make_obstacle_static(self, prim_path: str) -> str:  # pragma: no cover - GPU path
        """Give a REFERENCED prop colliders and refuse one that brings its own
        dynamics. Returns the one-line audit string the caller prints.

        The collider recipe is C3, ADOPTED BY MEASUREMENT (W0 gate ⓑ, CEO ruling
        ⑤): walk the subtree, ``UsdPhysics.CollisionAPI.Apply`` on every Gprim,
        plus ``MeshCollisionAPI`` with ``approximation="convexHull"`` on the
        Meshes. The vendor one-liner that looks like this
        (``omni.physx.scripts.utils.setCollider(prim, "convexHull")``) was
        measured NOT to convexify anything: it leaves the child meshes as triangle
        meshes, and every contact then emits a PhysX
        ``getMaterialFromInternalFaceIndex`` warning (up to 6,035 per run against
        0 in the obstacle-free control arm). Contact COUNT did not separate the
        candidates — the warning class did (G-105).

        v1 obstacles are STATIC, and a prop carrying ``RigidBodyAPI`` or
        ``ArticulationRootAPI`` is REFUSED here instead of neutralized (ruling ④).
        W0 measured all three failure modes on one such asset: with colliders
        added its contacts drop to ZERO (a ghost obstacle that still reads as a
        pass), disabling the body raises a vehicle-schema error, and leaving it
        dynamic let the prop travel 297.8 m out of the scene. The three registry
        assets carry no rigid bodies, so this only fires on a consumer's own USD —
        loudly, at boot, with the offending prims named.
        """
        import omni.usd  # noqa: PLC0415 (legal only after SimulationApp)
        from pxr import Usd, UsdGeom, UsdPhysics  # noqa: PLC0415

        stage = omni.usd.get_context().get_stage()
        root = stage.GetPrimAtPath(prim_path)
        if not root.IsValid():
            raise RuntimeError(
                f"obstacle prim {prim_path!r} is not on the stage after referencing its "
                "asset — the reference did not compose (bad usd_path? unreachable root?)"
            )
        dynamic = [
            str(prim.GetPath())
            for prim in Usd.PrimRange(root)
            if prim.HasAPI(UsdPhysics.RigidBodyAPI) or prim.HasAPI(UsdPhysics.ArticulationRootAPI)
        ]
        if dynamic:
            raise RuntimeError(
                f"obstacle asset at {prim_path!r} ships its own dynamics on {len(dynamic)} "
                f"prim(s) {dynamic[:5]} (RigidBodyAPI/ArticulationRootAPI) — v1 obstacles are "
                "STATIC. Measured: adding colliders to such a prop yields ZERO contacts (a "
                "ghost obstacle that still passes), and leaving it dynamic lets it be shoved "
                "into the next sample's world. Use a static prop, or the built-in 'box'."
            )
        gprims = 0
        meshes = 0
        for prim in Usd.PrimRange(root):
            if not prim.IsA(UsdGeom.Gprim):
                continue
            UsdPhysics.CollisionAPI.Apply(prim)
            if prim.IsA(UsdGeom.Mesh):
                mesh_api = UsdPhysics.MeshCollisionAPI.Apply(prim)
                mesh_api.CreateApproximationAttr().Set("convexHull")
                meshes += 1
            gprims += 1
        if not gprims:
            raise RuntimeError(
                f"obstacle asset at {prim_path!r} exposed NO Gprim to give a collider to — "
                "an obstacle with no collider is invisible to the telemetry reduction, i.e. "
                "a sample that silently drives through it and passes (W0 R1)"
            )
        return obstacle_physics_log_line(
            prim_path, f"applied(C3/convexHull, {gprims} gprim, {meshes} mesh)", 0, 0
        )

    def spawn_obstacle_pool(self, plan: dict) -> None:  # pragma: no cover - GPU path
        """Author the WHOLE pool once, every member PARKED (pre-reset hook, boot only).

        This is the only site in the runner that creates a p7 obstacle prim, and
        it runs exactly once per process — the same contract
        ``spawn_debug_obstacle`` carries, for the same measured reason (p6c2 §2.1:
        2 -> 48 material prims over 24 delete+respawn iterations). The batch loop
        RE-POSES these prims (``apply_obstacle_set``); it never creates one.

        Members are born at ``obstacle_park_position`` and NOTHING here reads a
        declared x/y/yaw: "where sample 0's obstacles go" is a placement question,
        answered by the SECOND pre-reset hook (``apply_obstacle_set``), so there
        is one definition of placement rather than a spawn-time copy of it.

        do-not-reinvent: a box is Isaac's ``FixedCuboid`` (the legacy debug
        obstacle's body), a prop is ``add_reference_to_stage`` + the C3 collider
        walk — we author no geometry and no physics schema of our own.
        """
        import numpy as np  # noqa: PLC0415 (legal post-SimulationApp, D-C)
        from isaacsim.core.api.objects import FixedCuboid  # noqa: PLC0415
        from isaacsim.core.prims import SingleXFormPrim  # noqa: PLC0415
        from isaacsim.core.utils.stage import add_reference_to_stage  # noqa: PLC0415

        pool = obstacle_pool_paths(plan)
        self._obstacle_pool = pool
        index = 0
        for (designator, dimensions), paths in pool.items():
            asset = resolve_obstacle_asset(designator)
            for prim_path in paths:
                position = obstacle_park_position(index)
                if asset is None:
                    width, depth, height = dimensions
                    FixedCuboid(
                        prim_path=prim_path,
                        name=prim_path.rsplit("/", 1)[-1],
                        position=np.array(position),
                        scale=np.array([width, depth, height]),
                    )
                    audit = obstacle_physics_log_line(
                        prim_path, "FixedCuboid (built-in, collider comes with the body)", 0, 0
                    )
                else:
                    add_reference_to_stage(
                        usd_path=_asset_url(asset.usd_path, "obstacle asset"),
                        prim_path=prim_path,
                    )
                    # W0 recipe: pump the app once so the reference composes before
                    # the collider walk traverses it (a walk over an uncomposed
                    # reference finds no Gprim and gives the prop nothing).
                    self.simulation_app.update()
                    audit = self._make_obstacle_static(prim_path)
                    SingleXFormPrim(prim_path).set_world_pose(position=np.array(position))
                print(audit, flush=True)
                index += 1
        print(
            f"[cv-runner] obstacle pool spawned: {index} prim(s) under "
            f"{OBSTACLE_POOL_ROOT} (parked) buckets={plan}",
            flush=True,
        )

    def apply_obstacle_set(self, entries: list[dict]) -> None:  # pragma: no cover - GPU path
        """Place THIS sample's obstacles on the existing pool; PARK every surplus.

        The batch loop's obstacle-SET seam, sibling of ``move_debug_obstacle``. It
        never creates and never removes a prim: every member authored at boot is
        written to either its declared world pose or its parking pose, so the prim
        census across samples is a CONSTANT (that constancy is NEG-6 gate 5's
        ``pool`` column).

        ``entries == []`` is NOT "nothing to do" — it is "this sample declares no
        obstacles, so park the whole pool". The caller's ``None`` means "there is
        no pool at all" and never reaches here. Collapsing the two is the first-order
        trap of this feature: a 0-obstacle sample would inherit the previous
        sample's placement and be judged against obstacles it never declared.

        do-not-reinvent: the write goes through ``SingleXFormPrim.set_world_pose``,
        the same wrapper the legacy move and the declared initial pose use — an
        obstacle is a static prim, not an articulation (which is why
        ``repose_robot`` needed a second spelling and this does not).
        """
        import numpy as np  # noqa: PLC0415 (legal post-SimulationApp, D-C)
        from isaacsim.core.prims import SingleXFormPrim  # noqa: PLC0415

        pool = self._obstacle_pool
        if pool is None:
            raise RuntimeError(
                "apply_obstacle_set ran before the pool was authored — the boot registers "
                "spawn_obstacle_pool as the pre-reset hook BEFORE this one, so a missing pool "
                "means this sample would be judged in an obstacle-free world"
            )
        placed, parked = obstacle_placement_plan(entries, pool)
        for prim_path, spec in placed:
            position, orientation = obstacle_place_transform(
                spec, resolve_obstacle_asset(str(spec["asset"]))
            )
            SingleXFormPrim(prim_path).set_world_pose(
                position=np.array(position), orientation=np.array(orientation)
            )
        parking_index = {path: index for index, path in enumerate(obstacle_pool_members(pool))}
        for prim_path in parked:
            SingleXFormPrim(prim_path).set_world_pose(
                position=np.array(obstacle_park_position(parking_index[prim_path]))
            )
        print(obstacle_set_log_line(placed, parked), flush=True)

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
        self,
        pose: dict | None = None,
        obstacle: dict | None = None,
        obstacle_set: list[dict] | None = None,
    ) -> None:  # pragma: no cover - GPU path (W1/W2/W3 measured)
        """Bring the world to the NEXT sample's start state (p6 batch iteration seam).

        NOT ``load_scene``: that re-opens the stage, and avoiding that cost is the
        entire reason the batch carrier exists. The steps and THEIR ORDER are the
        contract:

        1. **obstacle move FIRST** — it is an authored (kinematic) prim, so the new
           transform must be on the stage before the reset publishes the start
           state to physics.
        1b. **obstacle SET (p7)** — same reason, same window: the pool members are
           re-posed here, before the reset. ``obstacle_set=None`` means "this
           carrier has no pool" (nothing to do); ``[]`` means "this sample places
           NOTHING — park the whole pool", and those are different instructions.
           Folding ``[]`` into the None branch would leave a 0-obstacle sample
           standing on sample i-1's placement and judge it against obstacles it
           never declared. THAT is the first-order trap of this feature.
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
        if obstacle_set is not None:
            self.apply_obstacle_set(obstacle_set)
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
