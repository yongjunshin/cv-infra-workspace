"""REST submit surface (M3 §3.1/§7) — REQ-INTAKE-001, exactly TWO endpoints.

``create_app`` builds the FastAPI app around the p4c1 control-plane core
(fanout / JobQueue / SlotAccountant / ParallelSupervisor / DomainIdAllocator /
rollup / Store — reused, never reimplemented):

* ``POST /envelopes`` — submit an envelope, get ``{"envelope_id": ...}`` back
  immediately (202; async submission, M3 §7 — verification takes minutes).
* ``GET /envelopes/{envelope_id}`` — job states + per-request ``RequestRollup``s
  + the envelope-level ``report_outcome``.

Wire format (D-1 wire v2, decisions/2026-07-13-p4c2-envelope-contract-timing.md):
the JSON body ``{"requests": [...], "oracle_plugin_dirs": [...]}`` is an
INTERNAL representation — the user-facing RequestEnvelope contract (YAML
schema, apiVersion, friendly file errors) freezes with the M8 batch-CLI submit
cycle together with M1; this module adapts to it then. Wrapper keys other than
these two are not interpreted this cycle (formal envelope semantics, e.g.
``trigger_source``, land with that contract). Each request document IS
validated NOW: it goes through the full M1 6-stage admit gate
(``contract.loader.load_request`` — no contract bypass; the JSON document is
fed to the loader as a canonical indented-JSON stream, which any YAML loader
parses, so error line/col point into that rendering).

``oracle_plugin_dirs`` (optional, p4c3) carries per-request stage-5 custom
oracle anchors: when present it must be a list of the SAME length as
``"requests"`` whose items are ``null`` (no anchor) or an ABSOLUTE directory
path string, forwarded as ``load_request(..., plugin_dir=...)`` so
scenario-adjacent ``module:Class`` oracles admit over REST too (the M8
file-submit path re-admits its scenario dirs here). Absent field = previous
behavior, unchanged; entry-point oracles resolve without any anchor.
Same-host trusted-path assumption (MVP, M8 §8 g5): submitter and API share a
filesystem, so the anchor is used as-is on THIS host. Beyond admit (p4c4, D-1
wiring #3 잔여 반쪽): each request's anchor rides its fanned-out Jobs
(``Job.oracle_plugin_dir``) so the production runner seam hands it to
``run_job(oracle_plugin_dir=...)`` — ro mount + ``CV_ORACLE_PLUGIN_DIR``,
runner-only. Likewise (p4c4 glue, T1 report §7-1 (a)) each ADMITTED request
materializes into the canonical per-job JOB_SPEC (``_job_spec_for``) riding —
and persisting with — its Jobs (``Job.job_spec``), so ``RunJobRunner`` drives
the real ``run_job`` without ever re-admitting; the env-configured production
wiring lives in ``serve.py``.

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
         "state": "queued|running|completed|failed|timeout", "attempt_count": int,
         # last-attempt failure diagnostics (p4c5 실패 관측성; null when the job
         # never ran / the last attempt was clean) — operational breadcrumbs, NOT
         # domain detail: a bounded reason string + the runner's container exit
         # code (137 = OOM-kill, 139 = segfault, ... vs a plain non-zero exit).
         "runner_exit_code": int | null, "infra_error": str | null},
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
is the resident-service cycle's concern (P5 compose).

Persistence (p4c4 — in-memory 유실 해소): job state transitions persist through
the Store (REQ-ORCH-011) as before; the envelope->request registry is now
persisted at submit and the per-request ``RequestRollup``s + envelope
``report_outcome`` (or crash ``error``) at completion. A status read for an
envelope this process never saw (orchestrator restart) is served from the store
— never recomputed from results, which did not survive. Envelope supervision
itself is NOT resumed after a restart: ``supervisor.reconcile_at_restart``
(R14) re-labels the orphaned jobs and marks the envelope failed-with-error, so
the read stays loud rather than stuck 'running'.
"""

from __future__ import annotations

import asyncio
import io
import json
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from fastapi import FastAPI, HTTPException, Request

