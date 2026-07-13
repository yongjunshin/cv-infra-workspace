"""Orchestrator-internal models (M3, Phase 0 skeleton).

PLACEHOLDER scope: stdlib-only Job lifecycle state + rollup placeholders that let the control
plane (fanout / queue / scheduler / state-machine / rollup) be unit-tested on CPU against a
fake runner (DoD-P1-06, M3 §5 — 제어 평면 CPU 골격). The authoritative Envelope / Verification
Request / Verification Result pydantic models are owned by M1 (contract package) and are NOT
redefined here. Job is shared with the M1 contract in Phase 3; RequestRollup fields finalize in
Phase 4 (정식 모델·계약 공유는 P3/P4). No third-party runtime dependency — stdlib only.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum


class JobState(StrEnum):
    """Verification Job lifecycle states (M3 §3.3 state machine; REQ-ORCH-003/009/010/011).

    Transitions are persisted to SQLite in Phase 4 so an orchestrator restart can restore state.
    """

    QUEUED = "queued"
    RUNNING = "running"
    COMPLETED = "completed"  # exit 0 + result.json (termination contract)
    FAILED = "failed"  # exit != 0
    TIMEOUT = "timeout"  # wall-clock watchdog kill


class Verdict(StrEnum):
    """Placeholder pass/fail verdict for request-level rollup wiring (REQ-ORCH-012/013, SR-10).

    The authoritative verdict comes from oracle evaluation (M2) carried in the M1 Verification
    Result; this enum is only a control-plane placeholder so the rollup seam is typed in Phase 1.
    """

    PASS = "pass"
    FAIL = "fail"


@dataclass
class Job:
    """A single verification job — one unit of the 2-axis fan-out (M3 §3.2; REQ-ORCH-001/002).

    Identified by (request_id, repeat_index); repeat_index is unique within a request
    (0..repeats-1). PLACEHOLDER: shares with the M1 contract Job model in Phase 3 (no contract
    field duplication here). attempt_count backs the retry policy (REQ-ORCH-010).

    ``oracle_plugin_dir`` (D-1 2026-07-11 wiring, p4c4): the per-request stage-5 custom-oracle
    anchor (absolute host directory) riding the job so the production runner seam can hand it
    to ``run_job(oracle_plugin_dir=...)`` — None = no anchor (entry-point oracles only). It is
    request-level data denormalized onto the job (the job spec is self-contained) and is
    persisted with the job (REQ-ORCH-011) so a restored/retried job keeps its anchor.
    """

    request_id: str
    repeat_index: int
    state: JobState = JobState.QUEUED
    attempt_count: int = 0
    oracle_plugin_dir: str | None = None


@dataclass
class JobResult:
    """Control-plane view of a runner's terminal handoff (termination contract; REQ-EXEC-015).

    NOT the M1 Verification Result (domain matrix / recording / per-criteria detail) — only the
    terminal JobState + rollup verdict the control plane needs. Real result.json parsing lands in
    Phase 2/3.
    """

    job: Job
    state: JobState
    verdict: Verdict | None = None


@dataclass
class RequestRollup:
    """Request-level rollup of repeated jobs + flakiness (M3 §3.7; SR-10, REQ-ORCH-012/013).

    Emitted to M4 for the report-level pass/fail matrix (SR-19) — the two aggregation
    responsibilities are split (LOCKED §7.12). ``verdict`` is the rolled-up request verdict
    under the any-fail=fail policy; ``flakiness`` (repeat disagreement) is surfaced
    SEPARATELY, never folded into the verdict — policy + metric are pinned in
    ``rollup.roll_up``. M4 consumes this shape: field additions are allowed, renames frozen.
    """

    request_id: str
    verdicts: list[Verdict] = field(default_factory=list)
    flakiness: float | None = None
    verdict: Verdict | None = None
