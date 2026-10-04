"""The dashboard: a read-only lens over the run history and the live host.

The history is a real SQLite file written through ``cv_infra.history``; the dashboard
opens it read-only. The server is started for real on an ephemeral port and asked over
HTTP. The page itself (static JS) is served and checked for presence, not rendered.
"""

from __future__ import annotations

import json
import threading
import urllib.request

import pytest

from cv_infra import history
from cv_infra.cli import main as cli
from cv_infra.dashboard import server

NOW = 1_800_000_000.0


def run_row(run_id="r1", started=NOW - 600, ended=NOW - 60, **extra):
    row = {
        "run_id": run_id,
        "started_at": started,
        "ended_at": ended,
        "status": "done",
        "repo": "acme/robot",
        "sha": "abc1234def",
        "event": "push",
        "mode": "gate",
        "cases_planned": 3,
        "cases_run": 3,
        "cases_pass": 2,
        "cases_fail": 1,
        "cases_error": 0,
        "peak_concurrency": 2,
        "outcome": "fail",
    }
    row.update(extra)
    return row


def sample_row(t, run_id=None, running=None, level=None, util=50.0):
    return {
        "t": t,
        "run_id": run_id,
        "gpu_used_mib": 8000.0,
        "gpu_total_mib": 96000.0,
        "gpu_util_pct": util,
        "ram_available_mib": 100_000.0,
        "ram_total_mib": 128_000.0,
        "load1": 4.0,
        "running": running,
        "level": level,
    }


# --- the numbers ------------------------------------------------------------------------


def test_overview_counts_finished_runs_and_breaks_them_down():
    rows = [
        run_row("a", outcome="pass", cases_fail=0, cases_pass=3),
        run_row("b", repo="acme/other", started=NOW - 3 * 86400, ended=None),
        run_row("c", status="running", ended=None, outcome=None),
        run_row("d", status="stale", ended=None),
        run_row("e", outcome="errored", cases_error=1),
    ]
    out = server.overview(rows, now=NOW, window_s=86400 * 2, host={"load1": 1.0})
    assert out["kpi"]["runs"] == 5 and out["kpi"]["runs_done"] == 3
    assert out["kpi"]["runs_passed"] == 1 and out["kpi"]["runs_stale"] == 1
    assert [row["run_id"] for row in out["active"]] == ["c"]
    assert out["kpi"]["cases"] == {"pass": 7, "fail": 2, "error": 1}
    assert [row["repo"] for row in out["by_repo"]] == ["acme/other", "acme/robot"]
    days = out["daily"]
    assert sum(day["pass"] + day["fail"] + day["error"] for day in days) == 2  # b is too old
    assert sum(day["error"] for day in days) == 1


def test_an_empty_window_has_no_rates():
    out = server.overview([], now=NOW, window_s=3600, host={})
    assert out["kpi"]["case_pass_rate"] is None and out["kpi"]["mean_duration_s"] is None


def test_series_buckets_sum_concurrent_runs_and_keep_gaps():
    samples = [
        sample_row(NOW + 1, run_id="a", running=2, level=3, util=40.0),
        sample_row(NOW + 2, run_id="a", running=3, level=3, util=60.0),
        sample_row(NOW + 3, run_id="b", running=1, level=1),
        sample_row(NOW + 4),  # the server's own sample: no run
        {**sample_row(NOW + 95), "ram_total_mib": None, "gpu_util_pct": None},
    ]
    points = server.series(samples, since=NOW, until=NOW + 100, buckets=10)
    assert len(points) == 2  # the empty buckets in between are a gap, not zeros
    assert points[0]["running"] == 4  # a's latest 3 + b's latest 1
    assert points[0]["level"] == 3 and points[0]["gpu_util_pct"] == 50.0
    assert points[1]["running"] == 0 and points[1]["ram_used_mib"] is None
    assert points[1]["gpu_util_pct"] is None


# --- the app ----------------------------------------------------------------------------


@pytest.fixture
def app(tmp_path):
    """A history written by verify's side, then looked at through the lens."""
    store = history.Store(tmp_path / "h.sqlite3")
    store.upsert_run(run_row("r1"), {"summary": {}})
    store.add_samples(
        [sample_row(NOW - 300, run_id="r1", running=2, level=2), sample_row(NOW - 30)]
    )
    store.close()
    return server.Dashboard(
        tmp_path / "h.sqlite3", clock=lambda: NOW, snapshot=lambda: {"load1": 2.0}
    )


def get(app, path):
    status, kind, body = app.handle(path)
    return status, kind, (json.loads(body) if kind.startswith("application/json") else body)


def test_the_api_answers_overview_runs_run_and_series(app):
    status, _, body = get(app, "/api/overview?window=3600")
    assert status == 200 and body["kpi"]["runs"] == 1 and body["host"] == {"load1": 2.0}
    assert get(app, "/api/runs")[2]["runs"][0]["run_id"] == "r1"
    assert get(app, "/api/runs/r1")[2]["report"] == {"summary": {}}
    series = get(app, "/api/series?window=3600&buckets=5")[2]
    assert series["points"] and series["runs"][0]["run_id"] == "r1"
    one = get(app, "/api/series?run_id=r1")[2]
    assert one["since"] == NOW - 600 and one["runs"] == []


