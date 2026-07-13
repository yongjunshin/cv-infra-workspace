"""SQLite(WAL) persistence for the control plane (M3 §3.8) — REQ-ORCH-011, R-DB.

One file, one writer: job state transitions (M3 §3.3), live ``ROS_DOMAIN_ID``
allocations (M3 §3.6 D-O), the envelope->request registry and the per-request
``RequestRollup``s (p4c4 — the api.py registry is no longer memory-only) are
persisted so an orchestrator restart restores state — ``load_jobs()`` returns
every job with its state / ``attempt_count`` / stage-5 anchor / canonical
``job_spec`` (v3, p4c4 glue),
``domain_ids_in_use()`` returns live allocations, ``load_envelope()`` /
``load_rollup()`` restore the submit-surface view. RUNNING orphans left behind
by a crash are re-labeled by ``supervisor.reconcile_at_restart`` (M3 §3.9, R14).

Schema compatibility (p4c4 방침): ``PRAGMA user_version`` stamps the schema
version. Upgrades are ADDITIVE in place — new tables via CREATE IF NOT EXISTS,
new columns via a guarded ALTER — so a v1 file (p4c1/p4c3) opens and upgrades
transparently; a file stamped NEWER than this build refuses loudly instead of
writing blind. No migration framework (LOCKED — MVP single file).

Write discipline (M3 §3.8 R-DB): WAL allows concurrent readers but serializes
writers, so every write goes through this ONE connection guarded by a
process-level lock, plus ``PRAGMA busy_timeout`` so a competing writer waits out
a lock instead of failing ``database is locked``. M4 baseline writes arrive
through this same Store API later (LOCKED §7.13) — the baseline table itself is
deliberately NOT created here (M4 scope; no speculative schema). Stdlib only.
"""

from __future__ import annotations

import json
import sqlite3
import threading
from dataclasses import dataclass, field
from pathlib import Path

from cv_infra.orchestrator.models import Job, JobState, RequestRollup, Verdict

# Operational lock-wait guard (R-DB) — how long a writer waits out a competing
# lock before sqlite errors. Not an NFR quantity; purely a liveness backstop.
_BUSY_TIMEOUT_MS = 5_000

# Stamped via PRAGMA user_version (module docstring). v1 = p4c1/p4c3 (jobs +
# ros_domain_ids, unstamped = 0); v2 = p4c4 (jobs.oracle_plugin_dir + envelope
# registry + rollups); v3 = p4c4 glue (jobs.job_spec — the canonical JOB_SPEC
# JSON riding each job, T1 report §7-1 (a) 재기동 대비 영속).
_SCHEMA_VERSION = 3

_SCHEMA = """
CREATE TABLE IF NOT EXISTS jobs (
    request_id        TEXT    NOT NULL,
    repeat_index      INTEGER NOT NULL,
    state             TEXT    NOT NULL,
    attempt_count     INTEGER NOT NULL,
    oracle_plugin_dir TEXT,
    job_spec          TEXT,
    PRIMARY KEY (request_id, repeat_index)
);
CREATE TABLE IF NOT EXISTS ros_domain_ids (
    domain_id INTEGER PRIMARY KEY,
    job_id    TEXT NOT NULL UNIQUE
);
CREATE TABLE IF NOT EXISTS envelopes (
    envelope_id    TEXT PRIMARY KEY,
    status         TEXT NOT NULL,
    report_outcome TEXT,
    error          TEXT
);
CREATE TABLE IF NOT EXISTS envelope_requests (
    envelope_id       TEXT    NOT NULL,
    position          INTEGER NOT NULL,
    request_id        TEXT    NOT NULL UNIQUE,
    oracle_plugin_dir TEXT,
    PRIMARY KEY (envelope_id, position)
);
CREATE TABLE IF NOT EXISTS request_rollups (
    request_id TEXT PRIMARY KEY,
    verdicts   TEXT NOT NULL,
    flakiness  REAL,
    verdict    TEXT
);
"""

# Envelope status literals persisted in the envelopes table — the same two the
# api.py wire exposes (module docstring shape pin).
ENVELOPE_RUNNING = "running"
ENVELOPE_COMPLETED = "completed"


@dataclass
class StoredEnvelope:
    """Store-row view of one submitted envelope (api.py registry, persisted).

    ``request_ids`` keeps submission order; ``oracle_plugin_dirs`` is the
    equal-length per-request stage-5 anchor list (None = no anchor).
    ``report_outcome`` / ``error`` stay None until completion / a crash marker.
    """

    envelope_id: str
    request_ids: list[str] = field(default_factory=list)
    oracle_plugin_dirs: list[str | None] = field(default_factory=list)
    status: str = ENVELOPE_RUNNING
    report_outcome: str | None = None
    error: str | None = None


def job_key(job: Job) -> str:
    """Stable control-plane identity of a fanned-out job: ``<request_id>:<repeat_index>``.

    The 2-axis fan-out key (REQ-ORCH-001/002) flattened to one string — used as
    the allocator/label job id wherever a scalar handle is needed.
    """
    return f"{job.request_id}:{job.repeat_index}"


