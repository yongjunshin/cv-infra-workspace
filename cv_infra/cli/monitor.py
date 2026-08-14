"""``cv-infra monitor`` — the operational-view CLI table (M6 §3.6 CLI axis, M8).

GET ``/monitor.json`` from the orchestrator and render the M6 operational
projection as an operator table for SSH/headless ops (``REQ-MONITOR-003`` CLI
axis, ``NFR-MONITOR-001``: "무엇이 몇 건 pass/fail, 어디서 깨졌나" at a glance).
The httpx client seam, the connection/HTTP error idiom, and the output plumbing
are REUSED verbatim from the batch surface (``cv_infra/cli/batch.py``) — this
module invents no new HTTP or error convention.

DISPLAY ONLY — same projection, no re-aggregation (M6 §3.3/§4)
-------------------------------------------------------------
The server-provided counts/states are shown VERBATIM; nothing is re-summed or
re-interpreted here. The canonical projection model is
``cv_infra.orchestrator.monitor.OperationalRecord`` (read reference only). Parsing
is LENIENT (G-17): only the pinned OperationalRecord field set is displayed,
unknown keys are ignored, and absent scalars degrade to ``n/a`` — a monitor peek
must never crash on a richer-than-known payload.

Informational query — never gates a verdict (D-O)
--------------------------------------------------
A successful read is exit 0 (a monitor peek must never turn a CI job red by
itself); this command has NO coupling to the batch exit-map (0/1/3/2). Connection
or HTTP errors take the SAME infra path as ``status`` (``batch._infra`` -> exit 3
+ "not a SUT verdict"): an unreachable/unhealthy orchestrator is a platform
condition, not a SUT judgement.
"""

from __future__ import annotations

import argparse
import asyncio
from typing import Any

import httpx

from cv_infra.cli import batch
from cv_infra.cli.main import EXIT_PASS, _one_line

#: Null render idiom (vram/util never sampled, flakiness = repeat verdict n/a, …).
_NA = "n/a"


def _fmt(value: Any) -> str:
    """Render one scalar for the operator table (null -> ``n/a``, json-style bool)."""
    if value is None:
        return _NA
    if isinstance(value, bool):
        return "true" if value else "false"
    return str(value)


def _render_table(headers: list[str], rows: list[list[str]]) -> list[str]:
    """Minimal fixed-width table (no dependency): aligned header + rows -> lines."""
    widths = [len(h) for h in headers]
    for row in rows:
        for i, cell in enumerate(row):
            widths[i] = max(widths[i], len(cell))

    def line(cells: list[str]) -> str:
        return "  ".join(cell.ljust(widths[i]) for i, cell in enumerate(cells)).rstrip()

    return [line(headers), *(line(row) for row in rows)]


_REQUEST_HEADERS = [
    "ENVELOPE",
    "REQUEST",
    "STATUS",
    "OUTCOME",
    # Self-test marker (M7 / REQ-SELFTEST-004): the projection carries the bool
    # ``is_self_test`` since store v8, and the HTML dashboard shows it — this
    # column keeps the SSH/headless axis aligned with it, so an operator who ran
    # ``cv-infra selftest`` can tell that envelope apart from user submissions.
    # The free-text ``origin`` deliberately stays OFF this view (store/audit
    # only — T3 report §7-3: a free-text field on the operational view is the
    # domain-leak shape NEG-3 does not catch). A pre-v8 projection has no such
    # key -> ``n/a`` by the same G-17 lenient rule as concurrency_budget_k.
    "SELFTEST",
    "PASS",
    "FAIL",
    "ERROR",
    "FLAKINESS",
]


