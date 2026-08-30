"""VerificationReport assembly (M4 §3.3 A + §3.4, SR-19) — REQ-REPORT-001/007.

Assembles the report JSON (M4<->M8 합의 스키마, §3.4) from three inputs per
request: the M3 ``RequestRollup`` (verdict/flakiness), the M1 Verification Request
wire dump (identity/sut/scenario), and the per-repeat Result wire dumps
(metrics/artifacts). It reuses ``matrix.build_matrix`` for the report-level
pass/fail matrix — so the LOCKED §7.12 재계산-금지 idiom (rollup verdict/flakiness
consumed VERBATIM, never recomputed from ``verdicts``) lives in ONE place and is
consumed here, and layers on:

* ``request_identity_key`` + regression judgement per row (via ``regression`` +
  ``baseline`` — baseline read from the internal store only, C-1);
* ``report_outcome`` (pass|fail|errored) — the exit-driving key M8 owns the
  mapping for (LOCKED §7.9); ``errored>0`` -> ``errored`` (exit-3 priority, §3.3 D);
* artifact selection per the 2026-07-16 decisions (all failure jobs + one
  deterministic representative pass; per-job size-cap exclusion + warning; policy
  only — actual file upload/sizing is M8's plane).

Stdlib only (no pydantic): the report is a plain dict M8 renders as ~수십 줄
markdown, and the core produces it standalone with no GitHub token (M4-09 이식성).
"""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

from cv_infra.contract.apiversion import API_VERSION
from cv_infra.orchestrator.models import RequestRollup
from cv_infra.orchestrator.store import Store
from cv_infra.report.baseline import find_baseline
from cv_infra.report.matrix import build_matrix
from cv_infra.report.regression import (
    STATUS_IMPROVED,
    STATUS_NO_BASELINE,
    STATUS_REGRESSED,
    STATUS_UNCHANGED,
    identity_key,
    judge_regression,
)

#: Artifact selection policy provenance (decisions/2026-07-16-p5-artifact-return.md).
_ARTIFACT_POLICY = (
    "failures-all + representative-pass-1 (결정 #1); per-job MCAP 상한 초과 시 제외+경고,"
    " 부분 bag 트렁케이션 금지 (결정 #2, 상한 = 32 MiB provisional); retention = GitHub Actions"
    " 기본값 재사용 (결정 #3). 실제 업로드/용량 측정은 M8 plane."
)


@dataclass
class RequestReportInput:
    """One request's inputs to the report (aligns the three producers per request).

    * ``request`` — the M1 Verification Request wire dump (``model_dump(mode="json",
      by_alias=True)``): source of ``request_identity_key``, ``sut_ref``, scenario.
    * ``rollup`` — the M3 ``RequestRollup`` (SR-10): verdict/flakiness consumed
      VERBATIM (LOCKED §7.12), matched to this request by ``request_id``.
    * ``results`` — the per-repeat M1 Result wire dumps IN REPEAT ORDER (index 0 =
      repeat 0): source of metrics + artifacts + per-job artifact selection. An
      optional ``result_json`` key (path to the result.json file) and ``mcap_bytes``
      hint (for the size-cap policy) may ride each result dump — both supplied by
      the persistence/M8 plane, absent here by default.
    """

    request: dict[str, Any]
    rollup: RequestRollup
    results: list[dict[str, Any]] = field(default_factory=list)


def build_report(
    inputs: list[RequestReportInput],
    store: Store,
    *,
    envelope_id: str,
    trigger_source: str,
    generated_at: str | None = None,
    max_mcap_bytes: int | None = None,
) -> dict[str, Any]:
    """Assemble the VerificationReport dict (§3.4) for one envelope.

    ``store`` is the internal cv-infra store — the ONLY baseline source (C-1).
    ``max_mcap_bytes`` is the per-job MCAP cap (결정 #2, 32 MiB provisional in api.py);
    ``None`` = no cap -> no exclusions. This function is READ-ONLY w.r.t. baselines;
    advancing them for future runs is a separate ``baseline.update_baseline`` call.
    """
    generated_at = generated_at or datetime.now(UTC).isoformat()
    # LOCKED §7.12: the report-level matrix (verdict/flakiness/summary counts) is
    # built by the ONE idiom in matrix.build_matrix, which consumes rollup values
    # verbatim. build_report never re-derives a verdict — it only enriches.
    core = build_matrix([inp.rollup for inp in inputs])
    core_by_id = {row["request_id"]: row for row in core["matrix"]}
    # Iterate in the same request_id sort build_matrix used, so rows align 1:1.
    rows = [
        _report_row(inp, core_by_id[inp.rollup.request_id], store, max_mcap_bytes)
        for inp in sorted(inputs, key=lambda i: i.rollup.request_id)
    ]
    return {
        "apiVersion": API_VERSION,
        "kind": "VerificationReport",
        "envelope_id": envelope_id,
        "trigger_source": trigger_source,
        "generated_at": generated_at,
        "summary": _summary(core["summary"]),
        "matrix": rows,
        "baseline_summary": _baseline_summary(rows),
    }


