"""Baselines — "the same case, the last time this runner judged it".

A baseline is one row per ``(case_id, verdict key)`` on the RUNNER HOST (SQLite,
``CV_BASELINE_DB``). It exists so a check that used to pass and now fails is a
REGRESSION (exit 1) rather than one more red run, and so a metric's drift is visible
without ever gating. Nothing else: no history, no branches, no thresholds.

Four rules, each with the reason it is that way and not the obvious alternative:

* **A check compares PASS RATIOS.** ``current < baseline`` regresses (and gates),
  ``>`` improves, equal is ok. Ratios, not booleans, because ``repeats`` turns one
  case into N samples and 2/3 -> 1/3 is a regression worth seeing.
* **``repeats_run == 1`` only LABELS.** The detail carries ``single_sample: true`` so a
  reader knows the ratio came from one draw — the gate still fires. Downgrading a
  single-sample failure to a warning is how a real regression gets ignored; saying out
  loud that the evidence is thin is not.
* **A metric NEVER gates.** A changed mean is reported with its delta. A threshold is
  the consumer's own ``bool`` to write (verdict.py): the platform has no idea which
  direction of "z_final moved 0.03" is bad.
* **Every access is BEST EFFORT.** A missing directory, a corrupt file, a locked
  writer, a file stamped by a newer build — any exception at all — prints ONE stderr
  line and yields "unavailable, all skipped". A baseline is an ADDED signal; it must
  never be the reason a run cannot report. Absent baseline = ``no_baseline`` = skip,
  which is also the honest first-run state.

Write discipline is ``orchestrator/store.py``'s (since removed; see git
history — the two shorthand ``store.py`` mentions below are that same file): one
connection, one lock, WAL + ``busy_timeout`` so concurrent cases wait out a lock
instead of erroring, and ``PRAGMA user_version`` so a file written by a NEWER build
refuses to be written blind rather than being silently downgraded.

The read path deliberately does NOT create the database's parent directory (a
comparison that fabricates state can only fabricate green); the update path does,
because ``--update-baseline`` must work on a runner whose ``~/.cv-infra/`` does not
exist yet. Stdlib only.
"""

from __future__ import annotations

import sqlite3
import sys
import threading
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

# Operational lock-wait guard (store.py idiom) — how long a writer waits out a
# competing lock before sqlite errors. Not an NFR quantity; a liveness backstop.
_BUSY_TIMEOUT_MS = 5_000

#: Stamped via ``PRAGMA user_version``. v1 = this schema. A file carrying a HIGHER
#: number was written by a newer build and is refused (see the module docstring).
_SCHEMA_VERSION = 1

_SCHEMA = """
CREATE TABLE IF NOT EXISTS baselines (
  case_key       TEXT NOT NULL,
  name           TEXT NOT NULL,
  kind           TEXT NOT NULL CHECK (kind IN ('check','metric')),
  pass_ratio     REAL,
  value          REAL,
  established_at TEXT NOT NULL,
  source         TEXT,
  PRIMARY KEY (case_key, name)
) WITHOUT ROWID;
"""

_COLUMNS = "case_key, name, kind, pass_ratio, value, established_at, source"

_UPSERT = f"""
INSERT INTO baselines ({_COLUMNS}) VALUES (?, ?, ?, ?, ?, ?, ?)
ON CONFLICT (case_key, name) DO UPDATE SET
  kind = excluded.kind,
  pass_ratio = excluded.pass_ratio,
  value = excluded.value,
  established_at = excluded.established_at,
  source = excluded.source
"""

KIND_CHECK = "check"
KIND_METRIC = "metric"

STATUS_OK = "ok"
STATUS_REGRESSED = "regressed"
STATUS_IMPROVED = "improved"
STATUS_CHANGED = "changed"
STATUS_NO_BASELINE = "no_baseline"
STATUS_SKIPPED = "skipped"


@dataclass(frozen=True)
class BaselineRow:
    """One stored reference. ``pass_ratio`` is set for a check, ``value`` for a metric;
    ``established_at``/``source`` are stamped by ``BaselineStore.upsert``, so a row built
    for writing leaves them at their defaults."""

    case_key: str
    name: str
    kind: str
    pass_ratio: float | None = None
    value: float | None = None
    established_at: str = ""
    source: str | None = None


