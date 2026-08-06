"""CPU unit tests for recording planning + backend seams (REQ-EXEC-008/009/014).

The capture bodies are GPU/backend-bound (T3); here we pin the pure planning
(artifact layout, bag topic set, record argv, capture cadence) and the LOUD
unavailability behavior of the MCAP seam (backend routing pends the M5 decision
in questions/runner-2026-07-08-mcap-recorder-routing.md).
"""

from pathlib import Path

import pytest

from cv_infra.contract.adapter_schema import Ros2AdapterConfig
from cv_infra.runner import recording


def _cfg() -> Ros2AdapterConfig:
    return Ros2AdapterConfig.model_validate(
        {"odom_topics": ["/odom", "/chassis/odom"]}  # measured dualization (cycle-3)
    )


# --------------------------------------------------------------------------- #
# Artifact layout under RESULT_OUT.
# --------------------------------------------------------------------------- #
def test_plan_artifacts_layout(tmp_path):
    plan = recording.plan_artifacts(tmp_path)
    assert plan.bag_dir == tmp_path / "bag"  # rosbag2 output DIR (mcap inside)
    assert plan.video_mp4 == tmp_path / "recording.mp4"


# --------------------------------------------------------------------------- #
# Bag planning: /clock always first, nav streams, dedupe (REQ-EXEC-008).
# --------------------------------------------------------------------------- #
def test_bag_topics_clock_plus_nav_streams():
    topics = recording.bag_topics(_cfg())
    assert topics[0] == "/clock"  # sim-time keying is non-negotiable
    assert topics == ["/clock", "/odom", "/chassis/odom", "/cmd_vel"]


def test_bag_topics_dedupes_preserving_order():
    cfg = Ros2AdapterConfig.model_validate({"odom_topics": ["/odom", "/odom"]})
    assert recording.bag_topics(cfg) == ["/clock", "/odom", "/cmd_vel"]


# --------------------------------------------------------------------------- #
# Opt-in sensor capture (p5c12: the determinism probe needs the sensor stream in
# the bag — history 2026-08-05 follow-up ① 선결). Default stays Phase-2 exact.
# --------------------------------------------------------------------------- #
def _cfg_with_sensors(topic: str = "/front_3d_lidar/lidar_points") -> Ros2AdapterConfig:
    return Ros2AdapterConfig.model_validate(
        {
            "odom_topics": ["/odom", "/chassis/odom"],
            "sensors": [{"topic": topic, "type": "sensor_msgs/msg/PointCloud2"}],
        }
    )


def test_declared_sensors_are_excluded_by_default():
    # G-59: the input DECLARES a sensor, so the default-off branch is armed —
    # a config without sensors would pass whatever the code did.
    assert recording.bag_topics(_cfg_with_sensors()) == [
        "/clock",
        "/odom",
        "/chassis/odom",
        "/cmd_vel",
    ]


def test_opt_in_appends_the_declared_sensor_topics():
    cfg = _cfg_with_sensors("/some_other_lidar/points")  # NOT a house literal
    topics = recording.bag_topics(cfg, include_sensors=True)
    assert topics[0] == "/clock"  # sim-time keying still first
    assert topics[-1] == "/some_other_lidar/points"  # derived from adapter_config
    assert recording.bag_topics(cfg) == topics[:-1]  # opt-in is the ONLY difference


def test_opt_in_dedupes_a_sensor_that_is_already_a_nav_stream():
    cfg = Ros2AdapterConfig.model_validate(
        {
            "odom_topics": ["/odom"],
            "sensors": [{"topic": "/odom", "type": "nav_msgs/msg/Odometry"}],
        }
    )
    assert recording.bag_topics(cfg, include_sensors=True) == ["/clock", "/odom", "/cmd_vel"]


@pytest.mark.parametrize(
    ("env", "expected"),
    [
        ({}, False),  # production default: nav streams only
        ({recording.BAG_SENSORS_ENV: ""}, False),  # set-but-empty = unset (G-26)
        ({recording.BAG_SENSORS_ENV: "1"}, True),
        ({recording.BAG_SENSORS_ENV: "yes"}, True),
    ],
)
def test_bag_sensors_requested_reads_the_opt_in_env(env, expected):
    assert recording.bag_sensors_requested(env) is expected


def test_recorder_resolves_the_opt_in_env_at_construction(tmp_path, monkeypatch):
    paths = recording.plan_artifacts(tmp_path)
    monkeypatch.delenv(recording.BAG_SENSORS_ENV, raising=False)
    assert recording.RosbagRecorder(paths, _cfg_with_sensors()).include_sensors is False
    monkeypatch.setenv(recording.BAG_SENSORS_ENV, "1")
    assert recording.RosbagRecorder(paths, _cfg_with_sensors()).include_sensors is True
    # An explicit argument still wins over the env (measurement harnesses).
    assert (
        recording.RosbagRecorder(paths, _cfg_with_sensors(), include_sensors=False).include_sensors
        is False
    )


