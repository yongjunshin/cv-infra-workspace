"""The published surfaces. What is pinned here is what a human sees on a PR and what
GitHub is told about the run — both of which are read as truth without opening the
artifact, so the honesty rules (a non-gating run says so, an ERROR is never folded into
a green count, a truncated run shows what it covered) are asserted line by line.
"""

from __future__ import annotations

from cv_infra.report import github
from tests.conftest import CASE_ID, make_case_row, make_report


def body(report):
    return github.render_step_summary(report)


# --- the Check Run payload ------------------------------------------------------------


def test_check_run_carries_the_stable_name_and_a_counting_title():
    payload = github.render_check_run(make_report())
    assert payload["name"] == github.CHECK_RUN_NAME
    assert payload["status"] == "completed"
    assert payload["conclusion"] == "success"
    assert payload["output"]["title"] == (
        "cv-infra gate: pass · 1/1 cases, 1 runs · 0 check(s) failed · 0 errored"
    )
    assert "## CV-Infra verification" in payload["output"]["summary"]


def test_conclusion_comes_from_the_exit_code_table_including_neutral_for_infra():
    assert github.conclusion_of(make_report(summary={"exit_code": 1})) == "failure"
    assert github.conclusion_of(make_report(summary={"exit_code": 2})) == "failure"
    assert github.conclusion_of(make_report(summary={"exit_code": 3})) == "neutral"


def test_an_infra_report_leads_with_the_not_a_robot_verdict_line():
    rendered = body(make_report(summary={"exit_code": 3, "report_outcome": "errored"}))
    assert github.INFRA_INCOMPLETE_MESSAGE in rendered


# --- non-gating runs ------------------------------------------------------------------


def test_a_sweep_is_neutral_and_says_it_does_not_gate():
    report = make_report(mode="sweep", inputs={"oracle_script": None})
    assert github.conclusion_of(report) == "neutral"
    assert "**this check does not gate**" in body(report)


def test_report_only_is_neutral_too_even_when_a_check_failed():
    report = make_report(inputs={"report_only": True}, summary={"exit_code": 0})
    assert github.conclusion_of(report) == "neutral"
    assert github.NON_GATING_BANNER in body(report)


def test_a_gating_run_carries_no_banner():
    assert "does not gate" not in body(make_report())


# --- the matrix -----------------------------------------------------------------------


def test_matrix_row_shows_axes_ratio_metric_and_baseline_status():
    rendered = body(make_report())
    assert "| case | axes | result | repeats | checks | metrics | vs baseline |" in rendered
    assert "| lighting=dim | pass | 1/1 | upright 1.00 (n=1) | z_final=0.12 | ok |" in rendered


def test_a_long_case_id_is_abbreviated_and_the_legend_says_where_the_full_one_is():
    rendered = body(make_report())
    assert f"| {CASE_ID[:19]}… |" in rendered
    assert "the full ids are in `report.json`" in rendered


def test_a_short_case_id_renders_verbatim_with_no_legend():
    rendered = body(make_report(rows=[make_case_row(case_id="sha256:ab")]))
    assert "| sha256:ab |" in rendered
    assert "the full ids are" not in rendered


def test_absent_values_render_as_na_never_as_zero():
    row = make_case_row(case_id=None, axes={}, checks={}, metrics={})
    rendered = body(make_report(rows=[row], inputs={"checkout_sha": None}))
    assert "| n/a | n/a | pass | 1/1 | n/a | n/a | ok |" in rendered
    assert "commit `n/a`" in rendered


def test_a_pipe_in_a_value_cannot_break_the_table():
    row = make_case_row(axes={"note": "a|b"})
    assert "note=a\\|b" in body(make_report(rows=[row]))


def test_a_regressed_case_shows_the_numbers_that_moved():
    row = make_case_row(
        checks={"upright": {"pass_ratio": 0.5, "n": 2}},
        regression={
            "status": "regressed",
            "details": [
                {
                    "name": "upright",
                    "kind": "check",
                    "status": "regressed",
                    "baseline": 1.0,
                    "current": 0.5,
                    "single_sample": False,
                }
            ],
        },
    )
    assert "regressed: upright 1→0.5" in body(make_report(rows=[row]))


def test_an_empty_matrix_says_so_instead_of_rendering_an_empty_table():
    assert "_(no case ran)_" in body(make_report(rows=[]))


# --- errors, truncation, baseline -----------------------------------------------------


def test_errored_runs_get_their_own_section_with_the_message():
    row = make_case_row(result="error", checks={}, metrics={})
    row["runs"][0]["error"] = "the sim exited rc=1 — this case produced no trustworthy verdict"
    rendered = body(make_report(rows=[row]))
    assert "### Errors (not robot verdicts)" in rendered
    assert "repeat 0: the sim exited rc=1" in rendered


def test_a_clean_run_has_no_error_section_at_all():
    assert "### Errors" not in body(make_report())


def test_a_truncated_run_reports_what_ran_and_what_it_covers():
    report = make_report(
        summary={
            "cases_planned": 14,
            "cases_run": 9,
            "coverage": {"requested_k": 2, "achieved": 0.912, "truncated_after_case": 8},
        }
    )
    assert "ran 9/14 cases (truncated after case 8), 91.2% of the 2-wise coverage." in body(report)


def test_a_run_the_budget_cut_entirely_says_so_instead_of_showing_no_line():
    report = make_report(
        summary={
            "cases_planned": 4,
            "cases_run": 0,
            "coverage": {"requested_k": 2, "achieved": 0.0, "truncated_after_case": -1},
        }
    )
    assert "ran 0/4 cases (the budget was spent before the first case)" in body(report)


def test_an_untruncated_run_carries_no_budget_line():
    assert "budget reached" not in body(make_report())


def test_baseline_section_counts_and_flags_an_update():
    rendered = body(make_report(baseline={"compared": 3, "absent": 1, "updated": True}))
    assert "compared 3 key(s) · 1 without a baseline" in rendered
    assert "baseline UPDATED from this run" in rendered


def test_an_unavailable_baseline_is_reported_not_hidden():
    rendered = body(make_report(baseline={"available": False, "db": "/nope/db.sqlite3"}))
    assert "unavailable (`/nope/db.sqlite3`)" in rendered
    assert "every case skipped" in rendered


def test_single_sample_ratios_are_called_thin_evidence_but_still_gate():
    row = make_case_row(
        regression={
            "status": "ok",
            "details": [{"name": "upright", "status": "ok", "single_sample": True}],
        }
    )
    assert "thin evidence, still gating" in body(make_report(rows=[row]))


# --- the sticky comment ---------------------------------------------------------------


def test_sticky_comment_leads_with_the_marker_and_shares_the_body():
    report = make_report()
    sticky = github.render_sticky_comment(report)
    assert sticky.startswith(github.STICKY_COMMENT_MARKER + "\n")
    assert sticky[len(github.STICKY_COMMENT_MARKER) + 1 :] == github.render_step_summary(report)


def test_rendering_is_byte_deterministic():
    """The upsert edits the comment in place only if an unchanged report renders the
    same bytes; a clock or a set iteration in here would post a new comment per push."""
    report = make_report()
    assert github.render_sticky_comment(report) == github.render_sticky_comment(report)
    assert github.render_check_run(report) == github.render_check_run(report)
