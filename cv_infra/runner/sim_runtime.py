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
import random
import time
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


class SimRuntime:
    """Wraps the SimulationApp / World lifecycle. Isaac bodies are deferred-import."""

    def __init__(self, config: SimConfig) -> None:
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

    def boot(self) -> object:
        """Instantiate SimulationApp FIRST, then it is legal to import omni/isaacsim."""
        eula_boot_guard()
        # LOCKED §7.7 — SimulationApp before any omni.*/isaacsim.* import.
        from isaacsim import SimulationApp  # noqa: PLC0415 (deferred by design)

        self.simulation_app = SimulationApp({"headless": True})
        return self.simulation_app

    def load_scene(self) -> None:  # pragma: no cover - GPU path (T3 proves)
        """open_stage(sample scene) + locate pre-wired robot; pin dt/seed; reset.

        REUSE (do-not-reinvent): the sample scene ships the Nova Carter robot with
        its ROS2 action graphs pre-wired — no robot spawn/graph authoring here for
        mapped scenes; we only locate the robot prim (candidates -> loud error).
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

        t0 = time.monotonic()
        if not omni.usd.get_context().open_stage(scene_path):
            raise RuntimeError(f"open_stage failed for {scene_path!r}")
        self.simulation_app.update()
        # P2-09 cold/warm attribution: report how long the runner actually spent
        # loading the scene (open_stage + first app pump). Lets the cold penalty be
        # split into asset-download vs shader/compute-compile terms. The resolved
        # path/URL is logged so cold/warm & local/remote can be told apart post-hoc.
        print(
            f"[cv-runner] scene load: {scene_path} took "
            f"{time.monotonic() - t0:.2f}s (open_stage+update)",
            flush=True,
        )

        # Determinism pins (REQ-EXEC-003, LOCKED §6): seed before physics init;
        # fixed dt on the World. numpy is legal here (post-SimulationApp, D-C).
        random.seed(self.config.seed)
        import numpy as np  # noqa: PLC0415

        np.random.seed(self.config.seed & 0xFFFFFFFF)
        self.world = World(
            physics_dt=self.config.physics_dt,
            rendering_dt=self.config.rendering_dt,
            stage_units_in_meters=1.0,
        )

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

        for hook in self.pre_reset:
            hook(self.world)
        self.world.reset()

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
        criteria-params ride-along). Dimension defaults below stay RUNNER-owned
        (schema None = "runner default applies"). A LOW box (default 0.15 m,
        below the 2D-lidar scan plane) stays invisible to the blackbox nav's
        costmaps, so it deterministically meets the chassis. Called as a
        pre-reset hook so the physics parse includes the collider.
        """
        import numpy as np  # noqa: PLC0415 (legal post-SimulationApp, D-C)
        from isaacsim.core.api.objects import FixedCuboid  # noqa: PLC0415

        height = float(spec.get("height", 0.15))
        FixedCuboid(
            prim_path="/World/cv_debug_obstacle",
            name="cv_debug_obstacle",
            position=np.array([float(spec["x"]), float(spec["y"]), height / 2.0]),
            scale=np.array([float(spec.get("width", 1.2)), float(spec.get("depth", 0.4)), height]),
        )

    def step(self, render: bool = True) -> None:  # pragma: no cover - GPU path
        """One fixed-dt step (render=True: Nova Carter RTX lidar needs off-screen render)."""
        if self.world is None:
            raise RuntimeError("load_scene() must run before step()")
        self.world.step(render=render)
        for callback in self.on_step:
            callback()

    def close(self) -> None:
        """Clean shutdown — returns VRAM/slots (REQ-EXEC-015, NFR-EXEC-002/004)."""
        if self.simulation_app is not None:  # pragma: no cover - GPU path
            self.simulation_app.close()
            self.simulation_app = None
