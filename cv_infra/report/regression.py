"""Request-level regression judgement (M4 §3.3 B, SR-20) — REQ-REPORT-002/003/004.

Two thin pieces on top of M3's rollup + M4's baseline:

* ``identity_key`` (REQ-REPORT-002): the ``request_identity_key`` — a normalized
  sha256 over a Verification Request that DELIBERATELY drops the SUT axis, so two
  runs that differ ONLY in SUT map to the SAME key ("same request, only SUT
  differs"). M1 owns the key's *model field* (``Result.request_identity_key``);
  M4 owns the key *derivation* (LOCKED §7.13). The normalization SCOPE (which
  fields identify a request) was deferred at requirements time — pinned here, see
  ``_IDENTITY_EXCLUDE`` and the report's surfaced-assumption section.

* ``judge_regression`` (REQ-REPORT-003/004): compares the current rolled-up
  verdict against the matched baseline. ``pass->fail`` = regression (the SUT got
  worse across versions); baseline absent = skip, treated as NORMAL, never a
  failure (NFR-REPORT-002). Identity of the regression (which request vs which
  SUT version) is surfaced in the result fields (NFR-REPORT-001).

Stdlib only (hashlib + json) — no pydantic, no I/O, no network: this module is
pure and portable (M4-09 이식성). The baseline it compares against is fetched by
``report.baseline`` from the internal store (C-1); this module never reaches for
one itself.
"""

from __future__ import annotations

import copy
import hashlib
import json
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

# --- request_identity_key normalization scope (M4-owned, surfaced assumption) --
#
# The key = sha256 of the request's *identity projection* = the FULL Verification
# Request wire dump MINUS these keys. A deny-list (not an allow-list) is chosen so
# the key AUTO-TRACKS contract growth: any field M1 adds to a request flows into
# the key by default, and the only way two requests collide is if they are
# identical modulo the excluded axes. That fails SAFE toward "keys differ -> no
# baseline match -> regression skip" (NFR-REPORT-002), never toward a false match.
#
#   * ``sut`` / ``apiVersion`` / ``api_version`` — the SUT is THE variable axis
#     (the whole point, REQ-REPORT-002); ``apiVersion`` is schema-version metadata
#     (a re-run of identical content under a newer contract version is the SAME
#     request, not a new one).
#   * ``execution_settings.repeats`` — a pure fan-out orchestration knob (M3
#     consumes it, ``fanout.py``): it changes HOW MANY samples are taken for
#     flakiness, not WHAT is verified. The rolled-up verdict already collapses N
#     repeats to one verdict, so bumping repeats must NOT invalidate a baseline.
#
# Everything else IS in the key: scenario (scene/robot/goal/seed/timeout_s/
# debug_obstacle — determinism + world + mission), interface (sim<->SUT wiring),
# acceptance_criteria (the judgement, ORDER-SENSITIVE — order is not normalized;
# a reordered criteria list is conservatively a different request, skip-safe), and
# execution_settings.fixed_dt (determinism dt affects sim behavior).
_IDENTITY_EXCLUDE_TOP: frozenset[str] = frozenset({"sut", "apiVersion", "api_version"})
_IDENTITY_EXCLUDE_SETTINGS: frozenset[str] = frozenset({"repeats"})

# Regression statuses (report JSON §3.4 regression.status vocabulary).
STATUS_REGRESSED = "regressed"
STATUS_IMPROVED = "improved"
STATUS_UNCHANGED = "unchanged"
STATUS_NO_BASELINE = "no-baseline"


@dataclass(frozen=True)
class RegressionVerdict:
    """One request's regression judgement (fills report JSON ``matrix[].regression``).

    ``status`` is one of the four ``STATUS_*`` values. The ``baseline_*`` fields
    are populated only when a baseline was actually compared (status regressed/
    improved/unchanged) and carry the NFR-REPORT-001 식별성: which SUT ref, when
    established, what its verdict was. ``detail`` is the human message (§7.3).
    """

    status: str
    baseline_sut_ref: str | None = None
    baseline_established_at: str | None = None
    baseline_verdict: str | None = None
    detail: str | None = None


