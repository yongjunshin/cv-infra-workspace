"""The verification host's run history — what ``cv-infra verify`` remembers of itself.

Before this, a run left two things behind on the host: the baseline DB, and a report in
the runner's temp directory that the next run wiped (the full report survives only as a
GitHub artifact). This module is the run LOG the infrastructure keeps of its own work,
next to the baseline DB: every verify run, its cases, and the host while it ran. It is
written by ``cv-infra verify`` and nothing else; ``cv-infra dashboard`` only reads it
(read-only connection — the dashboard is a lens, not a recorder).

One SQLite file (``history.sqlite3`` beside the baseline DB, or ``$CV_HISTORY_DB``). Same
discipline as ``cv_infra.baselines``: one connection + one lock, WAL + ``busy_timeout``
(several CLI processes — one per runner — write it), a ``user_version`` a newer build
refuses to write under, and BEST EFFORT from verify's side: the first failure prints one
line and turns the log off for that run; a verification never fails over its log.

Three tables, nothing derived:

* ``runs``      one row per ``cv-infra verify`` — the request (repository, commit, event,
                inputs), its timing, its summary and the whole report JSON.
* ``case_runs`` one row per case+repeat — when it started and ended, and its result.
* ``samples``   the host every ``SAMPLE_EVERY_S`` while a run is in flight: GPU memory
                and utilisation, RAM, load, the run's in-flight cases and scheduler level.

Stdlib only.
"""

from __future__ import annotations

import json
import os
import socket
import sqlite3
import sys
import threading
import time
import uuid
from collections.abc import Callable, Iterable, Iterator, Mapping
from contextlib import contextmanager
from pathlib import Path
from typing import Any

from cv_infra import scheduler

DB_ENV = "CV_HISTORY_DB"
DB_FILENAME = "history.sqlite3"
_SCHEMA_VERSION = 1
_BUSY_TIMEOUT_MS = 5000

_SCHEMA = """
CREATE TABLE IF NOT EXISTS runs (
  run_id           TEXT PRIMARY KEY,
  started_at       REAL NOT NULL,
  ended_at         REAL,
  status           TEXT NOT NULL,
  host             TEXT,
  repo             TEXT,
  sha              TEXT,
  ref              TEXT,
  event            TEXT,
  actor            TEXT,
  gh_run_id        TEXT,
  gh_run_url       TEXT,
  mode             TEXT,
  sim_image        TEXT,
  sim_script       TEXT,
  input_space      TEXT,
  pict_k           INTEGER,
  repeats          INTEGER,
  concurrency      TEXT,
  budget_s         REAL,
  cases_planned    INTEGER,
  cases_run        INTEGER,
  cases_pass       INTEGER,
  cases_fail       INTEGER,
  cases_error      INTEGER,
  checks_failed    INTEGER,
  regressions      INTEGER,
  peak_concurrency INTEGER,
  exit_code        INTEGER,
  outcome          TEXT,
  report_json      TEXT
);
CREATE INDEX IF NOT EXISTS runs_started ON runs (started_at);
CREATE TABLE IF NOT EXISTS case_runs (
  run_id      TEXT NOT NULL,
  case_index  INTEGER NOT NULL,
  repeat      INTEGER NOT NULL,
  case_id     TEXT NOT NULL,
  axes_json   TEXT NOT NULL,
  started_at  REAL,
  ended_at    REAL,
  wall_s      REAL,
  lane        TEXT,
  error       TEXT,
  gpu_retries INTEGER NOT NULL DEFAULT 0,
  checks_json TEXT,
  metrics_json TEXT,
  notes_json  TEXT,
  PRIMARY KEY (run_id, case_index, repeat)
);
CREATE TABLE IF NOT EXISTS samples (
  t                 REAL NOT NULL,
  run_id            TEXT,
  gpu_used_mib      REAL,
  gpu_total_mib     REAL,
  gpu_util_pct      REAL,
  ram_available_mib REAL,
  ram_total_mib     REAL,
  load1             REAL,
  running           INTEGER,
  level             INTEGER
);
CREATE INDEX IF NOT EXISTS samples_t ON samples (t);
"""

