"""isaacsim.ros2.bridge enable + env honor + boot glue (M2, REQ-EXEC-004/006).

Enables the ROS 2 bridge (bundled internal Jazzy) and *honors* the isolation env
that M3 injects — ``ROS_DOMAIN_ID`` / ``RMW_IMPLEMENTATION`` / FastDDS profile — it
never assigns them (LOCKED §5, D-2). ``enable_bridge`` defers the Isaac import;
``honored_env`` / ``bootstrap_bridge_env`` are pure env logic and CPU-testable.

FU-14 (runner side, cycle-5 PM ruling — knowledge-ownership split): scenario-derived
values (``ROS_DISTRO``/``RMW_IMPLEMENTATION``) are supervisor-owned keys injected by
M3 when present; the runner only FILLS ABSENT keys from ``adapter_config`` defaults
so it works with an older supervisor too. Image-internal knowledge (the bundled
jazzy ext location) is the runner's: ``bootstrap_bridge_env`` prepends the jazzy
``lib`` to ``LD_LIBRARY_PATH`` and puts the bundled rclpy site on ``sys.path``
(cycle-3 measured: the site alone makes rclpy 7.1.5 + nav2_msgs importable without
SimulationApp). Layout measured 2026-07-08 on ``cv-infra-runner:p2``:
``/isaac-sim/exts/isaacsim.ros2.bridge/jazzy/{lib,rclpy}``.

[VERIFIED @T3 p2c5 probe-01, 2026-07-08] The in-python ``LD_LIBRARY_PATH`` set is
NOT enough: the dynamic loader snapshots the value at process start, so the bridge
logged "Could not load ... librosidl_runtime_c.so" + "ROS2 Bridge startup failed"
even with the prepend in ``os.environ``. The measured fix stays M2-owned
(image-internal knowledge, no M5 entrypoint change): when bootstrap had to prepend
the path, ``reexec_for_bridge_lib`` re-executes the interpreter ONCE so the loader
starts with the bundled jazzy ``lib`` visible. Idempotent by construction — after
the re-exec the marker is already in the env, ``ld_path_prepended`` is False, and
no further exec happens.
"""

from __future__ import annotations

import os
import sys
from dataclasses import dataclass
from pathlib import Path

# Env keys the runner honors (never sets when present) — diagnostics + [VERIFY].
HONORED_ENV_KEYS = (
    "ROS_DOMAIN_ID",
    "RMW_IMPLEMENTATION",
    "FASTRTPS_DEFAULT_PROFILES_FILE",
    "ROS_DISTRO",
)
JAZZY_LD_MARKER = "isaacsim.ros2.bridge/jazzy/lib"

# Bundled jazzy ext discovery (image-internal knowledge, measured 2026-07-08).
# ``ISAAC_PATH`` (base-image env) wins over the measured default root.
DEFAULT_ISAAC_ROOTS = ("/isaac-sim",)
JAZZY_EXT_GLOB = "exts*/isaacsim.ros2.bridge*/jazzy"


@dataclass(frozen=True)
class BridgeEnv:
    """Snapshot of the honored ROS/DDS env (diagnostics, not a source of truth)."""

    ros_domain_id: str | None
    rmw_implementation: str | None
    fastdds_profile: str | None
    ros_distro: str | None
    jazzy_on_ld_path: bool


def honored_env(env: dict | None = None) -> BridgeEnv:
    """Read (do not set) the isolation env M3/M5 injected — CPU-testable."""
    environ = os.environ if env is None else env
    return BridgeEnv(
        ros_domain_id=environ.get("ROS_DOMAIN_ID"),
        rmw_implementation=environ.get("RMW_IMPLEMENTATION"),
        fastdds_profile=environ.get("FASTRTPS_DEFAULT_PROFILES_FILE"),
        ros_distro=environ.get("ROS_DISTRO"),
        jazzy_on_ld_path=JAZZY_LD_MARKER in environ.get("LD_LIBRARY_PATH", ""),
    )


@dataclass(frozen=True)
class BridgeBootstrap:
    """What ``bootstrap_bridge_env`` found/changed (log surface for T3 evidence)."""

    jazzy_root: str | None
    ros_distro_defaulted: bool
    rmw_defaulted: bool
    ld_path_prepended: bool
    rclpy_site_added: bool