def _report_row(
    inp: RequestReportInput,
    core_row: dict[str, Any],
    store: Store,
    max_mcap_bytes: int | None,
) -> dict[str, Any]:
    """One §3.4 ``matrix`` row: the core row ENRICHED, never re-judged.

    ``core_row`` carries the rollup's verdict/flakiness verbatim (LOCKED §7.12 —
    nothing here re-derives them); this adds the identity key, the C-1 baseline
    judgement (internal store only), the representative metrics and the artifact
    selection."""
    rollup = inp.rollup
    request_id = rollup.request_id
    current_verdict = core_row["verdict"]  # rollup verdict verbatim (may be None)
    ikey = identity_key(inp.request)
    reg = judge_regression(request_id, current_verdict, find_baseline(store, ikey))
    return {
        "request_id": request_id,
        "request_identity_key": ikey,
        "sut_ref": _sut_ref(inp.request),
        "scenario": _scenario_label(inp.request),
        "rollup": {
            "repeats": len(rollup.verdicts),
            "verdicts": [v.value for v in rollup.verdicts],
            "flaky": bool(rollup.flakiness),
            "verdict": current_verdict,
        },
        # p6 §0-14: the request's DECLARED judgement policy (None = the frozen
        # any-fail rule). A ROW-level sibling of ``rollup`` — never inside it,
        # because ``rollup`` mirrors M3's frozen ``RequestRollup`` shape and the
        # ratio is not a field of it (it is an input the caller applied).
        "min_pass_ratio": _declared_min_pass_ratio(inp.request),
        "flakiness": core_row["flakiness"],
        "metrics": _metrics(inp.results, current_verdict),
        "regression": {
            "status": reg.status,
            "baseline_sut_ref": reg.baseline_sut_ref,
            "baseline_established_at": reg.baseline_established_at,
            "baseline_verdict": reg.baseline_verdict,
            "detail": reg.detail,
        },
        "artifacts": _select_artifacts(inp.results, max_mcap_bytes),
    }


def _summary(core_summary: dict[str, Any]) -> dict[str, Any]:
    """§3.4 ``summary`` = the core counts (total/passed/failed/errored) + two keys.

    ``verdict`` = pure domain pass/fail (any domain failure -> fail), computed
    INDEPENDENTLY of errored (§3.3 D "verdict와 별개로"). ``report_outcome`` is that
    verdict with the errored tri-state layered ON TOP (errored wins — exit-3
    priority) and is what M8 keys exit off (LOCKED §7.9); the failure threshold is
    written ONCE so the two keys can never disagree about what "failed" means."""
    summary = dict(core_summary)
    verdict = "fail" if summary["failed"] > 0 else "pass"
    summary["verdict"] = verdict
    summary["report_outcome"] = "errored" if summary["errored"] > 0 else verdict
    return summary


def _baseline_summary(rows: list[dict[str, Any]]) -> dict[str, Any]:
    """§3.4 ``baseline_summary``, DERIVED from the rows that carry the judgements.

    Counting off ``regression.status`` (the same field ``github._rows_with_status``
    enumerates by) makes "회귀 N건" and the N lines under it structurally the same
    set — a tally kept alongside the loop could drift from the rows it describes.
    ``matched`` = a baseline was actually compared; ``absent`` = skipped (no
    baseline OR errored current), so matched+absent == total."""
    statuses = Counter(row["regression"]["status"] for row in rows)
    absent = statuses[STATUS_NO_BASELINE]
    regressed = statuses[STATUS_REGRESSED]
    return {
        "matched": len(rows) - absent,
        "absent": absent,
        "regressed": regressed,
        "improved": statuses[STATUS_IMPROVED],
        "unchanged": statuses[STATUS_UNCHANGED],
        "note": (
            f"baseline 미비교 {absent}건은 정상(skip: baseline 부재 또는 errored 요청);"
            f" 회귀 {regressed}건 (NFR-REPORT-002)"
        ),
    }


# --------------------------------------------------------------------------- #
# Row helpers (pure)
# --------------------------------------------------------------------------- #
def _sut_ref(request: dict[str, Any]) -> str | None:
    """Render the SUT ref for the row: ``image_ref`` or ``image_ref@image_id``."""
    sut = request.get("sut") or {}
    image_ref = sut.get("image_ref")
    image_id = sut.get("image_id")
    if image_ref and image_id:
        return f"{image_ref}@{image_id}"
    return image_ref


