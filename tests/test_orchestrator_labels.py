"""run_job attaches crash-reconciliation labels to BOTH containers (M3 §3.9, R14).

The label keys are the allocator-owned constants (``cv-infra.job_id`` /
``cv-infra.ros_domain_id``); a restarted orchestrator scans them to re-attach
in-flight jobs and restore live domain ids instead of re-assigning. Reuses the
duck-typed docker fakes from test_supervisor_min (same M3-owned test surface).
"""

from __future__ import annotations

from cv_infra.orchestrator.allocator import (
    LABEL_JOB_ID,
    LABEL_ROS_DOMAIN_ID,
    allocate_ros_domain_id,
)
from tests.test_supervisor_min import JOB_ID, FakeClient, put_result, run_min


def test_run_job_labels_runner_and_sut_with_job_and_domain_ids(tmp_path):
    put_result(tmp_path)
    client = FakeClient()
    run_min(tmp_path, client)
    (_, runner_kwargs), (_, sut_kwargs) = client.run_calls
    expected = {
        LABEL_JOB_ID: JOB_ID,
        LABEL_ROS_DOMAIN_ID: str(allocate_ros_domain_id(JOB_ID)),
    }
    assert runner_kwargs["labels"] == expected
    assert sut_kwargs["labels"] == expected  # both containers belong to the job
    # the labelled domain id is the very one injected into the containers' env
    assert runner_kwargs["environment"]["ROS_DOMAIN_ID"] == expected[LABEL_ROS_DOMAIN_ID]
    # the per-job network carries the same labels (p4c4) so the restart sweep
    # (reconcile_at_restart) can find and remove it alongside the containers
    assert client.network_labels == expected
