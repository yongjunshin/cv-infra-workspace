"""SQLite(WAL) persistence for the control plane (M3 §3.8) — REQ-ORCH-011, R-DB.

One file, one writer: job state transitions (M3 §3.3), live ``ROS_DOMAIN_ID``
allocations (M3 §3.6 D-O), the envelope->request registry and the per-request
``RequestRollup``s (p4c4 — the api.py registry is no longer memory-only) are
persisted so an orchestrator restart restores state — ``load_jobs()`` returns
every job with its state / ``attempt_count`` / stage-5 anchor / canonical
``job_spec`` (v3, p4c4 glue) / last-attempt failure diagnostics
(``runner_exit_code`` + ``infra_error`` — v4, p4c5 실패 관측성),
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
through this same Store API (LOCKED §7.13): the ``request_baselines`` table
(v6, p5c1) + its ``upsert_baseline`` / ``load_baseline`` accessors live here so
the single writer stays single (M4 ``report/baseline.py`` calls them, never
touches SQLite directly), keeping the C-1 baseline home inside cv-infra's own
store (no separate DB service, C-2). Stdlib only.
"""

from __future__ import annotations

import json
import sqlite3
import threading
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from cv_infra.orchestrator.models import Job, JobState, RequestRollup, Verdict

# Operational lock-wait guard (R-DB) — how long a writer waits out a competing
# lock before sqlite errors. Not an NFR quantity; purely a liveness backstop.
_BUSY_TIMEOUT_MS = 5_000

# Terminal job states (a job's ``ended_at`` is stamped when it reaches one of
# these — the operational duration anchor, M6 §3.2).
_TERMINAL_STATES = frozenset({JobState.COMPLETED, JobState.FAILED, JobState.TIMEOUT})


def _now_iso() -> str:
    """Timezone-aware UTC ISO8601 stamp — the single source of operational timestamps
    (envelope submit / job start-end / resource sample, M6 §3.2)."""
    return datetime.now(UTC).isoformat()


# Stamped via PRAGMA user_version (module docstring). v1 = p4c1/p4c3 (jobs +
# ros_domain_ids, unstamped = 0); v2 = p4c4 (jobs.oracle_plugin_dir + envelope
# registry + rollups); v3 = p4c4 glue (jobs.job_spec — the canonical JOB_SPEC
# JSON riding each job, T1 report §7-1 (a) 재기동 대비 영속); v4 = p4c5 실패
# 관측성 (jobs.runner_exit_code + jobs.infra_error — the last terminal attempt's
# diagnostics, previously dropped in the JobOutcome->JobResult fold: a hard-crash
# 137/139 was indistinguishable from exit 1 in the store, history 2026-07-14 놀란 점 7);
# v5 = p4c6 M6 운영뷰 (envelopes.submitted_at + jobs.started_at/ended_at operational
# timestamps + the resource_health_sample latest-row snapshot the sampler upserts —
# all ADDITIVE, the operational read model's inputs, M6 §3.2/§3.4). Time-series
# retention of samples is post-MVP (one latest row only — 과설계 금지);
# v6 = p5c1 M4 회귀 baseline (request_baselines — the request-level baseline table
# M4 SR-21/C-1 owns, keyed by request_identity_key; ADDITIVE new table via
# CREATE IF NOT EXISTS so every older file gains it transparently on open, no ALTER
# needed. This is the ONLY baseline home — LOCKED §7.13 cv-infra 내부 SQLite 한정);
# v7 = p5c2 M4 리포트 영속 (envelope_reports — the server-side-assembled
# VerificationReport JSON per envelope the api _persist_terminal writes at clean
# completion, keyed by envelope_id; ADDITIVE new table via CREATE IF NOT EXISTS so
# ``GET /envelopes/{id}/report`` serves the durable report AFTER an orchestrator
# restart, never re-assembling from results that did not survive — same 영속본
# 서빙 discipline as the rollups/registry above).
_SCHEMA_VERSION = 7