def test_the_api_says_what_it_does_not_have(app):
    assert get(app, "/api/runs/nope")[0] == 404
    assert get(app, "/api/series?run_id=nope")[0] == 404
    assert get(app, "/api/elsewhere")[0] == 404
    assert get(app, "/api/overview?window=soon")[0] == 400


def test_the_page_and_its_assets_are_served_and_nothing_else(app):
    status, kind, body = app.handle("/")
    assert status == 200 and kind.startswith("text/html") and b"app.js" in body
    assert app.handle("/static/app.js")[1].endswith("charset=utf-8")
    assert app.handle("/static/style.css")[0] == 200
    assert app.handle("/static/../store.py")[0] == 404  # never outside static/
    assert app.handle("/static/missing.png")[0] == 404


def test_an_asset_that_is_not_text_keeps_its_own_type(tmp_path, app, monkeypatch):
    (tmp_path / "logo.png").write_bytes(b"\x89PNG")
    monkeypatch.setattr(server, "STATIC_DIR", tmp_path)
    status, kind, _ = app.handle("/static/logo.png")
    assert status == 200 and kind == "image/png"


def test_the_server_serves_over_http_until_stopped(tmp_path, capsys):
    stop = threading.Event()
    box = {}

    def ready(httpd):
        box["port"] = httpd.server_address[1]
        box["ready"].set()

    box["ready"] = threading.Event()
    thread = threading.Thread(
        target=server.serve,
        args=(tmp_path / "h.sqlite3",),
        kwargs={"port": 0, "stop": stop, "ready": ready},
        daemon=True,
    )
    thread.start()
    assert box["ready"].wait(10)
    url = f"http://127.0.0.1:{box['port']}"
    with urllib.request.urlopen(f"{url}/api/overview?window=60", timeout=10) as response:
        assert response.headers["Cache-Control"] == "no-store"
        assert json.load(response)["kpi"]["runs"] == 0
    stop.set()
    thread.join(10)
    assert not thread.is_alive()
    assert "cv-infra dashboard: http://127.0.0.1:" in capsys.readouterr().out


def test_the_cli_command_serves_on_the_named_db_and_ctrl_c_is_a_clean_exit(tmp_path, monkeypatch):
    seen = {}

    def fake_serve(db, **kwargs):
        seen.update(db=db, **kwargs)
        raise KeyboardInterrupt

    monkeypatch.setattr(server, "serve", fake_serve)
    assert cli.main(["dashboard", "--port", "9999", "--db", str(tmp_path / "x.db")]) == 0
    assert seen["db"] == tmp_path / "x.db" and seen["port"] == 9999
    code = cli.dashboard([], {"CV_BASELINE_DB": str(tmp_path / "b.sqlite3")})
    assert code == 0 and seen["db"] == tmp_path / history.DB_FILENAME


def test_live_is_the_host_now_and_needs_no_history(tmp_path):
    lens = server.Dashboard(
        tmp_path / "none.sqlite3", clock=lambda: NOW, snapshot=lambda: {"load1": 3.0}
    )
    status, _, body = get(lens, "/api/live")
    assert status == 200 and body == {"now": NOW, "host": {"load1": 3.0}}


def test_before_verify_ever_wrote_a_history_the_pages_are_empty_not_broken(tmp_path):
    lens = server.Dashboard(tmp_path / "none.sqlite3", clock=lambda: NOW, snapshot=lambda: {})
    overview = get(lens, "/api/overview")[2]
    assert overview["kpi"]["runs"] == 0 and overview["history"]["present"] is False
    assert get(lens, "/api/runs")[2]["runs"] == []
    assert get(lens, "/api/series")[2]["points"] == []
    assert get(lens, "/api/runs/r1")[0] == 404
    assert not (tmp_path / "none.sqlite3").exists()  # looking never creates it


def test_the_lens_never_writes_the_history(app):
    before = app.db_path.read_bytes()
    for path in (
        "/api/overview",
        "/api/runs",
        "/api/runs/r1",
        "/api/series",
        "/api/series?run_id=r1",
    ):
        get(app, path)
    assert app.db_path.read_bytes() == before


def test_concurrent_runs_are_summed_at_one_moment_not_at_their_separate_peaks():
    """Two runs whose peaks fall at different times in one wide bucket: the sum of their
    peaks (5 + 4) never happened; what was in flight together at the end was 1 + 4."""
    samples = [
        sample_row(NOW + 1, run_id="a", running=5),
        sample_row(NOW + 2, run_id="b", running=4),
        sample_row(NOW + 3, run_id="a", running=1),
    ]
    (point,) = server.series(samples, since=NOW, until=NOW + 10, buckets=1)
    assert point["running"] == 5