def render_monitor(record: dict[str, Any]) -> str:
    """Render the ``/monitor.json`` projection as an operator table (display only).

    Reads ONLY the pinned ``OperationalRecord`` keys (G-17): a per-request rollup
    table (envelope/request id · status · report_outcome · pass/fail/error counts ·
    flakiness) plus the broken (error-categorised) jobs with their state /
    category / runner exit code / infra_error. Counts are surfaced verbatim — the
    server already aggregated them (M6 §3.3), this only lays them out.
    ``concurrency_budget_k`` sits next to ``running_k`` (same order as the HTML
    dashboard) so the operator can judge ``running_k > k`` inside one surface.
    """
    health = record.get("health") or {}
    resources = record.get("resources") or {}
    requests = record.get("requests") or []

    lines = [
        f"cv-infra monitor (generated_at={_fmt(record.get('generated_at'))})",
        (
            "health:    "
            f"orchestrator_up={_fmt(health.get('orchestrator_up'))}  "
            f"gpu_reachable={_fmt(health.get('gpu_reachable'))}  "
            f"last_sample_at={_fmt(health.get('last_sample_at'))}"
        ),
        (
            "resources: "
            f"queue_depth={_fmt(resources.get('queue_depth'))}  "
            f"running_k={_fmt(resources.get('running_k'))}  "
            f"concurrency_budget_k={_fmt(resources.get('concurrency_budget_k'))}  "
            f"over_launch_count={_fmt(resources.get('over_launch_count'))}  "
            f"vram={_fmt(resources.get('vram_used_mib'))}/"
            f"{_fmt(resources.get('vram_total_mib'))} MiB  "
            f"gpu_util={_fmt(resources.get('gpu_util_pct'))}%"
        ),
        "",
    ]

    if not requests:
        lines.append("requests: none")
        return "\n".join(lines)

    rows: list[list[str]] = []
    broken: list[dict[str, Any]] = []
    for req in requests:
        if not isinstance(req, dict):
            continue
        rows.append(
            [
                _fmt(req.get("envelope_id")),
                _fmt(req.get("request_id")),
                _fmt(req.get("envelope_status")),
                _fmt(req.get("report_outcome")),
                _fmt(req.get("is_self_test")),
                _fmt(req.get("pass_count")),
                _fmt(req.get("fail_count")),
                _fmt(req.get("error_count")),
                _fmt(req.get("flakiness")),
            ]
        )
        for job in req.get("jobs") or []:
            # A job is "broken" when the SERVER already categorised it (error_category
            # non-null on FAILED/TIMEOUT states) — we display that flag, never re-derive.
            if isinstance(job, dict) and job.get("error_category") is not None:
                broken.append(job)

    lines.append(f"requests ({len(rows)}):")
    lines.extend(_render_table(_REQUEST_HEADERS, rows))
    lines.append("")

    if not broken:
        lines.append("broken jobs: none")
        return "\n".join(lines)

    lines.append(f"broken jobs ({len(broken)}):")
    for job in broken:
        lines.append(
            f"  {_fmt(job.get('job_id'))}  "
            f"state={_fmt(job.get('state'))}  "
            f"category={_fmt(job.get('error_category'))}  "
            f"exit={_fmt(job.get('runner_exit_code'))}"
        )
        infra_error = job.get("infra_error")
        if infra_error is not None:
            lines.append(f"      infra_error: {infra_error}")
    return "\n".join(lines)


async def _monitor_async(args: argparse.Namespace) -> int:
    api = batch._resolve_api(args.api)
    async with batch._make_client(api) as client:
        try:
            response = await client.get("/monitor.json")
        except httpx.HTTPError as exc:
            return batch._infra("monitor", f"orchestrator unreachable at {api}: {_one_line(exc)}")
        if response.status_code != 200:
            return batch._infra(
                "monitor", f"unexpected orchestrator response {response.status_code}"
            )
        body = batch._body_json(response)
        if not isinstance(body, dict):
            return batch._infra("monitor", "orchestrator returned a non-JSON monitor body")
        print(render_monitor(body))
        return EXIT_PASS  # informational read: never gates a verdict (D-O)


def cmd_monitor(args: argparse.Namespace) -> int:
    """``cv-infra monitor [--api URL]`` — operational-view table (informational, exit 0)."""
    return asyncio.run(_monitor_async(args))
