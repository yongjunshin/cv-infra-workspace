"""``cv-infra monitor`` operational-view CLI tests (M6 §3.6 CLI axis, M8).

The projection is consumed DISPLAY-ONLY: the pinned ``/monitor.json`` response
(verbatim from the p4c6 T2 merge ``bbad1e4`` — CPU host, 3-request envelope: r0
repeats=2 with 1 flaky-fail, r1 pass, r2 hard-crash 137) is fed through the
SAME httpx client seam the batch surface uses (``batch._make_client``, patched
here with an ``httpx.MockTransport``) so the connection/HTTP error idioms are
literally shared. ``_MONITOR_SAMPLE`` is the pin VERBATIM (do not hand-edit) —
the render golden asserts the operator sees the counts / ids / error category
without any re-aggregation.
"""

from __future__ import annotations

import json
from pathlib import Path

import httpx
import pytest

from cv_infra.cli import batch, monitor
from cv_infra.cli.main import EXIT_INFRA, EXIT_PASS, main

# VERBATIM pin (task dx-2026-07-16-p4c6-monitor-cli §핀 계약, measured on bbad1e4):
# tests/fixtures/monitor_sample.json holds the /monitor.json response byte-for-byte.
# Kept as a .json fixture (not an inline string) so the pin's long lines survive
# the line-length linters unchanged — do NOT hand-edit the fixture.
_MONITOR_SAMPLE_JSON = (Path(__file__).parent / "fixtures" / "monitor_sample.json").read_text(
    encoding="utf-8"
)
_MONITOR_SAMPLE = json.loads(_MONITOR_SAMPLE_JSON)


def _wire_ok(monkeypatch, payload, *, status_code: int = 200) -> list[httpx.Request]:
    """Patch the SHARED ``batch._make_client`` seam with a MockTransport that
    replies to ``GET /monitor.json`` with ``payload``. Returns the recorded
    request list (asserts the path the CLI hit)."""
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(status_code, json=payload)

    monkeypatch.setattr(
        batch,
        "_make_client",
        lambda api_base: httpx.AsyncClient(
            transport=httpx.MockTransport(handler), base_url="http://cv-infra.test"
        ),
    )
    return seen


# --------------------------------------------------------------------------- #
# (1) pinned fixture -> operator table golden (counts · ids · category surface)
# --------------------------------------------------------------------------- #


def test_monitor_renders_the_pinned_projection_as_operator_table(monkeypatch, capsys):
    seen = _wire_ok(monkeypatch, _MONITOR_SAMPLE)

    rc = main(["monitor"])

    assert rc == EXIT_PASS  # informational read is always exit 0
    out = capsys.readouterr().out
    assert [r.url.path for r in seen] == ["/monitor.json"]  # GET the projection endpoint

    # generated_at + health/resources header (null vram/util -> n/a idiom).
    assert "generated_at=2026-07-16T02:26:53.560521+00:00" in out
    assert "orchestrator_up=true" in out and "gpu_reachable=false" in out
    assert "queue_depth=0" in out and "running_k=0" in out and "over_launch_count=0" in out
    assert "vram=n/a/n/a MiB" in out and "gpu_util=n/a%" in out

    lines = out.splitlines()

    # One row per request_id (each carries its envelope_id + the repeated
    # envelope-level report_outcome — the pin's shape note ①②).
    r0 = next(line for line in lines if "env-a95d204e4481/r0" in line)
    assert "completed" in r0 and "errored" in r0
    # pass=1 fail=1 error=0 flakiness=0.5 — server counts shown verbatim.
    assert r0.split() == [
        "env-a95d204e4481",
        "env-a95d204e4481/r0",
        "completed",
        "errored",
        "1",
        "1",
        "0",
        "0.5",
    ]

    r2 = next(line for line in lines if "env-a95d204e4481/r2" in line)
    # error_count=1, flakiness null -> n/a (반복 판정 불가).
    assert r2.split() == [
        "env-a95d204e4481",
        "env-a95d204e4481/r2",
        "completed",
        "errored",
        "0",
        "0",
        "1",
        "n/a",
    ]

    # Broken-job section: the ONE crashed job with its server-assigned category,
    # exit code, and infra_error ("어디서 깨졌나").
    assert "broken jobs (1):" in out
    broken = next(line for line in lines if "env-a95d204e4481/r2:0" in line)
    assert "state=failed" in broken
    assert "category=runner-crash" in broken
    assert "exit=137" in broken
    assert "expected exactly 1 result.json under /out/.../result, found 0 (REQ-EXEC-013)" in out

    # The passing jobs are NOT listed as broken (no error_category).
    assert "env-a95d204e4481/r0:0" not in out
    assert "env-a95d204e4481/r1:0" not in out