def find_jazzy_root(
    roots: tuple[str, ...] = DEFAULT_ISAAC_ROOTS, env: dict | None = None
) -> Path | None:
    """Locate the bundled jazzy ext dir (``lib`` must exist) — CPU-testable."""
    environ = os.environ if env is None else env
    search: list[str] = []
    isaac_path = environ.get("ISAAC_PATH")
    if isaac_path:
        search.append(isaac_path)
    search.extend(roots)
    for root in search:
        for candidate in sorted(Path(root).glob(JAZZY_EXT_GLOB)):
            if (candidate / "lib").is_dir():
                return candidate
    return None


def bootstrap_bridge_env(
    default_ros_distro: str,
    default_rmw: str,
    env: dict | None = None,
    sys_path: list[str] | None = None,
    roots: tuple[str, ...] = DEFAULT_ISAAC_ROOTS,
) -> BridgeBootstrap:
    """FU-14 runner-side boot glue. Call BEFORE SimulationApp boot (main step 0.5).

    Supervisor-injected ``ROS_DISTRO``/``RMW_IMPLEMENTATION`` are honored untouched;
    absent keys are defaulted from ``adapter_config`` (the runner must work without
    the T1 supervisor too — task data contract). The jazzy ``lib``/rclpy-site paths
    are image-internal knowledge and always ensured (idempotent).
    """
    environ = os.environ if env is None else env
    path_list = sys.path if sys_path is None else sys_path

    ros_distro_defaulted = not environ.get("ROS_DISTRO")
    if ros_distro_defaulted:
        environ["ROS_DISTRO"] = default_ros_distro
    rmw_defaulted = not environ.get("RMW_IMPLEMENTATION")
    if rmw_defaulted:
        environ["RMW_IMPLEMENTATION"] = default_rmw

    jazzy = find_jazzy_root(roots, environ)
    ld_prepended = False
    site_added = False
    if jazzy is not None:
        ld = environ.get("LD_LIBRARY_PATH", "")
        if JAZZY_LD_MARKER not in ld:
            environ["LD_LIBRARY_PATH"] = str(jazzy / "lib") + (f":{ld}" if ld else "")
            ld_prepended = True
        site = str(jazzy / "rclpy")  # bundled site dir (contains rclpy, nav2_msgs, ...)
        if site not in path_list:
            path_list.insert(0, site)
            site_added = True

    return BridgeBootstrap(
        jazzy_root=str(jazzy) if jazzy is not None else None,
        ros_distro_defaulted=ros_distro_defaulted,
        rmw_defaulted=rmw_defaulted,
        ld_path_prepended=ld_prepended,
        rclpy_site_added=site_added,
    )


def reexec_for_bridge_lib(
    bootstrap: BridgeBootstrap,
    argv: list[str] | None = None,
    execv: object = None,
) -> bool:
    """Re-exec the interpreter once when the jazzy lib was prepended in-process.

    Measured (p2c5 probe-01): the glibc loader consumes ``LD_LIBRARY_PATH`` at
    process start — an in-python prepend leaves the bridge's shared libs
    unresolvable ("ROS2 Bridge startup failed"). Called at main step 0.5 BEFORE
    SimulationApp boot; ``os.execv`` inherits the already-patched ``os.environ``,
    and the re-exec'd process finds the marker present (``ld_path_prepended``
    False) so it never loops. Returns False when no re-exec is needed
    (CPU-testable via the injectable ``execv``).
    """
    if not bootstrap.ld_path_prepended:
        return False
    run = os.execv if execv is None else execv
    args = argv if argv is not None else [sys.executable, "-m", "cv_infra.runner.main"]
    run(args[0], args)  # pragma: no cover - process image is replaced on GPU path
    return True  # only reachable with an injected (test) execv


def enable_bridge(simulation_app: object) -> None:  # pragma: no cover - GPU path
    """Enable isaacsim.ros2.bridge after SimulationApp boot (cycle 2-3)."""
    from isaacsim.core.utils.extensions import enable_extension  # noqa: PLC0415 (deferred)

    if not enable_extension("isaacsim.ros2.bridge"):
        raise RuntimeError("could not enable isaacsim.ros2.bridge")
    simulation_app.update()
