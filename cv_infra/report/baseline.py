"""Regression baseline lifecycle (M4 §3.3 C, SR-21) — REQ-REPORT-005/006, C-1.

A THIN policy layer over the store's ``request_baselines`` accessors. The store
(M3 ``store.py``) is the single writer and the ONLY home of a baseline (LOCKED
§7.13 C-1): this module reads via ``store.load_baseline`` and writes via
``store.upsert_baseline`` — it holds NO SQLite handle, opens NO socket, spawns NO
subprocess, and reads NO consumer CI/git history. A baseline can therefore ONLY
come from a prior run of the SAME cv-infra deployment (REQ-REPORT-006 부정 계약).

Best-effort establish/advance policy (REQ-REPORT-005, skip-when-absent):

* errored (verdict ``None``) -> never baselined (an infra outcome is not a
  reference); no-op.
* no baseline yet -> ESTABLISH with the current definite verdict (pass OR fail):
  a first-run fail can be a baseline so a later fix reads as ``improved``.
* baseline exists + current PASS -> ADVANCE (last-known-good moves to the newer
  SUT/timestamp).
* baseline exists + current FAIL -> KEEP the existing baseline (a fail never
  overwrites a good reference, so the regression stays visible on re-runs until
  fixed) — no-op.

Per-branch/approval/multi-baseline policies are post-MVP (module §9). Read is
separated from write on purpose: report assembly (``aggregate.build_report``) only
READS baselines for the regression judgement; advancing the baseline for future
runs is an EXPLICIT call the integration layer makes after a report is generated.
Stdlib only.
"""

from __future__ import annotations

from datetime import UTC, datetime

from cv_infra.orchestrator.models import Verdict
from cv_infra.orchestrator.store import BaselineRow, Store


def find_baseline(store: Store, request_identity_key: str) -> BaselineRow | None:
    """The matched baseline for an identity key, or ``None`` (absent = skip 신호).

    The single lookup path — internal store only (C-1). ``None`` is the normal
    first-run/new-SUT state, NOT an error (NFR-REPORT-002)."""
    return store.load_baseline(request_identity_key)


def update_baseline(
    store: Store,
    *,
    request_identity_key: str,
    sut_ref: str,
    verdict: str | None,
    key_metrics: dict | None = None,
    source_result_ref: str | None = None,
    established_at: str | None = None,
) -> bool:
    """Best-effort establish/advance of a baseline (module docstring policy).

    Returns ``True`` if a row was written, ``False`` on a policy no-op (errored,
    or a fail that must not overwrite an existing good baseline). Never raises for
    a policy skip — best-effort (REQ-REPORT-005). The write is serialized through
    the store's single writer (C-1)."""
    if verdict is None:
        return False  # errored — an infra outcome is never a reference
    existing = store.load_baseline(request_identity_key)
    if existing is not None and verdict != Verdict.PASS.value:
        return False  # keep last-known-good: a fail never overwrites a good baseline
    store.upsert_baseline(
        BaselineRow(
            request_identity_key=request_identity_key,
            sut_ref=sut_ref,
            verdict=verdict,
            key_metrics=key_metrics or {},
            established_at=established_at or datetime.now(UTC).isoformat(),
            source_result_ref=source_result_ref,
        )
    )
    return True
