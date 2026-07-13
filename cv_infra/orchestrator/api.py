"""REST submit surface (M3 §3.1/§7) — REQ-INTAKE-001, exactly TWO endpoints.

``create_app`` builds the FastAPI app around the p4c1 control-plane core
(fanout / JobQueue / SlotAccountant / ParallelSupervisor / DomainIdAllocator /
rollup / Store — reused, never reimplemented):

* ``POST /envelopes`` — submit an envelope, get ``{"envelope_id": ...}`` back
  immediately (202; async submission, M3 §7 — verification takes minutes).
* ``GET /envelopes/{envelope_id}`` — job states + per-request ``RequestRollup``s
  + the envelope-level ``report_outcome``.

Wire format (D-1, decisions/2026-07-13-p4c2-envelope-contract-timing.md): the
JSON body ``{"requests": [<request document>, ...]}`` is an INTERNAL
representation — the user-facing RequestEnvelope contract (YAML schema,
apiVersion, friendly file errors) freezes with the M8 batch-CLI submit cycle
together with M1; this module adapts to it then. Wrapper keys other than
``"requests"`` are not interpreted this cycle (formal envelope semantics,
e.g. ``trigger_source``, land with that contract). Each request document IS
validated NOW: it goes through the full M1 6-stage admit gate
(``contract.loader.load_request`` — no contract bypass; the JSON document is
fed to the loader as a canonical indented-JSON stream, which any YAML loader
parses, so error line/col point into that rendering). Scenario-adjacent custom
oracles ("module:Class" next to a YAML file) need a directory anchor and
therefore also arrive with the M8 file-submit cycle; entry-point oracles
resolve here already.

Submission is all-or-nothing (비전파): every request must admit before ANY job
is created — one bad request rejects the whole envelope with a structured 422
whose body is ``{"detail": {"errors": [<ContractError annotation dict>, ...]}}``
(the M1 8-key shape, one entry per failing request/violation; never a 500,
never a raw traceback — M3 §7 / NFR-INTAKE-001).

Status response shape (pinned; ``RequestRollup`` keys are the p4c1 frozen
shape M4 consumes — renames frozen)::

    {
      "envelope_id": "<id>",
      "status": "running" | "completed",        # completed = supervision done
      "jobs": [
        {"request_id": str, "repeat_index": int,
         "state": "queued|running|completed|failed|timeout", "attempt_count": int},
        ...
      ],
      "rollups": [   # one per request, submission order (empty verdicts while running)
        {"request_id": str, "verdicts": ["pass"|"fail", ...],
         "flakiness": float | null, "verdict": "pass" | "fail" | null},
        ...
      ],
      "report_outcome": "pass" | "fail" | "errored" | null   # null until completed
    }

``report_outcome`` (M8 exit-code 매핑의 입력, M3 §7 / blueprint §9 — errored
우선): any terminal job WITHOUT a verdict (failed / timeout / verdict-less
completion = infra outcome) -> ``"errored"``; else any FAIL verdict ->
``"fail"``; else -> ``"pass"``. Exit-code folding itself stays M8's single
source (D-I) — this field is the aggregate it consumes.

Envelope supervision runs as an asyncio background task on the app's loop,
single-flight across envelopes (an app-level lock): jobs WITHIN an envelope run
k-parallel via ``ParallelSupervisor``; envelopes queue behind each other so the
operator budget k is never exceeded globally. Cross-envelope parallel admission
is the resident-service cycle's concern (P5 compose). Job state transitions are
persisted through the Store (REQ-ORCH-011); the envelope->request registry is
in-memory this cycle (restart re-attachment of envelopes rides the R14
reconciliation cycle).
"""

from __future__ import annotations

import asyncio
import io
import json
import uuid
from dataclasses import dataclass, field
from typing import Any

from fastapi import FastAPI, HTTPException, Request

from cv_infra.contract.errors import ContractError
from cv_infra.contract.loader import load_request
from cv_infra.orchestrator.allocator import DomainIdAllocator
from cv_infra.orchestrator.fake_runner import Runner
from cv_infra.orchestrator.fanout import fan_out_requests
from cv_infra.orchestrator.models import Job, JobResult, RequestRollup, Verdict
from cv_infra.orchestrator.queue import JobQueue
from cv_infra.orchestrator.rollup import roll_up
from cv_infra.orchestrator.scheduler import SlotAccountant
from cv_infra.orchestrator.store import Store
from cv_infra.orchestrator.supervisor import ParallelSupervisor

