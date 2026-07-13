"""``cv-infra {submit,status,wait}`` — the batch/orchestrator CLI surface (M8).

Phase-4 counterpart of ``run`` (REQ-INTAKE-001/003, M8 §3.1): ``submit`` sends
a RequestEnvelope (scenario file-reference list, decision
2026-07-13-p4c3-envelope-file-refs) to the M3 REST surface (``POST
/envelopes``), ``wait`` polls ``GET /envelopes/{id}`` until the terminal
aggregated verdict and folds it into the exit-code contract, ``status`` is a
purely informational read. Lazily imported by ``main`` dispatch only — the
``--help`` path never loads httpx/fastapi/pydantic.

Exit-code mapping — M8 single source (LOCKED §9 / D-I, M8-D11)
---------------------------------------------------------------
``REPORT_OUTCOME_EXIT`` maps the envelope-level ``report_outcome`` literal the
orchestrator aggregates (imported from ``cv_infra.orchestrator.api`` — never
re-defined here) to the process exit code::

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
  server is NEVER called. Tests monkeypatch this seam with verbatim-shaped
  stubs; the real-loader round-trip is measured at the PM merge gate.
* ``_wire_body`` builds the wire-v2 POST body ``{"requests": [<raw_doc>...]}``.
  The ``oracle_plugin_dirs`` sibling field (equal length, absolute dirs) is
  CODE-READY behind ``_INCLUDE_ORACLE_PLUGIN_DIRS`` but stays OFF the wire
  until the M3 server-side acceptance lands (Wave 2 — PM flips the flag at
  that merge gate).
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

from cv_infra.cli.main import EXIT_CONTRACT, EXIT_FAIL, EXIT_INFRA, EXIT_PASS, _one_line
from cv_infra.contract.errors import ANNOTATION_KEYS, ContractError
from cv_infra.orchestrator.api import (
    REPORT_OUTCOME_ERRORED,
    REPORT_OUTCOME_FAIL,
    REPORT_OUTCOME_PASS,
)

#: Default orchestrator base URL (MVP topology: CLI and orchestrator share the
#: host — M8 §8 cicd-g5). Overridden by ``CV_INFRA_API`` env or ``--api``.
_DEFAULT_API = "http://127.0.0.1:8000"
_API_ENV = "CV_INFRA_API"

#: The one polling-cadence constant (no config layer). Verification takes
#: minutes (M3 §7 async submission), so a 1s poll is generous, not chatty.
_POLL_INTERVAL_S = 1.0

#: Wire v2 prepared field (task pin 2026-07-13): ``oracle_plugin_dirs`` rides
#: the POST body equal-length with ``requests`` once the M3 server accepts it
#: (Wave 2). OFF by default — the current server ignores unknown wrapper keys,
#: but the contract stays explicit until both sides speak it.
_INCLUDE_ORACLE_PLUGIN_DIRS = False

#: ``report_outcome`` literal -> exit code. THE M8 single source (D-I) —
#: literals imported from ``cv_infra.orchestrator.api`` (재정의 금지), exit
#: constants from ``cv_infra.cli.main`` (the Phase-0 contract). See the module
#: docstring for the priority/scope semantics.
REPORT_OUTCOME_EXIT: dict[str, int] = {
    REPORT_OUTCOME_PASS: EXIT_PASS,
    REPORT_OUTCOME_FAIL: EXIT_FAIL,
    REPORT_OUTCOME_ERRORED: EXIT_INFRA,
}


def exit_code_for_report_outcome(outcome: Any) -> int:
    """Pure fold: terminal ``report_outcome`` -> process exit code.

    Unknown or absent outcomes fold to ``EXIT_INFRA`` (3), NEVER to FAIL —
    the same rule ``main._VERDICT_EXIT`` applies to unknown verdicts (a value
    we cannot interpret is a platform problem, not a SUT judgement).
    """
    if isinstance(outcome, str) and outcome in REPORT_OUTCOME_EXIT:
        return REPORT_OUTCOME_EXIT[outcome]
    return EXIT_INFRA


# --- seams (module docstring) ------------------------------------------------


def _load_envelope(source: str) -> Any:
    """SINGLE adapter around the M1 envelope loader (G-17 seam).

    Verbatim producer contract (T1, same task pin)::

        LoadedEnvelope(api_version: str, requests: tuple[LoadedRequestRef, ...])
        LoadedRequestRef(admitted, raw_doc: dict, scenario_path: str,
                         oracle_plugin_dir: str)
        load_envelope(source: str | Path) -> LoadedEnvelope   # 실패 = ContractError

    Lazy import: ``cv_infra.contract.envelope`` lands in the parallel M1
    branch — tests stub THIS function, and the real round-trip is measured at
    the PM merge gate.
    """
    from cv_infra.contract.envelope import load_envelope

    return load_envelope(source)


def _make_client(api_base: str) -> httpx.AsyncClient:
    """HTTP client seam — tests inject ``httpx.ASGITransport`` here."""
    return httpx.AsyncClient(base_url=api_base)


def _resolve_api(flag_value: str | None) -> str:
    """``--api`` flag > ``CV_INFRA_API`` env > the localhost default."""
    return flag_value or os.environ.get(_API_ENV) or _DEFAULT_API


def _wire_body(envelope: Any) -> dict[str, Any]:
    """LoadedEnvelope -> wire-v2 POST body (raw docs verbatim, D-1 internal
    representation). ``oracle_plugin_dirs`` is prepared but gated (see the
    module constant) — when included it is equal-length with ``requests``."""
    body: dict[str, Any] = {"requests": [ref.raw_doc for ref in envelope.requests]}
    if _INCLUDE_ORACLE_PLUGIN_DIRS:
        body["oracle_plugin_dirs"] = [ref.oracle_plugin_dir for ref in envelope.requests]
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
    async with _make_client(api) as client:
        try:
            response = await client.post("/envelopes", json=_wire_body(envelope))
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
