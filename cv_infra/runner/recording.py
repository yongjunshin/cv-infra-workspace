"""Recording hooks: rosbag2 (MCAP) + mp4 (M2, REQ-EXEC-008/009/014).

Two *distinct* recorders (M2 §2.2 note): the MCAP rosbag captures quantitative
telemetry (/clock + nav topics, sim-time keyed — REQ-EXEC-008); the mp4 captures
the off-screen render for visual review (REQ-EXEC-009/014).

mp4 path (measured 2026-07-08 on ``cv-infra-runner:p2``): the bundled interpreter
ships ``cv2`` 4.11.0 -> frames come from an ``omni.replicator`` rgb annotator and
are CPU-encoded by ``cv2.VideoWriter`` (D-O: no NVENC contention, no ffmpeg
binary needed — none is in the image).

MCAP path (measured 2026-07-08): the runner image has NO rosbag2 in ANY form
(no ``rosbag2_py``, no mcap libs in the vendored jazzy ext) — backend routing is
an M5 image decision pending in
``questions/runner-2026-07-08-mcap-recorder-routing.md``. ``RosbagRecorder`` is
authored as the recommended route-A candidate (``ros2 bag record`` subprocess —
genuine rosbag2 reuse) behind a runtime availability check that raises
``RecorderUnavailable`` LOUDLY; ``main`` degrades gracefully (artifact stays
None + stderr warning) so the pass-verdict E2E (P2-01) is never hostage to the
pending decision (cycle plan's honest-carryover clause covers P2-02).
"""

from __future__ import annotations

import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path

from cv_infra.adapter.adapter_schema import Ros2AdapterConfig

BAG_DIR_NAME = "bag"
VIDEO_NAME = "recording.mp4"

# mp4 capture defaults (module policy, not consumer contract — the camera an
# operator reviews is not scenario wiring; revisit at the P3 recording
# formalization if consumers need to pick a camera).
DEFAULT_CAMERA_PATH = "/OmniverseKit_Persp"
DEFAULT_RESOLUTION = (1280, 720)
DEFAULT_VIDEO_FPS = 10.0


class RecorderUnavailable(RuntimeError):
    """A recording backend is not present in the runner image (loud, actionable)."""


@dataclass(frozen=True)
class ArtifactPaths:
    """Where the two recording artifacts land (under the RESULT_OUT dir)."""

    bag_dir: Path  # rosbag2 output dir (contains <name>.mcap + metadata.yaml)
    video_mp4: Path


def plan_artifacts(out_dir: str | Path) -> ArtifactPaths:
    """Compute artifact paths under the output dir — CPU-testable, no I/O."""
    out = Path(out_dir)
    return ArtifactPaths(bag_dir=out / BAG_DIR_NAME, video_mp4=out / VIDEO_NAME)


# --------------------------------------------------------------------------- #
# Pure planning helpers — CPU unit-test surface.
# --------------------------------------------------------------------------- #
def bag_topics(config: Ros2AdapterConfig) -> list[str]:
    """Topics the MCAP bag records: /clock (always — sim-time keying) + the nav
    streams (odom fan-out + cmd_vel). Sensor topics (PointCloud2 etc.) are
    deliberately excluded in Phase 2 (artifact size; R12)."""
    topics = [config.clock_topic, *config.odom_topics, config.cmd_vel.topic]
    return list(dict.fromkeys(topics))  # dedupe, order-preserving


def bag_record_cmd(bag_dir: Path, topics: list[str]) -> list[str]:
    """``ros2 bag record`` argv (storage=mcap) — route-A candidate glue."""
    return ["ros2", "bag", "record", "--storage", "mcap", "--output", str(bag_dir), *topics]


def capture_stride(sim_fps: float, video_fps: float) -> int:
    """Capture every Nth sim step for the target video fps (>=1)."""
    if video_fps <= 0:
        raise ValueError("video_fps must be > 0")
    return max(1, round(sim_fps / video_fps))