_DOC_LINK = "M3-orchestrator.md §7 (submit wire — D-1 internal representation)"

#: The three envelope-level outcome literals M8/M4 consume (M3 §7, verbatim).
REPORT_OUTCOME_PASS = "pass"
REPORT_OUTCOME_FAIL = "fail"
REPORT_OUTCOME_ERRORED = "errored"


def report_outcome_of(results: list[JobResult]) -> str:
    """Fold terminal job results into the envelope ``report_outcome`` literal.

    errored 우선 (M3 §7 / blueprint §9): a verdict-less terminal job is an
    infra outcome (exit-3 territory downstream) and outranks FAIL, which
    outranks PASS — infra noise must never masquerade as a domain judgement.
    """
    if any(r.verdict is None for r in results):
        return REPORT_OUTCOME_ERRORED
    if any(r.verdict is Verdict.FAIL for r in results):
        return REPORT_OUTCOME_FAIL
    return REPORT_OUTCOME_PASS


@dataclass
class _EnvelopeRecord:
    """In-memory registry entry for one submitted envelope (module docstring)."""

    request_ids: list[str]  # submission order
    jobs: list[Job]  # live objects — states mutate in place as the queue drives them
    results: list[JobResult] = field(default_factory=list)  # terminal, set when done
    done: bool = False
    error: str | None = None  # supervision crash (loud 500 on status reads)


def _wire_error(field_path: str, expected: str, got: str) -> ContractError:
    """Structured wrapper-level violation (same 8-key shape as M1 admit errors)."""
    return ContractError(
        field_path=field_path,
        expected=expected,
        got=got,
        example='{"requests": [{"apiVersion": "cv-infra/v1", ...}]}',
        doc_link=_DOC_LINK,
    )


def _parse_envelope(body: Any) -> list[dict[str, Any]]:
    """Validate the internal wire wrapper -> the raw request documents.

    Wrapper-only checks (each document's validation is the M1 loader's):
    the body must be a JSON object whose ``"requests"`` is a non-empty list
    of objects. Violations raise ``ContractError`` (rendered as 422).
    """
    if not isinstance(body, dict):
        raise _wire_error("(document)", 'a JSON object body {"requests": [...]}', repr(body))
    requests = body.get("requests")
    if not isinstance(requests, list) or not requests:
        raise _wire_error(
            "requests",
            "a non-empty list of Verification Request documents (REQ-INTAKE-001)",
            repr(requests),
        )
    for i, doc in enumerate(requests):
        if not isinstance(doc, dict):
            raise _wire_error(f"requests[{i}]", "a Verification Request object", repr(doc))
    return requests


def _admit_all(documents: list[dict[str, Any]]) -> tuple[list[int], list[dict[str, Any]]]:
    """Run EVERY document through the M1 admit gate before any job exists.

    Returns ``(per-request repeats, admit errors as annotation dicts)`` — a
    non-empty error list means the whole envelope is rejected (all-or-nothing,
    비전파). One error per failing request (the loader raises its first
    violation), so a multi-bad envelope still reports every bad request.
    """
    repeats: list[int] = []
    errors: list[dict[str, Any]] = []
    for i, doc in enumerate(documents):
        # Canonical indented-JSON stream through the REAL M1 gate (module
        # docstring — JSON is YAML; line/col point into this rendering).
        stream = io.StringIO(json.dumps(doc, indent=2, sort_keys=True))
        try:
            admitted = load_request(stream, source_path=f"requests[{i}]")
        except ContractError as err:
            errors.append(err.to_annotation_dict())
            continue
        repeats.append(admitted.request.execution_settings.repeats)
    return repeats, errors


