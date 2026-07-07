"""isaacsim.ros2.bridge enable + env honor (M2, REQ-EXEC-004/006, REQ-ORCH-008).

Enables the ROS 2 bridge (bundled internal Jazzy) and *honors* the isolation env
that M3 injects — ``ROS_DOMAIN_ID`` / ``RMW_IMPLEMENTATION`` / FastDDS profile — it
never assigns them (LOCKED §5, D-2). The internal-Jazzy ``LD_LIBRARY_PATH`` is set
by the container env/entrypoint BEFORE ``python.sh`` boots (M2 §3.3 [VERIFY]), not
here. ``enable_bridge`` defers the Isaac import; ``honored_env`` is a pure env read
and is CPU-testable.
"""

from __future__ import annotations

import os
from dataclasses import dataclass

# Env keys the runner honors (never sets) — surfaced for diagnostics + [VERIFY].
HONORED_ENV_KEYS = (
    "ROS_DOMAIN_ID",
    "RMW_IMPLEMENTATION",
    "FASTRTPS_DEFAULT_PROFILES_FILE",
    "ROS_DISTRO",
)
JAZZY_LD_MARKER = "isaacsim.ros2.bridge/jazzy/lib"


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


def enable_bridge(simulation_app: object) -> None:  # pragma: no cover - GPU path
    """Enable isaacsim.ros2.bridge after SimulationApp boot (cycle 2-3)."""
    from isaacsim.core.utils.extensions import enable_extension  # noqa: PLC0415 (deferred)

    if not enable_extension("isaacsim.ros2.bridge"):
        raise RuntimeError("could not enable isaacsim.ros2.bridge")
    simulation_app.update()