class RosbagRecorder:
    """rosbag2 MCAP writer (/clock always included, sim-time keyed).

    Route-A candidate: a ``ros2 bag record`` child process in the runner
    container (same DDS domain via the honored env). The backend is ABSENT from
    the current image (measured) — ``start`` raises ``RecorderUnavailable``
    until the M5 routing decision lands; main treats that as a loud, non-fatal
    degradation (mcap=None in the Result).
    """

    def __init__(self, paths: ArtifactPaths, config: Ros2AdapterConfig | None = None) -> None:
        self.paths = paths
        self.config = config if config is not None else Ros2AdapterConfig()
        self._proc: subprocess.Popen | None = None
        self._log_file = None

    def start(self) -> None:
        if shutil.which("ros2") is None:
            raise RecorderUnavailable(
                "no rosbag2 backend in the runner image (measured 2026-07-08: no "
                "ros2 CLI / rosbag2_py / mcap libs) — MCAP recording waits on the "
                "M5 image routing decision "
                "(questions/runner-2026-07-08-mcap-recorder-routing.md)"
            )
        self.paths.bag_dir.parent.mkdir(parents=True, exist_ok=True)
        # G-18 evidence culture: keep the recorder's own output as a file.
        self._log_file = open(  # noqa: SIM115 (lifetime spans the recording)
            self.paths.bag_dir.parent / "rosbag2.log", "w", encoding="utf-8"
        )
        self._proc = subprocess.Popen(  # pragma: no cover - needs the backend
            bag_record_cmd(self.paths.bag_dir, bag_topics(self.config)),
            stdout=self._log_file,
            stderr=subprocess.STDOUT,
        )

    def stop(self) -> Path:  # pragma: no cover - needs the backend
        """SIGINT for a clean rosbag2 close; return the produced .mcap path."""
        if self._proc is not None:
            import signal  # noqa: PLC0415

            self._proc.send_signal(signal.SIGINT)
            try:
                self._proc.wait(timeout=15.0)
            except subprocess.TimeoutExpired:
                self._proc.kill()
            self._proc = None
        if self._log_file is not None:
            self._log_file.close()
            self._log_file = None
        mcaps = sorted(self.paths.bag_dir.glob("*.mcap"))
        if not mcaps:
            raise RuntimeError(f"rosbag2 produced no .mcap under {self.paths.bag_dir}")
        return mcaps[0]

    def abort(self) -> None:
        """Failure-path cleanup (idempotent): never leak the child process."""
        if self._proc is not None:  # pragma: no cover - needs the backend
            self._proc.kill()
            self._proc = None
        if self._log_file is not None:
            self._log_file.close()
            self._log_file = None


class VideoRecorder:
    """Off-screen render product -> mp4 via bundled cv2 (CPU encode, D-O).

    ``capture_frame`` is registered on the SimRuntime step listeners so frames
    accumulate during the mission step loop without the adapter knowing about
    recording. Encoding is incremental (VideoWriter) — no frame buffer growth.
    """

    def __init__(
        self,
        paths: ArtifactPaths,
        camera_path: str = DEFAULT_CAMERA_PATH,
        resolution: tuple[int, int] = DEFAULT_RESOLUTION,
        sim_fps: float = 60.0,
        video_fps: float = DEFAULT_VIDEO_FPS,
    ) -> None:
        self.paths = paths
        self.camera_path = camera_path
        self.resolution = resolution
        self.stride = capture_stride(sim_fps, video_fps)
        self.video_fps = video_fps
        self._annotator = None
        self._writer = None
        self._step_count = 0
        self._frames_written = 0

    def start(self) -> None:  # pragma: no cover - GPU path (T3)
        import cv2  # noqa: PLC0415 (bundled, measured 4.11.0)
        import omni.replicator.core as rep  # noqa: PLC0415

        render_product = rep.create.render_product(self.camera_path, self.resolution)
        self._annotator = rep.AnnotatorRegistry.get_annotator("rgb")
        self._annotator.attach(render_product)

        self.paths.video_mp4.parent.mkdir(parents=True, exist_ok=True)
        self._writer = cv2.VideoWriter(
            str(self.paths.video_mp4),
            cv2.VideoWriter_fourcc(*"mp4v"),
            self.video_fps,
            self.resolution,
        )

    def capture_frame(self) -> None:  # pragma: no cover - GPU path (T3)
        """Step listener: encode every Nth rendered frame (RGBA -> BGR)."""
        if self._writer is None:
            return
        self._step_count += 1
        if self._step_count % self.stride:
            return
        import numpy as np  # noqa: PLC0415 (legal post-SimulationApp)

        data = self._annotator.get_data()
        if data is None or getattr(data, "size", 0) == 0:
            return  # renderer warm-up: no frame yet
        frame = np.asarray(data)
        if frame.ndim != 3 or frame.shape[2] < 3:
            return
        bgr = np.ascontiguousarray(frame[:, :, 2::-1])  # RGBA/RGB -> BGR
        self._writer.write(bgr)
        self._frames_written += 1

    def stop(self) -> Path:  # pragma: no cover - GPU path (T3)
        if self._writer is not None:
            self._writer.release()
            self._writer = None
        self._annotator = None
        if self._frames_written == 0:
            raise RuntimeError(
                "video recorder wrote 0 frames — render product/annotator produced "
                f"no data for camera {self.camera_path!r} (T3: verify the headless "
                "default camera and replicator capture timing)"
            )
        return self.paths.video_mp4

    def abort(self) -> None:
        """Failure-path cleanup (idempotent): release the writer without raising."""
        if self._writer is not None:  # pragma: no cover - GPU path
            self._writer.release()
            self._writer = None
        self._annotator = None
