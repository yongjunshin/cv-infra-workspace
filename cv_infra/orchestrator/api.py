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
cycle together with M1; this module adapts to it then. One formal envelope key
IS now threaded (p5c3): an optional top-level ``"trigger_source"`` records human
vs CI provenance (REQ-INTAKE-003 — ``_parse_envelope``); the remaining wrapper
keys are still not interpreted this cycle. Each request document IS
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

Report + baseline seam (p5c2, SR-19/SR-21 handoff): at CLEAN completion
``_persist_terminal`` assembles the M4 ``VerificationReport`` server-side
(``report.aggregate.build_report`` — M4 code called, never modified), persists it
(store v7) so ``GET /envelopes/{id}/report`` serves the durable twin
restart-surviving, and ONLY THEN advances the request-level regression baselines
from the report rows (``report.baseline.update_baseline`` — advance-on-pass,
전달-not-재도출). The order is invariant: the report's regression judgement compares
against the PRE-advance baseline (advancing first would let a request regress
against itself). The baseline is the C-1 internal store (LOCKED §7.13) — no CI/git
path is touched. A crashed envelope assembles no report and writes no baseline.
"""

from __future__ import annotations

import asyncio
import io
import json
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, get_args

from fastapi import FastAPI, HTTPException, Request

from cv_infra.contract.errors import ContractError
from cv_infra.contract.loader import AdmittedRequest, load_request
from cv_infra.contract.schema import RequestEnvelope
from cv_infra.orchestrator.allocator import DomainIdAllocator
from cv_infra.orchestrator.fake_runner import Runner
from cv_infra.orchestrator.fanout import fan_out_requests
from cv_infra.orchestrator.models import Job, JobResult, RequestRollup, Verdict
from cv_infra.orchestrator.monitor import register as register_monitor
from cv_infra.orchestrator.queue import JobQueue
from cv_infra.orchestrator.rollup import roll_up
from cv_infra.orchestrator.scheduler import SlotAccountant
from cv_infra.orchestrator.store import Store, job_key
from cv_infra.orchestrator.supervisor import ParallelSupervisor
from cv_infra.report.aggregate import RequestReportInput, build_report
from cv_infra.report.baseline import update_baseline

_DOC_LINK = "M3-orchestrator.md §7 (submit wire — D-1 internal representation)"

#: The three envelope-level outcome literals M8/M4 consume (M3 §7, verbatim).
REPORT_OUTCOME_PASS = "pass"
REPORT_OUTCOME_FAIL = "fail"
REPORT_OUTCOME_ERRORED = "errored"

#: Envelope ``trigger_source`` recorded verbatim into the assembled report
#: (REQ-INTAKE-003). The submit wire now carries an optional top-level
#: ``trigger_source`` (p5c3, ``_parse_envelope``): the SUBMITTED value wins and is
#: recorded so ``build_report`` reads a recorded value rather than RE-DERIVING one at
#: report time (재도출 금지). When ABSENT the recorded value is this documented default
#: ``human-manual`` (M8 §3.1: 기본값 human-manual; the Action passes
#: ``--trigger-source ci-cd``) — a bare REST/CLI submission with no provenance is a
#: human one, never falsely CI. The seam already reads it off the record, not this
#: constant.
_DEFAULT_TRIGGER_SOURCE = "human-manual"

#: The legal ``trigger_source`` wire values — DERIVED from the M1
#: ``RequestEnvelope.trigger_source`` ``Literal`` so this wrapper-level check stays in
#: lockstep with the contract (M1 Literal 정합; never a hand-copied set that could drift,
#: G-25). An illegal submitted value is a 422 wrapper violation (``_parse_envelope``).
_TRIGGER_SOURCES: tuple[str, ...] = get_args(
    RequestEnvelope.model_fields["trigger_source"].annotation
)


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
    store's envelope registry + rollups + assembled report (written at submit /
    completion).

    ``request_dumps`` (p5c2 report seam) keeps each request's M1 wire dump
    (``model_dump(mode="json", by_alias=True)``) captured AT SUBMIT — the report
    assembly at completion consumes it for the identity key / sut_ref / scenario
    (전달-not-재도출). ``trigger_source`` is the envelope provenance recorded verbatim
    into the report (``_DEFAULT_TRIGGER_SOURCE`` until the formal wire carries it).
    """

    envelope_id: str
    request_ids: list[str]  # submission order
    jobs: list[Job]  # live objects — states mutate in place as the queue drives them
    request_dumps: dict[str, dict[str, Any]] = field(default_factory=dict)  # request_id->dump
    trigger_source: str = _DEFAULT_TRIGGER_SOURCE
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


