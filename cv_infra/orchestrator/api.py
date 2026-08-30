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
cycle together with M1; this module adapts to it then. Three formal envelope keys
ARE now threaded: an optional top-level ``"trigger_source"`` records human vs CI
provenance (p5c3, REQ-INTAKE-003 — ``_parse_envelope``), and the optional
``"is_self_test"`` / ``"origin"`` pair records self-test provenance (p5c15,
REQ-SELFTEST-001/002 — the built-in stub envelope ``orchestrator/selftest.py``
builds rides this SAME path, no self-test branch anywhere below); the remaining
wrapper keys are still not interpreted this cycle. Each request document IS
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
wiring lives in ``serve.py``. That builder is M1's own
(``contract.job_spec.build_job_spec``, imported here under the frozen local
name): p8c1 replaced this plane's twin COPY of the assembly with the single
definition both submission planes now call — the M8 CLI keeps its own handle,
and neither plane imports the other (layer direction unchanged).

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
persisted at submit (store v8: WITH its self-test markers, so the provenance
survives a restart and reaches the M6 operational projection —
REQ-SELFTEST-004) and the per-request ``RequestRollup``s + envelope
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

from cv_infra.contract.derive import materialize_request
from cv_infra.contract.errors import ContractError
from cv_infra.contract.job_spec import build_job_spec as _job_spec_for  # M1 owns the shape
from cv_infra.contract.loader import AdmittedRequest, load_request
from cv_infra.contract.schema import RequestEnvelope, ResourceBudget
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
from cv_infra.report.regression import identity_key

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


def report_outcome_of(results: list[JobResult], rollups: list[RequestRollup]) -> str:
    """Fold one envelope's terminal state into its ``report_outcome`` literal.

    Priority (M3 §7 / blueprint §9), and WHICH plane each level is read from:

    1. **errored** — any terminal job that produced no verdict at all. Read off the
       ``JobResult``s, i.e. still the JOB plane: a verdict-less sample is an infra outcome
       (exit-3 territory downstream, P5-13) and no request-level policy may average it
       away — ``roll_up`` deliberately drops it from ``verdicts`` rather than counting it
       as a failure, so asking the rollup about it would be asking the wrong object.
    2. **fail** — any request whose ROLLUP verdict is FAIL. This level is read off the
       REQUEST plane (p6c4 T1b 수리): the rollup is where the request's judgement policy
       lives (any-fail by default, ``min_pass_ratio`` when declared), and folding raw job
       verdicts here made a ratio-satisfying request read ``fail`` on the envelope/CLI exit
       while the report matrix — which consumes the same rollup — said ``pass``. One
       envelope must not answer the PR Check and the process exit differently.
    3. **pass** — everything else.

    For a request that declares NO ``min_pass_ratio`` the two planes are EQUIVALENT (the
    rollup's any-fail rule is exactly "some sample failed"), so every pre-p6 envelope folds
    byte-identically; that equivalence is pinned, not assumed
    (``test_orchestrator_batch.py::test_rollup_fold_equals_the_job_fold_without_a_ratio``).

    ``rollups`` is REQUIRED rather than optional-with-fallback on purpose: a default would
    be a SECOND fail policy living in this function, and the whole defect being repaired was
    two planes disagreeing about one envelope. Both callers already hold the rollups they
    persist/serve, and they pass those very instances (computed once — no re-aggregation).
    """
    if any(r.verdict is None for r in results):
        return REPORT_OUTCOME_ERRORED
    if any(rollup.verdict is Verdict.FAIL for rollup in rollups):
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


def _self_test_markers_of(body: dict[str, Any]) -> tuple[bool, str | None]:
    """Parse the optional ``is_self_test`` / ``origin`` envelope markers (p5c15).

    Both are M1 ``RequestEnvelope`` fields (``bool`` / ``str | None``) that the
    built-in stub envelope sets (``orchestrator/selftest.py`` — REQ-SELFTEST-001/002);
    absent = the M1 defaults (False / None), i.e. every existing submission is
    byte-identical. A present value must match the contract's type or it is a 422
    wrapper violation (same 8-key shape as an admit error) — a self-test marker is
    provenance, and provenance that silently coerces is worse than none.
    """
    is_self_test = body.get("is_self_test")
    if is_self_test is None:
        is_self_test = False
    elif not isinstance(is_self_test, bool):
        raise _wire_error(
            "is_self_test",
            "a boolean (self-test envelope marker, REQ-SELFTEST-002), or absent for false",
            repr(is_self_test),
            example='{"requests": [{...}], "is_self_test": true, "origin": "built-in-stub"}',
        )
    origin = body.get("origin")
    if origin is not None and not isinstance(origin, str):
        raise _wire_error(
            "origin",
            "a string recording where the request came from (REQ-SELFTEST-001"
            ' — the built-in stub uses "built-in-stub"), or absent',
            repr(origin),
            example='{"requests": [{...}], "is_self_test": true, "origin": "built-in-stub"}',
        )
    return is_self_test, origin


