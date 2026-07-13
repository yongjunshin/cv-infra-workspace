"""ROS dual-isolation allocator (M3 §3.6) — REQ-ORCH-008, NFR-ORCH-002, LOCKED §7.5.

Owns the per-job isolation *assignment* accounting:

* ``ROS_DOMAIN_ID`` in the safe range 0..101 (linux ephemeral-port collision
  avoidance), allocated per job and released on completion, persisted to SQLite
  via the Store so a restart sees live allocations (M3 §3.6 D-O — a live id is
  never re-assigned);
* the per-job docker bridge NETWORK NAME (``network_name_for``) — actual docker
  network creation/teardown stays with the supervisor (M5 substrate);
* the container label KEYS crash reconciliation (M3 §3.9, R14) uses to restore
  in-flight allocations from live containers.

``ROS_DOMAIN_ID_SPACE`` / ``allocate_ros_domain_id`` / ``network_name_for``
moved here verbatim from supervisor.py (behavior-preserving refactor; the
supervisor re-exports them so the frozen single-runner import path still
works). Stdlib only.
"""

from __future__ import annotations

import hashlib
import re

from cv_infra.orchestrator.store import Store

# Container label keys (M3 §3.9, R14): the supervisor attaches these to every
# spawned container so a restarted orchestrator can re-attach in-flight jobs
# and restore their live domain ids from `docker ps` instead of re-assigning.
LABEL_JOB_ID = "cv-infra.job_id"
LABEL_ROS_DOMAIN_ID = "cv-infra.ros_domain_id"

# LOCKED §7.5 dual isolation: per-job docker network + ROS_DOMAIN_ID in 0..101.
ROS_DOMAIN_ID_SPACE = 102


def allocate_ros_domain_id(job_id: str, in_use: frozenset[int] = frozenset()) -> int:
    """Deterministically allocate a ``ROS_DOMAIN_ID`` in 0..101 (LOCKED §7.5).

    Derivation is a stable hash of ``job_id`` (sha256, NOT Python's randomized
    ``hash()``) with linear probing over the domain space to skip ``in_use`` ids.
    Pure function — the SQLite-backed liveness feed is ``DomainIdAllocator``.
    """
    if len(in_use) >= ROS_DOMAIN_ID_SPACE:
        raise ValueError(f"all {ROS_DOMAIN_ID_SPACE} ROS domain ids are in use")
    digest = hashlib.sha256(job_id.encode("utf-8")).digest()
    start = int.from_bytes(digest[:4], "big") % ROS_DOMAIN_ID_SPACE
    for offset in range(ROS_DOMAIN_ID_SPACE):
        candidate = (start + offset) % ROS_DOMAIN_ID_SPACE
        if candidate not in in_use:
            return candidate
    raise AssertionError("unreachable: in_use guard above")  # pragma: no cover


def network_name_for(job_id: str) -> str:
    """Per-job docker bridge network name — deterministic, docker-safe, collision-free.

    ``job_id`` is slugged to docker's allowed charset and suffixed with a short stable
    hash of the FULL id, so distinct job_ids that slug identically still get distinct
    networks. Same-name leftovers are prevented by the finally-teardown, not the name.
    """
    slug = re.sub(r"[^a-zA-Z0-9_.-]+", "-", job_id).strip("-.")[:24] or "job"
    suffix = hashlib.sha256(job_id.encode("utf-8")).hexdigest()[:8]
    return f"cvj-{slug}-{suffix}"


class DomainIdAllocator:
    """SQLite-backed ``ROS_DOMAIN_ID`` allocate/release — one live id per job.

    Liveness is the Store's ``ros_domain_ids`` table (M3 §3.6 D-O), so
    allocations survive an orchestrator restart and a live id is never handed
    out twice. Deterministic preference + probing comes from
    ``allocate_ros_domain_id`` — the same job id gets the same id back when the
    space is free, which keeps parallel runs reproducible.

    Allocation unit = one ATTEMPT, not one job lifetime (p4c1 follow-up ⑤
    pinned, PM 룰링 cycle-plan 2026-07-13): the supervisor allocates at
    admission and releases when the attempt terminates, so a retried job
    releases its id and RE-allocates on the next attempt (deterministic
    preference usually hands the same id back, but that is a convenience, not
    a hold). Rationale: isolation only matters while containers are live —
    holding an id across the re-queue wait would shrink the 0..101 space for
    no isolation gain. Between attempts the job holds NO live id
    (``in_use()`` excludes it).
    """

    def __init__(self, store: Store) -> None:
        self._store = store

    def allocate(self, job_id: str) -> int:
        """Allocate a free domain id for ``job_id`` and persist the liveness row.

        Raises ValueError when all 102 ids are live — with single-digit k
        (LOCKED §7.4) exhaustion can only mean a reclaim leak, so it is loud.
        """
        domain_id = allocate_ros_domain_id(job_id, frozenset(self._store.domain_ids_in_use()))
        self._store.record_domain_id(domain_id, job_id)
        return domain_id

    def release(self, job_id: str) -> None:
        """Release ``job_id``'s live id (raises KeyError if it holds none —
        allocate/release must stay 1:1, 회수 누락 0)."""
        self._store.release_domain_id(job_id)

    def in_use(self) -> dict[int, str]:
        """Live allocations: ``{domain_id: job_id}``."""
        return self._store.domain_ids_in_use()