def _trigger_source_of(body: dict[str, Any]) -> str:
    """Parse the optional top-level ``trigger_source`` (p5c3, REQ-INTAKE-003).

    Absent (or explicit null) -> the documented default ``human-manual``
    (``_DEFAULT_TRIGGER_SOURCE``); a present value must be one of the M1
    ``RequestEnvelope`` literals (``_TRIGGER_SOURCES``) or it is a 422 wrapper
    violation (M1 Literal 정합, same 8-key shape as an admit error). The submitted
    value wins — the record carries it verbatim into the report (재도출 금지)."""
    trigger_source = body.get("trigger_source")
    if trigger_source is None:  # absent or explicit null -> documented default
        return _DEFAULT_TRIGGER_SOURCE
    if trigger_source not in _TRIGGER_SOURCES:
        raise _wire_error(
            "trigger_source",
            f"one of {list(_TRIGGER_SOURCES)} (REQ-INTAKE-003 provenance), or absent"
            f" for the default {_DEFAULT_TRIGGER_SOURCE!r}",
            repr(trigger_source),
            example='{"requests": [{...}], "trigger_source": "ci-cd"}',
        )
    return trigger_source


def _parse_envelope(body: Any) -> tuple[list[dict[str, Any]], list[str | None], str]:
    """Validate the internal wire wrapper -> (request documents, stage-5 anchors,
    trigger_source).

    Wrapper-only checks (each document's validation is the M1 loader's):
    the body must be a JSON object whose ``"requests"`` is a non-empty list
    of objects. ``"oracle_plugin_dirs"`` (wire v2, optional) must — when
    present — be an equal-length list of ``null`` (no anchor) or absolute
    directory path strings; absent/null field means no anchors (previous
    behavior, unchanged). ``"trigger_source"`` (p5c3, optional) is parsed by
    ``_trigger_source_of`` (absent -> ``human-manual``, illegal -> 422). Anchors
    are same-host trusted paths (module docstring — MVP, M8 §8 g5). Violations
    raise ``ContractError`` (422).
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
    trigger_source = _trigger_source_of(body)
    plugin_dirs = body.get("oracle_plugin_dirs")
    if plugin_dirs is None:  # field absent (or explicit null): no anchors — unchanged path
        return requests, [None] * len(requests), trigger_source
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
    return requests, plugin_dirs, trigger_source


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


def _result_wire(result: JobResult) -> dict[str, Any]:
    """One terminal ``JobResult`` -> the per-repeat Result wire dict the report
    consumes (``aggregate._select_artifacts`` / ``_metrics``).

    p5c3 Result 캡처: when the control-plane fold captured the runner's result.json
    (``JobResult.result_doc`` — ``supervisor._job_result_of``), this emits that doc's
    declared ``metrics`` map + ``artifacts`` (``{mcap, mp4}``) VERBATIM (재계산·키 가공 0)
    plus the host ``result_json`` path, so the report row shows real values (P5-02/P5-10).
    No doc — a fake-runner outcome, a collection violation, an unreadable file — keeps the
    previous empty ``{}`` (정직한 부재, 회귀 0); the ``result_json`` ride-along is emitted
    only when a path exists (optional per ``aggregate.RequestReportInput`` — absent by
    default, consumed via ``.get``), so the fake path stays byte-identical.

    The ``verdict`` stays the CONTROL-PLANE folded verdict (PASS/FAIL ->
    ``"pass"``/``"fail"``; verdict-less errored/timeout job -> ``None``, classified a
    failure-class artifact) — the doc's OWN verdict is deliberately never re-surfaced
    (verdict 날조 0, ``_classify`` 불변); the doc rides only for metrics/artifacts. The
    ``mcap_bytes`` size-cap ride-along stays the M8 plane (aggregate docstring), absent here.
    """
    doc = result.result_doc
    metrics = doc.get("metrics") if isinstance(doc, dict) else None
    artifacts = doc.get("artifacts") if isinstance(doc, dict) else None
    wire: dict[str, Any] = {
        "job_id": job_key(result.job),
        "verdict": result.verdict.value if result.verdict is not None else None,
        "metrics": dict(metrics) if isinstance(metrics, dict) else {},
        "artifacts": dict(artifacts) if isinstance(artifacts, dict) else {},
    }
    if result.result_json_path is not None:
        wire["result_json"] = result.result_json_path  # optional ride-along (path to result.json)
    return wire


def _report_inputs(record: _EnvelopeRecord) -> list[RequestReportInput]:
    """Assemble the per-request report inputs from the terminal record (전달-not-재도출).

    One ``RequestReportInput`` per request in SUBMISSION order: the captured M1
    request wire dump, the ``roll_up`` of its per-repeat results (M3 SR-10 산출
    그대로), and the per-repeat Result wire dumps IN REPEAT ORDER. The rollup here
    is the SAME value persisted below (computed once, consumed by both the report
    and ``upsert_rollup`` — no divergent second aggregation)."""
    inputs: list[RequestReportInput] = []
    for rid in record.request_ids:
        repeats = sorted(
            (r for r in record.results if r.job.request_id == rid),
            key=lambda r: r.job.repeat_index,
        )
        inputs.append(
            RequestReportInput(
                request=record.request_dumps[rid],
                rollup=roll_up(rid, repeats),
                results=[_result_wire(r) for r in repeats],
            )
        )
    return inputs


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
        """Write-through at completion (p4c4 영속 + p5c2 report/baseline seam).

        A crashed supervision persists the error marker only — NO report is
        assembled and NO baseline is written (its results are not trustworthy).

        A clean completion (순서 불변식):
          ① assemble the VerificationReport server-side (``build_report`` reads the
             PRE-advance baseline for the regression judgement — C-1 internal store
             the only source);
          ② persist it (store v7) so ``GET /envelopes/{id}/report`` survives restart;
          ③ ONLY THEN advance baselines from the report rows (전달-not-재도출: the
             advance-on-pass / errored-skip / fail-no-overwrite policy is owned by
             ``update_baseline``; the seam passes row values, never re-deriving) —
             advancing BEFORE ① would let a request regress against itself;
          ④ persist the per-request rollups + envelope ``report_outcome`` (unchanged
             — the job-level fold M8 keys exit off).
        """
        if record.error is not None:
            store.complete_envelope(record.envelope_id, error=record.error)
            return
        inputs = _report_inputs(record)
        report = build_report(
            inputs,
            store,
            envelope_id=record.envelope_id,
            trigger_source=record.trigger_source,  # 봉투 기록값 verbatim (재도출 금지)
            # 잡별 상한 = 32 MiB provisional (결정 #2 · 실측-후-기입 §2-4). T3 p5c5 실측:
            # 실 p5c4 bag 레이트 ~34–38 KB/s 상수 → 최악(scenario timeout 120s) ≈ 4.5–8 MB;
            # 32 MiB는 4–7x 마진(정상 bag 미제외)·폭주(raw PointCloud2 >GB) 아래(오설정 bag 제외+경고).
            # 실 120s consent 미션으로 확정 권고(decisions/2026-07-16-p5-artifact-return.md 결정2).
            max_mcap_bytes=32 * 1024 * 1024,
        )
        store.save_report(record.envelope_id, report)  # ② 영속 BEFORE ③ advance
        for row in report["matrix"]:  # ③ advance-on-pass — values off the report rows
            update_baseline(
                store,
                request_identity_key=row["request_identity_key"],
                sut_ref=row["sut_ref"],
                verdict=row["rollup"]["verdict"],
                key_metrics=row["metrics"],
                established_at=report["generated_at"],
            )
        for inp in inputs:  # ④ rollups + outcome (unchanged job-level fold)
            store.upsert_rollup(inp.rollup)
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
            documents, plugin_dirs, trigger_source = _parse_envelope(body)
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
        record = _EnvelopeRecord(
            envelope_id=envelope_id,
            request_ids=request_ids,
            jobs=jobs,
            # Capture each request's M1 wire dump AT SUBMIT (p5c2 report seam): the
            # completion-time assembly consumes it for identity_key/sut_ref/scenario
            # (전달-not-재도출) — the admitted models would otherwise be gone by then.
            request_dumps={
                rid: admitted_of[rid].request.model_dump(mode="json", by_alias=True)
                for rid in request_ids
            },
            trigger_source=trigger_source,  # p5c3: submitted value (or default), recorded verbatim
        )
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

    @app.get("/envelopes/{envelope_id}/report")
    async def envelope_report(envelope_id: str) -> dict[str, Any]:
        """Serve the DURABLE assembled VerificationReport (p5c2, 재시작 생존).

        Always the persisted twin (never re-assembled): a completed envelope's
        report was written by ``_persist_terminal`` and is returned verbatim (200).
        Absence is disambiguated off the envelope registry — unknown -> 404 (same
        body as ``GET /envelopes/{id}``); a supervision-crash marker -> 409
        supervision-error; a still-in-flight envelope -> 409 not-terminal.
        """
        report = store.load_report(envelope_id)
        if report is not None:
            return report
        stored = store.load_envelope(envelope_id)
        if stored is None:
            raise HTTPException(status_code=404, detail=f"unknown envelope {envelope_id!r}")
        if stored.error is not None:
            raise HTTPException(
                status_code=409,
                detail={"reason": "supervision-error", "error": stored.error},
            )
        raise HTTPException(
            status_code=409, detail={"reason": "not-terminal", "status": stored.status}
        )

    # M6 operational view (DoD-P4-12/13): read-only projection surfaces on the
    # SAME app (no separate server). Routes only — the resident sampler is wired
    # in production (serve.build_app), never on the TestClient path.
    register_monitor(app, store)
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