@dataclass(frozen=True)
class CaseObservation:
    """This run's numbers for one case — checks as PASS RATIOS, metrics as MEANS.

    ``errored`` cases carry no numbers at all: an infrastructure fault is not the robot
    regressing, so such a case is skipped by the comparison and never baselined."""

    case_key: str
    checks: Mapping[str, float] = field(default_factory=dict)
    metrics: Mapping[str, float] = field(default_factory=dict)
    repeats_run: int = 1
    errored: bool = False


@dataclass(frozen=True)
class CaseRegression:
    """One case's verdict against its baseline: the folded ``status`` plus the per-key
    ``details`` the report renders (name/kind/status/baseline/current)."""

    status: str
    details: tuple[dict[str, Any], ...] = ()


@dataclass(frozen=True)
class BaselineOutcome:
    """The whole comparison. ``regressed`` is the only field that moves the exit code."""

    db: str
    available: bool
    cases: dict[str, CaseRegression]
    compared: int = 0
    absent: int = 0
    regressed: int = 0
    improved: int = 0
    metric_changes: int = 0
    error: str | None = None


class BaselineStore:
    """The SQLite file, opened. One connection + one lock (``store.py`` discipline);
    ``check_same_thread=False`` is safe under it because every write goes through the
    lock and sqlite3 serializes statements regardless."""

    def __init__(self, db_path: str | Path) -> None:
        self._conn = sqlite3.connect(str(db_path), check_same_thread=False)
        self._write_lock = threading.Lock()
        try:
            self._conn.execute(f"PRAGMA busy_timeout = {_BUSY_TIMEOUT_MS}")
            self._conn.execute("PRAGMA journal_mode = WAL")
            (version,) = self._conn.execute("PRAGMA user_version").fetchone()
            if version > _SCHEMA_VERSION:
                raise RuntimeError(
                    f"baseline db {db_path} carries schema v{version}, newer than this"
                    f" build's v{_SCHEMA_VERSION} — refusing to write blind"
                )
            with self._write_lock, self._conn:
                self._conn.executescript(_SCHEMA)
                self._conn.execute(f"PRAGMA user_version = {_SCHEMA_VERSION}")
        except Exception:
            # Never leave the handle open behind a failed open — the caller's
            # best-effort wrapper only sees the exception, not this connection.
            self._conn.close()
            raise

    def load(self, case_keys: Iterable[str]) -> dict[tuple[str, str], BaselineRow]:
        """Every stored row for these cases, keyed ``(case_key, name)``.

        One statement with one placeholder per case: a covering array is tens to
        hundreds of cases, well under sqlite's variable limit."""
        keys = sorted(set(case_keys))
        if not keys:
            return {}
        placeholders = ",".join("?" * len(keys))
        rows = self._conn.execute(
            f"SELECT {_COLUMNS} FROM baselines WHERE case_key IN ({placeholders})", keys
        ).fetchall()
        return {(row[0], row[1]): BaselineRow(*row) for row in rows}

    def upsert(self, rows: Iterable[BaselineRow], source: str | None) -> int:
        """Establish or advance rows, all stamped with ONE timestamp and ``source``.

        Advance, not append: a baseline is "the last accepted run", so the newest write
        wins outright. Returns how many rows were written."""
        stamped = _now_iso()
        payload = [
            (row.case_key, row.name, row.kind, row.pass_ratio, row.value, stamped, source)
            for row in rows
        ]
        with self._write_lock, self._conn:
            self._conn.executemany(_UPSERT, payload)
        return len(payload)

    def close(self) -> None:
        self._conn.close()


def compare_best_effort(db_path: str | Path, current: Iterable[CaseObservation]) -> BaselineOutcome:
    """Judge this run against the stored baselines — never raising, whatever happens.

    ANY failure (missing directory, corrupt file, locked writer, newer schema) becomes
    one stderr line and an outcome whose every case is ``skipped``: the run still
    reports, it just reports without this signal."""
    observations = list(current)
    try:
        store = BaselineStore(db_path)
        try:
            baselines = store.load(obs.case_key for obs in observations if not obs.errored)
        finally:
            store.close()
        return _compare(str(db_path), observations, baselines)
    except Exception as exc:
        print(
            f"cv-infra: baseline db {db_path} unavailable ({exc}) — every case skipped",
            file=sys.stderr,
        )
        return BaselineOutcome(
            db=str(db_path),
            available=False,
            cases={obs.case_key: CaseRegression(STATUS_SKIPPED) for obs in observations},
            error=str(exc),
        )