_SCHEMA = """
CREATE TABLE IF NOT EXISTS jobs (
    request_id        TEXT    NOT NULL,
    repeat_index      INTEGER NOT NULL,
    state             TEXT    NOT NULL,
    attempt_count     INTEGER NOT NULL,
    oracle_plugin_dir TEXT,
    job_spec          TEXT,
    runner_exit_code  INTEGER,
    infra_error       TEXT,
    started_at        TEXT,
    ended_at          TEXT,
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
    error          TEXT,
    submitted_at   TEXT
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
CREATE TABLE IF NOT EXISTS resource_health_sample (
    id                INTEGER PRIMARY KEY CHECK (id = 1),
    sampled_at        TEXT    NOT NULL,
    gpu_reachable     INTEGER NOT NULL,
    vram_used_mib     INTEGER,
    vram_total_mib    INTEGER,
    gpu_util_pct      INTEGER,
    queue_depth       INTEGER NOT NULL,
    running_k         INTEGER NOT NULL,
    over_launch_count INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS request_baselines (
    request_identity_key TEXT PRIMARY KEY,
    sut_ref              TEXT NOT NULL,
    verdict              TEXT NOT NULL,
    key_metrics          TEXT,
    established_at       TEXT NOT NULL,
    source_result_ref    TEXT
);
CREATE TABLE IF NOT EXISTS envelope_reports (
    envelope_id TEXT PRIMARY KEY,
    report_json TEXT NOT NULL
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
    ``submitted_at`` (v5) is the ISO8601 submit timestamp (None for legacy rows).
    """

    envelope_id: str
    request_ids: list[str] = field(default_factory=list)
    oracle_plugin_dirs: list[str | None] = field(default_factory=list)
    status: str = ENVELOPE_RUNNING
    report_outcome: str | None = None
    error: str | None = None
    submitted_at: str | None = None


@dataclass
class OperationalJobRow:
    """Operational-only projection of one job row (M6 read model, DoD-P4-13).

    The SELECT behind ``load_operational_jobs`` reads EXACTLY these columns — the
    domain-carrying ``job_spec`` (scenario/criteria/sut명세) and the
    ``oracle_plugin_dir`` anchor are STRUCTURALLY absent from the projection's
    SELECT list, so the operational view cannot leak them (누수 불가 = 출처 분리,
    not a display filter). ``started_at`` / ``ended_at`` are the v5 operational
    timestamps; ``runner_exit_code`` / ``infra_error`` are the p4c5 breadcrumbs
    already proven leak-free (bounded 300-char reason)."""

    request_id: str
    repeat_index: int
    state: str
    attempt_count: int
    started_at: str | None
    ended_at: str | None
    runner_exit_code: int | None
    infra_error: str | None


@dataclass
class ResourceSample:
    """Latest resource/health snapshot the M6 sampler upserts (M3 §3.4 store 적재).

    ONE latest row (id=1 upsert); time-series retention is post-MVP. NVML fields
    are None on a GPU-free/degraded host (``gpu_reachable=False``)."""

    sampled_at: str
    gpu_reachable: bool
    vram_used_mib: int | None
    vram_total_mib: int | None
    gpu_util_pct: int | None
    queue_depth: int
    running_k: int
    over_launch_count: int