from cv_infra.contract.errors import ContractError
from cv_infra.contract.loader import AdmittedRequest, load_request
from cv_infra.orchestrator.allocator import DomainIdAllocator
from cv_infra.orchestrator.fake_runner import Runner
from cv_infra.orchestrator.fanout import fan_out_requests
from cv_infra.orchestrator.models import Job, JobResult, RequestRollup, Verdict
from cv_infra.orchestrator.queue import JobQueue
from cv_infra.orchestrator.rollup import roll_up
from cv_infra.orchestrator.scheduler import SlotAccountant
from cv_infra.orchestrator.store import Store, job_key
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
    """In-process registry entry for one submitted envelope (module docstring).

    The live view while this process supervises; the durable twin is the
    store's envelope registry + rollups (written at submit / completion).
    """

    envelope_id: str
    request_ids: list[str]  # submission order
    jobs: list[Job]  # live objects — states mutate in place as the queue drives them
    results: list[JobResult] = field(default_factory=list)  # terminal, set when done
    done: bool = False
    error: str | None = None  # supervision crash (loud 500 on status reads)


_ANCHOR_EXAMPLE = '{"requests": [{...}, {...}], "oracle_plugin_dirs": ["/abs/scenario/dir", null]}'


def _wire_error(
    field_path: str, expected: str, got: str, *, example: str | None = None
) -> ContractError:
    """Structured wrapper-level violation (same 8-key shape as M1 admit errors)."""
    return ContractError(
        field_path=field_path,
        expected=expected,
        got=got,
        example=example or '{"requests": [{"apiVersion": "cv-infra/v1", ...}]}',
        doc_link=_DOC_LINK,
    )


