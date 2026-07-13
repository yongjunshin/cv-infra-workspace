"""CPU unit tests for the runner-side boot glue (ros_bridge bootstrap + R4 cap).

Pins the ownership split (cycle-5 PM ruling): supervisor-injected
ROS_DISTRO/RMW_IMPLEMENTATION are honored untouched; absent keys default from
adapter_config; the image-internal jazzy paths (measured 2026-07-08 layout:
``<root>/exts/isaacsim.ros2.bridge/jazzy/{lib,rclpy}``) are ensured idempotently.
Real bridge startup (no internal-fallback noise) is T3 workstation evidence.

R4 (p4c4 T2): the texture-streaming budget cap's launch-config assembly and the
loud boot log line are CPU-asserted here; the carb set/read-back itself is the
GPU path (Wave 2 T4 observes ``at_boot=``/``readback=`` on the workstation).
"""

from cv_infra.runner import ros_bridge, sim_runtime


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


# --------------------------------------------------------------------------- #
# LD re-exec (measured p2c5 probe-01: loader snapshots LD_LIBRARY_PATH at
# process start — in-python prepend alone leaves the bridge libs unresolvable).
# --------------------------------------------------------------------------- #
def _bootstrap(prepended: bool) -> ros_bridge.BridgeBootstrap:
    return ros_bridge.BridgeBootstrap(
        jazzy_root="/isaac-sim/exts/isaacsim.ros2.bridge/jazzy",
        ros_distro_defaulted=False,
        rmw_defaulted=False,
        ld_path_prepended=prepended,
        rclpy_site_added=True,
    )


def test_reexec_skipped_when_marker_was_already_present():
    calls: list = []
    did = ros_bridge.reexec_for_bridge_lib(
        _bootstrap(prepended=False), execv=lambda *a: calls.append(a)
    )
    assert did is False and calls == []  # no loop after the re-exec'd process


def test_reexec_fires_once_with_runner_entry_argv():
    calls: list = []
    did = ros_bridge.reexec_for_bridge_lib(
        _bootstrap(prepended=True), execv=lambda path, args: calls.append((path, args))
    )
    assert did is True and len(calls) == 1
    path, args = calls[0]
    assert path == args[0]
    assert args[1:] == ["-m", "cv_infra.runner.main"]  # runner entry preserved


def test_reexec_honors_explicit_argv():
    calls: list = []
    ros_bridge.reexec_for_bridge_lib(
        _bootstrap(prepended=True),
        argv=["/isaac-sim/kit/python/bin/python3", "/cv/probes/scene_probe.py"],
        execv=lambda path, args: calls.append((path, args)),
    )
    assert calls == [
        (
            "/isaac-sim/kit/python/bin/python3",
            ["/isaac-sim/kit/python/bin/python3", "/cv/probes/scene_probe.py"],
        )
    ]


# --------------------------------------------------------------------------- #
# R4 texture streaming budget cap (sim_runtime boot glue, p4c4 T2).
# --------------------------------------------------------------------------- #
def test_launch_config_stays_headless():
    # LOCKED §7.7 invariant preserved by the R4 change (the cap rides ALONGSIDE
    # headless, never replaces it).
    assert sim_runtime.simulation_app_launch_config()["headless"] is True


def test_launch_config_carries_texture_budget_cap_arg():
    # Full-literal pin: kit CLI settings-override form (`--/path=value` — the
    # exact form R4's verification column names) with the CANDIDATE key + the
    # R4 plan-policy 60% (0.6 fraction, NOT a measured NFR — §2-4 untouched).
    # The key is a documented-default candidate awaiting GPU confirmation
    # (Wave 2 T4) — see the anchor comment on TEXTURE_BUDGET_SETTING.
    assert sim_runtime.simulation_app_launch_config()["extra_args"] == [
        "--/rtx-transient/resourcemanager/texturestreaming/memoryBudget=0.6"
    ]


def test_texture_budget_fraction_is_r4_policy_value():
    fraction = sim_runtime.TEXTURE_BUDGET_FRACTION
    assert fraction == 0.6  # R4 plan policy (60% cap)
    assert 0.0 < fraction <= 1.0  # fraction-of-total semantics, never MB


def test_texture_budget_log_line_carries_grep_marker_and_readback():
    line = sim_runtime.texture_budget_log_line(at_boot=0.6, readback=0.6)
    # Verbatim grep gate for Wave 2 T4/QA (G-26 prove-it-ran): marker + value.
    assert "texture_budget_applied=0.6" in line
    assert line.startswith("[cv-runner] ")
    assert "at_boot=0.6" in line and "readback=0.6" in line
    assert f"key={sim_runtime.TEXTURE_BUDGET_SETTING}" in line


def test_texture_budget_log_line_none_readback_is_loud():
    # `none` = the candidate settings key does not exist in this build — the
    # loud wrong-key signal (an observation, never an echo of intent).
    line = sim_runtime.texture_budget_log_line(at_boot=None, readback=None)
    assert "at_boot=none" in line and "readback=none" in line
    assert "texture_budget_applied=0.6" in line  # requested policy still visible
