"""Runner-plane SUT adapters (M2). DDS-only; SUT spawn/teardown is M3 (D-2/D-D)."""

from cv_infra.runner.adapter.base import SimAdapter
from cv_infra.runner.adapter.ros2 import Ros2Adapter

__all__ = ["SimAdapter", "Ros2Adapter"]
