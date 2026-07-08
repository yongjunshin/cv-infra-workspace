"""CPU unit tests for recording planning + backend seams (REQ-EXEC-008/009/014).

The capture bodies are GPU/backend-bound (T3); here we pin the pure planning
(artifact layout, bag topic set, record argv, capture cadence) and the LOUD
unavailability behavior of the MCAP seam (backend routing pends the M5 decision
in questions/runner-2026-07-08-mcap-recorder-routing.md).
"""

from pathlib import Path

import pytest

from cv_infra.adapter.adapter_schema import Ros2AdapterConfig
from cv_infra.runner import recording


def _cfg() -> Ros2AdapterConfig:
    return Ros2AdapterConfig.from_dict(
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
    cfg = Ros2AdapterConfig.from_dict({"odom_topics": ["/odom", "/odom"]})
    assert recording.bag_topics(cfg) == ["/clock", "/odom", "/cmd_vel"]


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
# MCAP seam: absent backend fails LOUD with the pending-decision pointer.
# --------------------------------------------------------------------------- #
def test_rosbag_recorder_unavailable_without_backend(tmp_path, monkeypatch):
    # Deterministic: the dev host may carry a ROS overlay with a `ros2` CLI.
    monkeypatch.setattr(recording.shutil, "which", lambda _: None)
    recorder = recording.RosbagRecorder(recording.plan_artifacts(tmp_path), _cfg())
    with pytest.raises(recording.RecorderUnavailable) as excinfo:
        recorder.start()
    assert "mcap-recorder-routing" in str(excinfo.value)  # actionable pointer


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
