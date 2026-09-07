"""Mission drivers (M2) — how the runner drives the SUT through one case.

The platform ships ONE driver, ``goal_pose``: publish a pose, wait for arrival.
It is the default and every pre-v2 request gets it unchanged. It is also an
ASSUMPTION ABOUT THE SHAPE OF THE APPLICATION, and that assumption has a
measured cost. A patrol application's native mission interface is not "here is
one pose"; satisfying the contract cost that consumer a second, foreign action
server standing in front of its own, and its scenario document says so out loud
— the goal it declares is described there as "a verdict anchor, not a search
hint". A driver that has to be lied to is a driver in the wrong place.

So ``mission.kind`` also accepts ``module:Class``, resolved from the request's
ride-along directory exactly like a custom oracle. Judgment was already the
consumer's; this makes DRIVING the consumer's too, which is the remaining
requirement for a robot SW the platform has never seen.

The seam is deliberately tiny — ``start`` / ``poll`` / ``stop`` over a handle the
runner already holds. A driver that needs to watch the running system does it
here, because this is the only place that runs while the system is alive; the
oracle still sees files afterwards.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Any

from cv_infra.contract.errors import ContractError

#: The drivers the platform itself ships, by ``mission.kind``.
BUILTIN_MISSIONS: tuple[str, ...] = ("goal_pose",)

_DOC_LINK = "docs/user-guide.md#mission"


class MissionDriver(ABC):
    """One mission, driven for the life of one case.

    Implementations receive the declared ``params`` verbatim — the platform does
    not interpret them, the same relationship it has with a custom oracle's
    params and with a metric name.
    """

    def __init__(self, params: dict[str, Any] | None = None) -> None:
        self.params = dict(params or {})

    @abstractmethod
    def start(self, handle: Any) -> None:
        """Begin the mission. ``handle`` is the runner's live ROS/sim surface."""

    def poll(self, handle: Any) -> bool:
        """Return True when the mission is over. Default: never ends on its own,
        so the case runs to its declared sim-time budget — which is the honest
        default for a driver that does not know what 'done' means."""
        return False

    def stop(self, handle: Any) -> None:
        """Release anything ``start`` acquired. Always called, including on
        timeout and on failure, so a driver never leaks a goal handle.

        Doing nothing is a legitimate implementation (a driver that only
        publishes has nothing to release), so this is a concrete default rather
        than an abstract method a trivial driver would have to stub out."""
        return


def load_mission(kind: str, params: dict[str, Any] | None = None) -> MissionDriver:
    """Resolve ``mission.kind`` to a driver instance.

    A name with a colon is a consumer plugin (``module:Class``), imported from
    the ride-along directory the loader put on ``sys.path`` at admit — the SAME
    mechanism, and the same failure prose, as a custom oracle. A bare name must
    be one the platform ships; anything else is a rejected request rather than a
    runner that starts and then does nothing.
    """
    if ":" in kind:
        return _instantiate(_load_explicit_path(kind), kind, params)
    if kind not in BUILTIN_MISSIONS:
        raise _reject(
            kind,
            f"a built-in driver {list(BUILTIN_MISSIONS)} or an explicit "
            "'module:Class' path to your own",
        )
    from cv_infra.runner.missions.goal_pose import GoalPoseMission

    return GoalPoseMission(params)


def _load_explicit_path(name: str) -> type:
    module_name, _, class_name = name.partition(":")
    if not module_name or not class_name:
        raise _reject(name, "'module:Class' with both halves present")
    try:
        module = __import__(module_name, fromlist=["_"])
    except ImportError as exc:
        raise _reject(
            name,
            f"an importable module — {module_name!r} is not on the path the request "
            "directory provides (the mission module rides along with the request)",
        ) from exc
    try:
        return getattr(module, class_name)
    except AttributeError as exc:
        raise _reject(name, f"a class named {class_name!r} in module {module_name!r}") from exc


def _instantiate(cls: Any, name: str, params: dict[str, Any] | None) -> MissionDriver:
    if not (isinstance(cls, type) and issubclass(cls, MissionDriver)):
        raise _reject(name, f"a subclass of {MissionDriver.__module__}.MissionDriver")
    return cls(params)


def _reject(name: str, expected: str) -> ContractError:
    return ContractError(
        field_path="mission.kind",
        expected=expected,
        got=repr(name),
        example="mission:\n  kind: goal_pose\n  params: {x: -6.0, y: 5.0, yaw: 1.5708}",
        doc_link=_DOC_LINK,
    )
