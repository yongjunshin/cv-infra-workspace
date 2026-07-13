"""DomainIdAllocator + isolation-assignment tests (M3 §3.6) — REQ-ORCH-008,
NFR-ORCH-002, LOCKED §7.5.

CPU-only: SQLite-backed allocate/release (one live id per job, 회수 누락 0),
exhaustion of the 0..101 space is loud, allocations survive a restart (a live
id is never re-assigned), and the crash-reconciliation label keys are pinned.
"""

from __future__ import annotations

import pytest

from cv_infra.orchestrator import supervisor
from cv_infra.orchestrator.allocator import (
    LABEL_JOB_ID,
    LABEL_ROS_DOMAIN_ID,
    ROS_DOMAIN_ID_SPACE,
    DomainIdAllocator,
    allocate_ros_domain_id,
    network_name_for,
)
from cv_infra.orchestrator.store import Store


@pytest.fixture
def store(tmp_path):
    with Store(tmp_path / "cv.sqlite3") as s:
        yield s


def _job_id_preferring(target_id: int, avoid: str) -> str:
    """Find a DIFFERENT job id whose deterministic preference is ``target_id``.

    102 buckets -> expected ~100 tries; the search is deterministic (sha256).
    """
    for i in range(100_000):
        candidate = f"probe-{i}"
        if candidate != avoid and allocate_ros_domain_id(candidate) == target_id:
            return candidate
    raise AssertionError("no colliding job id found (hash space anomaly)")


def test_label_key_constants_verbatim():
    # R14 reconciliation scans these exact keys on live containers — frozen.
    assert LABEL_JOB_ID == "cv-infra.job_id"
    assert LABEL_ROS_DOMAIN_ID == "cv-infra.ros_domain_id"


def test_helpers_moved_home_and_reexported_from_supervisor():
    # Behavior-preserving move (M3 §3.6 home): the frozen supervisor import
    # path must resolve to the SAME objects.
    assert supervisor.allocate_ros_domain_id is allocate_ros_domain_id
    assert supervisor.network_name_for is network_name_for
    assert supervisor.ROS_DOMAIN_ID_SPACE == ROS_DOMAIN_ID_SPACE


def test_allocate_yields_unique_live_ids_and_persists(store):
    allocator = DomainIdAllocator(store)
    ids = [allocator.allocate(f"job-{i}") for i in range(10)]
    assert len(set(ids)) == 10  # unique among live allocations
    assert all(0 <= d < ROS_DOMAIN_ID_SPACE for d in ids)
    assert store.domain_ids_in_use() == {d: f"job-{i}" for i, d in enumerate(ids)}


def test_release_frees_id_for_reuse_and_restores_determinism(store):
    allocator = DomainIdAllocator(store)
    first = allocator.allocate("job-a")
    allocator.release("job-a")
    assert allocator.in_use() == {}
    assert allocator.allocate("job-a") == first  # deterministic preference restored


def test_same_preferred_id_probes_past_live_allocation(store):
    allocator = DomainIdAllocator(store)
    first = allocator.allocate("job-a")
    collider = _job_id_preferring(first, avoid="job-a")
    second = allocator.allocate(collider)  # same hash preference, id is live
    assert second != first  # a live id is never handed out twice (LOCKED §7.5)


def test_one_live_id_per_job_is_enforced(store):
    allocator = DomainIdAllocator(store)
    allocator.allocate("job-a")
    with pytest.raises(Exception):  # sqlite3.IntegrityError — accounting bug is loud
        allocator.allocate("job-a")  # a job holding a live id cannot allocate again


def test_exhaustion_of_domain_space_raises_loud(store):
    allocator = DomainIdAllocator(store)
    for i in range(ROS_DOMAIN_ID_SPACE):
        allocator.allocate(f"job-{i}")
    with pytest.raises(ValueError):
        allocator.allocate("one-too-many")


def test_allocations_survive_restart_and_block_reassignment(tmp_path):
    db = tmp_path / "cv.sqlite3"
    with Store(db) as store:
        live = DomainIdAllocator(store).allocate("job-a")
        # "crash" with the allocation live (no release)
    with Store(db) as reopened:
        allocator = DomainIdAllocator(reopened)
        assert allocator.in_use() == {live: "job-a"}  # restored liveness
        collider = _job_id_preferring(live, avoid="job-a")
        assert allocator.allocate(collider) != live  # restored id is not re-assigned