class Store:
    """Single-file SQLite(WAL) store — the persistence seam of the control plane.

    All writes are serialized through one connection + one lock (module
    docstring, R-DB). ``check_same_thread=False`` is safe under that discipline:
    the supervisor's executor threads never touch the store directly (writes
    happen on the event-loop thread via JobQueue), and sqlite3 serializes
    statement execution regardless.
    """

    def __init__(self, db_path: str | Path) -> None:
        self._conn = sqlite3.connect(str(db_path), check_same_thread=False)
        self._write_lock = threading.Lock()
        self._conn.execute(f"PRAGMA busy_timeout = {_BUSY_TIMEOUT_MS}")
        self._conn.execute("PRAGMA journal_mode = WAL")
        (version,) = self._conn.execute("PRAGMA user_version").fetchone()
        if version > _SCHEMA_VERSION:
            raise RuntimeError(
                f"store file {db_path} carries schema v{version}, newer than this build's"
                f" v{_SCHEMA_VERSION} — refusing to write blind (module docstring 방침)"
            )
        with self._write_lock, self._conn:
            self._conn.executescript(_SCHEMA)
            # Older files predate the jobs columns below (v1: oracle_plugin_dir,
            # v2: job_spec): additive in-place upgrade — CREATE IF NOT EXISTS
            # cannot add a column.
            columns = {row[1] for row in self._conn.execute("PRAGMA table_info(jobs)")}
            for column in ("oracle_plugin_dir", "job_spec"):
                if column not in columns:
                    self._conn.execute(f"ALTER TABLE jobs ADD COLUMN {column} TEXT")
            self._conn.execute(f"PRAGMA user_version = {_SCHEMA_VERSION}")

    # -- jobs (REQ-ORCH-011: every transition is persisted) -------------------

    def upsert_job(self, job: Job) -> None:
        """Persist the job's CURRENT state + attempt_count + anchor + spec (insert or update)."""
        with self._write_lock, self._conn:
            self._conn.execute(
                "INSERT INTO jobs (request_id, repeat_index, state, attempt_count,"
                " oracle_plugin_dir, job_spec) VALUES (?, ?, ?, ?, ?, ?)"
                " ON CONFLICT(request_id, repeat_index) DO UPDATE SET"
                " state = excluded.state, attempt_count = excluded.attempt_count,"
                " oracle_plugin_dir = excluded.oracle_plugin_dir,"
                " job_spec = excluded.job_spec",
                (
                    job.request_id,
                    job.repeat_index,
                    job.state.value,
                    job.attempt_count,
                    job.oracle_plugin_dir,
                    json.dumps(job.job_spec, sort_keys=True) if job.job_spec is not None else None,
                ),
            )

    def load_jobs(self) -> list[Job]:
        """Restore every persisted job (restart recovery input, M3 §3.9)."""
        rows = self._conn.execute(
            "SELECT request_id, repeat_index, state, attempt_count, oracle_plugin_dir, job_spec"
            " FROM jobs ORDER BY request_id, repeat_index"
        ).fetchall()
        return [
            Job(
                request_id=request_id,
                repeat_index=repeat_index,
                state=JobState(state),
                attempt_count=attempt_count,
                oracle_plugin_dir=oracle_plugin_dir,
                job_spec=json.loads(job_spec) if job_spec is not None else None,
            )
            for request_id, repeat_index, state, attempt_count, oracle_plugin_dir, job_spec in rows
        ]

    # -- ROS_DOMAIN_ID rows (M3 §3.6 D-O: allocated/released via SQLite) ------

    def record_domain_id(self, domain_id: int, job_id: str) -> None:
        """Record a live allocation. A collision (live id re-recorded) raises
        ``sqlite3.IntegrityError`` — re-assigning a live id breaks the dual
        isolation (LOCKED §7.5) and must be loud, never absorbed."""
        with self._write_lock, self._conn:
            self._conn.execute(
                "INSERT INTO ros_domain_ids (domain_id, job_id) VALUES (?, ?)",
                (domain_id, job_id),
            )

    def release_domain_id(self, job_id: str) -> None:
        """Release ``job_id``'s allocation. Releasing a job that holds none raises
        KeyError — allocate/release must stay 1:1 (회수 누락 0, REQ-ORCH-006 결)."""
        with self._write_lock, self._conn:
            cursor = self._conn.execute("DELETE FROM ros_domain_ids WHERE job_id = ?", (job_id,))
            if cursor.rowcount != 1:
                raise KeyError(
                    f"no ROS_DOMAIN_ID allocated to job {job_id!r}"
                    " (allocate/release accounting must be 1:1)"
                )

    def domain_ids_in_use(self) -> dict[int, str]:
        """Live allocations: ``{domain_id: job_id}``."""
        rows = self._conn.execute("SELECT domain_id, job_id FROM ros_domain_ids").fetchall()
        return dict(rows)

    def release_all_domain_ids(self) -> int:
        """Clear EVERY liveness row; returns the count cleared.

        Restart-reconciliation only (M3 §3.9 — after the label sweep tore every
        cv-infra container down, every row is stale by the single-deployment
        assumption). Never a substitute for per-job ``release_domain_id``.
        """
        with self._write_lock, self._conn:
            return self._conn.execute("DELETE FROM ros_domain_ids").rowcount

    # -- envelope->request registry (p4c4 — api.py 유실 해소) -------------------

    def record_envelope(
        self,
        envelope_id: str,
        request_ids: list[str],
        oracle_plugin_dirs: list[str | None] | None = None,
    ) -> None:
        """Persist a submitted envelope's registry row + ordered request refs.

        ``oracle_plugin_dirs`` (optional) must be equal-length when given —
        the per-request stage-5 anchors ride the registry so a restart knows
        them (None entry = no anchor).
        """
        anchors: list[str | None] = (
            oracle_plugin_dirs if oracle_plugin_dirs is not None else [None] * len(request_ids)
        )
        if len(anchors) != len(request_ids):
            raise ValueError(
                f"oracle_plugin_dirs must have {len(request_ids)} items, got {len(anchors)}"
            )
        with self._write_lock, self._conn:
            self._conn.execute(
                "INSERT INTO envelopes (envelope_id, status) VALUES (?, ?)",
                (envelope_id, ENVELOPE_RUNNING),
            )
            self._conn.executemany(
                "INSERT INTO envelope_requests"
                " (envelope_id, position, request_id, oracle_plugin_dir) VALUES (?, ?, ?, ?)",
                [
                    (envelope_id, position, request_id, anchor)
                    for position, (request_id, anchor) in enumerate(
                        zip(request_ids, anchors, strict=True)
                    )
                ],
            )

    def complete_envelope(
        self, envelope_id: str, *, report_outcome: str | None = None, error: str | None = None
    ) -> None:
        """Mark an envelope terminal: ``report_outcome`` on success, ``error`` on a crash."""
        with self._write_lock, self._conn:
            cursor = self._conn.execute(
                "UPDATE envelopes SET status = ?, report_outcome = ?, error = ?"
                " WHERE envelope_id = ?",
                (ENVELOPE_COMPLETED, report_outcome, error, envelope_id),
            )
            if cursor.rowcount != 1:
                raise KeyError(f"unknown envelope {envelope_id!r} (record_envelope first)")

    def fail_running_envelopes(self, error: str) -> int:
        """Mark every still-RUNNING envelope failed-with-``error``; returns the count.

        Restart reconciliation (M3 §3.9): supervision of an in-flight envelope
        died with the orchestrator and is not resumed — a loud error marker
        beats an envelope stuck 'running' forever.
        """
        with self._write_lock, self._conn:
            return self._conn.execute(
                "UPDATE envelopes SET status = ?, error = ? WHERE status = ?",
                (ENVELOPE_COMPLETED, error, ENVELOPE_RUNNING),
            ).rowcount

    def load_envelope(self, envelope_id: str) -> StoredEnvelope | None:
        """Restore one envelope's registry view (None when unknown)."""
        row = self._conn.execute(
            "SELECT status, report_outcome, error FROM envelopes WHERE envelope_id = ?",
            (envelope_id,),
        ).fetchone()
        if row is None:
            return None
        status, report_outcome, error = row
        refs = self._conn.execute(
            "SELECT request_id, oracle_plugin_dir FROM envelope_requests"
            " WHERE envelope_id = ? ORDER BY position",
            (envelope_id,),
        ).fetchall()
        return StoredEnvelope(
            envelope_id=envelope_id,
            request_ids=[request_id for request_id, _ in refs],
            oracle_plugin_dirs=[anchor for _, anchor in refs],
            status=status,
            report_outcome=report_outcome,
            error=error,
        )

    # -- request-level rollups (SR-10 — p4c4 영속) ------------------------------

    def upsert_rollup(self, rollup: RequestRollup) -> None:
        """Persist one request's terminal ``RequestRollup`` (insert or update)."""
        with self._write_lock, self._conn:
            self._conn.execute(
                "INSERT INTO request_rollups (request_id, verdicts, flakiness, verdict)"
                " VALUES (?, ?, ?, ?)"
                " ON CONFLICT(request_id) DO UPDATE SET verdicts = excluded.verdicts,"
                " flakiness = excluded.flakiness, verdict = excluded.verdict",
                (
                    rollup.request_id,
                    json.dumps([v.value for v in rollup.verdicts]),
                    rollup.flakiness,
                    rollup.verdict.value if rollup.verdict is not None else None,
                ),
            )

    def load_rollup(self, request_id: str) -> RequestRollup | None:
        """Restore one request's persisted rollup (None when never persisted)."""
        row = self._conn.execute(
            "SELECT verdicts, flakiness, verdict FROM request_rollups WHERE request_id = ?",
            (request_id,),
        ).fetchone()
        if row is None:
            return None
        verdicts, flakiness, verdict = row
        return RequestRollup(
            request_id=request_id,
            verdicts=[Verdict(v) for v in json.loads(verdicts)],
            flakiness=flakiness,
            verdict=Verdict(verdict) if verdict is not None else None,
        )

    # -- lifecycle -------------------------------------------------------------

    def close(self) -> None:
        self._conn.close()

    def __enter__(self) -> Store:
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.close()