RUN_FIELDS = (
    "run_id", "started_at", "ended_at", "status", "host", "repo", "sha", "ref", "event",
    "actor", "gh_run_id", "gh_run_url", "mode", "sim_image", "sim_script", "input_space",
    "pict_k", "repeats", "concurrency", "budget_s", "cases_planned", "cases_run",
    "cases_pass", "cases_fail", "cases_error", "checks_failed", "regressions",
    "peak_concurrency", "exit_code", "outcome",
)  # fmt: skip
SAMPLE_FIELDS = (
    "t", "run_id", "gpu_used_mib", "gpu_total_mib", "gpu_util_pct", "ram_available_mib",
    "ram_total_mib", "load1", "running", "level",
)  # fmt: skip
CASE_FIELDS = (
    "run_id", "case_index", "repeat", "case_id", "axes_json", "started_at", "ended_at",
    "wall_s", "lane", "error", "gpu_retries", "checks_json", "metrics_json", "notes_json",
)  # fmt: skip

#: A run still "running" with no sample for this long was killed with its process.
STALE_AFTER_S = 300.0


def default_path(environ: Mapping[str, str], baseline_db: str | Path) -> Path:
    """``$CV_DASHBOARD_DB``, else ``dashboard.sqlite3`` beside the baseline DB."""
    explicit = environ.get(DB_ENV)
    if explicit:
        return Path(explicit).expanduser()
    return Path(baseline_db).expanduser().parent / DB_FILENAME


