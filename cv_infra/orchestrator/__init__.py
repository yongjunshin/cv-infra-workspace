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
    "JobResult",
    "JobState",
    "RequestRollup",
    "Runner",
    "Scheduler",
    "Verdict",
    "fan_out",
    "roll_up",
    "transition",
]
