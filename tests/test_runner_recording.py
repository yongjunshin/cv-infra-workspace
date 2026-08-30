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


class _FakeWriter:
    """A cv2.VideoWriter-shaped stand-in (the only per-sample allocation)."""

    def __init__(self) -> None:
        self.released = False

    def release(self) -> None:
        self.released = True


def _loop_recorder(monkeypatch, writers: list):
    recorder = recording.LoopVideoRecorder()
    monkeypatch.setattr(recorder, "_open_writer", lambda path: writers.pop(0))
    return recorder


def test_loop_recorder_shares_the_single_job_frame_policy():
    """Camera/resolution/fps/stride are IMPORTED, not restated (G-25): two
    recorders of the same mission must not produce two different videos."""
    recorder = recording.LoopVideoRecorder()
    assert recorder.camera_path == recording.DEFAULT_CAMERA_PATH
    assert recorder.resolution == recording.DEFAULT_RESOLUTION
    assert recorder.video_fps == recording.DEFAULT_VIDEO_FPS
    assert recorder.stride == recording.capture_stride(60.0, recording.DEFAULT_VIDEO_FPS)


def test_loop_recorder_returns_none_when_the_sample_captured_no_frames(tmp_path, monkeypatch):
    """A 0-frame mp4 is unplayable, so the artifact field must say "no video"
    rather than point at one (P2-02: recording degrades loudly, never silently)."""
    writer = _FakeWriter()
    recorder = _loop_recorder(monkeypatch, [writer])
    recorder.begin_iteration(tmp_path / "results" / "0" / "recording.mp4")
    assert recorder.end_iteration() is None
    assert writer.released is True  # the writer is closed either way


def test_loop_recorder_returns_the_path_when_frames_were_written(tmp_path, monkeypatch):
    target = tmp_path / "results" / "1" / "recording.mp4"
    recorder = _loop_recorder(monkeypatch, [_FakeWriter()])
    recorder.begin_iteration(target)
    recorder.last_frame_count = 42  # what capture_frame does on GPU
    assert recorder.end_iteration() == target
    assert target.parent.is_dir()  # the sample's out-dir was created


def test_loop_recorder_cycles_only_the_writer_between_samples(tmp_path, monkeypatch):
    """The render product is created ONCE per carrier (a per-sample one would
    re-add the VRAM growth term p6c2 removed); only the writer is cycled."""
    first, second = _FakeWriter(), _FakeWriter()
    recorder = _loop_recorder(monkeypatch, [first, second])
    recorder.begin_iteration(tmp_path / "0.mp4")
    recorder.last_frame_count = 5
    recorder.end_iteration()
    recorder.begin_iteration(tmp_path / "1.mp4")
    assert recorder.last_frame_count == 0  # counters reset per sample
    assert first.released and not second.released
    assert recorder._annotator is None  # never re-created by begin_iteration


def test_loop_recorder_capture_between_samples_is_a_noop(tmp_path):
    """The hook stays registered on SimRuntime for the carrier's whole life, so
    frames produced by a restage/realign belong to no sample and are dropped."""
    recorder = recording.LoopVideoRecorder()
    recorder.capture_frame()  # no writer -> silently skips
    recorder.abort()
    recorder.abort()  # idempotent
    assert recorder.end_iteration() is None


def test_recorder_unavailable_is_runtime_error():
    assert issubclass(recording.RecorderUnavailable, RuntimeError)


# --------------------------------------------------------------------------- #
# p8c2 — start()'s glue past the availability probe, and the two abort paths.
#
# The backend itself stays absent on CPU (that is what the pragma on the Popen
# call says); what is pinned here is everything start() DECIDES before handing
# over: the two measured M5 §6-1 constraints (sourced-env argv, interpreter-key
# stripping) and the G-26/G-18 evidence it leaves behind.
# --------------------------------------------------------------------------- #
class _FakePopen:
    """Records the child the recorder would have spawned."""

    calls: list = []

    def __init__(self, args, stdout=None, stderr=None, env=None) -> None:
        self.args = args
        self.stdout = stdout
        self.env = env
        self.killed = 0
        _FakePopen.calls.append(self)

    def kill(self) -> None:
        self.killed += 1


def _fake_backend(monkeypatch, tmp_path):
    """Make the availability probe find a setup script, and stub out the spawn."""
    setup = tmp_path / "opt" / "ros" / "jazzy" / "setup.bash"
    setup.parent.mkdir(parents=True)
    setup.write_text("# a sourced env script\n", encoding="utf-8")
    monkeypatch.setattr(recording, "ros_setup_script", lambda distro: setup)
    _FakePopen.calls = []
    monkeypatch.setattr(recording.subprocess, "Popen", _FakePopen)
    return setup


def test_rosbag_start_spawns_the_sourced_child_and_says_what_it_records(
    tmp_path, monkeypatch, capsys
):
    monkeypatch.setenv("PYTHONPATH", "/isaac-sim/site")  # the bundled-interpreter key
    monkeypatch.setenv("ROS_DOMAIN_ID", "11")  # the DDS join key
    setup = _fake_backend(monkeypatch, tmp_path)
    paths = recording.plan_artifacts(tmp_path / "out")
    config = _cfg()

    recording.RosbagRecorder(paths, config).start()

    topics = recording.bag_topics(config, include_sensors=False)
    # G-26 feature-on gate: what it records is what it SAYS it records.
    assert capsys.readouterr().out == f"[cv-runner] bag topics ({len(topics)}): {topics}\n"
    child = _FakePopen.calls[-1]
    # M5 §6-1 constraint 1: bare ``ros2`` is not executable -> source, then exec.
    assert child.args == recording.bag_record_shell_cmd(paths.bag_dir, topics, setup)
    # M5 §6-1 constraint 2: the py3.11 bundle's keys would poison the py3.12 CLI,
    # while the DDS keys must pass through or the child joins the wrong domain.
    assert "PYTHONPATH" not in child.env
    assert child.env["ROS_DOMAIN_ID"] == "11"
    # G-18 evidence culture: the recorder's own output is kept as a file.
    assert (paths.bag_dir.parent / "rosbag2.log").is_file()
    assert child.stdout is not None


def test_rosbag_abort_closes_the_log_file_and_stays_idempotent(tmp_path, monkeypatch):
    """``main``/``batch`` call ``abort`` from a ``finally`` on failure paths, and
    a leaked open file (or a second close raising) would turn a mission failure
    into a teardown failure."""
    _fake_backend(monkeypatch, tmp_path)
    paths = recording.plan_artifacts(tmp_path / "out")
    recorder = recording.RosbagRecorder(paths, _cfg())
    recorder.start()

    recorder.abort()
    recorder.abort()

    assert recorder._log_file is None
    assert recorder._proc is None
    assert _FakePopen.calls[-1].killed == 1  # killed once, not twice, never leaked


def test_loop_recorder_abort_releases_the_open_writer(tmp_path, monkeypatch):
    """Mid-sample failure: the writer allocated by ``begin_iteration`` is the only
    per-sample resource, so aborting must release it — a leaked cv2 writer holds
    the mp4 open and the NEXT sample's iteration inherits the leak."""
    writer = _FakeWriter()
    recorder = _loop_recorder(monkeypatch, [writer])
    recorder.begin_iteration(tmp_path / "results" / "0" / "recording.mp4")

    recorder.abort()
    recorder.abort()  # idempotent: teardown may run twice on a failure path

    assert writer.released is True
    assert recorder._writer is None
