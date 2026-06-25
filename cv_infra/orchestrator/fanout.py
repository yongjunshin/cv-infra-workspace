"""2-axis fan-out (M3 §3.2) — Request Envelope → Verification Job[].

The fan-out is the product of two axes (REQ-ORCH-001/002):

* axis 1 — envelope length: one Verification Request per entry (REQ-ORCH-001),
  represented here by its ``request_id``;
* axis 2 — ``execution_settings.repeats``: ``repeats`` Jobs per request, indexed
  ``repeat_index`` 0..repeats-1 (REQ-ORCH-002).

So total jobs = ``len(request_ids) * repeats`` and each Job is uniquely identified
by ``(request_id, repeat_index)``. Stdlib only — no third-party runtime dependency.

``repeats`` comes from the M1 contract ``execution_settings`` in Phase 3
(modules/M1-contract-and-schema.md); Phase 1 takes it as a plain int so the
fan-out rule can be unit-tested on CPU without the contract models.
"""

from __future__ import annotations

from cv_infra.orchestrator.models import Job


def fan_out(request_ids: list[str], repeats: int) -> list[Job]:
    """Expand requests across the repeats axis into uniquely-keyed Jobs.

    Args:
        request_ids: one id per Verification Request in the envelope (axis 1).
        repeats: ``execution_settings.repeats`` — Jobs per request (axis 2), >= 1.

    Returns:
        ``len(request_ids) * repeats`` Jobs, each QUEUED with ``attempt_count`` 0
        and a unique ``(request_id, repeat_index)`` key.

    Raises:
        ValueError: if ``repeats`` < 1.
    """
    if repeats < 1:
        raise ValueError(f"repeats must be >= 1, got {repeats}")
    return [
        Job(request_id=request_id, repeat_index=repeat_index)
        for request_id in request_ids
        for repeat_index in range(repeats)
    ]