def _parse_envelope(body: Any) -> tuple[list[dict[str, Any]], list[str | None]]:
    """Validate the internal wire wrapper -> (request documents, stage-5 anchors).

    Wrapper-only checks (each document's validation is the M1 loader's):
    the body must be a JSON object whose ``"requests"`` is a non-empty list
    of objects. ``"oracle_plugin_dirs"`` (wire v2, optional) must — when
    present — be an equal-length list of ``null`` (no anchor) or absolute
    directory path strings; absent/null field means no anchors (previous
    behavior, unchanged). Anchors are same-host trusted paths (module
    docstring — MVP, M8 §8 g5). Violations raise ``ContractError`` (422).
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
    plugin_dirs = body.get("oracle_plugin_dirs")
    if plugin_dirs is None:  # field absent (or explicit null): no anchors — unchanged path
        return requests, [None] * len(requests)
    if not isinstance(plugin_dirs, list) or len(plugin_dirs) != len(requests):
        raise _wire_error(
            "oracle_plugin_dirs",
            f"a list of exactly {len(requests)} items — one per request, null = no anchor",
            repr(plugin_dirs),
            example=_ANCHOR_EXAMPLE,
        )
    for i, anchor in enumerate(plugin_dirs):
        if anchor is not None and not (isinstance(anchor, str) and Path(anchor).is_absolute()):
            raise _wire_error(
                f"oracle_plugin_dirs[{i}]",
                "null or an absolute directory path string (stage-5 oracle anchor)",
                repr(anchor),
                example=_ANCHOR_EXAMPLE,
            )
    return requests, plugin_dirs


def _admit_all(
    documents: list[dict[str, Any]], plugin_dirs: list[str | None]
) -> tuple[list[AdmittedRequest], list[dict[str, Any]]]:
    """Run EVERY document through the M1 admit gate before any job exists.

    Returns ``(admitted requests, admit errors as annotation dicts)`` — a
    non-empty error list means the whole envelope is rejected (all-or-nothing,
    비전파). One error per failing request (the loader raises its first
    violation), so a multi-bad envelope still reports every bad request.
    ``plugin_dirs`` (parsed, equal length) rides into stage 5 per request.
    The ADMITTED models are kept (p4c4 glue, T1 report §7-1 (a)): they carry
    the repeats axis AND materialize into the per-job canonical JOB_SPEC —
    admit-then-discard would leave the production runner nothing to run.
    """
    admitted_requests: list[AdmittedRequest] = []
    errors: list[dict[str, Any]] = []
    for i, (doc, plugin_dir) in enumerate(zip(documents, plugin_dirs, strict=True)):
        # Canonical indented-JSON stream through the REAL M1 gate (module
        # docstring — JSON is YAML; line/col point into this rendering).
        stream = io.StringIO(json.dumps(doc, indent=2, sort_keys=True))
        try:
            admitted = load_request(stream, source_path=f"requests[{i}]", plugin_dir=plugin_dir)
        except ContractError as err:
            errors.append(err.to_annotation_dict())
            continue
        admitted_requests.append(admitted)
    return admitted_requests, errors


def _job_spec_for(request: Any, job_id: str) -> dict[str, Any]:
    """Admitted M1 ``schema.VerificationRequest`` -> canonical JOB_SPEC dict (p4c4 glue).

    The wire shape is the frozen Phase-2 M3->M2 seam (supervisor JOB_SPEC file
    -> runner ``resolve_job_spec_dict``): exact top-level key set ``{job_id,
    scenario, sut_image_ref, interface, acceptance_criteria}`` with
    ``sut.image_ref`` flattened (REQ-INTAKE-006). ``exclude_none=True`` keeps
    "None = downstream default applies" fields ABSENT (a present-but-null
    known-key param would defeat the oracle ``read_field`` fallback); free-form
    custom-criterion params are not filtered, so explicit user nulls survive.

    SOURCE OF TRUTH anchor (G-25): the envelope-less producer of this exact
    shape is ``cv_infra/cli/main.py::_job_spec_from_request`` (M8, ``cv-infra
    run``). This REST-path twin is kept verbatim-equal by the mechanical parity
    guard ``tests/test_orchestrator_rest_glue.py`` — production M3 deliberately
    does NOT import the M8 CLI plane (layer direction: M8 wraps M3).
    """
    return {
        "job_id": job_id,
        "scenario": request.scenario.model_dump(exclude_none=True),
        "sut_image_ref": request.sut.image_ref,  # flattened canonical field (REQ-INTAKE-006)
        "interface": request.interface.model_dump(exclude_none=True),
        "acceptance_criteria": [
            criterion.model_dump(exclude_none=True) for criterion in request.acceptance_criteria
        ],
    }


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
    (CPU tests inject fakes; production injects ``supervisor.RunJobRunner`` —
    the env-configured wiring is ``serve.build_app``). ``k`` is the computed
    concurrency cap (``compute_k`` output — never a constant); the queue
    policy knobs mirror ``JobQueue``.
    """
    app = FastAPI(title="cv-infra orchestrator", docs_url=None, redoc_url=None)
    envelopes: dict[str, _EnvelopeRecord] = {}
    allocator = DomainIdAllocator(store)
    drive_lock = asyncio.Lock()  # single-flight envelopes (module docstring)
    drive_tasks: set[asyncio.Task[None]] = set()  # strong refs — a bare create_task can be GC'd

    def _persist_terminal(record: _EnvelopeRecord) -> None:
        """Write-through at completion (p4c4 영속): rollups + envelope outcome.

        A crashed supervision persists the error marker only (its results are
        not trustworthy); a clean completion persists one rollup per request
        plus the envelope-level ``report_outcome``.
        """
        if record.error is not None:
            store.complete_envelope(record.envelope_id, error=record.error)
            return
        for rid in record.request_ids:
            store.upsert_rollup(
                roll_up(rid, [r for r in record.results if r.job.request_id == rid])
            )
        store.complete_envelope(
            record.envelope_id, report_outcome=report_outcome_of(record.results)
        )

    async def _drive(record: _EnvelopeRecord, supervisor: ParallelSupervisor) -> None:
        async with drive_lock:
            try:
                record.results = await supervisor.run()
            except asyncio.CancelledError:
                # App/loop shutdown mid-envelope: leave the envelope 'running'
                # in the store — reconcile_at_restart (R14) marks it on the
                # next boot. Persisting a fabricated outcome from partial
                # results here would be a lie.
                raise
            except Exception as exc:  # loud on the status read, never swallowed
                record.error = f"{type(exc).__name__}: {exc}"
            try:
                _persist_terminal(record)
            except Exception as exc:  # persistence failure is loud too, never masked
                record.error = record.error or f"persist failed: {type(exc).__name__}: {exc}"
            finally:
                record.done = True

    @app.post("/envelopes", status_code=202)
    async def submit_envelope(request: Request) -> dict[str, str]:
        try:
            body = await request.json()
            documents, plugin_dirs = _parse_envelope(body)
        except json.JSONDecodeError as exc:
            err = _wire_error("(document)", "a JSON body", str(exc))
            raise HTTPException(
                status_code=422, detail={"errors": [err.to_annotation_dict()]}
            ) from exc
        except ContractError as err:
            raise HTTPException(
                status_code=422, detail={"errors": [err.to_annotation_dict()]}
            ) from err
        admitted, errors = _admit_all(documents, plugin_dirs)
        if errors:  # all-or-nothing: zero jobs were created (비전파)
            raise HTTPException(status_code=422, detail={"errors": errors})

        envelope_id = f"env-{uuid.uuid4().hex[:12]}"
        request_ids = [f"{envelope_id}/r{i}" for i in range(len(documents))]
        repeats = [a.request.execution_settings.repeats for a in admitted]
        jobs = fan_out_requests(list(zip(request_ids, repeats)))
        anchor_of = dict(zip(request_ids, plugin_dirs, strict=True))
        admitted_of = dict(zip(request_ids, admitted, strict=True))
        for job in jobs:
            # D-1 wiring #3 (p4c4): the stage-5 anchor rides each fanned-out job
            # so the runner seam can hand it to run_job(oracle_plugin_dir=...).
            job.oracle_plugin_dir = anchor_of[job.request_id]
            # p4c4 glue (T1 §7-1 (a)): the ADMITTED model materializes into the
            # canonical per-job JOB_SPEC riding (and persisting with) the job —
            # the production runner seam (RunJobRunner) drives run_job off it.
            job.job_spec = _job_spec_for(admitted_of[job.request_id].request, job_key(job))
        # Durable registry FIRST (p4c4 영속): a restart can then serve status for
        # this envelope even though the in-memory record below dies with us.
        store.record_envelope(envelope_id, request_ids, plugin_dirs)
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
        record = _EnvelopeRecord(envelope_id=envelope_id, request_ids=request_ids, jobs=jobs)
        envelopes[envelope_id] = record
        task = asyncio.get_running_loop().create_task(_drive(record, supervisor))
        drive_tasks.add(task)
        task.add_done_callback(drive_tasks.discard)
        return {"envelope_id": envelope_id}

    def _status_from_store(envelope_id: str) -> dict[str, Any]:
        """Serve status for an envelope this process never saw (restart path, p4c4).

        Everything comes from the persisted registry / jobs / rollups — never
        recomputed from results (which did not survive the restart). A crash /
        restart marker surfaces as the same loud 500 the in-memory path uses.
        """
        stored = store.load_envelope(envelope_id)
        if stored is None:
            raise HTTPException(status_code=404, detail=f"unknown envelope {envelope_id!r}")
        if stored.error is not None:
            raise HTTPException(
                status_code=500, detail=f"envelope supervision crashed: {stored.error}"
            )
        position = {rid: pos for pos, rid in enumerate(stored.request_ids)}
        jobs = sorted(
            (job for job in store.load_jobs() if job.request_id in position),
            key=lambda job: (position[job.request_id], job.repeat_index),
        )
        rollups = [
            store.load_rollup(rid) or RequestRollup(request_id=rid)  # empty while running
            for rid in stored.request_ids
        ]
        return _status_body(
            envelope_id,
            status=stored.status,
            jobs=jobs,
            rollups=rollups,
            report_outcome=stored.report_outcome,
        )

    @app.get("/envelopes/{envelope_id}")
    async def envelope_status(envelope_id: str) -> dict[str, Any]:
        record = envelopes.get(envelope_id)
        if record is None:
            return _status_from_store(envelope_id)
        if record.error is not None:
            raise HTTPException(
                status_code=500, detail=f"envelope supervision crashed: {record.error}"
            )
        rollups = [
            roll_up(rid, [r for r in record.results if r.job.request_id == rid])
            for rid in record.request_ids
        ]
        return _status_body(
            envelope_id,
            status="completed" if record.done else "running",
            jobs=record.jobs,
            rollups=rollups,
            report_outcome=report_outcome_of(record.results) if record.done else None,
        )

    return app


