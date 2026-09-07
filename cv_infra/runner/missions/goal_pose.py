"""``goal_pose`` — the built-in driver: send one pose, wait for arrival.

This is the behaviour every pre-v2 request got, unchanged and still the default.
It reads its pose from ``mission.params`` when the request declares a mission
block, and otherwise from ``scenario.goal``, so a document that never heard of
missions keeps working byte-for-byte.

The actual publish/wait lives with the ROS adapter (that is where the action
client and the sim clock are); this class is the thin binding that names it as
one driver among possible others, which is what makes a second driver a config
change rather than a platform change.
"""

from __future__ import annotations

from typing import Any

from cv_infra.runner.mission import MissionDriver


class GoalPoseMission(MissionDriver):
    """Drive to a single pose through the adapter's declared goal interface."""

    def start(self, handle: Any) -> None:
        self._handle = handle
        handle.send_goal(self.pose(handle))

    def poll(self, handle: Any) -> bool:
        return bool(handle.goal_settled())

    def stop(self, handle: Any) -> None:
        handle.cancel_goal()

    def pose(self, handle: Any) -> dict[str, float]:
        """The declared pose — ``mission.params`` wins, ``scenario.goal`` is the
        pre-v2 source. Never both silently: params are only consulted when the
        request actually wrote a mission block."""
        if self.params:
            return {k: float(v) for k, v in self.params.items() if k in ("x", "y", "yaw")}
        return handle.scenario_goal()
