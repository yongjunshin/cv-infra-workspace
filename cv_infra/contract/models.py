"""Contract data models (M1) — Phase 2 minimal shape.

Envelope / VerificationRequest / VerificationResult and their supporting types.
Phase 0 shipped import-able placeholders; Phase 2 fills the request/result shape
with the MINIMAL, NON-FROZEN fields the single-runner spine consumes (JOB_SPEC in
/ result.json out, decision 2026-07-07 D-2). The formal pydantic v2 models
(validators, apiVersion policy, JSON-schema export) are finalized in Phase 3
(modules/M1-contract-and-schema.md §3.2) — these dataclasses are stdlib-only and
deliberately un-validated (a minimal ``raise`` on a missing key is acceptable at
this phase; friendly errors are Phase 3).

These are the single definition of the contract models — consumers (M2/M3/M4)
import, never redefine. ``RequestEnvelope`` stays a Phase-4 placeholder: Phase 2
is envelope-less (REQ-INTAKE-002), so it is untouched here.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any, Literal

# Verdict values a runner may record for a job (REQ-EXEC-013). Kept as a Literal
# + tuple rather than an enum so the wire value is a plain string (matches the
# result.json field ``verdict: str``).
#
# verdict -> CLI exit-code mapping (COMMENT ONLY; contract LOCK = cycle-6 P2-07):
#   pass    -> 0
#   fail    -> 1
#   timeout -> 1   (SUT missed the sim-time budget = SUT verdict, not infra)
#   error   -> 3   (runner crash / Isaac unreached / EULA not agreed = platform, FU-8)
# (bad input, pre-sim -> exit 2 is raised on the CLI side, not carried in a Result.)
Verdict = Literal["pass", "fail", "timeout", "error"]
VERDICTS: tuple[str, ...] = ("pass", "fail", "timeout", "error")


class RequestEnvelope:
    """N>=1 VerificationRequest container (REQ-INTAKE-001).

    Phase-3 fields (placeholder): apiVersion, trigger_source (human-manual|ci-cd,
    REQ-INTAKE-003), is_self_test/origin (M7 marker), requests (list).
    """

    # Formalized as a pydantic v2 model in Phase 3.
    ...


@dataclass
class Goal:
    """Navigation goal pose (REQ-EXEC-004). Coordinates are expressed in ``frame``."""

    x: float
    y: float
    yaw: float
    frame: str = "map"

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> Goal:
        return cls(x=d["x"], y=d["y"], yaw=d["yaw"], frame=d.get("frame", "map"))


@dataclass
class Scenario:
    """Self-contained scene + goal + determinism inputs (REQ-INTAKE-006).

    ``timeout_s`` is a SIM-time (/clock) budget, NOT wall-clock — the wall-clock
    runaway watchdog is M3's (M1 §3.2). ``seed`` backs determinism (LOCKED §7-6).
    """

    scene: str
    robot: str
    goal: Goal
    seed: int
    timeout_s: float

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> Scenario:
        return cls(
            scene=d["scene"],
            robot=d["robot"],
            goal=Goal.from_dict(d["goal"]),
            seed=d["seed"],
            timeout_s=d["timeout_s"],
        )


@dataclass
class AcceptanceCriterion:
    """A named oracle + its params ("criteria are also input", REQ-INTAKE-007).

    ``oracle`` names the plugin (e.g. "reached_goal"); ``params`` configures it.
    The concrete oracles live in the evaluation engine (M2); M1 owns the shape.
    """

    oracle: str
    params: dict[str, Any] = field(default_factory=dict)

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> AcceptanceCriterion:
        return cls(oracle=d["oracle"], params=dict(d.get("params") or {}))


@dataclass
class Interface:
    """SUT wiring selector: ``type`` picks an adapter, ``adapter_config`` configures it.

    Boundary: ``adapter_config`` is kept as a raw mapping here because the contract
    package is the foundational import layer and must NOT depend on the adapter
    package (.importlinter: contract-is-foundational). The typed view is built by
    the consumer (M2, which imports both) via
    ``Ros2AdapterConfig.from_dict(interface.adapter_config)`` — the canonical
    ros2 key set lives in ``cv_infra/adapter/adapter_schema.py`` (SEAM-2), which
    loud-rejects unknown keys; this raw mapping performs no validation.
    """

    type: str = "ros2"
    adapter_config: dict[str, Any] = field(default_factory=dict)

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> Interface:
        return cls(
            type=d.get("type", "ros2"),
            adapter_config=dict(d.get("adapter_config") or {}),
        )


@dataclass
class VerificationRequest:
    """Self-contained verification instance = JOB_SPEC payload (REQ-INTAKE-002/006).

    Envelope-less single request (Phase 2). Required elements: ``sut_image_ref``,
    ``scenario``, ``acceptance_criteria`` (REQ-INTAKE-006); ``interface`` selects
    the blackbox SUT adapter. Formalized as a pydantic v2 model in Phase 3.
    """

    job_id: str
    scenario: Scenario
    sut_image_ref: str
    interface: Interface
    acceptance_criteria: list[AcceptanceCriterion] = field(default_factory=list)

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> VerificationRequest:
        return cls(
            job_id=d["job_id"],
            scenario=Scenario.from_dict(d["scenario"]),
            sut_image_ref=d["sut_image_ref"],
            interface=Interface.from_dict(d.get("interface") or {}),
            acceptance_criteria=[
                AcceptanceCriterion.from_dict(c) for c in d.get("acceptance_criteria", [])
            ],
        )

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class Metrics:
    """Declared metrics container (REQ-EXEC-012). Metric *values* are computed by
    the M2 oracle engine; M1 owns only the shape.

    ``min_clearance_m`` may be None in Phase 2 — measuring it needs a PhysX
    scene-query the runner may not wire until a later cycle.
    """

    time_to_goal_s: float | None = None
    min_clearance_m: float | None = None
    collision_count: int = 0
    path_len_m: float | None = None

    @classmethod
    def from_dict(cls, d: dict[str, Any] | None) -> Metrics:
        d = d or {}
        return cls(
            time_to_goal_s=d.get("time_to_goal_s"),
            min_clearance_m=d.get("min_clearance_m"),
            collision_count=d.get("collision_count", 0),
            path_len_m=d.get("path_len_m"),
        )


@dataclass
class CriterionResult:
    """Per-criterion evaluation outcome (one per AcceptanceCriterion).

    ``passed`` is this criterion's verdict; ``detail`` is a human-readable note.
    """

    oracle: str
    passed: bool
    detail: str | None = None

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> CriterionResult:
        return cls(oracle=d["oracle"], passed=d["passed"], detail=d.get("detail"))


@dataclass
class Artifacts:
    """Telemetry / recording artifact refs (REQ-EXEC-014). Filled in a later cycle."""

    mcap: str | None = None
    mp4: str | None = None

    @classmethod
    def from_dict(cls, d: dict[str, Any] | None) -> Artifacts:
        d = d or {}
        return cls(mcap=d.get("mcap"), mp4=d.get("mp4"))


@dataclass
class VerificationResult:
    """Exactly one result per job (REQ-EXEC-013); spec §3.2 names it "Result".

    ``to_dict()`` is the result.json serialization the runner writes (RESULT_OUT).
    ``verdict`` is one of ``VERDICTS``. ``request_identity_key`` is a field only —
    key derivation is M4's (Phase 5). ``origin`` / ``is_self_test`` are M7 markers
    (field only). Formalized as a pydantic v2 model in Phase 3.
    """

    job_id: str
    verdict: str  # one of VERDICTS: "pass" | "fail" | "timeout" | "error"
    metrics: Metrics = field(default_factory=Metrics)
    criteria_results: list[CriterionResult] = field(default_factory=list)
    artifacts: Artifacts = field(default_factory=Artifacts)
    request_identity_key: str | None = None
    origin: str | None = None
    is_self_test: bool = False

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> VerificationResult:
        return cls(
            job_id=d["job_id"],
            verdict=d["verdict"],
            metrics=Metrics.from_dict(d.get("metrics")),
            criteria_results=[CriterionResult.from_dict(c) for c in d.get("criteria_results", [])],
            artifacts=Artifacts.from_dict(d.get("artifacts")),
            request_identity_key=d.get("request_identity_key"),
            origin=d.get("origin"),
            is_self_test=d.get("is_self_test", False),
        )

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)