@dataclass(frozen=True)
class _ParsedEnvelope:
    """The validated wire wrapper (``_parse_envelope`` output) in one value."""

    documents: list[dict[str, Any]]
    plugin_dirs: list[str | None]
    trigger_source: str
    is_self_test: bool
    origin: str | None


def _parse_envelope(body: Any) -> _ParsedEnvelope:
    """Validate the internal wire wrapper -> ``_ParsedEnvelope``.

    Wrapper-only checks (each document's validation is the M1 loader's):
    the body must be a JSON object whose ``"requests"`` is a non-empty list
    of objects. ``"oracle_plugin_dirs"`` (wire v2, optional) must — when
    present — be an equal-length list of ``null`` (no anchor) or absolute
    directory path strings; absent/null field means no anchors (previous
    behavior, unchanged). ``"trigger_source"`` (p5c3, optional) is parsed by
    ``_trigger_source_of`` (absent -> ``human-manual``, illegal -> 422);
    ``"is_self_test"``/``"origin"`` (p5c15, optional) by ``_self_test_markers_of``.
    Anchors are same-host trusted paths (module docstring — MVP, M8 §8 g5).
    Violations raise ``ContractError`` (422).
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
    is_self_test, origin = _self_test_markers_of(body)
    return _ParsedEnvelope(
        documents=requests,
        plugin_dirs=_plugin_dirs_of(body, len(requests)),
        trigger_source=trigger_source,
        is_self_test=is_self_test,
        origin=origin,
    )


def _plugin_dirs_of(body: dict[str, Any], request_count: int) -> list[str | None]:
    """Parse the optional ``oracle_plugin_dirs`` per-request stage-5 anchors (p4c3).

    Absent (or explicit null) -> ``[None] * request_count``, i.e. no anchors (previous
    behavior, unchanged). When present it must be an equal-length list whose items are
    ``null`` (no anchor) or ABSOLUTE directory path strings; anything else is a 422
    wrapper violation (same 8-key shape as an admit error). Anchors are same-host
    trusted paths (module docstring — MVP, M8 §8 g5); existence is the loader's check,
    not this one's."""
    plugin_dirs = body.get("oracle_plugin_dirs")
    if plugin_dirs is None:  # field absent (or explicit null): no anchors — unchanged path
        return [None] * request_count
    if not isinstance(plugin_dirs, list) or len(plugin_dirs) != request_count:
        raise _wire_error(
            "oracle_plugin_dirs",
            f"a list of exactly {request_count} items — one per request, null = no anchor",
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
    return plugin_dirs


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


def _min_pass_ratio(request_dump: dict[str, Any]) -> float | None:
    """The request's own rollup judgement policy, off its M1 wire dump (p6 §0-14).

    ``execution_settings.min_pass_ratio`` — read from the SAME captured dump the
    report assembly consumes (전달-not-재도출: the admitted models are gone by
    completion time), so the live status read and the persisted report apply the
    same policy to the same request by construction. Absent / null / non-numeric
    -> None = the frozen any-fail rule (``rollup.roll_up``), never a guessed
    threshold. Like ``repeats`` it is deliberately OFF the JOB_SPEC wire
    (``_job_spec_for``): it judges the request's samples as a SET, which is not a
    fact about the one sample a runner holds.
    """
    settings = request_dump.get("execution_settings")
    if not isinstance(settings, dict):
        return None
    ratio = settings.get("min_pass_ratio")
    if isinstance(ratio, bool) or not isinstance(ratio, (int, float)):
        return None
    return float(ratio)


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
                rollup=roll_up(
                    rid, repeats, min_pass_ratio=_min_pass_ratio(record.request_dumps[rid])
                ),
                results=[_result_wire(r) for r in repeats],
            )
        )
    return inputs