def upsert_best_effort(
    db_path: str | Path, rows: Iterable[BaselineRow], *, source: str | None
) -> bool:
    """Write the new reference (``--update-baseline`` only), never raising.

    Creates the db's parent directory — unlike the read path (module docstring): a
    fresh runner has no ``~/.cv-infra/`` and an update that cannot write is a silently
    frozen baseline. Returns whether the write happened."""
    try:
        Path(db_path).parent.mkdir(parents=True, exist_ok=True)
        store = BaselineStore(db_path)
        try:
            store.upsert(rows, source)
        finally:
            store.close()
    except Exception as exc:
        print(f"cv-infra: baseline db {db_path} not updated ({exc})", file=sys.stderr)
        return False
    return True


def rows_for(observations: Iterable[CaseObservation]) -> list[BaselineRow]:
    """This run's observations as writable rows — ERRORED cases excluded (an infra
    outcome is never a reference)."""
    rows: list[BaselineRow] = []
    for obs in observations:
        if obs.errored:
            continue
        rows.extend(
            BaselineRow(case_key=obs.case_key, name=name, kind=KIND_CHECK, pass_ratio=ratio)
            for name, ratio in sorted(obs.checks.items())
        )
        rows.extend(
            BaselineRow(case_key=obs.case_key, name=name, kind=KIND_METRIC, value=mean)
            for name, mean in sorted(obs.metrics.items())
        )
    return rows


def source_for(environ: Mapping[str, str]) -> str:
    """Provenance of a baseline write: the commit CI ran, else ``manual`` for a local
    run — so a surprising reference can be traced back to what produced it."""
    return environ.get("GITHUB_SHA") or "manual"


# --- internals ------------------------------------------------------------------------


def _now_iso() -> str:
    return datetime.now(UTC).isoformat()


def _compare(
    db: str,
    observations: list[CaseObservation],
    baselines: dict[tuple[str, str], BaselineRow],
) -> BaselineOutcome:
    cases: dict[str, CaseRegression] = {}
    compared = absent = regressed = improved = metric_changes = 0
    for obs in observations:
        if obs.errored:
            cases[obs.case_key] = CaseRegression(STATUS_SKIPPED)
            continue
        details: list[dict[str, Any]] = []
        for name, ratio in sorted(obs.checks.items()):
            row = baselines.get((obs.case_key, name))
            # A key whose stored kind (or value) does not match what it is TODAY has no
            # comparable history — a verdict key that changed type starts over.
            if row is None or row.kind != KIND_CHECK or row.pass_ratio is None:
                absent += 1
                status, baseline_ratio = STATUS_NO_BASELINE, None
            else:
                compared += 1
                baseline_ratio = row.pass_ratio
                if ratio < baseline_ratio:
                    status = STATUS_REGRESSED
                    regressed += 1
                elif ratio > baseline_ratio:
                    status = STATUS_IMPROVED
                    improved += 1
                else:
                    status = STATUS_OK
            details.append(
                {
                    "name": name,
                    "kind": KIND_CHECK,
                    "status": status,
                    "baseline": baseline_ratio,
                    "current": ratio,
                    # Label only — a one-draw ratio still gates (module docstring).
                    "single_sample": obs.repeats_run == 1,
                }
            )
        for name, mean in sorted(obs.metrics.items()):
            row = baselines.get((obs.case_key, name))
            if row is None or row.kind != KIND_METRIC or row.value is None:
                absent += 1
                status, baseline_value, delta = STATUS_NO_BASELINE, None, None
            else:
                compared += 1
                baseline_value = row.value
                delta = mean - baseline_value
                if delta == 0.0:
                    status = STATUS_OK
                else:
                    status = STATUS_CHANGED  # reported, never gating
                    metric_changes += 1
            details.append(
                {
                    "name": name,
                    "kind": KIND_METRIC,
                    "status": status,
                    "baseline": baseline_value,
                    "current": mean,
                    "delta": delta,
                }
            )
        cases[obs.case_key] = CaseRegression(_case_status(details), tuple(details))
    return BaselineOutcome(
        db=db,
        available=True,
        cases=cases,
        compared=compared,
        absent=absent,
        regressed=regressed,
        improved=improved,
        metric_changes=metric_changes,
    )


def _case_status(details: list[dict[str, Any]]) -> str:
    """Fold the per-key statuses into the case's own (report schema: ok / regressed /
    no_baseline / skipped — an improvement is an ok case, counted in the summary)."""
    if any(detail["status"] == STATUS_REGRESSED for detail in details):
        return STATUS_REGRESSED
    if any(detail["status"] != STATUS_NO_BASELINE for detail in details):
        return STATUS_OK
    return STATUS_NO_BASELINE  # nothing to compare against (also: a case with no keys)
