"""CPU unit tests for the FU-14 runner-side boot glue (ros_bridge bootstrap).

Pins the ownership split (cycle-5 PM ruling): supervisor-injected
ROS_DISTRO/RMW_IMPLEMENTATION are honored untouched; absent keys default from
adapter_config; the image-internal jazzy paths (measured 2026-07-08 layout:
``<root>/exts/isaacsim.ros2.bridge/jazzy/{lib,rclpy}``) are ensured idempotently.
Real bridge startup (no internal-fallback noise) is T3 workstation evidence.
"""

from cv_infra.runner import ros_bridge


def _fake_isaac_root(tmp_path, ext_parent="exts", ext_name="isaacsim.ros2.bridge"):
    jazzy = tmp_path / ext_parent / ext_name / "jazzy"
    (jazzy / "lib").mkdir(parents=True)
    (jazzy / "rclpy").mkdir()
    return tmp_path, jazzy


# --------------------------------------------------------------------------- #
# Bundled jazzy discovery (image-internal knowledge).
# --------------------------------------------------------------------------- #
def test_find_jazzy_root_in_exts(tmp_path):
    root, jazzy = _fake_isaac_root(tmp_path)
    assert ros_bridge.find_jazzy_root(roots=(str(root),), env={}) == jazzy


def test_find_jazzy_root_matches_extscache_and_versioned_names(tmp_path):
    root, jazzy = _fake_isaac_root(tmp_path, "extscache", "isaacsim.ros2.bridge-2.3.1")
    assert ros_bridge.find_jazzy_root(roots=(str(root),), env={}) == jazzy


def test_find_jazzy_root_isaac_path_env_wins(tmp_path):
    root_a, _ = _fake_isaac_root(tmp_path / "a")
    root_b, jazzy_b = _fake_isaac_root(tmp_path / "b")
    env = {"ISAAC_PATH": str(root_b)}
    assert ros_bridge.find_jazzy_root(roots=(str(root_a),), env=env) == jazzy_b


def test_find_jazzy_root_requires_lib_dir(tmp_path):
    jazzy = tmp_path / "exts" / "isaacsim.ros2.bridge" / "jazzy"
    jazzy.mkdir(parents=True)  # no lib/ inside
    assert ros_bridge.find_jazzy_root(roots=(str(tmp_path),), env={}) is None


# --------------------------------------------------------------------------- #
# Bootstrap precedence + idempotence (FU-14 ownership split).
# --------------------------------------------------------------------------- #
def test_bootstrap_defaults_absent_keys_from_adapter_config(tmp_path):
    root, jazzy = _fake_isaac_root(tmp_path)
    env: dict = {}
    path: list = []
    report = ros_bridge.bootstrap_bridge_env(
        "jazzy", "rmw_fastrtps_cpp", env=env, sys_path=path, roots=(str(root),)
    )
    assert env["ROS_DISTRO"] == "jazzy"
    assert env["RMW_IMPLEMENTATION"] == "rmw_fastrtps_cpp"
    assert report.ros_distro_defaulted and report.rmw_defaulted
    assert env["LD_LIBRARY_PATH"].startswith(str(jazzy / "lib"))
    assert path[0] == str(jazzy / "rclpy")
    assert report.jazzy_root == str(jazzy)


def test_bootstrap_honors_supervisor_injected_keys(tmp_path):
    root, _ = _fake_isaac_root(tmp_path)
    env = {"ROS_DISTRO": "humble", "RMW_IMPLEMENTATION": "rmw_cyclonedds_cpp"}
    report = ros_bridge.bootstrap_bridge_env(
        "jazzy", "rmw_fastrtps_cpp", env=env, sys_path=[], roots=(str(root),)
    )
    # Supervisor-owned keys are honored, never reassigned (LOCKED §5 direction).
    assert env["ROS_DISTRO"] == "humble"
    assert env["RMW_IMPLEMENTATION"] == "rmw_cyclonedds_cpp"
    assert not report.ros_distro_defaulted and not report.rmw_defaulted


def test_bootstrap_is_idempotent(tmp_path):
    root, jazzy = _fake_isaac_root(tmp_path)
    env: dict = {"LD_LIBRARY_PATH": "/existing"}
    path: list = []
    ros_bridge.bootstrap_bridge_env("jazzy", "rmw", env=env, sys_path=path, roots=(str(root),))
    first_ld = env["LD_LIBRARY_PATH"]
    report = ros_bridge.bootstrap_bridge_env(
        "jazzy", "rmw", env=env, sys_path=path, roots=(str(root),)
    )
    assert env["LD_LIBRARY_PATH"] == first_ld  # marker present -> no re-prepend
    assert path.count(str(jazzy / "rclpy")) == 1
    assert not report.ld_path_prepended and not report.rclpy_site_added
    assert first_ld == f"{jazzy / 'lib'}:/existing"  # original tail preserved


def test_bootstrap_without_jazzy_root_still_defaults_env():
    env: dict = {}
    report = ros_bridge.bootstrap_bridge_env(
        "jazzy", "rmw_fastrtps_cpp", env=env, sys_path=[], roots=("/nonexistent-root",)
    )
    assert report.jazzy_root is None
    assert env["ROS_DISTRO"] == "jazzy"  # bridge may still fall back internally (FU-14)
    assert "LD_LIBRARY_PATH" not in env
