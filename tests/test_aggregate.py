"""Report assembly: the ratio arithmetic, the exit fold, and the honesty fields.

The exit fold is the part worth testing exhaustively — it is the single decision the
whole tool exists to make, and every one of its branches is a way to be wrong in
production (a gate that cannot fail, an infra fault read as a robot failure, a sweep
that turns a PR red).
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from cv_infra.baselines import STATUS_SKIPPED, BaselineOutcome, CaseRegression
from cv_infra.contract.verdict import LANE_ERROR, LANE_OK, CaseRunResult
from cv_infra.report import aggregate

SPEC = SimpleNamespace(
    mode="gate",
    sim_script="verify/sim.py",
    sim_input_space="verify/param_space.pict",
    sim_output_dir="verify/out",
    oracle_script="verify/oracle.py",
    pict_k=2,
    repeats=1,
    budget_s=None,
    sim_image="isaac-sim:test",
    concurrency=1,
    report_only=False,
    checkout_sha="abc123",
)


def ok_run(repeat=0, checks=None, metrics=None, nulls=None, **overrides):
    result = CaseRunResult(
        lane=LANE_OK,
        checks=dict(checks or {}),
        metrics=dict(metrics or {}),
        nulls=list(nulls or []),
        verdict={**(checks or {}), **(metrics or {})},
    )
    fields = {"rc_sim": 0, "rc_oracle": 0, "wall_s": 1.0, "zip": None, "log": None}
    fields.update(overrides)
    return aggregate.RunRecord(repeat=repeat, seed=1, result=result, **fields)


def error_run(repeat=0, error="boom"):
    return aggregate.RunRecord(
        repeat=repeat,
        seed=1,
        rc_sim=None,
        rc_oracle=None,
        wall_s=0.5,
        result=CaseRunResult(lane=LANE_ERROR, error=error),
    )


def case(runs, case_id="sha256:aa", repeats_planned=1, axes=None):
    return aggregate.CaseRecord(
        case_id=case_id,
        axes=axes or {"lighting": "dim"},
        repeats_planned=repeats_planned,
        runs=tuple(runs),
    )


def build(cases, *, spec=SPEC, plan=None, outcome=None, **kwargs):
    plan = plan or aggregate.PlanInfo(requested_k=2, cases_planned=len(cases), cases_run=len(cases))
    outcome = outcome or BaselineOutcome(db="/db", available=True, cases={})
    return aggregate.build_report(spec, plan, cases, outcome, generated_at="T", **kwargs)


# --- the folds ------------------------------------------------------------------------


def test_check_is_a_pass_ratio_over_judged_runs_with_its_sample_count():
    fold = aggregate.fold_case(
        case(
            [
                ok_run(0, checks={"upright": True}),
                ok_run(1, checks={"upright": False}),
                ok_run(2, checks={"upright": True}),
            ],
            repeats_planned=3,
        )
    )
    assert fold.checks == {"upright": {"pass_ratio": pytest.approx(2 / 3), "n": 3}}
    assert fold.result == "fail"


def test_errored_runs_are_excluded_from_the_ratio_not_counted_as_false():
    """An infra fault must not read as the robot failing — the ratio is over what was
    actually judged, and the case still passes."""
    fold = aggregate.fold_case(
        case([ok_run(0, checks={"upright": True}), error_run(1)], repeats_planned=2)
    )
    assert fold.checks == {"upright": {"pass_ratio": 1.0, "n": 1}}
    assert fold.judged == 1
    assert fold.result == "pass"


def test_a_case_whose_runs_all_errored_is_an_error_case():
    assert aggregate.fold_case(case([error_run(0), error_run(1)])).result == "error"


def test_metrics_keep_every_value_and_their_mean_and_nulls_are_unioned():
    fold = aggregate.fold_case(
        case(
            [
                ok_run(0, metrics={"z": 0.1}, nulls=["visibility"]),
                ok_run(1, metrics={"z": 0.3}, nulls=["visibility", "grip"]),
            ],
            repeats_planned=2,
        )
    )
    assert fold.metrics == {"z": {"values": [0.1, 0.3], "mean": pytest.approx(0.2)}}
    assert fold.nulls == ["grip", "visibility"]


def test_observations_carry_ratios_means_and_the_judged_sample_count():
    observations = aggregate.observations(
        [
            case([ok_run(0, checks={"upright": True}, metrics={"z": 0.5})], case_id="sha256:a"),
            case([error_run(0)], case_id="sha256:b"),
        ]
    )
    assert observations[0].checks == {"upright": 1.0}
    assert observations[0].metrics == {"z": 0.5}
    assert observations[0].repeats_run == 1
    assert observations[0].errored is False
    assert observations[1].errored is True


# --- the report document --------------------------------------------------------------


def test_report_echoes_the_request_and_counts_what_ran():
    report = build([case([ok_run(0, checks={"upright": True})])])
    assert report["schema"] == aggregate.SCHEMA
    assert report["generated_at"] == "T"
    assert report["inputs"]["sim_script"] == "verify/sim.py"
    assert report["inputs"]["checkout_sha"] == "abc123"
    assert report["summary"]["runs_total"] == 1
    assert report["summary"]["cases_errored"] == 0
    assert report["matrix"][0]["axes"] == {"lighting": "dim"}
    assert report["matrix"][0]["runs"][0]["verdict"] == {"upright": True}


def test_generated_at_defaults_to_now_when_the_caller_does_not_pin_it():
    report = aggregate.build_report(
        SPEC,
        aggregate.PlanInfo(requested_k=2, cases_planned=1, cases_run=1),
        [case([ok_run(0, checks={"upright": True})])],
        BaselineOutcome(db="/db", available=True, cases={}),
    )
    assert report["generated_at"].startswith("20")


def test_truncation_is_reported_as_counts_plus_the_coverage_actually_achieved():
    plan = aggregate.PlanInfo(
        requested_k=2,
        cases_planned=14,
        cases_run=9,
        coverage_achieved=0.912,
        truncated_after_case=8,
    )
    report = build([case([ok_run(0, checks={"upright": True})])], plan=plan)
    assert report["summary"]["coverage"] == {
        "requested_k": 2,
        "achieved": 0.912,
        "truncated_after_case": 8,
    }
    assert report["summary"]["cases_run"] == 9


def test_artifacts_list_every_zip_and_log_and_name_the_oversize_ones():
    report = build(
        [
            case(
                [
                    ok_run(0, checks={"upright": True}, zip="zips/a.zip", log="logs/a.log"),
                    ok_run(
                        1,
                        checks={"upright": True},
                        zip="zips/b.zip",
                        log="logs/b.log",
                        zip_truncated=True,
                    ),
                ],
                repeats_planned=2,
            )
        ]
    )
    assert report["artifacts"]["zips"] == ["zips/a.zip", "zips/b.zip"]
    assert report["artifacts"]["logs"] == ["logs/a.log", "logs/b.log"]
    assert report["artifacts"]["oversize_replaced"] == ["zips/b.zip"]


def test_baseline_block_mirrors_the_outcome_and_records_whether_it_was_updated():
    outcome = BaselineOutcome(
        db="/db",
        available=True,
        cases={"sha256:aa": CaseRegression("regressed", ({"name": "upright"},))},
        compared=3,
        absent=1,
        regressed=1,
        improved=0,
        metric_changes=2,
    )
    report = build(
        [case([ok_run(0, checks={"upright": True})])], outcome=outcome, baseline_updated=True
    )
    assert report["baseline"]["compared"] == 3
    assert report["baseline"]["updated"] is True
    assert report["summary"]["regressions"] == 1
    assert report["matrix"][0]["regression"]["status"] == "regressed"


def test_a_case_the_outcome_never_saw_is_marked_skipped_not_ok():
    report = build([case([error_run(0)], case_id="sha256:zz")])
    assert report["matrix"][0]["regression"] == {"status": STATUS_SKIPPED, "details": []}


# --- the exit fold --------------------------------------------------------------------


def test_exit_0_when_every_check_passed():
    report = build([case([ok_run(0, checks={"upright": True})])])
    assert report["summary"]["exit_code"] == 0
    assert report["summary"]["report_outcome"] == "pass"


def test_exit_1_when_a_check_failed():
    report = build([case([ok_run(0, checks={"upright": False})])])
    assert report["summary"]["exit_code"] == 1
    assert report["summary"]["checks_failed"] == 1
    assert report["summary"]["report_outcome"] == "fail"


def test_exit_1_when_a_check_regressed_even_though_it_passed_today():
    outcome = BaselineOutcome(
        db="/db", available=True, cases={"sha256:aa": CaseRegression("regressed")}, regressed=1
    )
    report = build([case([ok_run(0, checks={"upright": True})])], outcome=outcome)
    assert report["summary"]["exit_code"] == 1


def test_exit_3_when_nothing_was_judged():
    """Every case ERRORed: the run is INCOMPLETE, which is never a robot verdict."""
    report = build([case([error_run(0)])])
    assert report["summary"]["exit_code"] == 3
    assert report["summary"]["report_outcome"] == "errored"


def test_exit_2_when_the_gate_has_no_boolean_to_check():
    """Verdicts arrived, none of them was a check: this gate cannot fail, so it is
    refused instead of reporting green."""
    report = build([case([ok_run(0, metrics={"z": 0.5})])])
    assert report["summary"]["exit_code"] == 2


def test_report_only_accepts_the_check_less_verdict_and_never_gates():
    spec = SimpleNamespace(**{**SPEC.__dict__, "report_only": True})
    assert build([case([ok_run(0, metrics={"z": 0.5})])], spec=spec)["summary"]["exit_code"] == 0
    # ...and a false check cannot fail the build either.
    assert (
        build([case([ok_run(0, checks={"upright": False})])], spec=spec)["summary"]["exit_code"]
        == 0
    )


def test_sweep_mode_runs_reports_and_never_gates():
    spec = SimpleNamespace(**{**SPEC.__dict__, "mode": "sweep", "oracle_script": None})
    report = build([case([ok_run(0)])], spec=spec)
    assert report["mode"] == "sweep"
    assert report["summary"]["exit_code"] == 0


def test_exit_code_of_reads_the_finished_report_back():
    """The fold is a pure function of the document, so a reader can re-derive the exit
    code from report.json alone."""
    report = build([case([ok_run(0, checks={"upright": False})])])
    assert aggregate.exit_code_of(report) == report["summary"]["exit_code"] == 1