def create_app(
    store: Store,
    runner: Runner,
    *,
    k: int,
    max_attempts: int = 1,
    retry_on_timeout: bool = True,
    job_timeout_s: float | None = None,
) -> FastAPI:
    """Build the submit-surface app around an injected store + runner seam.

    ``runner`` is the per-job blocking seam ``ParallelSupervisor`` drives
    (CPU tests inject fakes; the production callable wraps ``run_job`` — P5
    compose glue). ``k`` is the computed concurrency cap (``compute_k`` output
    — never a constant); the queue policy knobs mirror ``JobQueue``.
    """
    app = FastAPI(title="cv-infra orchestrator", docs_url=None, redoc_url=None)
    envelopes: dict[str, _EnvelopeRecord] = {}
    allocator = DomainIdAllocator(store)
    drive_lock = asyncio.Lock()  # single-flight envelopes (module docstring)
    drive_tasks: set[asyncio.Task[None]] = set()  # strong refs — a bare create_task can be GC'd

    async def _drive(record: _EnvelopeRecord, supervisor: ParallelSupervisor) -> None:
        async with drive_lock:
            try:
                record.results = await supervisor.run()
            except Exception as exc:  # loud on the status read, never swallowed
                record.error = f"{type(exc).__name__}: {exc}"
            finally:
                record.done = True

    @app.post("/envelopes", status_code=202)
    async def submit_envelope(request: Request) -> dict[str, str]:
        try:
            body = await request.json()
            documents = _parse_envelope(body)
        except json.JSONDecodeError as exc:
            err = _wire_error("(document)", "a JSON body", str(exc))
            raise HTTPException(
                status_code=422, detail={"errors": [err.to_annotation_dict()]}
            ) from exc
        except ContractError as err:
            raise HTTPException(
                status_code=422, detail={"errors": [err.to_annotation_dict()]}
            ) from err
        repeats, errors = _admit_all(documents)
        if errors:  # all-or-nothing: zero jobs were created (비전파)
            raise HTTPException(status_code=422, detail={"errors": errors})

        envelope_id = f"env-{uuid.uuid4().hex[:12]}"
        request_ids = [f"{envelope_id}/r{i}" for i in range(len(documents))]
        jobs = fan_out_requests(list(zip(request_ids, repeats)))
        queue = JobQueue(  # persists every job QUEUED via the store (REQ-ORCH-011)
            jobs, store=store, max_attempts=max_attempts, retry_on_timeout=retry_on_timeout
        )
        supervisor = ParallelSupervisor(
            queue,
            SlotAccountant(k=k),
            runner,
            allocator=allocator,
            job_timeout_s=job_timeout_s,
        )
        record = _EnvelopeRecord(request_ids=request_ids, jobs=jobs)
        envelopes[envelope_id] = record
        task = asyncio.get_running_loop().create_task(_drive(record, supervisor))
        drive_tasks.add(task)
        task.add_done_callback(drive_tasks.discard)
        return {"envelope_id": envelope_id}

    @app.get("/envelopes/{envelope_id}")
    async def envelope_status(envelope_id: str) -> dict[str, Any]:
        record = envelopes.get(envelope_id)
        if record is None:
            raise HTTPException(status_code=404, detail=f"unknown envelope {envelope_id!r}")
        if record.error is not None:
            raise HTTPException(
                status_code=500, detail=f"envelope supervision crashed: {record.error}"
            )
        rollups = [
            roll_up(rid, [r for r in record.results if r.job.request_id == rid])
            for rid in record.request_ids
        ]
        return {
            "envelope_id": envelope_id,
            "status": "completed" if record.done else "running",
            "jobs": [
                {
                    "request_id": job.request_id,
                    "repeat_index": job.repeat_index,
                    "state": job.state.value,
                    "attempt_count": job.attempt_count,
                }
                for job in record.jobs
            ],
            "rollups": [_rollup_dict(rollup) for rollup in rollups],
            "report_outcome": report_outcome_of(record.results) if record.done else None,
        }

    return app


def _rollup_dict(rollup: RequestRollup) -> dict[str, Any]:
    """``RequestRollup`` -> wire dict with EXACTLY the p4c1 frozen keys (M4 consume)."""
    return {
        "request_id": rollup.request_id,
        "verdicts": [v.value for v in rollup.verdicts],
        "flakiness": rollup.flakiness,
        "verdict": rollup.verdict.value if rollup.verdict is not None else None,
    }