def test_monitor_empty_projection_renders_without_rows(monkeypatch, capsys):
    """A fresh orchestrator (no envelopes yet) still renders the header + a
    'requests: none' line, exit 0 (usability, not a crash)."""
    empty = {
        "generated_at": "2026-07-16T00:00:00+00:00",
        "health": {"orchestrator_up": True, "gpu_reachable": False, "last_sample_at": None},
        "resources": {
            "queue_depth": 0,
            "running_k": 0,
            "over_launch_count": 0,
            "vram_used_mib": None,
            "vram_total_mib": None,
            "gpu_util_pct": None,
        },
        "requests": [],
    }
    _wire_ok(monkeypatch, empty)
    assert main(["monitor"]) == EXIT_PASS
    out = capsys.readouterr().out
    assert "requests: none" in out
    assert "last_sample_at=n/a" in out  # null last_sample_at -> n/a


# --------------------------------------------------------------------------- #
# (2) connection / HTTP errors follow the SAME status infra idiom (exit 3)
# --------------------------------------------------------------------------- #


def test_monitor_orchestrator_unreachable_exits_3(monkeypatch, capsys):
    def refuse(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("connection refused")

    monkeypatch.setattr(
        batch,
        "_make_client",
        lambda api_base: httpx.AsyncClient(
            transport=httpx.MockTransport(refuse), base_url="http://cv-infra.test"
        ),
    )
    assert main(["monitor"]) == EXIT_INFRA
    err = capsys.readouterr().err
    assert "cv-infra monitor:" in err
    assert "orchestrator unreachable" in err
    assert "not a SUT verdict" in err  # shared batch._infra idiom (status parity)


@pytest.mark.parametrize("status_code", [404, 500, 503])
def test_monitor_non_200_is_infra_exit_3(monkeypatch, capsys, status_code):
    _wire_ok(monkeypatch, {"generated_at": "x"}, status_code=status_code)
    assert main(["monitor"]) == EXIT_INFRA
    err = capsys.readouterr().err
    assert f"unexpected orchestrator response {status_code}" in err
    assert "not a SUT verdict" in err
    assert "Traceback" not in err  # raw traceback 0


def test_monitor_non_json_body_is_infra_exit_3(monkeypatch, capsys):
    def html(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, text="<html>not json</html>")

    monkeypatch.setattr(
        batch,
        "_make_client",
        lambda api_base: httpx.AsyncClient(
            transport=httpx.MockTransport(html), base_url="http://cv-infra.test"
        ),
    )
    assert main(["monitor"]) == EXIT_INFRA
    assert "non-JSON monitor body" in capsys.readouterr().err


# --------------------------------------------------------------------------- #
# (3) lenient parsing — unknown keys at every level are ignored, not a crash
# --------------------------------------------------------------------------- #


def test_monitor_ignores_unknown_keys(monkeypatch, capsys):
    """A richer-than-known payload (server adds fields at every level) still
    renders the pinned data (G-17 lenient parse — display only the known set)."""
    payload = json.loads(_MONITOR_SAMPLE_JSON)
    payload["future_top_level_field"] = {"anything": [1, 2, 3]}
    payload["health"]["new_health_metric"] = 99
    payload["resources"]["power_draw_w"] = 250
    payload["requests"][0]["some_new_rollup"] = "ignored"
    payload["requests"][2]["jobs"][0]["new_job_field"] = {"nested": True}

    seen = _wire_ok(monkeypatch, payload)
    assert main(["monitor"]) == EXIT_PASS
    out = capsys.readouterr().out
    assert [r.url.path for r in seen] == ["/monitor.json"]

    # Known data still surfaces; unknown keys are silently dropped.
    assert "env-a95d204e4481/r0" in out
    assert "category=runner-crash" in out and "exit=137" in out
    for unknown in (
        "future_top_level_field",
        "new_health_metric",
        "power_draw_w",
        "some_new_rollup",
        "new_job_field",
    ):
        assert unknown not in out


# --------------------------------------------------------------------------- #
# (4) wiring: monitor is on the --help surface; extra positionals -> usage error
# --------------------------------------------------------------------------- #


def test_monitor_on_help_surface(capsys):
    with pytest.raises(SystemExit) as exc:
        main(["--help"])
    assert exc.value.code == 0
    assert "monitor" in capsys.readouterr().out


def test_monitor_rejects_unexpected_positional(monkeypatch, capsys):
    _wire_ok(monkeypatch, _MONITOR_SAMPLE)
    from cv_infra.cli.main import EXIT_CONTRACT

    assert main(["monitor", "env-x"]) == EXIT_CONTRACT
    assert "unrecognized argument(s): env-x" in capsys.readouterr().err


def test_render_monitor_is_pure_display_no_reaggregation():
    """``render_monitor`` surfaces the server counts VERBATIM — it must not
    recompute pass/fail from the jobs list (M6 §3.3 재집계 금지). A doctored
    payload whose counts DISAGREE with its jobs proves the counts are copied,
    not derived."""
    doctored = json.loads(_MONITOR_SAMPLE_JSON)
    # r0 really has 2 completed jobs, but the server-provided summary says 1/1/0;
    # render must echo 1/1/0 (the projection), never recount to 2/0/0.
    row = monitor.render_monitor(doctored).splitlines()
    r0 = next(line for line in row if "env-a95d204e4481/r0" in line)
    assert r0.split()[4:8] == ["1", "1", "0", "0.5"]  # counts as given, not re-summed
