"""Reporting package (M4): pass/fail aggregation, regression review, GitHub publishing."""

from cv_infra.report.aggregate import RequestReportInput, build_report
from cv_infra.report.baseline import find_baseline, update_baseline
from cv_infra.report.github import (
    STICKY_COMMENT_MARKER,
    render_artifact_manifest,
    render_check_run,
    render_step_summary,
    render_sticky_comment,
)
from cv_infra.report.matrix import build_matrix, render_text
from cv_infra.report.regression import RegressionVerdict, identity_key, judge_regression

__all__ = [
    "STICKY_COMMENT_MARKER",
    "RegressionVerdict",
    "RequestReportInput",
    "build_matrix",
    "build_report",
    "find_baseline",
    "identity_key",
    "judge_regression",
    "render_artifact_manifest",
    "render_check_run",
    "render_step_summary",
    "render_sticky_comment",
    "render_text",
    "update_baseline",
]
