"""SQLite(WAL) persistence for the control plane (M3 §3.8) — REQ-ORCH-011, R-DB.

One file, one writer: job state transitions (M3 §3.3) and live ``ROS_DOMAIN_ID``
allocations (M3 §3.6 D-O) are persisted so an orchestrator restart restores
state — ``load_jobs()`` returns every job with its state / ``attempt_count``,
``domain_ids_in_use()`` returns live allocations (the docker-label half of crash
reconciliation, M3 §3.9 / R14, is a later cycle).

Write discipline (M3 §3.8 R-DB): WAL allows concurrent readers but serializes
writers, so every write goes through this ONE connection guarded by a
process-level lock, plus ``PRAGMA busy_timeout`` so a competing writer waits out
a lock instead of failing ``database is locked``. M4 baseline writes arrive
through this same Store API later (LOCKED §7.13) — the baseline table itself is
deliberately NOT created here (M4 scope; no speculative schema). Stdlib only.
"""

from __future__ import annotations

import sqlite3
import threading
from pathlib import Path

from cv_infra.orchestrator.models import Job, JobState

# Operational lock-wait guard (R-DB) — how long a writer waits out a competing
# lock before sqlite errors. Not an NFR quantity; purely a liveness backstop.
_BUSY_TIMEOUT_MS = 5_000

_SCHEMA = """
CREATE TABLE IF NOT EXISTS jobs (
    request_id    TEXT    NOT NULL,
    repeat_index  INTEGER NOT NULL,
    state         TEXT    NOT NULL,
    attempt_count INTEGER NOT NULL,
    PRIMARY KEY (request_id, repeat_index)
);
CREATE TABLE IF NOT EXISTS ros_domain_ids (
    domain_id INTEGER PRIMARY KEY,
    job_id    TEXT NOT NULL UNIQUE
);
"""


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
        with self._write_lock, self._conn:
            self._conn.executescript(_SCHEMA)

    # -- jobs (REQ-ORCH-011: every transition is persisted) -------------------

    def upsert_job(self, job: Job) -> None:
        """Persist the job's CURRENT state + attempt_count (insert or update)."""
        with self._write_lock, self._conn:
            self._conn.execute(
                "INSERT INTO jobs (request_id, repeat_index, state, attempt_count)"
                " VALUES (?, ?, ?, ?)"
                " ON CONFLICT(request_id, repeat_index) DO UPDATE SET"
                " state = excluded.state, attempt_count = excluded.attempt_count",
                (job.request_id, job.repeat_index, job.state.value, job.attempt_count),
            )

    def load_jobs(self) -> list[Job]:
        """Restore every persisted job (restart recovery input, M3 §3.9)."""
        rows = self._conn.execute(
            "SELECT request_id, repeat_index, state, attempt_count FROM jobs"
            " ORDER BY request_id, repeat_index"
        ).fetchall()
        return [
            Job(
                request_id=request_id,
                repeat_index=repeat_index,
                state=JobState(state),
                attempt_count=attempt_count,
            )
            for request_id, repeat_index, state, attempt_count in rows
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

    # -- lifecycle -------------------------------------------------------------

    def close(self) -> None:
        self._conn.close()

    def __enter__(self) -> Store:
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.close()