class Store:
    """The SQLite file, opened (see the module docstring for the discipline)."""

    def __init__(self, db_path: str | Path, *, readonly: bool = False) -> None:
        """Never creates the state directory (the baseline store's rule: a missing
        directory means "unavailable", not "make one"). ``readonly`` = the dashboard's
        connection: SQLite itself refuses any write through it."""
        if readonly:
            uri = f"file:{Path(db_path).resolve()}?mode=ro"
            self._conn = sqlite3.connect(uri, uri=True, check_same_thread=False)
        else:
            self._conn = sqlite3.connect(str(db_path), check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._lock = threading.Lock()
        try:
            self._conn.execute(f"PRAGMA busy_timeout = {_BUSY_TIMEOUT_MS}")
            (version,) = self._conn.execute("PRAGMA user_version").fetchone()
            if version > _SCHEMA_VERSION:
                raise RuntimeError(
                    f"history db {db_path} carries schema v{version}, newer than this"
                    f" build's v{_SCHEMA_VERSION} — refusing to read or write it blind"
                )
            if not readonly:
                self._conn.execute("PRAGMA journal_mode = WAL")
                with self._lock, self._conn:
                    self._conn.executescript(_SCHEMA)
                    self._conn.execute(f"PRAGMA user_version = {_SCHEMA_VERSION}")
        except Exception:
            self._conn.close()
            raise

    def close(self) -> None:
        self._conn.close()

    # --- writes ---------------------------------------------------------------------

    def upsert_run(self, row: Mapping[str, Any], report: Mapping[str, Any] | None = None) -> None:
        """Insert or replace one run (all of ``RUN_FIELDS``; absent keys are NULL)."""
        values = [row.get(name) for name in RUN_FIELDS]
        values.append(None if report is None else json.dumps(report, ensure_ascii=False))
        columns = ", ".join((*RUN_FIELDS, "report_json"))
        marks = ", ".join("?" * (len(RUN_FIELDS) + 1))
        with self._lock, self._conn:
            self._conn.execute(f"INSERT OR REPLACE INTO runs ({columns}) VALUES ({marks})", values)

    def add_case_run(self, row: Mapping[str, Any]) -> None:
        values = [row.get(name) for name in CASE_FIELDS]
        marks = ", ".join("?" * len(CASE_FIELDS))
        with self._lock, self._conn:
            self._conn.execute(
                f"INSERT OR REPLACE INTO case_runs ({', '.join(CASE_FIELDS)}) VALUES ({marks})",
                values,
            )

    def add_samples(self, rows: Iterable[Mapping[str, Any]]) -> None:
        payload = [[row.get(name) for name in SAMPLE_FIELDS] for row in rows]
        marks = ", ".join("?" * len(SAMPLE_FIELDS))
        with self._lock, self._conn:
            self._conn.executemany(
                f"INSERT INTO samples ({', '.join(SAMPLE_FIELDS)}) VALUES ({marks})", payload
            )

    # --- reads ----------------------------------------------------------------------

    def runs(self, *, since: float | None = None, limit: int = 500, now: float) -> list[dict]:
        """Newest first (see ``_with_status`` for ``stale``)."""
        query = f"SELECT {', '.join(RUN_FIELDS)} FROM runs"
        args: list[Any] = []
        if since is not None:
            query += " WHERE started_at >= ?"
            args.append(since)
        query += " ORDER BY started_at DESC LIMIT ?"
        args.append(limit)
        return self._with_status([dict(row) for row in self._all(query, args)], now)

    def run(self, run_id: str, *, now: float) -> dict | None:
        row = self._one(
            f"SELECT {', '.join(RUN_FIELDS)}, report_json FROM runs WHERE run_id = ?", (run_id,)
        )
        if row is None:
            return None
        detail = dict(row)
        report_json = detail.pop("report_json")
        detail = self._with_status([detail], now)[0]
        detail["report"] = json.loads(report_json) if report_json else None
        detail["cases"] = [
            {
                **{k: row[k] for k in CASE_FIELDS if not k.endswith("_json")},
                "axes": json.loads(row["axes_json"]),
                "checks": json.loads(row["checks_json"] or "{}"),
                "metrics": json.loads(row["metrics_json"] or "{}"),
                "notes": json.loads(row["notes_json"] or "{}"),
            }
            for row in self._all(
                "SELECT * FROM case_runs WHERE run_id = ? ORDER BY case_index, repeat", (run_id,)
            )
        ]
        return detail

    def samples(self, *, since: float, until: float, run_id: str | None = None) -> list[dict]:
        query = f"SELECT {', '.join(SAMPLE_FIELDS)} FROM samples WHERE t >= ? AND t <= ?"
        args: list[Any] = [since, until]
        if run_id is not None:
            query += " AND run_id = ?"
            args.append(run_id)
        return [dict(row) for row in self._all(query + " ORDER BY t", args)]

    def _all(self, query: str, args: Iterable[Any] = ()) -> list[sqlite3.Row]:
        """Reads take the lock too: one connection may be shared by threads (verify's
        sampler and its case workers), and interleaved cursors on it are not safe."""
        with self._lock:
            return self._conn.execute(query, list(args)).fetchall()

    def _one(self, query: str, args: Iterable[Any] = ()) -> sqlite3.Row | None:
        with self._lock:
            return self._conn.execute(query, list(args)).fetchone()

    def _with_status(self, rows: list[dict], now: float) -> list[dict]:
        """A ``running`` run with no sample for ``STALE_AFTER_S`` reads as ``stale``:
        its process died without finishing the row."""
        for row in rows:
            if row["status"] == "running":
                (last,) = self._one("SELECT MAX(t) FROM samples WHERE run_id = ?", (row["run_id"],))
                if now - (last or row["started_at"]) > STALE_AFTER_S:
                    row["status"] = "stale"
        return rows


# --- what verify writes ------------------------------------------------------------------

SAMPLE_EVERY_S = 5.0
MEMINFO = "/proc/meminfo"


def host_snapshot(
    probe: Callable[[], Any] | None = None, meminfo_path: str = MEMINFO
) -> dict[str, Any]:
    """The host now: the scheduler's probe (GPU + available RAM) plus total RAM and
    load. Fields it cannot read are None — a dashboard gap, not an error."""
    sample = (probe or scheduler.probe_host)()
    snapshot: dict[str, Any] = {
        "gpu_used_mib": None if sample is None else sample.gpu_used_mib,
        "gpu_total_mib": None if sample is None else sample.gpu_total_mib,
        "gpu_util_pct": None if sample is None else sample.gpu_util_pct,
        "ram_available_mib": None if sample is None else sample.ram_available_mib,
        "ram_total_mib": None,
        "load1": None,
    }
    try:
        with open(meminfo_path, encoding="utf-8") as handle:
            for line in handle:
                if line.startswith("MemTotal:"):
                    snapshot["ram_total_mib"] = int(line.split()[1]) / 1024.0
        snapshot["load1"] = os.getloadavg()[0]
    except (OSError, ValueError, IndexError):
        pass
    return snapshot


def request_of(spec: Any, environ: Mapping[str, str]) -> dict[str, Any]:
    """Who asked, for what: the GitHub context when the workflow runs us, else local."""
    server = environ.get("GITHUB_SERVER_URL", "https://github.com")
    repo = environ.get("GITHUB_REPOSITORY")
    gh_run_id = environ.get("GITHUB_RUN_ID")
    return {
        "host": socket.gethostname(),
        "repo": repo or f"(local) {spec.checkout.name}",
        "sha": environ.get("GITHUB_SHA") or spec.checkout_sha,
        "ref": environ.get("GITHUB_REF"),
        "event": environ.get("GITHUB_EVENT_NAME") or "local",
        "actor": environ.get("GITHUB_ACTOR"),
        "gh_run_id": gh_run_id,
        "gh_run_url": f"{server}/{repo}/actions/runs/{gh_run_id}" if repo and gh_run_id else None,
        "mode": spec.mode,
        "sim_image": spec.sim_image,
        "sim_script": spec.sim_script,
        "input_space": spec.sim_input_space,
        "pict_k": spec.pict_k,
        "repeats": spec.repeats,
        "concurrency": str(spec.concurrency),
        "budget_s": spec.budget_s,
    }


def summary_of(report: Mapping[str, Any]) -> dict[str, Any]:
    """The run row's result columns, read off a finished report."""
    summary = report["summary"]
    results = [row["result"] for row in report["matrix"]]
    return {
        "cases_planned": summary["cases_planned"],
        "cases_run": summary["cases_run"],
        "cases_pass": results.count("pass"),
        "cases_fail": results.count("fail"),
        "cases_error": results.count("error"),
        "checks_failed": summary["checks_failed"],
        "regressions": summary["regressions"],
        "peak_concurrency": summary.get("peak_concurrency"),
        "exit_code": summary["exit_code"],
        "outcome": summary["report_outcome"],
    }


class RunRecorder:
    """One verify run's writer (see the module docstring). ``store=None`` = off."""

    def __init__(self, store: Store | None, clock: Callable[[], float] = time.time):
        self._store = store
        self._clock = clock
        self.clock = clock
        self.run_id = uuid.uuid4().hex
        self._row: dict[str, Any] = {}

    @classmethod
    def begin(cls, spec: Any, environ: Mapping[str, str], planned: int) -> RunRecorder:
        recorder = cls(None)
        try:
            recorder = cls(Store(default_path(environ, spec.baseline_db)))
            recorder._row = {
                "run_id": recorder.run_id,
                "started_at": recorder._clock(),
                "status": "running",
                "cases_planned": planned,
                **request_of(spec, environ),
            }
            recorder._store.upsert_run(recorder._row)
        except Exception as exc:  # noqa: BLE001 - best effort, see the module docstring
            recorder._fail(exc)
        return recorder

    def case_run(self, case: Any, run: Any, started_at: float, ended_at: float) -> None:
        self._write(
            lambda store: store.add_case_run(
                {
                    "run_id": self.run_id,
                    "case_index": case.case_index,
                    "repeat": case.repeat,
                    "case_id": case.case_id,
                    "axes_json": _json(dict(case.axes)),
                    "started_at": started_at,
                    "ended_at": ended_at,
                    "wall_s": run.wall_s,
                    "lane": run.result.lane,
                    "error": run.result.error,
                    "gpu_retries": run.gpu_retries,
                    "checks_json": _json(run.result.checks),
                    "metrics_json": _json(run.result.metrics),
                    "notes_json": _json(run.result.notes),
                }
            )
        )

    def sample(self, running: int, level: int) -> None:
        if self._store is None:
            return
        snapshot = host_snapshot()
        self._write(
            lambda store: store.add_samples(
                [{"t": self._clock(), "run_id": self.run_id, "running": running,
                  "level": level, **snapshot}]  # fmt: skip
            )
        )

    @contextmanager
    def sampling(self, live: Any, governor: Any, every_s: float = SAMPLE_EVERY_S) -> Iterator[None]:
        """Sample in a daemon thread for the duration of the block."""
        stop = threading.Event()

        def level() -> int:
            return getattr(governor, "level", governor.max_parallel)

        def loop() -> None:
            while not stop.wait(every_s):
                self.sample(live.running, level())

        self.sample(live.running, level())
        thread = threading.Thread(target=loop, name="cv-dashboard-sampler", daemon=True)
        thread.start()
        try:
            yield
        finally:
            stop.set()
            thread.join(timeout=every_s + 1)

    def finish(self, report: Mapping[str, Any]) -> None:
        row = {**self._row, **summary_of(report), "ended_at": self._clock(), "status": "done"}
        self._write(lambda store: store.upsert_run(row, report))
        if self._store is not None:
            self._store.close()

    def _write(self, action: Callable[[Store], None]) -> None:
        if self._store is None:
            return
        try:
            action(self._store)
        except Exception as exc:  # noqa: BLE001 - best effort
            self._fail(exc)

    def _fail(self, exc: Exception) -> None:
        print(
            f"[cv-infra] run history off for this run ({type(exc).__name__}: {exc})",
            file=sys.stderr,
            flush=True,
        )
        if self._store is not None:
            self._store.close()
        self._store = None


def _json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True)
