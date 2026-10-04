"""The run history ``cv-infra verify`` keeps: the store, and what verify records into it.

Exercised on a real SQLite file in ``tmp_path``.
"""

from __future__ import annotations

import sqlite3
import time
from types import SimpleNamespace

import pytest

from cv_infra import history, scheduler
from cv_infra.cli import main as cli
from cv_infra.scheduler import HostSample

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


@pytest.fixture
def store(tmp_path):
    opened = history.Store(tmp_path / "hist.sqlite3")
    yield opened
    opened.close()


# --- store ------------------------------------------------------------------------------


def test_the_db_lives_beside_the_baseline_db_unless_named(tmp_path):
    assert history.default_path({}, tmp_path / "b.sqlite3") == tmp_path / history.DB_FILENAME
    named = history.default_path({history.DB_ENV: str(tmp_path / "x.db")}, "unused")
    assert named == tmp_path / "x.db"


def test_a_db_from_a_newer_build_is_refused(tmp_path):
    path = tmp_path / "d.sqlite3"
    sqlite3.connect(path).execute("PRAGMA user_version = 99").connection.close()
    with pytest.raises(RuntimeError, match="newer"):
        history.Store(path)


def test_runs_round_trip_newest_first_with_their_report_and_cases(store):
    store.upsert_run(run_row("old", started=NOW - 5000, ended=NOW - 4000))
    store.upsert_run(run_row("new"), {"summary": {"exit_code": 1}})
    store.add_case_run(
        {"run_id": "new", "case_index": 0, "repeat": 0, "case_id": "c0",
         "axes_json": '{"x": "1"}', "started_at": NOW - 500, "ended_at": NOW - 400,
         "lane": "ok", "checks_json": '{"ok": true}', "metrics_json": None,
         "notes_json": '{"note": "fine"}'}
    )  # fmt: skip

    assert [row["run_id"] for row in store.runs(now=NOW)] == ["new", "old"]
    assert [row["run_id"] for row in store.runs(since=NOW - 1000, now=NOW)] == ["new"]
    detail = store.run("new", now=NOW)
    assert detail["report"] == {"summary": {"exit_code": 1}}
    assert detail["cases"][0]["axes"] == {"x": "1"}
    assert detail["cases"][0]["checks"] == {"ok": True}
    assert detail["cases"][0]["metrics"] == {}
    assert detail["cases"][0]["notes"] == {"note": "fine"}
    assert store.run("old", now=NOW)["report"] is None
    assert store.run("missing", now=NOW) is None


def test_a_running_run_that_stopped_sampling_reads_as_stale(store):
    store.upsert_run(run_row("alive", status="running", ended=None))
    store.upsert_run(run_row("dead", status="running", ended=None, started=NOW - 9000))
    store.add_samples([sample_row(NOW - 10, run_id="alive", running=1, level=1)])
    status = {row["run_id"]: row["status"] for row in store.runs(now=NOW)}
    assert status == {"alive": "running", "dead": "stale"}


# --- recorder ---------------------------------------------------------------------------


def test_host_snapshot_merges_the_probe_with_total_ram_and_load(tmp_path):
    meminfo = tmp_path / "meminfo"
    meminfo.write_text("MemTotal: 131072 kB\nMemAvailable: 65536 kB\n", encoding="utf-8")
    snap = history.host_snapshot(
        probe=lambda: HostSample(1.0, 2.0, 3.0, 4.0), meminfo_path=str(meminfo)
    )
    assert (snap["gpu_used_mib"], snap["ram_total_mib"]) == (1.0, 128.0)
    assert snap["load1"] is not None


def test_host_snapshot_without_evidence_is_all_gaps(tmp_path):
    snap = history.host_snapshot(probe=lambda: None, meminfo_path=str(tmp_path / "absent"))
    assert all(value is None for value in snap.values())


def spec(tmp_path, **extra):
    fields = {
        "checkout": tmp_path / "checkout", "checkout_sha": None, "baseline_db": tmp_path / "b.db",
        "mode": "gate", "sim_image": "img@sha256:00", "sim_script": "verify/sim.py",
        "sim_input_space": "verify/space.pict", "pict_k": 2, "repeats": 1,
        "concurrency": "auto", "budget_s": None,
    }  # fmt: skip
    fields.update(extra)
    return SimpleNamespace(**fields)


def test_the_request_is_read_off_the_github_context(tmp_path):
    env = {"GITHUB_REPOSITORY": "acme/robot", "GITHUB_RUN_ID": "42", "GITHUB_SHA": "f00",
           "GITHUB_EVENT_NAME": "pull_request", "GITHUB_ACTOR": "dev"}  # fmt: skip
    row = history.request_of(spec(tmp_path), env)
    assert row["repo"] == "acme/robot" and row["sha"] == "f00"
    assert row["gh_run_url"] == "https://github.com/acme/robot/actions/runs/42"
    local = history.request_of(spec(tmp_path, checkout_sha="c0ffee"), {})
    assert (local["repo"], local["event"], local["sha"]) == ("(local) checkout", "local", "c0ffee")
    assert local["gh_run_url"] is None


