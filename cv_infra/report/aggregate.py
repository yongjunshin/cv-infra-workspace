"""Report assembly — the runs become one case-major document, and that document
decides the exit code.

Two public functions and one rule about their order: ``build_report`` folds every run
into the schema-1 report and stamps ``summary.exit_code`` on it with ``exit_code_of``,
so the JSON, the process exit status and the CI Check conclusion are three views of ONE
decision. Nothing downstream re-derives a verdict from the runs (``report/github.py``
reads ``summary.exit_code``; the workflow reads the process status).

The folds, and why they are these folds:

* A check is a **pass ratio** over the case's judged runs, not a boolean: ``repeats``
  makes one case N samples, and 2/3 is neither pass nor fail — it is the number the
  baseline compares. ``n`` rides with every ratio so a reader can see how thin it is.
* A metric keeps **every value plus the mean**. The mean is what the baseline compares;
  the values are what makes a suspicious mean readable.
* ERRORED runs contribute NOTHING to ratios or means. An infrastructure fault is not
  the robot failing, and folding it in would fabricate a regression. It is still
  counted, still shown, and a case whose runs ALL errored is the case's own ``error``
  result — the exit fold turns a run of nothing-but-errors into exit 3.
* Only cases that actually RAN appear in the matrix. A budget-truncated tail is
  reported as counts and coverage (``summary.coverage``), never as empty rows, because
  a row with no runs reads like a case that produced nothing.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

from cv_infra.baselines import STATUS_SKIPPED, CaseObservation
from cv_infra.cli.exit_codes import (
    EXIT_CONTRACT,
    EXIT_FAIL,
    EXIT_INFRA,
    EXIT_PASS,
    REPORT_OUTCOME_BY_EXIT,
)
from cv_infra.contract.verdict import LANE_OK, CaseRunResult

#: Report JSON schema version. Bump only with the consumers of ``report.json``.
SCHEMA = 1

RESULT_PASS = "pass"
RESULT_FAIL = "fail"
RESULT_ERROR = "error"


@dataclass(frozen=True)
class RunRecord:
    """One case+repeat as the pipeline observed it: the execution facts plus the judged
    result. ``zip``/``log`` are RUN-DIR-RELATIVE strings, because the report travels to
    a reader (a PR, an artifact zip) where the host's absolute paths mean nothing."""

    repeat: int
    seed: int
    rc_sim: int | None
    rc_oracle: int | None
    wall_s: float
    result: CaseRunResult
    zip: str | None = None
    log: str | None = None
    zip_truncated: bool = False


@dataclass(frozen=True)
class CaseRecord:
    """One case: its identity, its axis assignment and the runs it actually got."""

    case_id: str
    axes: Mapping[str, str]
    repeats_planned: int
    runs: tuple[RunRecord, ...] = ()


@dataclass(frozen=True)
class PlanInfo:
    """What the covering array asked for versus what the budget allowed.

    ``coverage_achieved`` is the order-k coverage the EXECUTED prefix retains
    (``contract.pict.coverage_of_prefix``) — not ``cases_run / cases_planned``: cutting
    a third of the rows does not cost a third of the combinations, and reporting the
    row fraction as "coverage" would understate what the run actually proved.

    ``truncated_after_case`` is the index of the last case that RAN when the budget
    cut the tail, ``-1`` when the budget was already spent before the first case, and
    ``None`` when nothing was cut — "everything was cut" and "nothing was cut" are
    different facts, and a reader of a 0-case run needs to be told which one it was.
    """

    requested_k: int
    cases_planned: int
    cases_run: int
    coverage_achieved: float = 1.0
    truncated_after_case: int | None = None


@dataclass(frozen=True)
class CaseFold:
    """One case's numbers: checks as pass ratios, metrics as value lists, and the keys
    the oracle could not judge."""

    checks: dict[str, dict[str, Any]] = field(default_factory=dict)
    metrics: dict[str, dict[str, Any]] = field(default_factory=dict)
    nulls: list[str] = field(default_factory=list)
    judged: int = 0

    @property
    def result(self) -> str:
        if not self.judged:
            return RESULT_ERROR
        if any(entry["pass_ratio"] < 1.0 for entry in self.checks.values()):
            return RESULT_FAIL
        return RESULT_PASS