def _identity_projection(request: Mapping[str, Any]) -> dict[str, Any]:
    """The identity-bearing subset of a request wire dump (see ``_IDENTITY_EXCLUDE``)."""
    projection = {
        key: copy.deepcopy(value)
        for key, value in request.items()
        if key not in _IDENTITY_EXCLUDE_TOP
    }
    settings = projection.get("execution_settings")
    if isinstance(settings, Mapping):
        projection["execution_settings"] = {
            key: value for key, value in settings.items() if key not in _IDENTITY_EXCLUDE_SETTINGS
        }
    return projection


def identity_key(request: Mapping[str, Any]) -> str:
    """Derive the ``request_identity_key`` (REQ-REPORT-002) for one Verification Request.

    ``request`` is the request's wire dump (``VerificationRequest.model_dump(
    mode="json", by_alias=True)`` — M4 consumes the M1-validated shape as a plain
    mapping so this module stays pydantic-free/portable). Same scenario+criteria+
    settings modulo SUT/apiVersion/repeats -> byte-identical canonical JSON ->
    identical ``sha256:`` key.
    """
    canonical = json.dumps(
        _identity_projection(request),
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    )
    return "sha256:" + hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def judge_regression(
    request_id: str,
    current_verdict: str | None,
    baseline: Any | None,
) -> RegressionVerdict:
    """Judge one request against its matched baseline (REQ-REPORT-003/004).

    ``current_verdict`` is the request's rolled-up verdict value (``"pass"`` /
    ``"fail"`` from M3's ``RequestRollup.verdict``, or ``None`` for an errored
    request with no domain verdict). ``baseline`` is the matched ``BaselineRow``
    (``store.load_baseline``) or ``None`` when absent.

    * current errored (``None``) -> ``no-baseline`` (regression skipped: there is
      no domain verdict to compare — the batch surfaces this via
      ``report_outcome=errored`` at exit-3, not as a per-request regression).
    * baseline absent -> ``no-baseline`` (skip = NORMAL, NFR-REPORT-002).
    * baseline pass, current fail -> ``regressed`` (REQ-REPORT-003).
    * baseline fail, current pass -> ``improved``.
    * same verdict -> ``unchanged``.
    """
    if current_verdict is None:
        return RegressionVerdict(
            status=STATUS_NO_BASELINE,
            detail=(
                f"회귀 판정 skip — 요청 {request_id}은(는) errored라 비교할 도메인 verdict가 없음"
                " (배치 report_outcome=errored로 표면)"
            ),
        )
    if baseline is None:
        return RegressionVerdict(
            status=STATUS_NO_BASELINE,
            detail=(
                f"baseline이 없어 회귀 판정을 skip했습니다 — 요청 {request_id}"
                " (최초 실행/신규 SUT의 정상 상태; 실패 아님)"
            ),
        )

    baseline_verdict = baseline.verdict
    common = {
        "baseline_sut_ref": baseline.sut_ref,
        "baseline_established_at": baseline.established_at,
        "baseline_verdict": baseline_verdict,
    }
    if baseline_verdict == "pass" and current_verdict == "fail":
        status, change = STATUS_REGRESSED, "pass→fail로 악화"
    elif baseline_verdict == "fail" and current_verdict == "pass":
        status, change = STATUS_IMPROVED, "fail→pass로 개선"
    else:
        status, change = (
            STATUS_UNCHANGED,
            f"{current_verdict} (baseline {baseline_verdict})로 변화 없음",
        )
    return RegressionVerdict(
        status=status,
        detail=(
            f"요청 {request_id}이(가) SUT {baseline.sut_ref}"
            f" (baseline 수립 {baseline.established_at}) 대비 {change}"
        ),
        **common,
    )
