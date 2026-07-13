"""Orchestrator package (M3): fan-out, resource-aware scheduling, job state, rollup."""

from cv_infra.orchestrator.allocator import (
    LABEL_JOB_ID,
    LABEL_ROS_DOMAIN_ID,
    ROS_DOMAIN_ID_SPACE,
    DomainIdAllocator,
    allocate_ros_domain_id,
    network_name_for,
)
from cv_infra.orchestrator.fake_runner import FakeRunner, Runner
from cv_infra.orchestrator.fanout import fan_out
from cv_infra.orchestrator.models import (
    Job,
    JobResult,
    JobState,
    RequestRollup,
    Verdict,
)
from cv_infra.orchestrator.queue import IllegalTransitionError, JobQueue, transition
from cv_infra.orchestrator.rollup import roll_up
from cv_infra.orchestrator.scheduler import (
    PynvmlVramGauge,
    Scheduler,
    SlotAccountant,
    VramGauge,
    compute_k,
)
from cv_infra.orchestrator.store import Store, job_key

__all__ = [
    "LABEL_JOB_ID",
    "LABEL_ROS_DOMAIN_ID",
    "ROS_DOMAIN_ID_SPACE",
    "DomainIdAllocator",
    "FakeRunner",
    "IllegalTransitionError",
    "Job",
    "JobOutcome",
    "JobQueue",
    "JobResult",
    "JobState",
    "ParallelSupervisor",
    "PynvmlVramGauge",
    "RequestRollup",
    "Runner",
    "Scheduler",
    "SlotAccountant",
    "Store",
    "Verdict",
    "VramGauge",
    "allocate_ros_domain_id",
    "compute_k",
    "fan_out",
    "job_key",
    "network_name_for",
    "roll_up",
    "run_job",
    "transition",
]

# supervisor exports are LAZY: the module is docker-free at import time (lazy SDK
# import inside run_job), but keeping it out of the package's eager imports
# guarantees `import cv_infra.orchestrator` never pulls the docker SDK indirectly —
# the runner image installs the wheel --no-deps, so docker is absent there
# (DoD-P2-12 regression guard). Canonical import stays
# `from cv_infra.orchestrator.supervisor import run_job, JobOutcome` (D-2 seam pin);
# ParallelSupervisor (Phase 4) follows the same discipline.
_SUPERVISOR_EXPORTS = ("JobOutcome", "ParallelSupervisor", "run_job")


def __getattr__(name: str):
    if name in _SUPERVISOR_EXPORTS:
        from cv_infra.orchestrator import supervisor

        return getattr(supervisor, name)
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