def fold_case(case: CaseRecord) -> CaseFold:
    """Fold one case's judged runs into ratios, value lists and null keys."""
    judged = [run.result for run in case.runs if run.result.lane == LANE_OK]
    checks: dict[str, dict[str, Any]] = {}
    metrics: dict[str, dict[str, Any]] = {}
    nulls: set[str] = set()
    for result in judged:
        for name, value in result.checks.items():
            entry = checks.setdefault(name, {"true": 0, "n": 0})
            entry["true"] += int(value)
            entry["n"] += 1
        for name, value in result.metrics.items():
            metrics.setdefault(name, {"values": []})["values"].append(value)
        nulls.update(result.nulls)
    return CaseFold(
        checks={
            name: {"pass_ratio": entry["true"] / entry["n"], "n": entry["n"]}
            for name, entry in sorted(checks.items())
        },
        metrics={
            name: {"values": entry["values"], "mean": sum(entry["values"]) / len(entry["values"])}
            for name, entry in sorted(metrics.items())
        },
        nulls=sorted(nulls),
        judged=len(judged),
    )


def observations(cases: Iterable[CaseRecord]) -> list[CaseObservation]:
    """The cases as baseline observations — the input to ``baselines.compare_best_effort``.

    ``repeats_run`` is the number of JUDGED runs, not the number attempted: it is what
    the ratios were computed from, and it is what ``single_sample`` labels.
    """
    out: list[CaseObservation] = []
    for case in cases:
        fold = fold_case(case)
        out.append(
            CaseObservation(
                case_key=case.case_id,
                checks={name: entry["pass_ratio"] for name, entry in fold.checks.items()},
                metrics={name: entry["mean"] for name, entry in fold.metrics.items()},
                repeats_run=fold.judged,
                errored=fold.judged == 0,
            )
        )
    return out


def build_report(
    spec: Any,
    plan: PlanInfo,
    cases: Sequence[CaseRecord],
    baseline_outcome: Any,
    *,
    baseline_updated: bool = False,
    generated_at: str | None = None,
) -> dict[str, Any]:
    """Everything the run produced, as the schema-1 report dict.

    ``spec`` is duck-typed (``contract.inputs.VerifySpec``) — it is read for the input
    echo and the mode only. ``generated_at`` is injectable so a test can pin the one
    non-deterministic field.
    """
    folds = [fold_case(case) for case in cases]
    rows = [
        _matrix_row(case, fold, baseline_outcome) for case, fold in zip(cases, folds, strict=True)
    ]
    report = {
        "schema": SCHEMA,
        "generated_at": generated_at or datetime.now(UTC).isoformat(),
        "mode": spec.mode,
        "inputs": {
            "sim_script": spec.sim_script,
            "sim_input_space": spec.sim_input_space,
            "sim_output_dir": spec.sim_output_dir,
            "oracle_script": spec.oracle_script,
            "run_command": getattr(spec, "run_command", None),
            "judge_command": getattr(spec, "judge_command", None),
            "pict_k": spec.pict_k,
            "repeats": spec.repeats,
            "budget_s": spec.budget_s,
            "sim_image": spec.sim_image,
            "concurrency": spec.concurrency,
            "report_only": spec.report_only,
            "checkout_sha": spec.checkout_sha,
        },
        "summary": {
            "exit_code": None,  # stamped below — exit_code_of reads the finished report
            "report_outcome": None,
            "cases_planned": plan.cases_planned,
            "cases_run": plan.cases_run,
            "cases_errored": sum(1 for fold in folds if fold.result == RESULT_ERROR),
            "runs_total": sum(len(case.runs) for case in cases),
            "checks_failed": sum(
                1 for fold in folds for entry in fold.checks.values() if entry["pass_ratio"] < 1.0
            ),
            "regressions": getattr(baseline_outcome, "regressed", 0),
            "coverage": {
                "requested_k": plan.requested_k,
                "achieved": plan.coverage_achieved,
                "truncated_after_case": plan.truncated_after_case,
            },
        },
        "matrix": rows,
        "baseline": {
            "db": str(getattr(baseline_outcome, "db", "")),
            "available": bool(getattr(baseline_outcome, "available", False)),
            "compared": getattr(baseline_outcome, "compared", 0),
            "absent": getattr(baseline_outcome, "absent", 0),
            "regressed": getattr(baseline_outcome, "regressed", 0),
            "improved": getattr(baseline_outcome, "improved", 0),
            "metric_changes": getattr(baseline_outcome, "metric_changes", 0),
            "updated": baseline_updated,
        },
        "artifacts": _artifacts(cases),
    }
    exit_code = exit_code_of(report)
    report["summary"]["exit_code"] = exit_code
    report["summary"]["report_outcome"] = REPORT_OUTCOME_BY_EXIT[exit_code]
    return report


