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

from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

from cv_infra.contract.apiversion import API_VERSION
from cv_infra.orchestrator.models import RequestRollup
from cv_infra.orchestrator.store import Store
from cv_infra.report.baseline import find_baseline
from cv_infra.report.matrix import build_matrix
from cv_infra.report.regression import STATUS_REGRESSED, identity_key, judge_regression

#: Artifact selection policy provenance (decisions/2026-07-16-p5-artifact-return.md).
_ARTIFACT_POLICY = (
    "failures-all + representative-pass-1 (결정 #1); per-job MCAP 상한 초과 시 제외+경고,"
    " 부분 bag 트렁케이션 금지 (결정 #2, 상한 수치 TBD 실측 후); retention = GitHub Actions"
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
    ``max_mcap_bytes`` is the per-job MCAP cap (결정 #2); ``None`` = cap not set
    (TBD 실측 후) -> no exclusions. This function is READ-ONLY w.r.t. baselines;
    advancing them for future runs is a separate ``baseline.update_baseline`` call.
    """
    generated_at = generated_at or datetime.now(UTC).isoformat()
    # LOCKED §7.12: the report-level matrix (verdict/flakiness/summary counts) is
    # built by the ONE idiom in matrix.build_matrix, which consumes rollup values
    # verbatim. build_report never re-derives a verdict — it only enriches.
    core = build_matrix([inp.rollup for inp in inputs])
    core_by_id = {row["request_id"]: row for row in core["matrix"]}

    matched = absent = regressed = improved = unchanged = 0
    rows: list[dict[str, Any]] = []
    # Iterate in the same request_id sort build_matrix used, so rows align 1:1.
    for inp in sorted(inputs, key=lambda i: i.rollup.request_id):
        request_id = inp.rollup.request_id
        core_row = core_by_id[request_id]
        current_verdict = core_row["verdict"]  # rollup verdict verbatim (may be None)

        ikey = identity_key(inp.request)
        baseline = find_baseline(store, ikey)
        reg = judge_regression(request_id, current_verdict, baseline)

        status = reg.status
        if status == STATUS_REGRESSED:
            regressed += 1
        elif status == "improved":
            improved += 1
        elif status == "unchanged":
            unchanged += 1
        # matched = a baseline was actually compared; absent = skipped (no baseline
        # OR errored current) — keeps matched+absent == total and consistent w/ status.
        if status == "no-baseline":
            absent += 1
        else:
            matched += 1

        rollup = inp.rollup
        rows.append(
            {
                "request_id": request_id,
                "request_identity_key": ikey,
                "sut_ref": _sut_ref(inp.request),
                "scenario": _scenario_label(inp.request),
                "rollup": {
                    "repeats": len(rollup.verdicts),
                    "verdicts": [v.value for v in rollup.verdicts],
                    "flaky": bool(rollup.flakiness),
                    "verdict": core_row["verdict"],
                },
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
        )

    summary = dict(core["summary"])  # total, passed, failed, errored
    # verdict = pure domain pass/fail (any domain failure -> fail), computed
    # INDEPENDENTLY of errored (§3.3 D "verdict와 별개로"). report_outcome layers the
    # errored tri-state on top and is what M8 keys exit off (LOCKED §7.9).
    summary["verdict"] = "fail" if summary["failed"] > 0 else "pass"
    summary["report_outcome"] = (
        "errored" if summary["errored"] > 0 else "fail" if summary["failed"] > 0 else "pass"
    )

    return {
        "apiVersion": API_VERSION,
        "kind": "VerificationReport",
        "envelope_id": envelope_id,
        "trigger_source": trigger_source,
        "generated_at": generated_at,
        "summary": summary,
        "matrix": rows,
        "baseline_summary": {
            "matched": matched,
            "absent": absent,
            "regressed": regressed,
            "improved": improved,
            "unchanged": unchanged,
            "note": (
                f"baseline 미비교 {absent}건은 정상(skip: baseline 부재 또는 errored 요청);"
                f" 회귀 {regressed}건 (NFR-REPORT-002)"
            ),
        },
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


def _scenario_label(request: dict[str, Any]) -> str | None:
    """Scenario label = ``scenario.scene`` (there is no separate name field, M1 §3.2)."""
    scenario = request.get("scenario") or {}
    return scenario.get("scene")


def _representative_index(results: list[dict[str, Any]], verdict: str | None) -> int | None:
    """Deterministic representative result index for metrics: first result matching
    the rollup verdict (pass-request -> first pass, fail/errored -> first non-pass),
    falling back to index 0. ``None`` when there are no results."""
    if not results:
        return None
    if verdict == "pass":
        match = next((i for i, r in enumerate(results) if r.get("verdict") == "pass"), None)
    else:
        match = next((i for i, r in enumerate(results) if r.get("verdict") != "pass"), None)
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
    rep_pass_index = next((i for i, r in enumerate(results) if r.get("verdict") == "pass"), None)
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

    No-op when the cap is unset (TBD 실측 후) or the size is unknown (M8 measures on
    its plane) — this cycle expresses the POLICY, not a hardcoded 상한 수치."""
    if cap_bytes is None or size_bytes is None:
        return
    if entry["rosbag_mcap"] is not None and size_bytes > cap_bytes:
        entry["warnings"].append(
            f"MCAP {size_bytes}B가 잡별 상한 {cap_bytes}B 초과 — 업로드 제외"
            " (부분 bag 트렁케이션 금지: 명시적 부재+경고, 결정 #2)"
        )
        entry["excluded"].append("rosbag_mcap")
        entry["rosbag_mcap"] = None