@dataclass
class _AppState:
    """One app's wiring in a single value — the explicit parameter the handlers take.

    The route handlers below used to be CLOSURES over ``create_app``'s locals (p8c1 T2
    분해: that made the factory a 24-branch function and every handler unreachable from
    a test or a reader without going through it). They are module-level functions now
    and receive this state explicitly; the objects, their lifetimes and their sharing
    are exactly what the closure captured — this is per-app state, never a global.
    """

    store: Store
    runner: Runner
    k: int
    max_attempts: int
    retry_on_timeout: bool
    job_timeout_s: float | None
    allocator: DomainIdAllocator
    envelopes: dict[str, _EnvelopeRecord] = field(default_factory=dict)
    #: single-flight envelopes (module docstring)
    drive_lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    #: strong refs — a bare create_task can be GC'd
    drive_tasks: set[asyncio.Task[None]] = field(default_factory=set)


def _persist_terminal(store: Store, record: _EnvelopeRecord) -> None:
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
        # 잡별 상한 = 32 MiB provisional (결정 #2 · 실측-후-기입 §2-4). QA 재검(75개 실 bag,
        # MCAP-only): max 50.7 KB/s → 최악(scenario timeout 120s) ≈ 5.94 MiB; 32 MiB는
        # 5.38x 마진(정상 bag 미제외)·폭주(raw PointCloud2 >GB) 아래(오설정 bag 제외+경고).
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
    for inp in inputs:  # ④ rollups + outcome
        store.upsert_rollup(inp.rollup)
    # The SAME rollup instances that were just persisted (and that the report matrix
    # above already carries) decide the envelope outcome — one aggregation, one answer.
    store.complete_envelope(
        record.envelope_id,
        report_outcome=report_outcome_of(record.results, [inp.rollup for inp in inputs]),
    )


async def _drive(state: _AppState, record: _EnvelopeRecord, supervisor: ParallelSupervisor) -> None:
    """Supervise one envelope to completion, then write through (background task)."""
    async with state.drive_lock:
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
            _persist_terminal(state.store, record)
        except Exception as exc:  # persistence failure is loud too, never masked
            record.error = record.error or f"persist failed: {type(exc).__name__}: {exc}"
        finally:
            record.done = True


async def _parse_request(request: Request) -> _ParsedEnvelope:
    """Read + validate the submitted wire wrapper, or raise the structured 422.

    A malformed body and a wrapper violation both surface as
    ``{"detail": {"errors": [<annotation dict>]}}`` — never a 500, never a raw
    traceback (M3 §7 / NFR-INTAKE-001).
    """
    try:
        body = await request.json()
        return _parse_envelope(body)
    except json.JSONDecodeError as exc:
        err = _wire_error("(document)", "a JSON body", str(exc))
        raise HTTPException(status_code=422, detail={"errors": [err.to_annotation_dict()]}) from exc
    except ContractError as err:
        raise HTTPException(status_code=422, detail={"errors": [err.to_annotation_dict()]}) from err