def test_bag_record_cmd_is_mcap_storage():
    cmd = recording.bag_record_cmd(Path("/out/bag"), ["/clock", "/odom"])
    assert cmd[:3] == ["ros2", "bag", "record"]
    assert ("--storage", "mcap") == (cmd[3], cmd[4])
    assert ("--output", "/out/bag") == (cmd[5], cmd[6])
    assert cmd[7:] == ["/clock", "/odom"]


# --------------------------------------------------------------------------- #
# mp4 capture cadence (low-fps window, D-O).
# --------------------------------------------------------------------------- #
def test_capture_stride():
    assert recording.capture_stride(60.0, 10.0) == 6
    assert recording.capture_stride(60.0, 60.0) == 1
    assert recording.capture_stride(30.0, 60.0) == 1  # never below every-step
    with pytest.raises(ValueError):
        recording.capture_stride(60.0, 0.0)


# --------------------------------------------------------------------------- #
# MCAP backend glue (M5 option-A apt layer; measured §6-1 constraints).
# --------------------------------------------------------------------------- #
def test_ros_setup_script_is_distro_derived():
    # ros_distro travels via adapter_config — never a hardcoded jazzy literal.
    assert recording.ros_setup_script("jazzy") == Path("/opt/ros/jazzy/setup.bash")
    assert recording.ros_setup_script("humble") == Path("/opt/ros/humble/setup.bash")


def test_bag_record_shell_cmd_sources_then_execs():
    cmd = recording.bag_record_shell_cmd(
        Path("/out/bag"), ["/clock", "/odom"], Path("/opt/ros/jazzy/setup.bash")
    )
    assert cmd[:2] == ["bash", "-c"]
    # measured M5 §6-1: bare ros2 is not executable — MUST source the apt env;
    # exec so SIGINT lands on rosbag2 itself (clean close), not a bash parent.
    assert cmd[2].startswith("source /opt/ros/jazzy/setup.bash && exec ros2 bag record ")
    assert cmd[2].endswith("--output /out/bag /clock /odom")


def test_recorder_subprocess_env_strips_bundled_interpreter_keys():
    base = {
        "PYTHONPATH": "/isaac-sim/site",  # python.sh export — poisons py3.12 CLI
        "LD_LIBRARY_PATH": "/isaac-sim/exts/isaacsim.ros2.bridge/jazzy/lib",
        "PYTHONHOME": "/isaac-sim/kit/python",
        "ROS_DOMAIN_ID": "7",  # DDS join keys pass through untouched
        "RMW_IMPLEMENTATION": "rmw_fastrtps_cpp",
        "ROS_DISTRO": "jazzy",
        "HOME": "/isaac-sim",
    }
    env = recording.recorder_subprocess_env(base)
    assert "PYTHONPATH" not in env and "LD_LIBRARY_PATH" not in env
    assert "PYTHONHOME" not in env
    assert env["ROS_DOMAIN_ID"] == "7"
    assert env["RMW_IMPLEMENTATION"] == "rmw_fastrtps_cpp"
    assert env["ROS_DISTRO"] == "jazzy"
    assert env["HOME"] == "/isaac-sim"
    assert base["PYTHONPATH"] == "/isaac-sim/site"  # input mapping not mutated


def test_rosbag_recorder_unavailable_without_backend(tmp_path, monkeypatch):
    # Deterministic on any host: point the availability probe at a missing file.
    monkeypatch.setattr(
        recording, "ros_setup_script", lambda d: tmp_path / "no-such" / "setup.bash"
    )
    recorder = recording.RosbagRecorder(recording.plan_artifacts(tmp_path), _cfg())
    with pytest.raises(recording.RecorderUnavailable) as excinfo:
        recorder.start()
    assert "rosbag2-layer" in str(excinfo.value)  # actionable pointer (M5 layer)


def test_rosbag_recorder_abort_is_idempotent_cpu_safe(tmp_path):
    recorder = recording.RosbagRecorder(recording.plan_artifacts(tmp_path), _cfg())
    recorder.abort()
    recorder.abort()  # no proc/log yet -> no-op both times


def test_video_recorder_capture_before_start_is_noop(tmp_path):
    recorder = recording.VideoRecorder(recording.plan_artifacts(tmp_path))
    recorder.capture_frame()  # writer not started (CPU) -> silently skips
    recorder.abort()
    assert recorder.stride == 6  # 60 sim fps -> 10 video fps default


def test_recorder_unavailable_is_runtime_error():
    assert issubclass(recording.RecorderUnavailable, RuntimeError)