def exit_code_of(report: Mapping[str, Any]) -> int:
    """The report's own exit code — the single fold, read back from the document.

    Two questions, and their ORDER is the contract:

    1. **Did anything come back clean?** A run whose every run ERRORed — and a run that
       started no case at all — is INCOMPLETE: it proved nothing, so it is an
       infrastructure fault (3) in EVERY mode. This is asked BEFORE the non-gating
       shortcut on purpose: a sweep whose containers all died (bad image, wrong script,
       every case timing out) must not read as a healthy sweep just because a sweep
       does not gate.
    2. **Does this run gate?** Sweep mode (no oracle) and ``--report-only`` report and
       stop there; the workflow marks the Check neutral.

    Admit rejections (2) never reach here — they exit before a report exists — with ONE
    exception that only the finished report can see: a gate whose judged verdicts
    contain no boolean at all asserts nothing, so it is refused (2) rather than
    reported green.
    """
    summary = report["summary"]
    ran_clean = any(run["error"] is None for row in report["matrix"] for run in row["runs"])
    if not ran_clean:
        return EXIT_INFRA  # every case ERRORed (or none ran): incomplete, not a verdict
    if report["mode"] == "sweep" or report["inputs"]["report_only"]:
        return EXIT_PASS
    if not any(row["checks"] for row in report["matrix"]):
        return EXIT_CONTRACT
    if summary["checks_failed"] or summary["regressions"]:
        return EXIT_FAIL
    return EXIT_PASS


# --- internals ------------------------------------------------------------------------


def _matrix_row(case: CaseRecord, fold: CaseFold, baseline_outcome: Any) -> dict[str, Any]:
    regression = getattr(baseline_outcome, "cases", {}).get(case.case_id)
    return {
        "case_id": case.case_id,
        "axes": dict(case.axes),
        "result": fold.result,
        "repeats_planned": case.repeats_planned,
        "repeats_run": len(case.runs),
        "runs": [_run_entry(run) for run in case.runs],
        "checks": fold.checks,
        "metrics": fold.metrics,
        "nulls": fold.nulls,
        "regression": {
            "status": getattr(regression, "status", STATUS_SKIPPED),
            "details": [dict(detail) for detail in getattr(regression, "details", ())],
        },
    }


def _run_entry(run: RunRecord) -> dict[str, Any]:
    return {
        "repeat": run.repeat,
        "seed": run.seed,
        "rc_sim": run.rc_sim,
        "rc_oracle": run.rc_oracle,
        "wall_s": round(run.wall_s, 3),
        "zip": run.zip,
        "log": run.log,
        "error": run.result.error,
        "verdict": run.result.verdict,
    }


def _artifacts(cases: Sequence[CaseRecord]) -> dict[str, Any]:
    """Every collected zip and log, plus the zips that hold a manifest instead of the
    files (the size cap tripped) — named so a reader is not left wondering why an
    archive is 2 KB."""
    runs = [run for case in cases for run in case.runs]
    return {
        "zips": [run.zip for run in runs if run.zip],
        "logs": [run.log for run in runs if run.log],
        "oversize_replaced": [run.zip for run in runs if run.zip and run.zip_truncated],
    }