def _materialize_envelope(
    state: _AppState,
    envelope_id: str,
    envelope: _ParsedEnvelope,
    admitted: list[AdmittedRequest],
) -> tuple[_EnvelopeRecord, ParallelSupervisor]:
    """Fan the admitted requests out into jobs and build this envelope's supervision.

    Everything the fan-out stamps onto a job (stage-5 anchor, canonical JOB_SPEC,
    request identity key) is derived from ONE per-request wire dump, computed here and
    reused by the completion-time report assembly (``_EnvelopeRecord.request_dumps``).
    The durable registry is written BEFORE the queue exists (p4c4 영속), so a restart
    can serve status for this envelope even though the in-memory record dies with us.
    """
    request_ids = [f"{envelope_id}/r{i}" for i in range(len(envelope.documents))]
    repeats = [a.request.execution_settings.repeats for a in admitted]
    jobs = fan_out_requests(list(zip(request_ids, repeats, strict=True)))
    anchor_of = dict(zip(request_ids, envelope.plugin_dirs, strict=True))
    admitted_of = dict(zip(request_ids, admitted, strict=True))
    # ONE wire dump per request, computed here and reused by BOTH consumers: the
    # runner-facing identity key just below and the completion-time report assembly
    # (``_EnvelopeRecord.request_dumps``). Same bytes in => same key out, so the
    # runner's CV_REQUEST_IDENTITY_KEY equals the report row's request_identity_key
    # by construction rather than by a parallel derivation.
    request_dumps = {
        rid: admitted_of[rid].request.model_dump(mode="json", by_alias=True) for rid in request_ids
    }
    # p5c18 T4 (DoD-P2-06 ①): the M4 단일 정의를 IMPORT 해서 부른다 (G-56). Deriving
    # the key from any other input (e.g. the JOB_SPEC) would produce *a different key
    # under the same name* — worse than the null the job plane reported until now.
    identity_of = {rid: identity_key(dump) for rid, dump in request_dumps.items()}
    for job in jobs:
        # D-1 wiring #3 (p4c4): the stage-5 anchor rides each fanned-out job
        # so the runner seam can hand it to run_job(oracle_plugin_dir=...).
        job.oracle_plugin_dir = anchor_of[job.request_id]
        # p4c4 glue (T1 §7-1 (a)): the ADMITTED model materializes into the
        # canonical per-job JOB_SPEC riding (and persisting with) the job —
        # the production runner seam (RunJobRunner) drives run_job off it.
        # p6c3: the request is first materialized for THIS job's sample index
        # (derive.materialize_request) — a static document is returned
        # unchanged (same object), so this line is byte-identical for every
        # pre-p6 request; a randomized one yields sample `repeat_index`.
        job.job_spec = _job_spec_for(
            materialize_request(admitted_of[job.request_id].request, job.repeat_index),
            job_key(job),
        )
        # p5c18 T4: the request identity key rides the job to the runner env, so the
        # job plane's own artifacts can name the request that produced them.
        job.request_identity_key = identity_of[job.request_id]
    # Durable registry FIRST (p4c4 영속): a restart can then serve status for
    # this envelope even though the in-memory record below dies with us. The
    # self-test markers ride the SAME write (v8) — the operational projection
    # reads them from the store, so they survive a restart too.
    state.store.record_envelope(
        envelope_id,
        request_ids,
        envelope.plugin_dirs,
        is_self_test=envelope.is_self_test,
        origin=envelope.origin,
    )
    queue = JobQueue(  # persists every job QUEUED via the store (REQ-ORCH-011)
        jobs,
        store=state.store,
        max_attempts=state.max_attempts,
        retry_on_timeout=state.retry_on_timeout,
    )
    supervisor = ParallelSupervisor(
        queue,
        SlotAccountant(k=state.k),
        state.runner,
        allocator=state.allocator,
        job_timeout_s=state.job_timeout_s,
    )
    record = _EnvelopeRecord(
        envelope_id=envelope_id,
        request_ids=request_ids,
        jobs=jobs,
        # Capture each request's M1 wire dump AT SUBMIT (p5c2 report seam): the
        # completion-time assembly consumes it for identity_key/sut_ref/scenario
        # (전달-not-재도출) — the admitted models would otherwise be gone by then.
        # Same object the job-plane keys above were derived from (one dump, two uses).
        request_dumps=request_dumps,
        # p5c3: submitted value (or default), recorded verbatim
        trigger_source=envelope.trigger_source,
    )
    return record, supervisor


async def _submit_envelope(state: _AppState, request: Request) -> dict[str, str]:
    """``POST /envelopes`` — admit all, fan out, start supervision, return 202."""
    envelope = await _parse_request(request)
    admitted, errors = _admit_all(envelope.documents, envelope.plugin_dirs)
    if errors:  # all-or-nothing: zero jobs were created (비전파)
        raise HTTPException(status_code=422, detail={"errors": errors})

    envelope_id = f"env-{uuid.uuid4().hex[:12]}"
    record, supervisor = _materialize_envelope(state, envelope_id, envelope, admitted)
    state.envelopes[envelope_id] = record
    task = asyncio.get_running_loop().create_task(_drive(state, record, supervisor))
    state.drive_tasks.add(task)
    task.add_done_callback(state.drive_tasks.discard)
    return {"envelope_id": envelope_id}


def _status_from_store(store: Store, envelope_id: str) -> dict[str, Any]:
    """Serve status for an envelope this process never saw (restart path, p4c4).

    Everything comes from the persisted registry / jobs / rollups — never
    recomputed from results (which did not survive the restart). A crash /
    restart marker surfaces as the same loud 500 the in-memory path uses.
    """
    stored = store.load_envelope(envelope_id)
    if stored is None:
        raise HTTPException(status_code=404, detail=f"unknown envelope {envelope_id!r}")
    if stored.error is not None:
        raise HTTPException(status_code=500, detail=f"envelope supervision crashed: {stored.error}")
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


