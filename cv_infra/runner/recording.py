"""Recording hooks: rosbag2 (MCAP) + mp4 (M2, REQ-EXEC-008/009/014).

Two *distinct* recorders (M2 §2.2 note): the MCAP rosbag captures quantitative
telemetry (/clock + nav topics, sim-time keyed — REQ-EXEC-008); the mp4 captures the
off-screen render for visual review (REQ-EXEC-009/014). Both are wired in cycle 4;
here we fix only the artifact *layout* under RESULT_OUT so ``main`` can attach uris
to the Result. Artifact-path planning is CPU-testable; the capture bodies are stubs.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

BAG_NAME = "telemetry.mcap"
VIDEO_NAME = "recording.mp4"


@dataclass(frozen=True)
class ArtifactPaths:
    """Where the two recording artifacts will be written (under RESULT_OUT dir)."""

    bag_mcap: Path
    video_mp4: Path


def plan_artifacts(out_dir: str | Path) -> ArtifactPaths:
    """Compute artifact paths under the output dir — CPU-testable, no I/O."""
    out = Path(out_dir)
    return ArtifactPaths(bag_mcap=out / BAG_NAME, video_mp4=out / VIDEO_NAME)


class RosbagRecorder:
    """rosbag2 MCAP writer (/clock always included, sim-time keyed) — cycle 4."""

    def __init__(self, paths: ArtifactPaths) -> None:
        self.paths = paths

    def start(self) -> None:  # pragma: no cover - GPU/ROS path
        raise NotImplementedError("rosbag2 MCAP recording wired in cycle 4")

    def stop(self) -> Path:  # pragma: no cover - GPU/ROS path
        raise NotImplementedError("rosbag2 MCAP recording wired in cycle 4")


class VideoRecorder:
    """Off-screen render-product -> mp4 (CPU ffmpeg encode, D-O) — cycle 4."""

    def __init__(self, paths: ArtifactPaths) -> None:
        self.paths = paths

    def start(self) -> None:  # pragma: no cover - GPU path
        raise NotImplementedError("mp4 render capture wired in cycle 4")

    def stop(self) -> Path:  # pragma: no cover - GPU path
        raise NotImplementedError("mp4 render capture wired in cycle 4")
