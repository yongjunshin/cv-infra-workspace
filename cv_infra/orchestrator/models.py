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
from typing import Any


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

    ``job_spec`` (p4c4 REST->runner glue, T1 report §7-1 (a)): the canonical per-job JOB_SPEC
    dict (frozen P2 M3->M2 seam shape incl. its OWN ``job_id`` = ``store.job_key``) that the
    api submit path materializes from the ADMITTED M1 model (``api._job_spec_for``) onto every
    fanned-out job — same denormalization rationale as the anchor above: the job is
    self-contained, so the production runner seam (``supervisor.RunJobRunner``) drives
    ``run_job(job_spec, ...)`` without re-admitting, and a restored/retried job keeps its
    spec (persisted, REQ-ORCH-011). None = CPU-skeleton jobs driven by fake runners only.

    ``runner_exit_code`` / ``infra_error`` (p4c5 실패 관측성): the diagnostics of this job's
    MOST RECENT terminal attempt, written back by ``ParallelSupervisor`` from the attempt's
    ``JobResult`` and persisted with the job (store v4) so a FAILED job says WHY in the
    store and on the status API — a runner hard-crash (137 OOM-kill / 139 segfault) is no
    longer indistinguishable from a plain non-zero exit (history 2026-07-14 놀란 점 7: both
    were dropped in the ``supervisor._job_result_of`` fold). Both are None for a job that
    never ran an attempt, and a later CLEAN attempt resets them to None (last-attempt
    semantics). Operational breadcrumbs only — bounded reason string + exit code, never a
    runner stderr dump, consent value or SUT domain detail (``supervisor._reason``).

    ``ros_domain_id`` (p4c6 §7-1 allocator 정합): the CURRENT attempt's ``ROS_DOMAIN_ID``,
    allocated at admission by the store-backed collision-avoiding ``DomainIdAllocator``
    (M3 §3.6, the SINGLE source of truth for the concurrent-job domain set) and carried onto
    the job so ``ParallelSupervisor`` -> ``RunJobRunner`` hands it verbatim to
    ``run_job(ros_domain_id=...)`` instead of run_job re-deriving a PURE-HASH id with no
    collision avoidance (the p4c5 defect: k>=~6 동시 admission에서 두 잡이 같은 도메인).
    NOT part of ``job_spec`` — a container-env/label seam value, not job명세 (D-2 계약 동결).
    Transient in-flight carrier: NOT persisted (the store restores live ids from the
    ``ros_domain_ids`` table + container labels at restart, M3 §3.9) and re-set on every
    admission; None = no allocator attached (single-run / CPU-fake path -> run_job pure-hash
    fallback, P2 ``cv-infra run`` 계약 불변).
    """

    request_id: str
    repeat_index: int
    state: JobState = JobState.QUEUED
    attempt_count: int = 0
    oracle_plugin_dir: str | None = None
    job_spec: dict[str, Any] | None = None
    runner_exit_code: int | None = None
    infra_error: str | None = None
    ros_domain_id: int | None = None


@dataclass
class JobResult:
    """Control-plane view of a runner's terminal handoff (termination contract; REQ-EXEC-015).

    NOT the M1 Verification Result (domain matrix / recording / per-criteria detail) — only the
    terminal JobState + rollup verdict the control plane needs. Real result.json parsing lands in
    Phase 2/3.

    ``runner_exit_code`` / ``infra_error`` (p4c5) carry the attempt's diagnostics ALONGSIDE the
    classification — they are INFORMATIONAL: the state/verdict fold
    (``supervisor._job_result_of``) is computed exactly as before and never reads them back
    (verdict fold 의미론 불변). ``ParallelSupervisor`` writes them onto the Job, whence they
    persist (store v4) and surface on the status API.

    ``result_doc`` / ``result_json_path`` (p5c3 Result 캡처) carry the runner-emitted
    result.json ADDITIVELY so the report row surfaces real per-repeat ``metrics`` +
    ``artifacts`` paths instead of empty placeholders (P5-02/P5-10). ``result_doc`` is the
    parsed result.json dict (M1 Result wire shape — ``metrics`` map + ``artifacts{mcap,mp4}``,
    consumed VERBATIM by ``api._result_wire`` -> M4 ``aggregate``); ``result_json_path`` is the
    host RESULT_OUT path to that file (the report's ``result_json`` ride-along). Both are ALSO
    informational: the state/verdict fold never reads them (verdict 날조 0, ``_classify`` 불변).
    Both are ``None`` on the honest-absence paths — a fake-runner outcome, a collection
    violation (0 or 2+ result.json), or an unreadable/non-dict file — where the report keeps
    its existing empty ``{}``/None (현행 동작 회귀 0). NOT persisted to the store (the durable
    twin is the assembled report itself, store v7 — real values already ride into it at
    completion, restart-surviving).
    """

    job: Job
    state: JobState
    verdict: Verdict | None = None
    runner_exit_code: int | None = None
    infra_error: str | None = None
    result_doc: dict[str, Any] | None = None
    result_json_path: str | None = None


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