async def _envelope_status(state: _AppState, envelope_id: str) -> dict[str, Any]:
    """``GET /envelopes/{id}`` — the live record when this process owns it, else the store."""
    record = state.envelopes.get(envelope_id)
    if record is None:
        return _status_from_store(state.store, envelope_id)
    if record.error is not None:
        raise HTTPException(status_code=500, detail=f"envelope supervision crashed: {record.error}")
    rollups = [
        roll_up(
            rid,
            [r for r in record.results if r.job.request_id == rid],
            min_pass_ratio=_min_pass_ratio(record.request_dumps[rid]),
        )
        for rid in record.request_ids
    ]
    return _status_body(
        envelope_id,
        status="completed" if record.done else "running",
        jobs=record.jobs,
        rollups=rollups,
        # Same instances the response body carries (no second aggregation).
        report_outcome=report_outcome_of(record.results, rollups) if record.done else None,
    )


async def _envelope_report(store: Store, envelope_id: str) -> dict[str, Any]:
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
    raise HTTPException(status_code=409, detail={"reason": "not-terminal", "status": stored.status})


def create_app(
    store: Store,
    runner: Runner,
    *,
    k: int,
    max_attempts: int = 1,
    retry_on_timeout: bool = True,
    job_timeout_s: float | None = None,
    resource_budget: ResourceBudget | None = None,
) -> FastAPI:
    """Build the submit-surface app around an injected store + runner seam.

    ``runner`` is the per-job blocking seam ``ParallelSupervisor`` drives
    (CPU tests inject fakes; production injects ``supervisor.RunJobRunner`` —
    the env-configured wiring is ``serve.build_app``). ``k`` is the computed
    concurrency cap (``compute_k`` output — never a constant); the queue
    policy knobs mirror ``JobQueue``.

    ``resource_budget`` is the operator Resource Budget k was computed FROM
    (REQ-DEPLOY-012, built by ``serve``); it is carried, never re-derived, and
    only reaches the operational read model — no scheduling decision reads it
    here, and it never touches a domain result surface. Default None = the
    caller supplied no budget (CPU test apps, and any deployment whose VRAM
    figure is unset), reported as ``null`` rather than invented.

    Wiring only (p8c1 T2): every handler body is a module-level function above,
    so this function stays a route table — the app-scoped state they share is the
    ONE ``_AppState`` value built here and passed explicitly.
    """
    app = FastAPI(title="cv-infra orchestrator", docs_url=None, redoc_url=None)
    state = _AppState(
        store=store,
        runner=runner,
        k=k,
        max_attempts=max_attempts,
        retry_on_timeout=retry_on_timeout,
        job_timeout_s=job_timeout_s,
        allocator=DomainIdAllocator(store),
    )

    @app.post("/envelopes", status_code=202)
    async def submit_envelope(request: Request) -> dict[str, str]:
        return await _submit_envelope(state, request)

    @app.get("/envelopes/{envelope_id}")
    async def envelope_status(envelope_id: str) -> dict[str, Any]:
        return await _envelope_status(state, envelope_id)

    @app.get("/envelopes/{envelope_id}/report")
    async def envelope_report(envelope_id: str) -> dict[str, Any]:
        """Serve the DURABLE assembled VerificationReport (p5c2, 재시작 생존).

        Always the persisted twin (never re-assembled): a completed envelope's
        report was written by ``_persist_terminal`` and is returned verbatim (200).
        Absence is disambiguated off the envelope registry — unknown -> 404 (same
        body as ``GET /envelopes/{id}``); a supervision-crash marker -> 409
        supervision-error; a still-in-flight envelope -> 409 not-terminal.
        """
        return await _envelope_report(state.store, envelope_id)

    # M6 operational view (DoD-P4-12/13): read-only projection surfaces on the
    # SAME app (no separate server). Routes only — the resident sampler is wired
    # in production (serve.build_app), never on the TestClient path. The admission
    # budget k rides along so the operator reads it next to running_k (D-7 (C)),
    # together with the Resource Budget it was computed from (REQ-DEPLOY-012).
    register_monitor(app, store, concurrency_budget_k=k, resource_budget=resource_budget)
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