def _status_body(
    envelope_id: str,
    *,
    status: str,
    jobs: list[Job],
    rollups: list[RequestRollup],
    report_outcome: str | None,
) -> dict[str, Any]:
    """Assemble the pinned status wire shape (module docstring) — one builder for
    both the in-memory and the restart/store read paths (no shape drift).

    The job entries read straight off the ``Job`` objects, so the p4c5 failure
    diagnostics surface identically on the live path (supervisor wrote them onto
    the job) and the restart path (the store restored them) — one source, no
    second assembler.
    """
    return {
        "envelope_id": envelope_id,
        "status": status,
        "jobs": [
            {
                "request_id": job.request_id,
                "repeat_index": job.repeat_index,
                "state": job.state.value,
                "attempt_count": job.attempt_count,
                "runner_exit_code": job.runner_exit_code,
                "infra_error": job.infra_error,
            }
            for job in jobs
        ],
        "rollups": [_rollup_dict(rollup) for rollup in rollups],
        "report_outcome": report_outcome,
    }


def _rollup_dict(rollup: RequestRollup) -> dict[str, Any]:
    """``RequestRollup`` -> wire dict with EXACTLY the p4c1 frozen keys (M4 consume)."""
    return {
        "request_id": rollup.request_id,
        "verdicts": [v.value for v in rollup.verdicts],
        "flakiness": rollup.flakiness,
        "verdict": rollup.verdict.value if rollup.verdict is not None else None,
    }
