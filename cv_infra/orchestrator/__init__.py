"""Orchestrator package (M3): fan-out, resource-aware scheduling, job state, rollup."""

from cv_infra.orchestrator.fake_runner import FakeRunner, Runner
from cv_infra.orchestrator.fanout import fan_out
from cv_infra.orchestrator.models import (
    Job,
    JobResult,
    JobState,
    RequestRollup,
    Verdict,
)
from cv_infra.orchestrator.rollup import roll_up
from cv_infra.orchestrator.scheduler import (
    IllegalTransitionError,
    Scheduler,
    transition,
)

__all__ = [
    "FakeRunner",
    "IllegalTransitionError",
    "Job",
    "JobOutcome",
    "JobResult",
    "JobState",
    "RequestRollup",
    "Runner",
    "Scheduler",
    "Verdict",
    "fan_out",
    "roll_up",
    "run_job",
    "transition",
]

# supervisor exports are LAZY: the module is docker-free at import time (lazy SDK
# import inside run_job), but keeping it out of the package's eager imports
# guarantees `import cv_infra.orchestrator` never pulls the docker SDK indirectly —
# the runner image installs the wheel --no-deps, so docker is absent there
# (DoD-P2-12 regression guard). Canonical import stays
# `from cv_infra.orchestrator.supervisor import run_job, JobOutcome` (D-2 seam pin).
_SUPERVISOR_EXPORTS = ("JobOutcome", "run_job")


def __getattr__(name: str):
    if name in _SUPERVISOR_EXPORTS:
        from cv_infra.orchestrator import supervisor

        return getattr(supervisor, name)
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