def finished_report():
    return {
        "summary": {"cases_planned": 2, "cases_run": 2, "checks_failed": 1, "regressions": 0,
                    "peak_concurrency": 2, "exit_code": 1, "report_outcome": "fail"},
        "matrix": [{"result": "pass"}, {"result": "fail"}],
    }  # fmt: skip


def test_a_recorded_run_carries_request_cases_samples_and_summary(tmp_path, monkeypatch):
    monkeypatch.setattr(history, "host_snapshot", lambda: {"gpu_util_pct": 33.0})
    env = {history.DB_ENV: str(tmp_path / "d.sqlite3")}
    recorder = history.RunRecorder.begin(spec(tmp_path), env, planned=2)
    case = SimpleNamespace(case_index=0, repeat=0, case_id="c0", axes={"x": "1"})
    run = SimpleNamespace(
        wall_s=3.0, gpu_retries=0,
        result=SimpleNamespace(lane="ok", error=None, checks={"ok": True}, metrics={"m": 1.0},
                               notes={"note": "n"}),
    )  # fmt: skip
    live, governor = scheduler.Live(running=1), scheduler.FixedGovernor(2)
    with recorder.sampling(live, governor, every_s=0.01):
        time.sleep(0.05)  # the thread samples too, not just the first look
        recorder.case_run(case, run, NOW, NOW + 3)
    recorder.finish(finished_report())

    store = history.Store(tmp_path / "d.sqlite3")
    detail = store.run(recorder.run_id, now=time.time())
    assert detail["status"] == "done" and detail["cases_pass"] == 1
    assert detail["outcome"] == "fail" and detail["cases"][0]["metrics"] == {"m": 1.0}
    samples = store.samples(since=0, until=time.time() + 10, run_id=recorder.run_id)
    assert len(samples) >= 2 and samples[0]["level"] == 2 and samples[0]["running"] == 1
    store.close()


def test_a_history_that_cannot_open_turns_itself_off_and_says_so_once(tmp_path, capsys):
    env = {history.DB_ENV: str(tmp_path / "absent" / "d.sqlite3")}
    recorder = history.RunRecorder.begin(spec(tmp_path), env, planned=1)
    recorder.sample(0, 1)
    with recorder.sampling(scheduler.Live(), scheduler.FixedGovernor(1), every_s=0.01):
        pass
    recorder.finish(finished_report())
    assert capsys.readouterr().err.count("run history off") == 1


def test_a_write_that_fails_mid_run_turns_the_history_off(tmp_path, capsys):
    env = {history.DB_ENV: str(tmp_path / "d.sqlite3")}
    recorder = history.RunRecorder.begin(spec(tmp_path), env, planned=1)

    def broken(_store):
        raise sqlite3.OperationalError("disk I/O error")

    recorder._write(broken)
    recorder._write(broken)  # already off: silent
    recorder.finish(finished_report())
    assert capsys.readouterr().err.count("disk I/O error") == 1


def test_the_state_directory_is_never_created(tmp_path):
    with pytest.raises(sqlite3.OperationalError):
        history.Store(tmp_path / "absent" / "h.sqlite3")
    assert not (tmp_path / "absent").exists()


def test_samples_are_windowed_and_filtered_by_run(store):
    store.add_samples([sample_row(NOW - 100), sample_row(NOW - 50, run_id="r1"), sample_row(NOW)])
    assert len(store.samples(since=NOW - 75, until=NOW)) == 2
    assert len(store.samples(since=0, until=NOW, run_id="r1")) == 1


def test_a_readonly_connection_reads_and_refuses_to_write(tmp_path):
    path = tmp_path / "h.sqlite3"
    writer = history.Store(path)
    writer.upsert_run(run_row("r1"))
    writer.close()
    reader = history.Store(path, readonly=True)
    assert [row["run_id"] for row in reader.runs(now=NOW)] == ["r1"]
    with pytest.raises(sqlite3.OperationalError, match="readonly"):
        reader.upsert_run(run_row("r2"))
    reader.close()


def test_a_verify_run_lands_in_the_history(tmp_path):
    """cli._run_case with a recorder writes one case row per run."""
    store_path = tmp_path / "h.sqlite3"
    recorder = history.RunRecorder.begin(
        spec(tmp_path), {history.DB_ENV: str(store_path)}, planned=1
    )
    case = SimpleNamespace(case_index=0, repeat=0, case_id="c0", axes={"a": "1"})
    fake_run = SimpleNamespace(
        wall_s=1.0, gpu_retries=0,
        result=SimpleNamespace(lane="ok", error=None, checks={}, metrics={}, notes={}),
    )  # fmt: skip
    original = cli._run_once
    cli._run_once = lambda *args, **kwargs: fake_run
    try:
        record = cli._run_case(SimpleNamespace(repeats=1), [case], None, {}, recorder=recorder)
    finally:
        cli._run_once = original
    assert record.runs == (fake_run,)
    recorder.finish(finished_report())
    check = history.Store(store_path, readonly=True)
    assert check.run(recorder.run_id, now=time.time())["cases"][0]["case_id"] == "c0"
    check.close()
