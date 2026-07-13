"""2-axis fan-out (M3 §3.2) — Request Envelope → Verification Job[].

The fan-out is the product of two axes (REQ-ORCH-001/002):

* axis 1 — envelope length: one Verification Request per entry (REQ-ORCH-001),
  represented here by its ``request_id``;
* axis 2 — ``execution_settings.repeats``: ``repeats`` Jobs per request, indexed
  ``repeat_index`` 0..repeats-1 (REQ-ORCH-002).

Repeats is a PER-REQUEST axis (each request carries its own
``execution_settings.repeats``), so total jobs = ``Σ_i repeats(i)`` (M3 §3.2
formula) and each Job is uniquely identified by ``(request_id, repeat_index)``.
``fan_out_requests`` is the general form the submit surface (api.py) drives;
``fan_out`` is the frozen P1 uniform-repeats special case. Stdlib only — no
third-party runtime dependency (``repeats`` values come from the M1 contract
``execution_settings``; this module takes plain ints so the fan-out rule stays
CPU-unit-testable without the contract models).
"""

from __future__ import annotations

from collections.abc import Sequence

from cv_infra.orchestrator.models import Job


def fan_out_requests(request_repeats: Sequence[tuple[str, int]]) -> list[Job]:
    """Expand ``(request_id, repeats)`` pairs into uniquely-keyed Jobs (general form).

    Args:
        request_repeats: one ``(request_id, execution_settings.repeats)`` pair
            per Verification Request in the envelope (axis 1 × per-request axis 2).

    Returns:
        ``Σ_i repeats(i)`` Jobs, each QUEUED with ``attempt_count`` 0 and a
        unique ``(request_id, repeat_index)`` key (``repeat_index`` 0..repeats-1).

    Raises:
        ValueError: if any ``repeats`` < 1, or a ``request_id`` occurs twice —
            duplicate ids would collide on the (request_id, repeat_index) store
            key and silently merge state (loud beats a silent upsert-merge).
    """
    seen: set[str] = set()
    jobs: list[Job] = []
    for request_id, repeats in request_repeats:
        if repeats < 1:
            raise ValueError(f"repeats must be >= 1, got {repeats} for request {request_id!r}")
        if request_id in seen:
            raise ValueError(f"duplicate request_id {request_id!r} in one fan-out")
        seen.add(request_id)
        jobs.extend(
            Job(request_id=request_id, repeat_index=repeat_index) for repeat_index in range(repeats)
        )
    return jobs


def fan_out(request_ids: list[str], repeats: int) -> list[Job]:
    """Uniform-repeats special case (frozen P1 surface) of ``fan_out_requests``.

    Args:
        request_ids: one id per Verification Request in the envelope (axis 1).
        repeats: ``execution_settings.repeats`` shared by every request (axis 2), >= 1.

    Returns:
        ``len(request_ids) * repeats`` Jobs, each QUEUED with ``attempt_count`` 0
        and a unique ``(request_id, repeat_index)`` key.

    Raises:
        ValueError: if ``repeats`` < 1 (or ``request_ids`` carries a duplicate).
    """
    if repeats < 1:
        raise ValueError(f"repeats must be >= 1, got {repeats}")
    return fan_out_requests([(request_id, repeats) for request_id in request_ids])
