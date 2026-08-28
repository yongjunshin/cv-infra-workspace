#!/usr/bin/env python3
"""p7c1 W0 — obstacle spike, IN-CONTAINER entry point. THROWAWAY (experiments/**).

Runs inside the runner image under ``/isaac-sim/python.sh`` and imports NOTHING from
``cv_infra`` on purpose (task §1: the spike is independent — production stays
untouched and un-imported, so a spike bug cannot be mistaken for a product bug).
Where a recipe already exists in production it is MIRRORED with a pointer, never
imported:

  * SimulationApp-first ordering + the EULA boot guard  -> sim_runtime.boot / eula_boot_guard
  * texture-streaming budget cap in the launch config   -> sim_runtime.simulation_app_launch_config
  * chassis-only PhysxContactReportAPI + contact sub    -> telemetry.PhysicsTelemetrySampler
  * declared-sensor render-product enable (FU-17 walk)  -> sim_runtime.enable_sensor_render_products
  * bundled-jazzy rclpy site / LD_LIBRARY_PATH          -> ros_bridge.bootstrap_bridge_env

Gates (pre-registered — implementation-plan/p7-obstacles-plan.md 부록 B §B8):

  enumerate  ⓐ assets root listing -> chair/desk/car candidates + bbox/z_offset +
               physics-API census (+ ⓔ per-asset load wall / VRAM / received bytes)
  cost       ⓔ per-asset load cost ONLY (run twice: cold cache root vs warm)
  collider   ⓑ asset x {C1,C2,C3,C4} grid, 3-criteria AND
  yaw        ⓒ {0, pi/2, pi, -pi/2} placement read-back + one render frame
  pool       ⓓ mixed pool + parking, n=12 soft-reset cycle (census/contacts/clock)
  scan       ⓓ /scan capture for ONE arm (``--arm with_pool`` | ``--arm no_pool``)

Every gate writes raw JSON under ``CV_SPIKE_OUT`` and prints ``MARK`` lines whose
epoch timestamps let the host-side 0.5 s VRAM CSV be windowed per phase (G-18).
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
import subprocess
import sys
import time
from pathlib import Path

LOG_PREFIX = "[p7c1]"

# Mirrored from cv_infra/runner/sim_runtime.py (R4 texture budget) so this spike's
# VRAM numbers are taken under the SAME renderer configuration production runs.
TEXTURE_BUDGET_SETTING = "/rtx-transient/resourcemanager/texturestreaming/memoryBudget"
TEXTURE_BUDGET_FRACTION = 0.6

# Measured scene/robot pins (tests/fixtures/nova_carter_warehouse_goal.yaml +
# sim_runtime.SCENE_ASSETS) — the spike consumes them, it does not invent them.
SCENE_USD = "/Isaac/Samples/ROS2/Scenario/carter_warehouse_navigation.usd"
ROBOT_PRIM = "/World/Nova_Carter_ROS"
CHASSIS_PRIM = "/World/Nova_Carter_ROS/chassis_link"
SCAN_TOPIC = "/front_2d_lidar/scan"

# Spike-local obstacle namespace (mirrors the p7 design's OBSTACLE_POOL_ROOT).
POOL_ROOT = "/World/cv_obstacles"
PARK_Z = -50.0
PARK_PITCH = 5.0

# Free-lane geometry of the warehouse sample, from the fixture's own measured
# values: AMCL start (-6.0, -1.0, yaw=pi) -> goal (-6.0, 5.0) is "a short straight
# drive up the same lane", so the lane between them is known-drivable free space.
ROBOT_HOME_XY = (-6.0, -1.0)
TEST_SPOT_XY = (-6.0, 2.0)
RAM_START_XY = (-6.0, 0.6)

JAZZY_GLOB = "exts*/isaacsim.ros2.bridge*/jazzy"
JAZZY_LD_MARKER = "isaacsim.ros2.bridge/jazzy/lib"


def log(msg: str) -> None:
    print(f"{LOG_PREFIX} {msg}", flush=True)


def mark(**fields) -> None:
    """One timestamped phase marker (host-side VRAM CSV windowing, G-18)."""
    body = " ".join(f"{k}={v}" for k, v in fields.items())
    print(f"{LOG_PREFIX} MARK t={time.time():.3f} {body}", flush=True)


# --------------------------------------------------------------------------- #
# Pre-boot: consent gate, ROS env bootstrap, host counters.
# --------------------------------------------------------------------------- #
def eula_boot_guard() -> None:
    """Mirror of sim_runtime.eula_boot_guard — no consent env, no Isaac (NEG-2)."""
    if not os.environ.get("ACCEPT_EULA"):
        raise SystemExit(
            f"{LOG_PREFIX} ACCEPT_EULA absent — refusing to boot Isaac (NEG-2). "
            "The operator supplies it per run; the host consent RECORD is checked by "
            "scripts/consent/check_consent.sh before this container is started."
        )


def bootstrap_ros_env() -> dict:
    """Put the bundled jazzy rclpy on sys.path; re-exec ONCE if LD_LIBRARY_PATH lacks it.

    run_gate.sh normally prepends the jazzy lib to the image's own LD_LIBRARY_PATH at
    ``docker run`` time (the loader snapshots it at process start — measured p2c5
    probe-01), so the re-exec below is a fallback that should not fire; the return
    value says which happened.
    """
    roots = [os.environ.get("ISAAC_PATH") or "/isaac-sim"]
    jazzy = None
    for root in roots:
        for cand in sorted(Path(root).glob(JAZZY_GLOB)):
            if (cand / "lib").is_dir():
                jazzy = cand
                break
    info = {"jazzy_root": str(jazzy) if jazzy else None, "reexec": False, "site_added": False}
    if jazzy is None:
        return info
    site = str(jazzy / "rclpy")
    if site not in sys.path:
        sys.path.insert(0, site)
        info["site_added"] = True
    ld = os.environ.get("LD_LIBRARY_PATH", "")
    info["ld_ok"] = JAZZY_LD_MARKER in ld
    if not info["ld_ok"]:
        os.environ["LD_LIBRARY_PATH"] = str(jazzy / "lib") + (f":{ld}" if ld else "")
        info["reexec"] = True
        log(f"re-exec for LD_LIBRARY_PATH (jazzy lib was absent): {sys.argv}")
        os.execv(sys.executable, [sys.executable, *sys.argv])
    return info


def gpu_used_mib() -> float | None:
    """GPU-WIDE memory.used from the in-container nvidia-smi (NOT per-PID).

    The authoritative per-PID series is the host sampler's 0.5 s CSV; this cheap
    reading exists so a per-asset delta has a same-process anchor. Labelled
    ``gpu_wide`` everywhere it is reported.
    """
    try:
        out = subprocess.run(
            ["nvidia-smi", "--query-gpu=memory.used", "--format=csv,noheader,nounits"],
            capture_output=True,
            text=True,
            timeout=10,
            check=True,
        )
        return float(out.stdout.strip().splitlines()[0])
    except Exception:
        return None


def net_rx_bytes() -> int | None:
    """Container-side received bytes across all non-loopback interfaces."""
    try:
        total = 0
        for line in Path("/proc/net/dev").read_text().splitlines()[2:]:
            iface, _, rest = line.partition(":")
            if iface.strip() == "lo":
                continue
            total += int(rest.split()[0])
        return total
    except Exception:
        return None


# --------------------------------------------------------------------------- #
# Small pure helpers (mirrors of the p7 design's pure layer).
# --------------------------------------------------------------------------- #
def yaw_to_quat_wxyz(yaw: float) -> tuple[float, float, float, float]:
    half = float(yaw) / 2.0
    return (math.cos(half), 0.0, 0.0, math.sin(half))


def quat_to_rpy(q) -> tuple[float, float, float]:
    """(w,x,y,z) -> (roll, pitch, yaw) radians, stdlib only."""
    w, x, y, z = (float(v) for v in q)
    roll = math.atan2(2.0 * (w * x + y * z), 1.0 - 2.0 * (x * x + y * y))
    sp = max(-1.0, min(1.0, 2.0 * (w * y - z * x)))
    pitch = math.asin(sp)
    yaw = math.atan2(2.0 * (w * z + x * y), 1.0 - 2.0 * (y * y + z * z))
    return roll, pitch, yaw


def angle_diff(a: float, b: float) -> float:
    return abs(math.atan2(math.sin(a - b), math.cos(a - b)))


def slug(key: str) -> str:
    readable = "".join(c if c.isalnum() else "_" for c in key.split("/")[-1])[:24] or "asset"
    return f"{readable}_{hashlib.sha256(key.encode()).hexdigest()[:8]}"


def park_position(index: int) -> tuple[float, float, float]:
    return (0.0, 0.0, PARK_Z - index * PARK_PITCH)


# --------------------------------------------------------------------------- #
# Boot.
# --------------------------------------------------------------------------- #
def boot():
    """SimulationApp FIRST (LOCKED §7.7) — mirrors sim_runtime.boot()."""
    eula_boot_guard()
    from isaacsim import SimulationApp  # noqa: PLC0415

    mark(phase="boot_begin")
    app = SimulationApp(
        {
            "headless": True,
            "extra_args": [f"--{TEXTURE_BUDGET_SETTING}={TEXTURE_BUDGET_FRACTION}"],
        }
    )
    mark(phase="boot_end")
    return app


def assets_root() -> str:
    from isaacsim.storage.native import get_assets_root_path  # noqa: PLC0415

    root = get_assets_root_path()
    if root is None:
        raise RuntimeError("get_assets_root_path() returned None — assets root unreachable")
    return root


# --------------------------------------------------------------------------- #
# Gate ⓐ — asset enumeration.
# --------------------------------------------------------------------------- #
#: Category keywords. These select from what the LISTING returned; they never
#: construct a path (G-28 — a path typed from memory is a 404 at reference time).
CATEGORY_KEYWORDS = {
    "chair": ("chair", "stool", "bench"),
    "desk": ("desk", "table", "workbench", "worktable"),
    "car": ("car", "sedan", "suv", "truck", "vehicle", "forklift", "van", "pickup"),
}
#: Matching is on the FILE NAME, not the whole URL: the first crawl matched
#: ``Props/Mounts/SeattleLabTable`` on the keyword "seat" (measured), and a
#: directory-name match says nothing about what the file is. Thumbnails
#: (``.thumbs/*.thumb.usd``) are excluded at crawl time — they reference nothing and
#: their world bbox comes back as the empty-box sentinel (+/-FLT_MAX, measured).
USD_SUFFIXES = (".usd", ".usda", ".usdc", ".usdz")


def _list_dir(url: str) -> tuple[str, list]:
    """omni.client.list one URL -> (result-name, entries). Never raises."""
    import omni.client  # noqa: PLC0415

    try:
        result, entries = omni.client.list(url)
        return str(result), list(entries)
    except Exception as exc:  # the API itself is a finding (§B8 ⓐ)
        return f"EXCEPTION:{exc!r}", []


def _is_dir(entry) -> bool:
    import omni.client  # noqa: PLC0415

    flags = getattr(entry, "flags", 0)
    can_have_children = getattr(omni.client.ItemFlags, "CAN_HAVE_CHILDREN", None)
    if can_have_children is not None:
        try:
            return bool(flags & can_have_children)
        except Exception:
            pass
    return not str(getattr(entry, "relative_path", "")).lower().endswith(USD_SUFFIXES)


def crawl(root: str, subdirs: list[str], max_dirs: int) -> tuple[list[dict], list[dict]]:
    """BFS the assets root -> (usd file records, per-directory listing log)."""
    files: list[dict] = []
    listing_log: list[dict] = []
    queue = [f"{root}{sub}" for sub in subdirs]
    seen: set[str] = set()
    listed = 0
    while queue and listed < max_dirs:
        url = queue.pop(0)
        if url in seen:
            continue
        seen.add(url)
        t0 = time.monotonic()
        result, entries = _list_dir(url)
        listed += 1
        listing_log.append(
            {
                "url": url,
                "result": result,
                "entries": len(entries),
                "wall_s": round(time.monotonic() - t0, 3),
            }
        )
        for entry in entries:
            rel = str(getattr(entry, "relative_path", "") or "")
            if not rel:
                continue
            if rel.startswith("."):
                continue  # .thumbs/ etc — thumbnails, not assets (measured)
            child = f"{url.rstrip('/')}/{rel}"
            if _is_dir(entry):
                queue.append(child)
            elif rel.lower().endswith(USD_SUFFIXES):
                files.append(
                    {
                        "url": child,
                        "name": rel,
                        "size": int(getattr(entry, "size", 0) or 0),
                    }
                )
    return files, listing_log


def categorize(files: list[dict]) -> dict[str, list[dict]]:
    hits: dict[str, list[dict]] = {k: [] for k in CATEGORY_KEYWORDS}
    for rec in files:
        low = rec["name"].lower()
        for cat, words in CATEGORY_KEYWORDS.items():
            if any(w in low for w in words):
                hits[cat].append(rec)
    for cat in hits:
        hits[cat].sort(key=lambda r: r["url"])
    return hits


def probe_asset(app, url: str) -> dict:
    """Reference ONE asset onto a fresh stage and measure it (ⓐ + ⓔ)."""
    import omni.usd  # noqa: PLC0415
    from pxr import Sdf, Usd, UsdGeom, UsdPhysics  # noqa: PLC0415

    ctx = omni.usd.get_context()
    ctx.new_stage()
    app.update()
    stage = ctx.get_stage()

    rec: dict = {"url": url}
    try:
        layer = Sdf.Layer.FindOrOpen(url)
        rec["default_prim"] = str(layer.defaultPrim) if layer else None
    except Exception as exc:
        rec["default_prim"] = f"ERROR:{exc!r}"

    vram0, rx0 = gpu_used_mib(), net_rx_bytes()
    mark(phase="asset_load_begin", url=url)
    t0 = time.monotonic()
    ok = True
    try:
        from isaacsim.core.utils.stage import add_reference_to_stage  # noqa: PLC0415

        add_reference_to_stage(usd_path=url, prim_path="/World/probe")
        rec["reference_api"] = "isaacsim.core.utils.stage.add_reference_to_stage"
    except Exception as exc:
        rec["reference_api"] = f"add_reference_to_stage FAILED: {exc!r}"
        try:
            prim = stage.DefinePrim("/World/probe", "Xform")
            prim.GetReferences().AddReference(url)
            rec["reference_api"] += " | fallback=Usd.Prim.GetReferences().AddReference"
        except Exception as exc2:
            rec["error"] = f"reference failed: {exc2!r}"
            ok = False
    if ok:
        app.update()
        rec["load_wall_s"] = round(time.monotonic() - t0, 3)
        app.update()
        time.sleep(1.5)  # settle so a 0.5 s sampler sees the post-load level
    mark(phase="asset_load_end", url=url)
    vram1, rx1 = gpu_used_mib(), net_rx_bytes()
    rec["vram_gpu_wide_before_mib"] = vram0
    rec["vram_gpu_wide_after_mib"] = vram1
    rec["vram_gpu_wide_delta_mib"] = None if None in (vram0, vram1) else round(vram1 - vram0, 1)
    rec["rx_bytes"] = None if None in (rx0, rx1) else rx1 - rx0
    if not ok:
        return rec

    prim = stage.GetPrimAtPath("/World/probe")
    # bbox: the API named by the plan first, BBoxCache as the measured fallback.
    try:
        bmin, bmax = ctx.compute_path_world_bounding_box("/World/probe")
        rec["bbox_api"] = "omni.usd.UsdContext.compute_path_world_bounding_box"
    except Exception as exc:
        cache = UsdGeom.BBoxCache(
            Usd.TimeCode.Default(), [UsdGeom.Tokens.default_, UsdGeom.Tokens.render]
        )
        box = cache.ComputeWorldBound(prim).ComputeAlignedBox()
        bmin, bmax = box.GetMin(), box.GetMax()
        rec["bbox_api"] = f"UsdGeom.BBoxCache (compute_path_world_bounding_box failed: {exc!r})"
    rec["bbox_min"] = [round(float(v), 6) for v in bmin]
    rec["bbox_max"] = [round(float(v), 6) for v in bmax]
    rec["size_xyz"] = [round(float(b) - float(a), 6) for a, b in zip(rec["bbox_min"], rec["bbox_max"])]
    min_z = rec["bbox_min"][2]
    if any(abs(v) > 1e30 for v in rec["bbox_min"] + rec["bbox_max"]):
        # Empty-box sentinel (+/-FLT_MAX): the prim has no boundable geometry. A
        # z_offset derived from it would be a fabricated number, so there is none.
        rec["bbox_degenerate"] = True
        rec["z_offset"] = None
    else:
        rec["bbox_degenerate"] = False
        rec["z_offset"] = 0.0 if abs(min_z) < 1e-4 else round(-min_z, 6)

    counts = {"prims": 0, "meshes": 0, "collision": 0, "rigid_body": 0, "articulation": 0}
    collision_paths, rigid_paths, art_paths = [], [], []
    types: dict[str, int] = {}
    for p in Usd.PrimRange(prim):
        counts["prims"] += 1
        tname = str(p.GetTypeName()) or "(typeless)"
        types[tname] = types.get(tname, 0) + 1
        if p.IsA(UsdGeom.Mesh):
            counts["meshes"] += 1
        if p.HasAPI(UsdPhysics.CollisionAPI):
            counts["collision"] += 1
            collision_paths.append(str(p.GetPath()))
        if p.HasAPI(UsdPhysics.RigidBodyAPI):
            counts["rigid_body"] += 1
            rigid_paths.append(str(p.GetPath()))
        if p.HasAPI(UsdPhysics.ArticulationRootAPI):
            counts["articulation"] += 1
            art_paths.append(str(p.GetPath()))
    rec["counts"] = counts
    rec["prim_types"] = dict(sorted(types.items(), key=lambda kv: -kv[1]))
    rec["collision_paths"] = collision_paths[:20]
    rec["rigid_body_paths"] = rigid_paths[:20]
    rec["articulation_paths"] = art_paths[:20]
    return rec


def gate_enumerate(app, out: Path, args) -> None:
    root = assets_root()
    log(f"assets root = {root} (isaacsim.storage.native.get_assets_root_path)")
    subdirs = args.subdirs.split(",")
    mark(phase="crawl_begin")
    files, listing_log = crawl(root, subdirs, args.max_dirs)
    mark(phase="crawl_end", files=len(files), dirs=len(listing_log))
    log(f"crawl: {len(listing_log)} dir(s) listed, {len(files)} usd file(s) found")
    hits = categorize(files)
    for cat, recs in hits.items():
        log(f"category {cat}: {len(recs)} keyword hit(s)")

    (out / "listing_log.json").write_text(json.dumps(listing_log, indent=1))
    (out / "usd_files.json").write_text(json.dumps(files, indent=1))
    (out / "category_hits.json").write_text(json.dumps(hits, indent=1))

    picks: list[dict] = []
    for cat, recs in hits.items():
        for rec in recs[: args.per_category]:
            probe = probe_asset(app, rec["url"])
            probe["category"] = cat
            probe["size"] = rec["size"]
            probe["name"] = rec["name"]
            probe["usd_path"] = rec["url"][len(root) :] if rec["url"].startswith(root) else rec["url"]
            picks.append(probe)
            log(
                f"probe {cat}/{rec['name']}: bbox_min_z={probe.get('bbox_min', [None]*3)[2]} "
                f"z_offset={probe.get('z_offset')} counts={probe.get('counts')} "
                f"load_wall_s={probe.get('load_wall_s')} vram_delta={probe.get('vram_gpu_wide_delta_mib')}"
            )
    (out / "assets.json").write_text(json.dumps({"root": root, "candidates": picks}, indent=1))
    log(f"wrote {out / 'assets.json'} ({len(picks)} probed candidate(s))")


def gate_probe(app, out: Path, args) -> None:
    """Probe a CURATED list of URLs -> assets.json (ⓐ + ⓔ for the shortlist).

    The list comes from ``--urls`` (a JSON array of ``{"category", "url"}``), and every
    URL in it must be one the crawl actually returned — the operator SELECTS from the
    measured listing, which is why this gate exists instead of a smarter keyword rule
    (G-28: a path that was not listed is a path that was invented).
    """
    entries = json.loads(Path(args.urls).read_text())
    root = assets_root()
    picks = []
    for entry in entries:
        rec = probe_asset(app, entry["url"])
        rec["category"] = entry.get("category")
        rec["name"] = entry["url"].rsplit("/", 1)[-1]
        rec["size"] = entry.get("size")
        rec["usd_path"] = entry["url"][len(root):] if entry["url"].startswith(root) else entry["url"]
        picks.append(rec)
        log(
            f"probe {rec['category']}/{rec['name']}: bbox_min={rec.get('bbox_min')} "
            f"size_xyz={rec.get('size_xyz')} z_offset={rec.get('z_offset')} "
            f"counts={rec.get('counts')} load_wall_s={rec.get('load_wall_s')} "
            f"vram_delta={rec.get('vram_gpu_wide_delta_mib')} rx={rec.get('rx_bytes')}"
        )
    (out / "assets.json").write_text(json.dumps({"root": root, "candidates": picks}, indent=1))
    log(f"wrote {out / 'assets.json'} ({len(picks)} probed)")


# --------------------------------------------------------------------------- #
# Gate ⓔ — per-asset cost only (run cold vs warm).
# --------------------------------------------------------------------------- #
def load_picks(args) -> tuple[str, list[dict]]:
    path = Path(args.assets)
    data = json.loads(path.read_text())
    root = data["root"]
    cands = data["candidates"]
    if args.pick:
        wanted = [w.strip() for w in args.pick.split(",") if w.strip()]
        chosen = [c for c in cands if c.get("name") in wanted or c.get("usd_path") in wanted]
    else:
        chosen = []
        for cat in CATEGORY_KEYWORDS:
            for c in cands:
                if c.get("category") == cat and "error" not in c:
                    chosen.append(c)
                    break
    return root, chosen


def gate_cost(app, out: Path, args) -> None:
    root, picks = load_picks(args)
    rows = []
    for cand in picks:
        rec = probe_asset(app, cand["url"])
        rec["category"] = cand.get("category")
        rec["name"] = cand.get("name")
        rows.append(rec)
        log(
            f"cost[{args.cache_label}] {cand.get('name')}: load_wall_s={rec.get('load_wall_s')} "
            f"rx_bytes={rec.get('rx_bytes')} vram_gpu_wide_delta_mib={rec.get('vram_gpu_wide_delta_mib')}"
        )
    (out / f"cost_{args.cache_label}.json").write_text(
        json.dumps({"root": root, "cache_label": args.cache_label, "rows": rows}, indent=1)
    )


# --------------------------------------------------------------------------- #
# Live-stage helpers shared by ⓑ / ⓒ / ⓓ / scan.
# --------------------------------------------------------------------------- #
class Live:
    """Warehouse scene + World + contact capture, assembled the production way."""

    def __init__(self, app):
        self.app = app
        self.world = None
        self.stage = None
        self.contacts: list[tuple[float, str, str]] = []
        self._sub = None
        self._chassis = None
        self._art = None
        self.pre_reset: list = []

    def load(self) -> None:
        import omni.usd  # noqa: PLC0415
        from isaacsim.core.api import World  # noqa: PLC0415

        url = assets_root() + SCENE_USD
        mark(phase="scene_load_begin")
        t0 = time.monotonic()
        if not omni.usd.get_context().open_stage(url):
            raise RuntimeError(f"open_stage failed for {url}")
        self.app.update()
        log(f"scene load: {url} took {time.monotonic() - t0:.2f}s")
        mark(phase="scene_load_end")
        self.stage = omni.usd.get_context().get_stage()
        # Determinism pins BEFORE physics init — mirrors sim_runtime.pin_determinism_seeds,
        # and it is what makes the two /scan arms differ only by the pool's presence.
        import random  # noqa: PLC0415

        import numpy as np  # noqa: PLC0415

        random.seed(0)
        np.random.seed(0)
        self.world = World(physics_dt=1.0 / 60.0, rendering_dt=1.0 / 60.0, stage_units_in_meters=1.0)
        if not self.stage.GetPrimAtPath(ROBOT_PRIM).IsValid():
            roots = [str(p.GetPath()) for p in self.stage.GetPseudoRoot().GetAllChildren()]
            raise RuntimeError(f"robot prim {ROBOT_PRIM} absent; stage roots={roots}")

    def bind_contacts(self) -> None:
        """PRE-reset: chassis-only PhysxContactReportAPI (D-E) — telemetry.bind mirror."""
        from pxr import PhysxSchema  # noqa: PLC0415

        prim = self.stage.GetPrimAtPath(CHASSIS_PRIM)
        if not prim.IsValid():
            raise RuntimeError(f"chassis prim {CHASSIS_PRIM} not found")
        api = PhysxSchema.PhysxContactReportAPI.Apply(prim)
        api.CreateThresholdAttr().Set(0.0)
        from isaacsim.core.prims import SingleRigidPrim  # noqa: PLC0415

        self._chassis = SingleRigidPrim(CHASSIS_PRIM)

    def attach_contacts(self) -> None:
        from omni.physx import get_physx_simulation_interface  # noqa: PLC0415
        from pxr import PhysicsSchemaTools  # noqa: PLC0415

        def on_contact(headers, data) -> None:
            for h in headers:
                self.contacts.append(
                    (
                        float(self.world.current_time),
                        str(PhysicsSchemaTools.intToSdfPath(h.actor0)),
                        str(PhysicsSchemaTools.intToSdfPath(h.actor1)),
                    )
                )

        self._sub = get_physx_simulation_interface().subscribe_contact_report_events(on_contact)

    def reset(self) -> None:
        for hook in self.pre_reset:
            hook(self.world)
        mark(phase="world_reset_begin")
        self.world.reset()
        mark(phase="world_reset_end")

    @property
    def articulation(self):
        if self._art is None:
            from isaacsim.core.prims import SingleArticulation  # noqa: PLC0415

            self._art = SingleArticulation(ROBOT_PRIM)
            self._art.initialize()
        return self._art

    def robot_z(self) -> float:
        pos, _ = self.articulation.get_world_pose()
        return float(pos[2])

    def teleport_robot(self, xy, yaw: float) -> None:
        import numpy as np  # noqa: PLC0415

        art = self.articulation
        pos, _ = art.get_world_pose()
        art.set_world_pose(
            position=np.array([xy[0], xy[1], float(pos[2])]),
            orientation=np.array(yaw_to_quat_wxyz(yaw)),
        )
        art.set_linear_velocity(np.zeros(3))
        art.set_angular_velocity(np.zeros(3))
        dof = art.num_dof
        if dof:
            art.set_joint_velocities(np.zeros(dof))

    def march(self, start_xy, end_xy, steps: int, render: bool = False) -> None:
        """Teleport-march the chassis into the target (deterministic ram).

        A nav mission would need the SUT; the gate only asks whether PhysX reports a
        contact against the obstacle prim, so the robot is walked in by pose writes
        (the same SingleArticulation.set_world_pose production's repose uses) and the
        physics step after each write is what produces the contact reports.
        """
        yaw = math.atan2(end_xy[1] - start_xy[1], end_xy[0] - start_xy[0])
        for s in range(steps + 1):
            f = s / steps
            self.teleport_robot(
                (
                    start_xy[0] + (end_xy[0] - start_xy[0]) * f,
                    start_xy[1] + (end_xy[1] - start_xy[1]) * f,
                ),
                yaw,
            )
            self.world.step(render=render)

    def pool_contacts(self, since: int) -> list[tuple[float, str, str]]:
        return [
            e
            for e in self.contacts[since:]
            if e[1].startswith(POOL_ROOT) or e[2].startswith(POOL_ROOT)
        ]


def reference_obstacle(stage, prim_path: str, url: str) -> None:
    from isaacsim.core.utils.stage import add_reference_to_stage  # noqa: PLC0415

    add_reference_to_stage(usd_path=url, prim_path=prim_path)


def set_pose(prim_path: str, position, yaw: float = 0.0) -> None:
    import numpy as np  # noqa: PLC0415
    from isaacsim.core.prims import SingleXFormPrim  # noqa: PLC0415

    SingleXFormPrim(prim_path).set_world_pose(
        position=np.array(position), orientation=np.array(yaw_to_quat_wxyz(yaw))
    )


def read_pose(prim_path: str):
    from isaacsim.core.prims import SingleXFormPrim  # noqa: PLC0415

    pos, quat = SingleXFormPrim(prim_path).get_world_pose()
    return [float(v) for v in pos], [float(v) for v in quat]


def apply_collider(stage, prim_path: str, candidate: str) -> dict:
    """C1..C4 — one candidate, fully reported (never silently 'worked')."""
    from pxr import Usd, UsdGeom, UsdPhysics  # noqa: PLC0415

    prim = stage.GetPrimAtPath(prim_path)
    rec = {"candidate": candidate, "prim": prim_path, "error": None}
    t0 = time.monotonic()
    try:
        if candidate == "C1":
            from omni.physx.scripts.utils import setCollider  # noqa: PLC0415

            setCollider(prim, approximationShape="convexHull")
            rec["api"] = "omni.physx.scripts.utils.setCollider(convexHull)"
        elif candidate == "C2":
            from isaacsim.core.prims import SingleGeometryPrim  # noqa: PLC0415

            SingleGeometryPrim(prim_path, collision=True)
            rec["api"] = "isaacsim.core.prims.SingleGeometryPrim(collision=True)"
        elif candidate == "C3":
            applied = 0
            for p in Usd.PrimRange(prim):
                if not p.IsA(UsdGeom.Gprim):
                    continue
                UsdPhysics.CollisionAPI.Apply(p)
                if p.IsA(UsdGeom.Mesh):
                    mesh_api = UsdPhysics.MeshCollisionAPI.Apply(p)
                    mesh_api.CreateApproximationAttr().Set("convexHull")
                applied += 1
            rec["api"] = f"UsdPhysics.CollisionAPI.Apply x{applied} (manual walk)"
        elif candidate == "C4":
            rec["api"] = "none (asset as shipped)"
        else:
            raise ValueError(candidate)
    except Exception as exc:
        rec["error"] = repr(exc)
    rec["apply_wall_s"] = round(time.monotonic() - t0, 4)
    after = {"collision": 0, "rigid_body": 0, "articulation": 0, "prims": 0}
    for p in Usd.PrimRange(prim):
        after["prims"] += 1
        after["collision"] += int(p.HasAPI(UsdPhysics.CollisionAPI))
        after["rigid_body"] += int(p.HasAPI(UsdPhysics.RigidBodyAPI))
        after["articulation"] += int(p.HasAPI(UsdPhysics.ArticulationRootAPI))
    rec["api_census_after"] = after
    return rec


def neutralize_dynamics(stage, prim_path: str) -> dict:
    """Ladder: RigidBodyEnabled=False -> kinematic -> articulation = REPORT (v1 reject)."""
    from pxr import Usd, UsdPhysics  # noqa: PLC0415

    prim = stage.GetPrimAtPath(prim_path)
    rec = {"rigid_body": [], "articulation": [], "path": prim_path}
    for p in Usd.PrimRange(prim):
        if p.HasAPI(UsdPhysics.ArticulationRootAPI):
            rec["articulation"].append(str(p.GetPath()))
        if not p.HasAPI(UsdPhysics.RigidBodyAPI):
            continue
        body = UsdPhysics.RigidBodyAPI(p)
        entry = {"prim": str(p.GetPath()), "step": None, "error": None}
        try:
            body.CreateRigidBodyEnabledAttr().Set(False)
            entry["step"] = "disabled"
        except Exception as exc:
            entry["error"] = repr(exc)
            try:
                body.CreateKinematicEnabledAttr().Set(True)
                entry["step"] = "kinematic"
            except Exception as exc2:
                entry["error"] += f" | kinematic: {exc2!r}"
        rec["rigid_body"].append(entry)
    return rec


# --------------------------------------------------------------------------- #
# Gate ⓑ — collider candidate grid.
# --------------------------------------------------------------------------- #
def gate_collider(app, out: Path, args) -> None:
    _root, picks = load_picks(args)
    candidates = [c.strip() for c in args.candidates.split(",") if c.strip()]
    live = Live(app)
    live.load()

    arms = []
    idx = 0
    # Control arm 1 — ram EMPTY SPACE. Without it, "N physics warnings appeared while
    # obstacles were present" is not attributable to the obstacles (G-103(b)): the
    # teleport-ram interpenetrates whatever is there, obstacle or not.
    arms.append(
        {
            "asset": "(none)",
            "category": "control",
            "url": None,
            "z_offset": 0.0,
            "candidate": "RAM_CONTROL",
            "prim": None,
            "pool_index": -1,
        }
    )
    for asset in picks:
        for cand in candidates:
            path = f"{POOL_ROOT}/{slug(asset['url'] + cand)}"
            arms.append(
                {
                    "asset": asset.get("name"),
                    "category": asset.get("category"),
                    "url": asset["url"],
                    "z_offset": float(asset.get("z_offset") or 0.0),
                    "candidate": cand,
                    "prim": path,
                    "pool_index": idx,
                }
            )
            idx += 1
        # Control arm 2 — same asset, dynamics NOT neutralized. It is what makes
        # criterion ② ("Δpos < 1e-3") falsifiable: a read-back that can never move is
        # a green light for free (G-35). Only meaningful where the asset HAS a body.
        if args.dynamic_control and (asset.get("counts") or {}).get("rigid_body"):
            arms.append(
                {
                    "asset": asset.get("name"),
                    "category": asset.get("category"),
                    "url": asset["url"],
                    "z_offset": float(asset.get("z_offset") or 0.0),
                    "candidate": "C4D",
                    "prim": f"{POOL_ROOT}/{slug(asset['url'] + 'C4D')}",
                    "pool_index": idx,
                }
            )
            idx += 1
    log(f"collider grid: {len(arms)} arm(s) = {len(picks)} asset(s) x {len(candidates)} candidate(s) + controls")

    # Author every arm PRE-reset (parked) so PhysX parses all of them once.
    for arm in arms:
        if arm["url"] is None:
            arm["collider"] = {"api": "n/a (ram control)", "error": None, "apply_wall_s": 0.0,
                               "api_census_after": {}}
            arm["dynamics"] = {"rigid_body": [], "articulation": [], "path": None}
            continue
        mark(phase="arm_author_begin", arm=f"{arm['asset']}|{arm['candidate']}")
        reference_obstacle(live.stage, arm["prim"], arm["url"])
        app.update()
        arm["collider"] = apply_collider(
            live.stage, arm["prim"], "C4" if arm["candidate"] == "C4D" else arm["candidate"]
        )
        if arm["candidate"] == "C4D":
            arm["dynamics"] = {"rigid_body": [], "articulation": [], "path": arm["prim"],
                               "note": "DYNAMICS CONTROL — neutralization deliberately skipped"}
        else:
            arm["dynamics"] = neutralize_dynamics(live.stage, arm["prim"])
        set_pose(arm["prim"], park_position(arm["pool_index"]))
        mark(phase="arm_author_end", arm=f"{arm['asset']}|{arm['candidate']}")
        log(
            f"authored {arm['prim']} ({arm['asset']}|{arm['candidate']}) "
            f"collider={arm['collider'].get('api')} error={arm['collider'].get('error')} "
            f"rigid_bodies={len(arm['dynamics']['rigid_body'])} "
            f"articulation={len(arm['dynamics']['articulation'])}"
        )

    live.bind_contacts()
    live.reset()
    live.attach_contacts()
    for _ in range(30):
        live.world.step(render=False)

    z = live.robot_z()
    log(f"robot z (asset-owned) = {z:.4f}")

    for arm in arms:
        tag = f"{arm['asset']}|{arm['candidate']}"
        print(f"{LOG_PREFIX} ARM_BEGIN {tag}", flush=True)
        mark(phase="arm_ram_begin", arm=tag)
        live.teleport_robot(ROBOT_HOME_XY, math.pi)
        for _ in range(10):
            live.world.step(render=False)
        if arm["prim"] is not None:
            set_pose(arm["prim"], (TEST_SPOT_XY[0], TEST_SPOT_XY[1], arm["z_offset"]))
        for _ in range(10):
            live.world.step(render=False)
        pose_before = read_pose(arm["prim"]) if arm["prim"] else ([0.0] * 3, [1.0, 0.0, 0.0, 0.0])
        since = len(live.contacts)
        live.march(RAM_START_XY, TEST_SPOT_XY, steps=args.ram_steps)
        for _ in range(20):
            live.world.step(render=False)
        pose_after = read_pose(arm["prim"]) if arm["prim"] else pose_before
        events = live.contacts[since:]
        own = (
            []
            if arm["prim"] is None
            else [
                e
                for e in events
                if arm["prim"] in (e[1], e[2])
                or e[1].startswith(arm["prim"] + "/")
                or e[2].startswith(arm["prim"] + "/")
            ]
        )
        dpos = math.dist(pose_before[0], pose_after[0])
        dyaw = angle_diff(quat_to_rpy(pose_before[1])[2], quat_to_rpy(pose_after[1])[2])
        arm["result"] = {
            "pose_before": pose_before,
            "pose_after": pose_after,
            "delta_pos_m": dpos,
            "delta_yaw_rad": dyaw,
            "contacts_total_window": len(events),
            "contacts_on_obstacle": len(own),
            "contact_sample": own[:5],
            "criterion1_contact_ge1": len(own) >= 1,
            "criterion2_static": dpos < 1e-3 and dyaw < 1e-3,
        }
        log(
            f"ARM {tag}: contacts_on_obstacle={len(own)} (window {len(events)}) "
            f"dpos={dpos:.6f} dyaw={dyaw:.6f}"
        )
        # park it again so the next arm's window is clean
        if arm["prim"] is not None:
            set_pose(arm["prim"], park_position(arm["pool_index"]))
        for _ in range(5):
            live.world.step(render=False)
        mark(phase="arm_ram_end", arm=tag)
        print(f"{LOG_PREFIX} ARM_END {tag}", flush=True)

    live.teleport_robot(ROBOT_HOME_XY, math.pi)
    for _ in range(10):
        live.world.step(render=False)
    ns_events = [e for e in live.contacts if e[1].startswith(POOL_ROOT) or e[2].startswith(POOL_ROOT)]
    (out / "collider_grid.json").write_text(
        json.dumps(
            {
                "arms": arms,
                # ALL obstacle-namespace contacts of the whole run (every arm's ram
                # lands here) — NOT a parking measurement; parking is gate ⓓ's per
                # iteration parked-path count.
                "pool_namespace_contact_events_total": len(ns_events),
            },
            indent=1,
        )
    )
    log(f"wrote {out / 'collider_grid.json'}")
    globals()["_LIVE"] = live  # reuse the booted stage for the yaw gate


# --------------------------------------------------------------------------- #
# Gate ⓒ — yaw.
# --------------------------------------------------------------------------- #
def gate_yaw(app, out: Path, args) -> None:
    _root, picks = load_picks(args)
    live = globals().get("_LIVE")
    fresh = live is None
    if fresh:
        live = Live(app)
        live.load()
        live.reset()

    rows = []
    frames_dir = out / "yaw_frames"
    frames_dir.mkdir(parents=True, exist_ok=True)
    for asset in picks:
        path = f"{POOL_ROOT}/yaw_{slug(asset['url'])}"
        if not live.stage.GetPrimAtPath(path).IsValid():
            reference_obstacle(live.stage, path, asset["url"])
            app.update()
        for yaw in (0.0, math.pi / 2.0, math.pi, -math.pi / 2.0):
            set_pose(path, (TEST_SPOT_XY[0], TEST_SPOT_XY[1], float(asset.get("z_offset") or 0.0)), yaw)
            app.update()
            live.world.step(render=True)
            pos, quat = read_pose(path)
            roll, pitch, got = quat_to_rpy(quat)
            rows.append(
                {
                    "asset": asset.get("name"),
                    "prim": path,
                    "declared_yaw": yaw,
                    "readback_quat_wxyz": quat,
                    "readback_yaw": got,
                    "yaw_abs_diff": angle_diff(got, yaw),
                    "roll": roll,
                    "pitch": pitch,
                    "position": pos,
                }
            )
            log(
                f"yaw {asset.get('name')} declared={yaw:.6f} readback={got:.6f} "
                f"diff={angle_diff(got, yaw):.3e} roll={roll:.3e} pitch={pitch:.3e}"
            )
            capture_frame(
                frames_dir / f"{slug(asset['url'])}_yaw_{yaw:+.3f}.png",
                (TEST_SPOT_XY[0] + 6.0, TEST_SPOT_XY[1] - 6.0, 4.0),
                (TEST_SPOT_XY[0], TEST_SPOT_XY[1], 0.5),
                live,
            )
        set_pose(path, park_position(90))
    (out / "yaw.json").write_text(json.dumps(rows, indent=1))
    log(f"wrote {out / 'yaw.json'} + {len(rows)} frame(s) in {frames_dir}")


#: ONE replicator render product for the whole yaw gate. Creating a fresh camera +
#: render product per frame is 16 renderer attach/detach cycles for a diagnostic
#: picture; production's VideoRecorder makes exactly one and re-uses it, so the spike
#: does the same (recording.py:263 "created ONCE ... only the cv2 writer is cycled").
_RENDER_PRODUCT = {"annotator": None, "rp": None, "how": None}


def _open_render_product(cam_pos, look_at) -> None:
    import omni.replicator.core as rep  # noqa: PLC0415

    try:
        cam = rep.create.camera(position=cam_pos, look_at=look_at)
        rp = rep.create.render_product(cam, (1280, 720))
        how = f"rep.create.camera(position={cam_pos}, look_at={look_at})"
    except Exception as exc:
        rp = rep.create.render_product("/OmniverseKit_Persp", (1280, 720))
        how = f"/OmniverseKit_Persp (rep.create.camera failed: {exc!r})"
    ann = rep.AnnotatorRegistry.get_annotator("rgb")
    ann.attach(rp)
    _RENDER_PRODUCT.update({"annotator": ann, "rp": rp, "how": how})
    log(f"render product opened via {how}")


def capture_frame(png: Path, cam_pos, look_at, live) -> None:
    """One off-screen RGB frame (replicator recipe mirrored from recording.py)."""
    try:
        import cv2  # noqa: PLC0415
        import numpy as np  # noqa: PLC0415

        if _RENDER_PRODUCT["annotator"] is None:
            _open_render_product(cam_pos, look_at)
        for _ in range(6):
            live.world.step(render=True)
        data = _RENDER_PRODUCT["annotator"].get_data()
        if data is None or getattr(data, "size", 0) == 0:
            log(f"WARNING: no frame data for {png.name}")
            return
        frame = np.asarray(data)
        cv2.imwrite(str(png), np.ascontiguousarray(frame[:, :, 2::-1]))
    except Exception as exc:
        log(f"WARNING: frame capture failed for {png.name}: {exc!r}")


# --------------------------------------------------------------------------- #
# Gate ⓓ — pool + parking, n=12 soft-reset cycle.
# --------------------------------------------------------------------------- #
def build_pool(live, app, picks: list[dict], args) -> list[dict]:
    """box x2 (two sizes) + every picked asset x its multiplicity -> parked pool."""
    import numpy as np  # noqa: PLC0415
    from isaacsim.core.api.objects import FixedCuboid  # noqa: PLC0415

    members: list[dict] = []
    box_specs = [(1.2, 0.4, 0.15), (0.6, 0.6, 0.4)]
    for i, (w, d, h) in enumerate(box_specs):
        key = f"box{w}x{d}x{h}"
        path = f"{POOL_ROOT}/{slug(key)}_0"
        FixedCuboid(
            prim_path=path,
            name=path.rsplit("/", 1)[-1],
            position=np.array(park_position(len(members))),
            scale=np.array([w, d, h]),
        )
        members.append({"path": path, "kind": "box", "key": key, "z_offset": h / 2.0, "index": i})
    mult = [int(v) for v in args.multiplicity.split(",")] if args.multiplicity else []
    for j, asset in enumerate(picks):
        n = mult[j] if j < len(mult) else 1
        for k in range(n):
            path = f"{POOL_ROOT}/{slug(asset['url'])}_{k}"
            reference_obstacle(live.stage, path, asset["url"])
            app.update()
            coll = apply_collider(live.stage, path, args.adopt)
            dyn = neutralize_dynamics(live.stage, path)
            set_pose(path, park_position(len(members)))
            members.append(
                {
                    "path": path,
                    "kind": "asset",
                    "key": asset["url"],
                    "asset": asset.get("name"),
                    "z_offset": float(asset.get("z_offset") or 0.0),
                    "index": len(members),
                    "collider": coll.get("api"),
                    "collider_error": coll.get("error"),
                    "rigid_bodies": len(dyn["rigid_body"]),
                    "articulation": len(dyn["articulation"]),
                }
            )
    for m in members:
        log(
            f"obstacle physics: {m['path']} kind={m['kind']} collider={m.get('collider', 'FixedCuboid')} "
            f"rigid_body={m.get('rigid_bodies', 0)} articulation={m.get('articulation', 0)}"
        )
    log(f"obstacle pool spawned: {len(members)} prim(s) under {POOL_ROOT} (parked)")
    return members


def census(live) -> dict:
    looks = live.stage.GetPrimAtPath("/World/Looks")
    phys = live.stage.GetPrimAtPath("/World/Physics_Materials")
    pool = live.stage.GetPrimAtPath(POOL_ROOT)
    return {
        "total_prims": len(list(live.stage.Traverse())),
        "looks": len(list(looks.GetAllChildren())) if looks.IsValid() else 0,
        "physics_materials": len(list(phys.GetAllChildren())) if phys.IsValid() else 0,
        "pool_children": len(list(pool.GetAllChildren())) if pool.IsValid() else 0,
    }


def gate_pool(app, out: Path, args) -> None:
    _root, picks = load_picks(args)
    live = Live(app)
    live.load()
    members = build_pool(live, app, picks, args)
    live.bind_contacts()
    live.reset()
    live.attach_contacts()
    for _ in range(30):
        live.world.step(render=True)

    pool_total = len(members)
    iters = []
    prev_time = -1.0
    for i in range(args.n):
        k = i % (pool_total + 1)
        placed, parked = [], []
        for j, m in enumerate(members):
            if j < k:
                pos = (TEST_SPOT_XY[0] + (j % 3) * 0.9 - 0.9, TEST_SPOT_XY[1] + j * 1.1 - 2.0, m["z_offset"])
                set_pose(m["path"], pos, yaw=(j * math.pi / 5.0))
                placed.append((m["path"], pos))
            else:
                set_pose(m["path"], park_position(j))
                parked.append(m["path"])
        print(
            f"{LOG_PREFIX} obstacle_set_applied={len(placed)} parked={len(parked)} "
            f"pool={pool_total} placed={[p for p, _ in placed]} parked_paths={parked}",
            flush=True,
        )
        mark(phase="iter_begin", iter=i + 1, placed=len(placed))
        # TRANSITION window: the poses were just rewritten, so the first steps after
        # the soft reset carry PhysX's contact-LOST reports for whatever the chassis
        # was touching in the PREVIOUS sample. Measured separately from the steady
        # window on purpose — collapsing them would either hide the transition or
        # blame parking for it.
        trans_since = len(live.contacts)
        t0 = time.monotonic()
        live.world.reset(soft=True)
        t_reset = time.monotonic() - t0
        t1 = time.monotonic()
        live.teleport_robot(ROBOT_HOME_XY, math.pi)
        for _ in range(args.transition_steps):
            live.world.step(render=True)
        transition = live.contacts[trans_since:]
        since = len(live.contacts)
        for _ in range(args.steps):
            live.world.step(render=True)
        # positive control: ram the FIRST placed member (nothing to ram at k=0)
        rammed = None
        if placed:
            target = placed[0][1]
            live.march((target[0], target[1] - 1.6), (target[0], target[1]), steps=40)
            rammed = placed[0][0]
            for _ in range(15):
                live.world.step(render=False)
        t_steps = time.monotonic() - t1
        window = live.contacts[since:]
        placed_paths = [p for p, _ in placed]
        on_placed = [e for e in window if any(e[1].startswith(p) or e[2].startswith(p) for p in placed_paths)]
        on_parked = [e for e in window if any(e[1].startswith(p) or e[2].startswith(p) for p in parked)]
        trans_parked = [
            e for e in transition if any(e[1].startswith(p) or e[2].startswith(p) for p in parked)
        ]
        sim_t = float(live.world.current_time)
        row = {
            "iter": i + 1,
            "placed": len(placed),
            "parked": len(parked),
            "pool": pool_total,
            "rammed": rammed,
            "census": census(live),
            "contacts_window": len(window),
            "contacts_on_placed": len(on_placed),
            "contacts_on_parked": len(on_parked),
            "contacts_on_parked_transition": len(trans_parked),
            "contacts_on_parked_transition_sample": trans_parked[:6],
            "contacts_on_parked_sample": on_parked[:6],
            "sim_time_s": sim_t,
            "sim_time_monotonic": sim_t >= prev_time,
            "reset_wall_s": round(t_reset, 4),
            "steps_wall_s": round(t_steps, 4),
            "gpu_wide_mib": gpu_used_mib(),
            "t_epoch": time.time(),
        }
        prev_time = sim_t
        iters.append(row)
        log(
            f"iter {i + 1}/{args.n}: placed={len(placed)} census={row['census']} "
            f"contacts placed={len(on_placed)} parked={len(on_parked)} "
            f"parked_transition={len(trans_parked)} "
            f"sim_t={sim_t:.2f} reset={t_reset:.3f}s steps={t_steps:.3f}s"
        )
        mark(phase="iter_end", iter=i + 1)
    (out / "pool_cycle.json").write_text(
        json.dumps({"pool": members, "pool_total": pool_total, "iters": iters}, indent=1)
    )
    log(f"wrote {out / 'pool_cycle.json'}")


# --------------------------------------------------------------------------- #
# Gate ⓓ (parking invisibility) — /scan capture, ONE arm per process.
# --------------------------------------------------------------------------- #
def enable_scan_render_products(live) -> list[str]:
    """FU-17 walk (mirrored from sim_runtime.enable_sensor_render_products).

    Declared topic -> publish node (``inputs:topicName``, slash-normalized) -> upstream
    BFS -> ``IsaacCreateRenderProduct.inputs:enabled = True``, inside a session-layer
    edit context so the asset itself is never modified.
    """
    from pxr import Usd  # noqa: PLC0415

    enabled: list[str] = []
    with Usd.EditContext(live.stage, live.stage.GetSessionLayer()):
        wanted = SCAN_TOPIC.lstrip("/")
        for prim in live.stage.Traverse():
            attr = prim.GetAttribute("inputs:topicName")
            value = attr.Get() if attr else None
            if value is None or str(value).lstrip("/") != wanted:
                continue
            seen = {str(prim.GetPath())}
            queue = [prim]
            while queue:
                node = queue.pop()
                for a in node.GetAttributes():
                    if not a.GetName().startswith("inputs:"):
                        continue
                    for src_path in a.GetConnections():
                        src = live.stage.GetPrimAtPath(src_path.GetPrimPath())
                        if not src or str(src.GetPath()) in seen:
                            continue
                        seen.add(str(src.GetPath()))
                        queue.append(src)
                        type_attr = src.GetAttribute("node:type")
                        node_type = str(type_attr.Get()) if type_attr else ""
                        if node_type.endswith("IsaacCreateRenderProduct"):
                            en = src.GetAttribute("inputs:enabled")
                            if en and not en.Get():
                                en.Set(True)
                                enabled.append(str(src.GetPath()))
    log(f"sensor render products enabled: {enabled or '(none needed)'}")
    return enabled


def gate_scan(app, out: Path, args) -> None:
    import base64  # noqa: PLC0415

    from isaacsim.core.utils.extensions import enable_extension  # noqa: PLC0415

    if not enable_extension("isaacsim.ros2.bridge"):
        raise RuntimeError("could not enable isaacsim.ros2.bridge")
    app.update()

    live = Live(app)
    live.load()
    members = []
    if args.arm == "with_pool":
        # The no_pool arm must not even need an assets file: its whole point is that
        # NOTHING obstacle-shaped exists in it.
        _root, picks = load_picks(args)
        members = build_pool(live, app, picks, args)
    else:
        log("arm=no_pool: authoring NO obstacle prims at all")

    enabled = enable_scan_render_products(live)

    # Robot pose is pinned IDENTICALLY in both arms (the scan must differ only by
    # the pool's presence). Contacts are ARMED here too: "0 parked contacts" from an
    # unsubscribed process is vacuous (G-35).
    live.bind_contacts()
    live.reset()
    live.attach_contacts()
    live.teleport_robot(ROBOT_HOME_XY, math.pi)
    for _ in range(30):
        live.world.step(render=True)

    import rclpy  # noqa: PLC0415
    from sensor_msgs.msg import LaserScan  # noqa: PLC0415

    rclpy.init()
    node = rclpy.create_node("p7c1_scan_probe")
    msgs: list[dict] = []

    def on_scan(msg) -> None:
        if len(msgs) >= args.scan_msgs:
            return
        import numpy as np  # noqa: PLC0415

        arr = np.asarray(msg.ranges, dtype="float32")
        msgs.append(
            {
                "stamp": float(msg.header.stamp.sec) + float(msg.header.stamp.nanosec) * 1e-9,
                "frame_id": msg.header.frame_id,
                "n": int(arr.size),
                "angle_min": float(msg.angle_min),
                "angle_increment": float(msg.angle_increment),
                "ranges_b64": base64.b64encode(arr.tobytes()).decode("ascii"),
            }
        )

    node.create_subscription(LaserScan, SCAN_TOPIC, on_scan, 10)
    steps = 0
    while len(msgs) < args.scan_msgs and steps < args.scan_max_steps:
        live.world.step(render=True)
        rclpy.spin_once(node, timeout_sec=0.0)
        steps += 1
    log(f"scan arm={args.arm}: {len(msgs)} msg(s) after {steps} step(s)")
    pool_contacts = [e for e in live.contacts if e[1].startswith(POOL_ROOT) or e[2].startswith(POOL_ROOT)]
    (out / f"scan_{args.arm}.json").write_text(
        json.dumps(
            {
                "arm": args.arm,
                "topic": SCAN_TOPIC,
                "steps": steps,
                "pool_members": [m["path"] for m in members],
                "render_products_enabled": enabled,
                "messages": msgs,
            },
            indent=1,
        )
    )
    (out / f"scan_{args.arm}_meta.json").write_text(
        json.dumps({"pool_contact_events": len(pool_contacts), "sample": pool_contacts[:5]}, indent=1)
    )
    node.destroy_node()
    rclpy.shutdown()


# --------------------------------------------------------------------------- #
# Gate ⓓ (parking invisibility, WITHIN-process) — the 2-arm design's control.
# --------------------------------------------------------------------------- #
def gate_scan2(app, out: Path, args) -> None:
    """Four /scan blocks in ONE process: A parked, B parked (no change), C placed, D parked.

    WHY this exists: the cross-process 2-arm form (gate ``scan``) measured a LARGER
    difference between two runs of the SAME arm than between the two different arms
    (38197 vs 499 differing rays), i.e. run-to-run variance dominates and the
    bit-identity criterion is undecidable that way (G-102 ①: a comparison without a
    self-consistency control cannot be read). Here A-vs-B is the variance floor
    measured INSIDE the very process whose parked pool is under test, A-vs-C is the
    positive control (obstacles that are actually visible MUST move the scan), and
    A-vs-D asks whether parking after a placement returns the scan to the floor.
    """
    import base64  # noqa: PLC0415

    from isaacsim.core.utils.extensions import enable_extension  # noqa: PLC0415

    if not enable_extension("isaacsim.ros2.bridge"):
        raise RuntimeError("could not enable isaacsim.ros2.bridge")
    app.update()

    _root, picks = load_picks(args)
    live = Live(app)
    live.load()
    members = build_pool(live, app, picks, args)
    enabled = enable_scan_render_products(live)
    live.bind_contacts()
    live.reset()
    live.attach_contacts()
    live.teleport_robot(ROBOT_HOME_XY, math.pi)
    for _ in range(60):
        live.world.step(render=True)

    import rclpy  # noqa: PLC0415
    from sensor_msgs.msg import LaserScan  # noqa: PLC0415

    rclpy.init()
    node = rclpy.create_node("p7c1_scan_blocks")
    sink: list = []

    def on_scan(msg) -> None:
        import numpy as np  # noqa: PLC0415

        arr = np.asarray(msg.ranges, dtype="float32")
        sink.append(
            {
                "stamp": float(msg.header.stamp.sec) + float(msg.header.stamp.nanosec) * 1e-9,
                "n": int(arr.size),
                "ranges_b64": base64.b64encode(arr.tobytes()).decode("ascii"),
            }
        )

    node.create_subscription(LaserScan, SCAN_TOPIC, on_scan, 10)

    def block(label: str) -> dict:
        sink.clear()
        steps = 0
        while len(sink) < args.scan_msgs and steps < args.scan_max_steps:
            live.world.step(render=True)
            rclpy.spin_once(node, timeout_sec=0.0)
            steps += 1
        log(f"scan block {label}: {len(sink)} msg(s) after {steps} step(s)")
        return {"label": label, "steps": steps, "messages": list(sink)}

    def park_all() -> None:
        for j, m in enumerate(members):
            set_pose(m["path"], park_position(j))

    def place_all() -> None:
        for j, m in enumerate(members):
            set_pose(
                m["path"],
                (TEST_SPOT_XY[0] + (j % 3) * 0.9 - 0.9, TEST_SPOT_XY[1] + j * 1.1 - 2.0, m["z_offset"]),
                yaw=(j * math.pi / 5.0),
            )

    blocks = [block("A_parked"), block("B_parked_nochange")]
    place_all()
    for _ in range(30):
        live.world.step(render=True)
    blocks.append(block("C_placed"))
    park_all()
    for _ in range(30):
        live.world.step(render=True)
    blocks.append(block("D_parked_again"))

    pool_contacts = [
        e for e in live.contacts if e[1].startswith(POOL_ROOT) or e[2].startswith(POOL_ROOT)
    ]
    (out / "scan_blocks.json").write_text(
        json.dumps(
            {
                "topic": SCAN_TOPIC,
                "pool_members": [m["path"] for m in members],
                "render_products_enabled": enabled,
                "pool_contact_events": len(pool_contacts),
                "blocks": blocks,
            },
            indent=1,
        )
    )
    log(f"wrote {out / 'scan_blocks.json'}")
    node.destroy_node()
    rclpy.shutdown()


# --------------------------------------------------------------------------- #
GATES = {
    "enumerate": gate_enumerate,
    "probe": gate_probe,
    "cost": gate_cost,
    "collider": gate_collider,
    "yaw": gate_yaw,
    "pool": gate_pool,
    "scan": gate_scan,
    "scan2": gate_scan2,
}


def main() -> int:
    ap = argparse.ArgumentParser(description="p7c1 W0 obstacle spike (throwaway)")
    ap.add_argument("gates", nargs="+", choices=sorted(GATES))
    ap.add_argument("--out", default=os.environ.get("CV_SPIKE_OUT", "/cv/out"))
    ap.add_argument("--assets", default=os.environ.get("CV_SPIKE_ASSETS", "/cv/assets.json"))
    ap.add_argument("--pick", default=os.environ.get("CV_SPIKE_PICK", ""))
    ap.add_argument("--urls", default=os.environ.get("CV_SPIKE_URLS", "/cv/urls.json"))
    ap.add_argument("--subdirs", default=os.environ.get("CV_SPIKE_SUBDIRS", "/Isaac/Props"))
    ap.add_argument("--max-dirs", type=int, default=int(os.environ.get("CV_SPIKE_MAX_DIRS", "400")))
    ap.add_argument(
        "--per-category", type=int, default=int(os.environ.get("CV_SPIKE_PER_CATEGORY", "3"))
    )
    ap.add_argument("--candidates", default=os.environ.get("CV_SPIKE_CANDIDATES", "C1,C2,C3,C4"))
    ap.add_argument("--ram-steps", type=int, default=int(os.environ.get("CV_SPIKE_RAM_STEPS", "60")))
    ap.add_argument("--n", type=int, default=int(os.environ.get("CV_SPIKE_N", "12")))
    ap.add_argument("--steps", type=int, default=int(os.environ.get("CV_SPIKE_STEPS", "60")))
    ap.add_argument(
        "--transition-steps", type=int, default=int(os.environ.get("CV_SPIKE_TRANSITION_STEPS", "5"))
    )
    ap.add_argument("--adopt", default=os.environ.get("CV_SPIKE_ADOPT", "C1"))
    ap.add_argument(
        "--dynamic-control",
        type=int,
        default=int(os.environ.get("CV_SPIKE_DYNAMIC_CONTROL", "1")),
    )
    ap.add_argument("--multiplicity", default=os.environ.get("CV_SPIKE_MULTIPLICITY", ""))
    ap.add_argument("--arm", default=os.environ.get("CV_SPIKE_ARM", "with_pool"))
    ap.add_argument("--scan-msgs", type=int, default=int(os.environ.get("CV_SPIKE_SCAN_MSGS", "100")))
    ap.add_argument(
        "--scan-max-steps", type=int, default=int(os.environ.get("CV_SPIKE_SCAN_MAX_STEPS", "6000"))
    )
    ap.add_argument("--cache-label", default=os.environ.get("CV_SPIKE_CACHE_LABEL", "warm"))
    args = ap.parse_args()

    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    ros = bootstrap_ros_env()
    log(f"argv={sys.argv} ros_bootstrap={ros}")
    log(f"gates={args.gates} out={out}")

    app = boot()
    status = 0
    try:
        for gate in args.gates:
            mark(phase="gate_begin", gate=gate)
            log(f"=== gate {gate} begin ===")
            GATES[gate](app, out, args)
            log(f"=== gate {gate} end ===")
            mark(phase="gate_end", gate=gate)
    except Exception:
        import traceback  # noqa: PLC0415

        traceback.print_exc()
        status = 3
    finally:
        (out / "spike_done.json").write_text(
            json.dumps({"gates": args.gates, "status": status, "t_epoch": time.time()}, indent=1)
        )
        sys.stdout.flush()
        sys.stderr.flush()
        # G-62: SimulationApp.close() ends the process with status 0 and eats the
        # exit code, so the spike ends the process itself — same stance as
        # cv_infra.runner.main.hard_exit.
        os._exit(status)
    return status  # unreachable


if __name__ == "__main__":
    sys.exit(main())