def _declared_min_pass_ratio(request: dict[str, Any]) -> float | None:
    """The request's declared ``execution_settings.min_pass_ratio`` (p6 §0-14), or None.

    Read off the SAME captured M1 wire dump the caller read it from when it rolled
    up (``api._min_pass_ratio`` -> ``roll_up(min_pass_ratio=...)``), so the row can
    only ever say what was actually applied to the verdict it displays. The
    normalization rule is DUPLICATED from ``api._min_pass_ratio`` rather than
    imported (importing ``orchestrator.api`` here would be circular — api imports
    this module — and would drag fastapi into the renderer's graph, M4-09); the
    duplicate is held to its source by
    ``tests/test_report_distribution_surface.py::test_row_ratio_agrees_with_the_rollup_caller``
    (G-25 복제본 + repo-내부 기계적 가드).
    """
    settings = request.get("execution_settings")
    if not isinstance(settings, dict):
        return None
    ratio = settings.get("min_pass_ratio")
    if isinstance(ratio, bool) or not isinstance(ratio, (int, float)):
        return None
    return float(ratio)


def _scenario_label(request: dict[str, Any]) -> str | None:
    """Scenario label = ``scenario.scene`` (there is no separate name field, M1 §3.2)."""
    scenario = request.get("scenario") or {}
    return scenario.get("scene")


def _first_repeat(results: list[dict[str, Any]], *, passing: bool) -> int | None:
    """Lowest repeat index whose result did (``passing``) / did not (``not passing``)
    pass, or ``None`` if there is none.

    The ONE "first matching repeat" rule, so the two places that need a
    deterministic representative — the metrics row and the artifact
    representative-pass (결정 #1) — mean the same thing by "first"."""
    for index, result in enumerate(results):
        if (result.get("verdict") == "pass") == passing:
            return index
    return None


def _representative_index(results: list[dict[str, Any]], verdict: str | None) -> int | None:
    """Deterministic representative result index for metrics: first result matching
    the rollup verdict (pass-request -> first pass, fail/errored -> first non-pass),
    falling back to index 0. ``None`` when there are no results."""
    if not results:
        return None
    match = _first_repeat(results, passing=verdict == "pass")
    return match if match is not None else 0


def _metrics(results: list[dict[str, Any]], verdict: str | None) -> dict[str, Any]:
    """The representative result's declared metrics map ({} when no results)."""
    index = _representative_index(results, verdict)
    if index is None:
        return {}
    return dict(results[index].get("metrics") or {})


def _select_artifacts(results: list[dict[str, Any]], max_mcap_bytes: int | None) -> dict[str, Any]:
    """Per-job artifact selection (결정 #1/#2). Returns ``{policy, selected}``.

    Selected = every failure-class job (verdict != pass) + the ONE representative
    pass (lowest repeat index, deterministic). Non-representative passes are
    dropped (용량 절제). Each selected entry reserves ``excluded``/``warnings`` for
    the size-cap policy (결정 #2); actual sizing/upload is M8's."""
    rep_pass_index = _first_repeat(results, passing=True)
    selected: list[dict[str, Any]] = []
    for index, result in enumerate(results):
        if result.get("verdict") != "pass":
            role = "failure"
        elif index == rep_pass_index:
            role = "representative-pass"
        else:
            continue  # non-representative pass — not uploaded (결정 #1 중복 가치 낮음)
        artifacts = result.get("artifacts") or {}
        entry = {
            "repeat_index": index,
            "role": role,
            "verdict": result.get("verdict"),
            "result_json": result.get("result_json"),
            "rosbag_mcap": artifacts.get("mcap"),
            "recording_mp4": artifacts.get("mp4"),
            "excluded": [],
            "warnings": [],
        }
        _apply_mcap_cap(entry, result.get("mcap_bytes"), max_mcap_bytes)
        selected.append(entry)
    return {"policy": _ARTIFACT_POLICY, "selected": selected}


def _apply_mcap_cap(entry: dict[str, Any], size_bytes: int | None, cap_bytes: int | None) -> None:
    """결정 #2: over-cap MCAP -> exclude from upload + warn (no truncation).

    No-op when the cap is unset (caller passed ``None``) or the size is unknown (M8
    measures on its plane) — this file expresses the POLICY and receives the cap as a
    param (32 MiB provisional wired in api.py), never hardcoding 상한 수치 here."""
    if cap_bytes is None or size_bytes is None:
        return
    if entry["rosbag_mcap"] is not None and size_bytes > cap_bytes:
        entry["warnings"].append(
            f"MCAP {size_bytes}B가 잡별 상한 {cap_bytes}B 초과 — 업로드 제외"
            " (부분 bag 트렁케이션 금지: 명시적 부재+경고, 결정 #2)"
        )
        entry["excluded"].append("rosbag_mcap")
        entry["rosbag_mcap"] = None
