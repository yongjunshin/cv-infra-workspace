"""``cv-infra {submit,status,wait}`` — the batch/orchestrator CLI surface (M8).

Phase-4 counterpart of ``run`` (REQ-INTAKE-001/003, M8 §3.1): ``submit`` sends
a RequestEnvelope (scenario file-reference list, decision
2026-07-13-p4c3-envelope-file-refs) to the M3 REST surface (``POST
/envelopes``), ``wait`` polls ``GET /envelopes/{id}`` until the terminal
aggregated verdict and folds it into the exit-code contract, ``status`` is a
purely informational read. Lazily imported by ``main`` dispatch only — the
``--help`` path never loads httpx/fastapi/pydantic.

Exit-code mapping — single source in ``cli/exit_codes.py`` (LOCKED §9 / D-I, M8-D11)
-----------------------------------------------------------------------------------
``REPORT_OUTCOME_EXIT`` / ``exit_code_for_report_outcome`` are imported from the
dependency-0 leaf ``cv_infra.cli.exit_codes`` (재정의 금지 — the M4
``report/github.py`` renderer imports that SAME module for the exit→conclusion
half of the contract). The fold maps the envelope-level ``report_outcome``
literal the orchestrator aggregates to the process exit code::

    report_outcome   exit   meaning
    --------------   ----   -------------------------------------------------
    pass             0      every request passed
    fail             1      >=1 request FAILed (SUT verdict)
    errored          3      >=1 job ended without a verdict (infra outcome)

The errored>fail PRIORITY itself lives upstream in ``api.report_outcome_of``
(single fold, errored 우선): by the time this table is consulted the literal
is already the batch aggregate, so ``errored -> 3`` IS "exit 3 outranks 1"
(infra noise never masquerades as a self-regression — D-I). Contract/usage
errors (client-side ``load_envelope`` rejection, server-side 422, unknown
envelope id, bad flags) take the separate ``EXIT_CONTRACT`` (2) path; an
unreachable orchestrator or an unknown/absent outcome folds to ``EXIT_INFRA``
(3), never to FAIL — same fold rule as ``main._VERDICT_EXIT``.

Per-command exit scope (D-O, M8 §3.2): only verdict-granting commands
(``submit --wait`` / ``wait``) gate on 0/1; ``status`` returns 0 even for a
failed batch (a query must never turn a CI job red by itself — only 404 -> 2
and infra -> 3).

Seams (G-17 — M1 T1 lands ``contract/envelope.py`` in parallel)
---------------------------------------------------------------
* ``_load_envelope`` is the ONE adapter around the M1 envelope loader; nothing
  else in this module touches ``cv_infra.contract.envelope``. Client-side
  pre-validation failure = the M1 friendly prose on stderr + exit 2, and the
  server is NEVER called. Unit tests monkeypatch this seam with
  verbatim-shaped stubs; the real-loader round-trip is covered by the E2E
  tests in ``tests/test_cli_batch.py`` (Wave-2 integration).
* ``_wire_body`` builds the wire-v2 POST body ``{"requests": [<raw_doc>...],
  "oracle_plugin_dirs": [...]}``. The anchor field (equal length; each item =
  the scenario file's parent dir, absolute — the stage-5 custom-oracle
  anchor, same-host trusted path per api.py) is ON since the M3 server
  acceptance landed (Wave 2); ``_INCLUDE_ORACLE_PLUGIN_DIRS`` stays as the
  explicit escape hatch. ``trigger_source`` (REQ-INTAKE-003) rides as an
  OPTIONAL top-level key: the CLI carries the ``--trigger-source`` flag value
  verbatim EXCEPT the default (human-manual), which is OMITTED so the server
  default applies (verbatim p5c3 data contract: "부재/기본 시 키 생략 허용") — a
  plain human ``submit`` stays byte-identical to the pre-p5c3 wire, and only a
  non-default (ci-cd, set by the Action) records provenance on the wire.
* ``_make_client`` builds the httpx ``AsyncClient`` (tests inject an
  ``ASGITransport`` over the real FastAPI app here). The command bodies are
  async for exactly this reason; each command is one ``asyncio.run``.

API base resolution: ``--api`` flag > ``CV_INFRA_API`` env > 127.0.0.1:8000
(current MVP topology = same host, M8 §8 cicd-g5). Polling cadence is the one
module constant ``_POLL_INTERVAL_S`` — no config layer.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
import time
from typing import Any

import httpx

from cv_infra.cli.exit_codes import (
    EXIT_CONTRACT,
    EXIT_INFRA,
    EXIT_PASS,
    exit_code_for_report_outcome,
)
from cv_infra.cli.exit_codes import (
    REPORT_OUTCOME_EXIT as REPORT_OUTCOME_EXIT,  # re-exported (test back-compat, 재정의 금지)
)
from cv_infra.cli.main import _one_line
from cv_infra.contract.errors import ANNOTATION_KEYS, ContractError
from cv_infra.orchestrator.api import REPORT_OUTCOME_ERRORED

#: Default orchestrator base URL (MVP topology: CLI and orchestrator share the
#: host — M8 §8 cicd-g5). Overridden by ``CV_INFRA_API`` env or ``--api``.
_DEFAULT_API = "http://127.0.0.1:8000"
_API_ENV = "CV_INFRA_API"

#: The one polling-cadence constant (no config layer). Verification takes
#: minutes (M3 §7 async submission), so a 1s poll is generous, not chatty.
_POLL_INTERVAL_S = 1.0

#: Wire v2 anchor field: ``oracle_plugin_dirs`` rides the POST body
#: equal-length with ``requests`` (scenario parent dirs — stage-5 custom
#: oracle anchors, same-host trusted paths per api.py). ON since the M3
#: server-side acceptance landed (Wave 2 merge, cycle p4-batch-cli); the gate
#: stays as an explicit escape hatch (both states unit-pinned).
_INCLUDE_ORACLE_PLUGIN_DIRS = True

#: The wire trigger_source default (REQ-INTAKE-003). MIRRORS the M1
#: ``RequestEnvelope.trigger_source`` Literal default AND
#: ``api._DEFAULT_TRIGGER_SOURCE`` (kept as a bare string so this batch module
#: does not import the contract/orchestrator just for its default) — a value
#: equal to this is OMITTED from the POST body so the server default applies.
_DEFAULT_TRIGGER_SOURCE = "human-manual"

# --- seams (module docstring) ------------------------------------------------


def _load_envelope(source: str) -> Any:
    """SINGLE adapter around the M1 envelope loader (G-17 seam).

    Verbatim producer contract (T1, same task pin)::

        LoadedEnvelope(api_version: str, requests: tuple[LoadedRequestRef, ...])
        LoadedRequestRef(admitted, raw_doc: dict, scenario_path: str,
                         oracle_plugin_dir: str)
        load_envelope(source: str | Path) -> LoadedEnvelope   # 실패 = ContractError

    Lazy import (keeps pydantic/yaml off the non-batch CLI paths). Unit tests
    stub THIS function; the real-loader round-trip is exercised end-to-end in
    ``tests/test_cli_batch.py`` (Wave-2 integration: real ``load_envelope`` ->
    wire v2 -> real server admit).
    """
    from cv_infra.contract.envelope import load_envelope

    return load_envelope(source)


def _make_client(api_base: str) -> httpx.AsyncClient:
    """HTTP client seam — tests inject ``httpx.ASGITransport`` here."""
    return httpx.AsyncClient(base_url=api_base)


def _resolve_api(flag_value: str | None) -> str:
    """``--api`` flag > ``CV_INFRA_API`` env > the localhost default."""
    return flag_value or os.environ.get(_API_ENV) or _DEFAULT_API


def _wire_trigger_source(flag: str) -> str | None:
    """Fold the ``--trigger-source`` flag to the wire value (REQ-INTAKE-003).

    The default (human-manual) folds to ``None`` = OMITTED from the body (the
    server default applies — verbatim p5c3 data contract); a non-default value
    (ci-cd, set by the Action) rides verbatim so the orchestrator records the
    provenance. Pure so the omission rule is unit-pinned without the ASGI wiring.
    """
    return None if flag == _DEFAULT_TRIGGER_SOURCE else flag


def _wire_body(envelope: Any, trigger_source: str | None = None) -> dict[str, Any]:
    """LoadedEnvelope -> wire-v2 POST body (raw docs verbatim, D-1 internal
    representation). ``oracle_plugin_dirs`` (equal-length stage-5 anchors) is
    emitted by default — see the module gate ``_INCLUDE_ORACLE_PLUGIN_DIRS``.
    ``trigger_source`` rides as an OPTIONAL top-level key iff provided (non-None):
    the caller passes ``_wire_trigger_source(args.trigger_source)`` so the default
    (human-manual) is omitted and only a ci-cd trigger records provenance."""
    body: dict[str, Any] = {"requests": [ref.raw_doc for ref in envelope.requests]}
    if _INCLUDE_ORACLE_PLUGIN_DIRS:
        body["oracle_plugin_dirs"] = [ref.oracle_plugin_dir for ref in envelope.requests]
    if trigger_source is not None:
        body["trigger_source"] = trigger_source
    return body


# --- rendering helpers --------------------------------------------------------


def _render_rejection(command: str, response: httpx.Response) -> None:
    """Render the orchestrator's structured 422 as M1 friendly prose.

    The 422 detail is ``{"errors": [<8-key ContractError annotation dict>,
    ...]}`` (api.py, all-or-nothing admit) — each entry is re-hydrated into a
    ``ContractError`` so stderr shows EXACTLY the prose a local rejection
    would (M1 owns the format, the CLI invents none; raw traceback 0).
    """
    entries: Any = None
    try:
        entries = response.json().get("detail", {}).get("errors")
    except (ValueError, AttributeError):
        pass
    if not isinstance(entries, list) or not entries:
        print(f"cv-infra {command}: envelope rejected (422): {response.text}", file=sys.stderr)
        return
    for entry in entries:
        if isinstance(entry, dict):
            kwargs = {k: entry[k] for k in ANNOTATION_KEYS if entry.get(k) is not None}
            print(f"cv-infra {command}: {ContractError(**kwargs)}", file=sys.stderr)
        else:
            print(f"cv-infra {command}: {entry}", file=sys.stderr)


def _infra(command: str, message: str) -> int:
    """Uniform infra-error stderr line (never a SUT verdict) + exit 3."""
    print(
        f"cv-infra {command}: {message} — infrastructure error, not a SUT verdict",
        file=sys.stderr,
    )
    return EXIT_INFRA


def _body_json(response: httpx.Response) -> Any:
    try:
        return response.json()
    except ValueError:
        return None


# --- command bodies -------------------------------------------------------------


async def _poll_until_terminal(
    client: httpx.AsyncClient, command: str, envelope_id: str, timeout_s: float | None
) -> int:
    """Poll envelope status until terminal, then fold ``report_outcome`` to exit.

    Shared by ``wait`` and ``submit --wait`` (identical semantics — M8-D11).
    ``--timeout`` exceeded = the envelope is NOT terminal within budget = an
    infrastructure condition (3); at least one poll always happens. 404 is a
    usage/contract error (2): the id does not exist on this orchestrator.
    """
    deadline = None if timeout_s is None else time.monotonic() + timeout_s
    while True:
        try:
            response = await client.get(f"/envelopes/{envelope_id}")
        except httpx.HTTPError as exc:
            return _infra(command, f"orchestrator unreachable: {_one_line(exc)}")
        if response.status_code == 404:
            print(
                f"cv-infra {command}: unknown envelope id {envelope_id!r} — use the id "
                "printed by 'cv-infra submit' against the same orchestrator (--api)",
                file=sys.stderr,
            )
            return EXIT_CONTRACT
        if response.status_code == 500:
            return _infra(command, f"envelope supervision crashed: {response.text}")
        if response.status_code != 200:
            return _infra(command, f"unexpected orchestrator response {response.status_code}")
        body = _body_json(response)
        if not isinstance(body, dict):
            return _infra(command, "orchestrator returned a non-JSON status body")
        if body.get("status") == "completed":
            outcome = body.get("report_outcome")
            code = exit_code_for_report_outcome(outcome)
            if code == EXIT_INFRA and outcome != REPORT_OUTCOME_ERRORED:
                print(
                    f"cv-infra {command}: unknown report_outcome {outcome!r} — "
                    "treating as infrastructure error",
                    file=sys.stderr,
                )
            print(
                f"cv-infra {command}: envelope {envelope_id} "
                f"report_outcome={outcome} exit={code}"
            )
            return code
        if deadline is not None and time.monotonic() >= deadline:
            return _infra(
                command,
                f"envelope {envelope_id} not terminal within --timeout {timeout_s}s",
            )
        await asyncio.sleep(_POLL_INTERVAL_S)


async def _submit_async(envelope: Any, args: argparse.Namespace) -> int:
    api = _resolve_api(args.api)
    wire_trigger = _wire_trigger_source(args.trigger_source)
    async with _make_client(api) as client:
        try:
            response = await client.post("/envelopes", json=_wire_body(envelope, wire_trigger))
        except httpx.HTTPError as exc:
            return _infra("submit", f"orchestrator unreachable at {api}: {_one_line(exc)}")
        if response.status_code == 422:
            # Server-side re-rejection (the server re-runs the M1 admit gate —
            # authoritative even after client pre-validation passed).
            _render_rejection("submit", response)
            return EXIT_CONTRACT
        if response.status_code != 202:
            return _infra("submit", f"unexpected orchestrator response {response.status_code}")
        body = _body_json(response)
        envelope_id = body.get("envelope_id") if isinstance(body, dict) else None
        if not isinstance(envelope_id, str) or not envelope_id:
            return _infra("submit", "202 response carried no envelope_id")
        print(envelope_id)  # bare id on stdout: ID=$(cv-infra submit ...) stays scriptable
        if not args.wait:
            return EXIT_PASS  # submission accepted; verdict gating needs --wait (D-O)
        return await _poll_until_terminal(client, "submit", envelope_id, args.timeout)


async def _wait_async(args: argparse.Namespace) -> int:
    async with _make_client(_resolve_api(args.api)) as client:
        return await _poll_until_terminal(client, "wait", args.envelope_id, args.timeout)


async def _status_async(args: argparse.Namespace) -> int:
    async with _make_client(_resolve_api(args.api)) as client:
        try:
            response = await client.get(f"/envelopes/{args.envelope_id}")
        except httpx.HTTPError as exc:
            return _infra("status", f"orchestrator unreachable: {_one_line(exc)}")
        if response.status_code == 404:
            print(
                f"cv-infra status: unknown envelope id {args.envelope_id!r} — use the id "
                "printed by 'cv-infra submit' against the same orchestrator (--api)",
                file=sys.stderr,
            )
            return EXIT_CONTRACT
        if response.status_code == 500:
            return _infra("status", f"envelope supervision crashed: {response.text}")
        if response.status_code != 200:
            return _infra("status", f"unexpected orchestrator response {response.status_code}")
        body = _body_json(response)
        if body is None:
            return _infra("status", "orchestrator returned a non-JSON status body")
        print(json.dumps(body, indent=2))
        return EXIT_PASS  # informational: the verdict NEVER rides this exit (D-O)


# --- entry points (main.py dispatch) -----------------------------------------


def cmd_submit(args: argparse.Namespace) -> int:
    """``cv-infra submit <envelope.yaml> [--wait] [--timeout N] [--api URL]``.

    Client-side pre-validation FIRST (M1 ``load_envelope`` via the adapter
    seam): a rejected envelope renders the M1 friendly prose + exit 2 and the
    orchestrator is never contacted (NFR-INTAKE-001/003 정신). Accepted ->
    wire-v2 POST; ``--wait`` joins the shared terminal-verdict poll.
    """
    if args.timeout is not None and not args.wait:
        print("cv-infra submit: --timeout requires --wait", file=sys.stderr)
        return EXIT_CONTRACT
    try:
        envelope = _load_envelope(args.envelope)
    except ContractError as err:
        print(f"cv-infra submit: {err}", file=sys.stderr)
        return EXIT_CONTRACT
    return asyncio.run(_submit_async(envelope, args))


def cmd_wait(args: argparse.Namespace) -> int:
    """``cv-infra wait <envelope_id> [--timeout N] [--api URL]`` -> verdict exit."""
    return asyncio.run(_wait_async(args))


def cmd_status(args: argparse.Namespace) -> int:
    """``cv-infra status <envelope_id> [--api URL]`` — informational (exit 0)."""
    return asyncio.run(_status_async(args))


# --- report: informational review surface (D-O, M8 §3.2 / §3.7) --------------
# A thin client over the M4 VerificationReport the orchestrator assembles +
# persists and serves at ``GET /envelopes/{id}/report``. A FETCHED report
# ALWAYS exits 0 even when the report itself says fail (a query must never gate
# CI by itself — D-O); only an infra/lookup problem (unreachable / unknown id /
# not-terminal / crash) exits 3. exit 1 and 2 are structurally unreachable on
# this path — an informational read is never a SUT verdict nor a contract error.


def _matrix_view(report: dict[str, Any]) -> dict[str, Any]:
    """Adapt a VerificationReport JSON into ``report.matrix.render_text``'s
    ``build_matrix`` shape (the report row carries a richer rollup than the P4
    preview table). The report JSON rollup exposes ``flaky`` (bool), not the
    ``flakiness`` float the P4 column formats, so that column renders ``-`` here
    — the full per-row detail stays available via ``--json``. Fields read here
    are anchored to the canonical fixture
    (``tests/test_report_verification_report.py``)."""
    summary = report.get("summary", {})
    rows: list[dict[str, Any]] = []
    for row in report.get("matrix", []):
        rollup = row.get("rollup", {})
        verdicts = rollup.get("verdicts", [])
        rows.append(
            {
                "request_id": row.get("request_id"),
                "verdict": rollup.get("verdict"),
                "flakiness": None,
                "jobs": rollup.get("repeats", len(verdicts)),
                "counts": {"pass": verdicts.count("pass"), "fail": verdicts.count("fail")},
            }
        )
    return {
        "matrix": rows,
        "summary": {
            "total": summary.get("total", len(rows)),
            "passed": summary.get("passed", 0),
            "failed": summary.get("failed", 0),
            "errored": summary.get("errored", 0),
        },
    }


def _render_baseline(report: dict[str, Any]) -> None:
    """C-1 baseline boundary messaging (M8 §3.7, LOCKED §13).

    baseline lives ONLY in cv-infra's internal SQLite — consumer CI/git history
    is never consulted (REQ-REPORT-006). Absent baseline = regression check
    *skip(정상)*, never a failure (REQ-REPORT-004); regressed = which request
    got worse against which SUT/date (NFR-REPORT-001).
    """
    bsum = report.get("baseline_summary") or {}
    absent = bsum.get("absent", 0)
    regressed = bsum.get("regressed", 0)
    if absent:
        print(
            f"baseline: {absent} request(s) had no baseline — regression check skipped "
            "(normal for a first run / new SUT, not a failure)."
        )
    if regressed:
        print(f"baseline: {regressed} request(s) regressed vs baseline:")
        for row in report.get("matrix", []):
            reg = row.get("regression") or {}
            if reg.get("status") == "regressed":
                print(
                    f"  {reg.get('detail')} — vs SUT {reg.get('baseline_sut_ref')} "
                    f"(baseline established {reg.get('baseline_established_at')})"
                )
    elif not absent:
        print("baseline: no regressions vs the established baseline.")


def _render_report(report: dict[str, Any]) -> None:
    """Human-readable review render: header + the M4 matrix table + the
    domain/outcome line + C-1 baseline messaging (D-O informational)."""
    from cv_infra.report.matrix import render_text

    summary = report.get("summary", {})
    print(
        f"Verification report — envelope {report.get('envelope_id')} "
        f"(trigger={report.get('trigger_source')}, generated {report.get('generated_at')})"
    )
    print(render_text(_matrix_view(report)))
    print(
        f"outcome: verdict={summary.get('verdict')} "
        f"report_outcome={summary.get('report_outcome')}"
    )
    _render_baseline(report)


def _report_not_ready(args: argparse.Namespace, response: httpx.Response) -> int:
    """Render a 409 (report not servable yet): the envelope is not terminal or
    its supervision crashed (contract pin, decision p5c2). Both are infra
    conditions (3); not-terminal points the user at ``cv-infra wait``."""
    detail: Any = {}
    body = _body_json(response)
    if isinstance(body, dict) and isinstance(body.get("detail"), dict):
        detail = body["detail"]
    reason = detail.get("reason")
    if reason == "not-terminal":
        status = detail.get("status")
        suffix = f" (status={status})" if status else ""
        return _infra(
            "report",
            f"envelope {args.envelope_id} is not terminal yet{suffix}; block on it with "
            f"'cv-infra wait {args.envelope_id}' to get the verdict",
        )
    if reason == "supervision-error":
        return _infra("report", f"envelope supervision crashed: {detail.get('error')}")
    return _infra("report", f"envelope report unavailable (409): {response.text}")


async def _report_async(args: argparse.Namespace) -> int:
    async with _make_client(_resolve_api(args.api)) as client:
        try:
            response = await client.get(f"/envelopes/{args.envelope_id}/report")
        except httpx.HTTPError as exc:
            return _infra("report", f"orchestrator unreachable: {_one_line(exc)}")
        if response.status_code == 404:
            # A missing envelope on an informational read is a lookup/infra
            # condition (3), NOT a contract error (2): report never returns 1/2.
            return _infra(
                "report",
                f"unknown envelope id {args.envelope_id!r} — use the id printed by "
                "'cv-infra submit' against the same orchestrator (--api)",
            )
        if response.status_code == 409:
            return _report_not_ready(args, response)
        if response.status_code != 200:
            return _infra("report", f"unexpected orchestrator response {response.status_code}")
        body = _body_json(response)
        if not isinstance(body, dict):
            return _infra("report", "orchestrator returned a non-JSON report body")
        if args.json:
            print(json.dumps(body, indent=2))
        else:
            _render_report(body)
        return EXIT_PASS  # informational: a fail report NEVER rides this exit (D-O)


def cmd_report(args: argparse.Namespace) -> int:
    """``cv-infra report <envelope_id> [--json] [--api URL]`` — informational
    review surface (D-O). Fetch the M4 report; exit 0 on any served report
    (even a failing one), exit 3 only on an infra/lookup problem."""
    return asyncio.run(_report_async(args))