@dataclass
class BaselineRow:
    """Store-row view of one request-level regression baseline (M4 SR-21, C-1).

    Keyed by ``request_identity_key`` (M4 ``regression.identity_key`` — the
    scenario+criteria+settings normalized hash, SUT excluded), so a later run of
    the SAME request with only a different SUT matches this row and its verdict
    can be compared (pass->fail = regression). ``sut_ref`` / ``established_at``
    record WHICH SUT version and WHEN the baseline was captured (NFR-REPORT-001
    식별성). ``key_metrics`` is a free JSON map (declared-metric snapshot);
    ``source_result_ref`` optionally points back at the establishing result.
    The store is the ONLY home of this row (LOCKED §7.13 C-1) — M4 writes it
    exclusively through ``upsert_baseline`` (single writer)."""

    request_identity_key: str
    sut_ref: str
    verdict: str
    key_metrics: dict[str, Any] = field(default_factory=dict)
    established_at: str = ""
    source_result_ref: str | None = None


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
            # v2: job_spec, v3: runner_exit_code + infra_error, v5: started_at +
            # ended_at): additive in-place upgrade — CREATE IF NOT EXISTS cannot add
            # a column. Legacy rows read back with NULLs in the new columns.
            job_columns = {row[1] for row in self._conn.execute("PRAGMA table_info(jobs)")}
            for column, column_type in (
                ("oracle_plugin_dir", "TEXT"),
                ("job_spec", "TEXT"),
                ("runner_exit_code", "INTEGER"),
                ("infra_error", "TEXT"),
                ("started_at", "TEXT"),
                ("ended_at", "TEXT"),
            ):
                if column not in job_columns:
                    self._conn.execute(f"ALTER TABLE jobs ADD COLUMN {column} {column_type}")
            # v5: envelopes.submitted_at — same additive discipline (the resource
            # sample table is new, so CREATE IF NOT EXISTS already handles it).
            env_columns = {row[1] for row in self._conn.execute("PRAGMA table_info(envelopes)")}
            if "submitted_at" not in env_columns:
                self._conn.execute("ALTER TABLE envelopes ADD COLUMN submitted_at TEXT")
            self._conn.execute(f"PRAGMA user_version = {_SCHEMA_VERSION}")

    # -- jobs (REQ-ORCH-011: every transition is persisted) -------------------

    def upsert_job(self, job: Job) -> None:
        """Persist the job's CURRENT state + attempt_count + anchor + spec + last-attempt
        failure diagnostics + operational timestamps (insert or update; v5).

        ``started_at`` is stamped once — the FIRST time the job persists RUNNING
        (COALESCE keeps the earliest, so a retry does not reset it); ``ended_at``
        is stamped when the job persists a terminal state. Derived from the state
        being written, so ``mark_running`` / ``record_outcome`` need no change
        (M6 §3.2 — the operational duration anchors ride the existing write path).
        """
        now = _now_iso()
        started_at = now if job.state is JobState.RUNNING else None
        ended_at = now if job.state in _TERMINAL_STATES else None
        with self._write_lock, self._conn:
            self._conn.execute(
                "INSERT INTO jobs (request_id, repeat_index, state, attempt_count,"
                " oracle_plugin_dir, job_spec, runner_exit_code, infra_error,"
                " started_at, ended_at)"
                " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)"
                " ON CONFLICT(request_id, repeat_index) DO UPDATE SET"
                " state = excluded.state, attempt_count = excluded.attempt_count,"
                " oracle_plugin_dir = excluded.oracle_plugin_dir,"
                " job_spec = excluded.job_spec,"
                " runner_exit_code = excluded.runner_exit_code,"
                " infra_error = excluded.infra_error,"
                " started_at = COALESCE(jobs.started_at, excluded.started_at),"
                " ended_at = excluded.ended_at",
                (
                    job.request_id,
                    job.repeat_index,
                    job.state.value,
                    job.attempt_count,
                    job.oracle_plugin_dir,
                    json.dumps(job.job_spec, sort_keys=True) if job.job_spec is not None else None,
                    job.runner_exit_code,
                    job.infra_error,
                    started_at,
                    ended_at,
                ),
            )

    def load_jobs(self) -> list[Job]:
        """Restore every persisted job (restart recovery input, M3 §3.9)."""
        rows = self._conn.execute(
            "SELECT request_id, repeat_index, state, attempt_count, oracle_plugin_dir, job_spec,"
            " runner_exit_code, infra_error FROM jobs ORDER BY request_id, repeat_index"
        ).fetchall()
        return [
            Job(
                request_id=request_id,
                repeat_index=repeat_index,
                state=JobState(state),
                attempt_count=attempt_count,
                oracle_plugin_dir=oracle_plugin_dir,
                job_spec=json.loads(job_spec) if job_spec is not None else None,
                runner_exit_code=runner_exit_code,
                infra_error=infra_error,
            )
            for (
                request_id,
                repeat_index,
                state,
                attempt_count,
                oracle_plugin_dir,
                job_spec,
                runner_exit_code,
                infra_error,
            ) in rows
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
                "INSERT INTO envelopes (envelope_id, status, submitted_at) VALUES (?, ?, ?)",
                (envelope_id, ENVELOPE_RUNNING, _now_iso()),
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
            "SELECT status, report_outcome, error, submitted_at FROM envelopes"
            " WHERE envelope_id = ?",
            (envelope_id,),
        ).fetchone()
        if row is None:
            return None
        status, report_outcome, error, submitted_at = row
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
            submitted_at=submitted_at,
        )

    def load_recent_envelopes(self, limit: int) -> list[StoredEnvelope]:
        """Most-recently-submitted envelopes first (M6 read model input).

        Ordered by ``submitted_at`` DESC (legacy NULLs sort last), rowid DESC as
        the stable tiebreaker. ``limit`` bounds the operational view (50 = P4
        scale; pagination is post-MVP)."""
        ids = [
            row[0]
            for row in self._conn.execute(
                "SELECT envelope_id FROM envelopes"
                " ORDER BY submitted_at DESC, rowid DESC LIMIT ?",
                (limit,),
            ).fetchall()
        ]
        return [envelope for eid in ids if (envelope := self.load_envelope(eid)) is not None]

    # -- operational read model (M6 §3.2 — read-only, domain columns absent) ---

    def load_operational_jobs(self) -> list[OperationalJobRow]:
        """Project every job to its OPERATIONAL columns only (DoD-P4-13).

        The SELECT list deliberately omits ``job_spec`` and ``oracle_plugin_dir``
        (the domain-carrying columns) — the operational view is a structural
        subset of the store, never a filtered domain view. Read-only."""
        rows = self._conn.execute(
            "SELECT request_id, repeat_index, state, attempt_count, started_at, ended_at,"
            " runner_exit_code, infra_error FROM jobs ORDER BY request_id, repeat_index"
        ).fetchall()
        return [
            OperationalJobRow(
                request_id=request_id,
                repeat_index=repeat_index,
                state=state,
                attempt_count=attempt_count,
                started_at=started_at,
                ended_at=ended_at,
                runner_exit_code=runner_exit_code,
                infra_error=infra_error,
            )
            for (
                request_id,
                repeat_index,
                state,
                attempt_count,
                started_at,
                ended_at,
                runner_exit_code,
                infra_error,
            ) in rows
        ]

    def job_state_counts(self) -> dict[str, int]:
        """Count jobs by state (queue depth / running_k source, M3 §3.4)."""
        return {
            state: count
            for state, count in self._conn.execute(
                "SELECT state, COUNT(*) FROM jobs GROUP BY state"
            ).fetchall()
        }

    # -- resource/health sample (M6 §3.4 — one latest row the sampler upserts) --

    def record_resource_sample(self, sample: ResourceSample) -> None:
        """Upsert the SINGLE latest resource/health snapshot (id=1)."""
        with self._write_lock, self._conn:
            self._conn.execute(
                "INSERT INTO resource_health_sample (id, sampled_at, gpu_reachable,"
                " vram_used_mib, vram_total_mib, gpu_util_pct, queue_depth, running_k,"
                " over_launch_count) VALUES (1, ?, ?, ?, ?, ?, ?, ?, ?)"
                " ON CONFLICT(id) DO UPDATE SET sampled_at = excluded.sampled_at,"
                " gpu_reachable = excluded.gpu_reachable, vram_used_mib = excluded.vram_used_mib,"
                " vram_total_mib = excluded.vram_total_mib, gpu_util_pct = excluded.gpu_util_pct,"
                " queue_depth = excluded.queue_depth, running_k = excluded.running_k,"
                " over_launch_count = excluded.over_launch_count",
                (
                    sample.sampled_at,
                    int(sample.gpu_reachable),
                    sample.vram_used_mib,
                    sample.vram_total_mib,
                    sample.gpu_util_pct,
                    sample.queue_depth,
                    sample.running_k,
                    sample.over_launch_count,
                ),
            )

    def load_resource_sample(self) -> ResourceSample | None:
        """Restore the latest resource/health snapshot (None when never sampled)."""
        row = self._conn.execute(
            "SELECT sampled_at, gpu_reachable, vram_used_mib, vram_total_mib, gpu_util_pct,"
            " queue_depth, running_k, over_launch_count FROM resource_health_sample"
            " WHERE id = 1"
        ).fetchone()
        if row is None:
            return None
        (
            sampled_at,
            gpu_reachable,
            vram_used_mib,
            vram_total_mib,
            gpu_util_pct,
            queue_depth,
            running_k,
            over_launch_count,
        ) = row
        return ResourceSample(
            sampled_at=sampled_at,
            gpu_reachable=bool(gpu_reachable),
            vram_used_mib=vram_used_mib,
            vram_total_mib=vram_total_mib,
            gpu_util_pct=gpu_util_pct,
            queue_depth=queue_depth,
            running_k=running_k,
            over_launch_count=over_launch_count,
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

    # -- request-level regression baselines (SR-21 / C-1 — p5c1, M4 write path) --

    def upsert_baseline(self, row: BaselineRow) -> None:
        """Persist one request's regression baseline (insert or update; v6).

        The SINGLE writer of the C-1 baseline home (LOCKED §7.13) — M4
        ``report/baseline.py`` routes every establish/advance through here so the
        write stays serialized (module docstring, R-DB). No CI/git/network path
        touches this row; the ONLY source is a prior cv-infra run's result."""
        with self._write_lock, self._conn:
            self._conn.execute(
                "INSERT INTO request_baselines (request_identity_key, sut_ref, verdict,"
                " key_metrics, established_at, source_result_ref) VALUES (?, ?, ?, ?, ?, ?)"
                " ON CONFLICT(request_identity_key) DO UPDATE SET sut_ref = excluded.sut_ref,"
                " verdict = excluded.verdict, key_metrics = excluded.key_metrics,"
                " established_at = excluded.established_at,"
                " source_result_ref = excluded.source_result_ref",
                (
                    row.request_identity_key,
                    row.sut_ref,
                    row.verdict,
                    (
                        json.dumps(row.key_metrics, sort_keys=True)
                        if row.key_metrics is not None
                        else None
                    ),
                    row.established_at,
                    row.source_result_ref,
                ),
            )

    def load_baseline(self, request_identity_key: str) -> BaselineRow | None:
        """Restore one request's baseline (None when never established — skip 신호)."""
        row = self._conn.execute(
            "SELECT sut_ref, verdict, key_metrics, established_at, source_result_ref"
            " FROM request_baselines WHERE request_identity_key = ?",
            (request_identity_key,),
        ).fetchone()
        if row is None:
            return None
        sut_ref, verdict, key_metrics, established_at, source_result_ref = row
        return BaselineRow(
            request_identity_key=request_identity_key,
            sut_ref=sut_ref,
            verdict=verdict,
            key_metrics=json.loads(key_metrics) if key_metrics is not None else {},
            established_at=established_at,
            source_result_ref=source_result_ref,
        )

    # -- envelope report (v7 — server-side-assembled VerificationReport 영속) -----

    def save_report(self, envelope_id: str, report: dict[str, Any]) -> None:
        """Persist one envelope's assembled VerificationReport JSON (insert or update).

        Written by the api ``_persist_terminal`` seam at clean completion AFTER the
        report is assembled and BEFORE baselines advance (순서 불변식). Serialized
        canonically (sort_keys) through the single writer so the durable twin is
        served verbatim by ``GET /envelopes/{id}/report`` — restart-surviving, never
        re-assembled from results that did not survive."""
        with self._write_lock, self._conn:
            self._conn.execute(
                "INSERT INTO envelope_reports (envelope_id, report_json) VALUES (?, ?)"
                " ON CONFLICT(envelope_id) DO UPDATE SET report_json = excluded.report_json",
                (envelope_id, json.dumps(report, sort_keys=True)),
            )

    def load_report(self, envelope_id: str) -> dict[str, Any] | None:
        """Restore one envelope's persisted VerificationReport (None when never saved).

        ``None`` distinguishes "no report yet" (still running / crashed pre-report)
        from a served report — the route maps it to 404/409 accordingly."""
        row = self._conn.execute(
            "SELECT report_json FROM envelope_reports WHERE envelope_id = ?",
            (envelope_id,),
        ).fetchone()
        if row is None:
            return None
        return json.loads(row[0])

    # -- lifecycle -------------------------------------------------------------

    def close(self) -> None:
        self._conn.close()

    def __enter__(self) -> Store:
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.close()
