"""The glue between a finished run and GitHub: payload files, inline annotations, and
the staging directory the artifact upload takes.

The staging guards get the most attention here on purpose — that code EMPTIES a
directory, so every refusal it makes is asserted, and the one test that would be
catastrophic to get wrong (a stray ``.`` naming the checkout) is asserted explicitly.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from cv_infra.cli import publish_glue
from tests.conftest import make_report

# --- publish --------------------------------------------------------------------------


def test_write_payloads_writes_the_three_fixed_names(tmp_path):
    written = publish_glue.write_payloads(make_report(), tmp_path / "payloads")
    assert set(written) == {
        publish_glue.CHECK_RUN_FILE,
        publish_glue.STICKY_COMMENT_FILE,
        publish_glue.STEP_SUMMARY_FILE,
    }
    check_run = json.loads(written[publish_glue.CHECK_RUN_FILE].read_text(encoding="utf-8"))
    assert check_run["conclusion"] == "success"
    sticky = written[publish_glue.STICKY_COMMENT_FILE].read_text(encoding="utf-8")
    assert sticky.startswith("<!-- cv-infra:verification-report -->")


def test_publish_mode_reads_the_report_from_disk(tmp_path, capsys):
    report_path = tmp_path / "report.json"
    report_path.write_text(json.dumps(make_report()), encoding="utf-8")
    assert publish_glue.main(["publish", str(report_path), str(tmp_path / "out")]) == 0
    assert (tmp_path / "out" / publish_glue.STEP_SUMMARY_FILE).is_file()
    assert capsys.readouterr().out == ""  # provenance goes to stderr; stdout stays clean


# --- annotate -------------------------------------------------------------------------


def test_annotation_maps_source_location_to_file_line_col():
    line = publish_glue.render_annotation(
        {
            "field_path": "--pict-k",
            "expected": "an integer >= 1",
            "got": "'x'",
            "source_path": "verify/param_space.pict",
            "source_line": 4,
            "source_col": 2,
        }
    )
    assert line.startswith("::error file=verify/param_space.pict,line=4,col=2::")
    assert "--pict-k: expected an integer >= 1, got 'x'" in line


def test_a_column_never_rides_without_a_line_and_a_locationless_error_is_still_emitted():
    assert publish_glue.render_annotation(
        {"expected": "a file", "source_path": "verify/sim.py", "source_col": 9}
    ).startswith("::error file=verify/sim.py::")
    assert publish_glue.render_annotation({"expected": "a request"}).startswith("::error::")


def test_workflow_command_metacharacters_are_escaped_on_both_segments():
    line = publish_glue.render_annotation(
        {
            "expected": "no\nnewline 100%",
            "got": "'x'",
            "source_path": "dir:a,b/sim.py",
            "source_line": 1,
        }
    )
    assert "file=dir%3Aa%2Cb/sim.py" in line
    assert "%0A" in line and "100%25" in line
    assert "\n" not in line


def test_errors_json_may_be_a_list_or_a_lone_object_and_junk_yields_nothing():
    entry = {"expected": "a file"}
    assert len(publish_glue.render_annotations([entry, entry])) == 2
    assert len(publish_glue.render_annotations(entry)) == 1
    assert publish_glue.render_annotations([entry, "not-a-dict"]) == [
        publish_glue.render_annotation(entry)
    ]
    assert publish_glue.render_annotations("nonsense") == []


def test_annotate_mode_prints_one_line_per_error(tmp_path, capsys):
    errors = tmp_path / "errors.json"
    errors.write_text(json.dumps([{"field_path": "--repeats", "expected": "an int"}]), "utf-8")
    assert publish_glue.main(["annotate", str(errors)]) == 0
    assert capsys.readouterr().out.strip().startswith("::error::--repeats:")


# --- stage-artifacts ------------------------------------------------------------------


def make_run_dir(tmp_path, *, logs=True):
    run_dir = tmp_path / "run"
    (run_dir / "payloads").mkdir(parents=True)
    (run_dir / "zips").mkdir()
    (run_dir / "payloads" / "step-summary.md").write_text("body", encoding="utf-8")
    (run_dir / "zips" / "case.zip").write_bytes(b"PK")
    (run_dir / "report.json").write_text("{}", encoding="utf-8")
    if logs:
        (run_dir / "logs").mkdir()
        (run_dir / "logs" / "case.sim.log").write_bytes(b"boot")
    return run_dir


def test_stage_copies_the_report_payloads_zips_and_logs(tmp_path):
    staging = tmp_path / "artifacts"
    summary = publish_glue.stage_artifacts(make_run_dir(tmp_path), staging)
    assert summary == {"staged": 4, "skipped": 0}
    assert (staging / "report.json").is_file()
    assert (staging / "payloads" / "step-summary.md").read_text(encoding="utf-8") == "body"
    assert (staging / "zips" / "case.zip").is_file()
    assert (staging / "logs" / "case.sim.log").is_file()


def test_an_absent_entry_is_skipped_loudly_and_never_fails_the_upload(tmp_path, capsys):
    summary = publish_glue.stage_artifacts(
        make_run_dir(tmp_path, logs=False), tmp_path / "artifacts"
    )
    assert summary == {"staged": 3, "skipped": 1}
    assert "skip (absent)" in capsys.readouterr().err


def test_a_previous_run_left_in_the_staging_dir_is_cleared_first(tmp_path, capsys):
    """Measured failure: a self-hosted runner reuses its workspace, so yesterday's
    failure recordings rode along in today's green artifact."""
    staging = tmp_path / "artifacts"
    (staging / "stale-dir").mkdir(parents=True)
    (staging / "stale-file").write_text("old", encoding="utf-8")
    (staging / "stale-link").symlink_to(tmp_path / "elsewhere")
    publish_glue.stage_artifacts(make_run_dir(tmp_path), staging)
    assert sorted(p.name for p in staging.iterdir()) == ["logs", "payloads", "report.json", "zips"]
    assert "cleared 3 stale entries" in capsys.readouterr().err


def test_clearing_reports_a_single_entry_in_the_singular(tmp_path, capsys):
    staging = tmp_path / "artifacts"
    staging.mkdir()
    (staging / "stale").write_text("old", encoding="utf-8")
    publish_glue.stage_artifacts(make_run_dir(tmp_path), staging)
    assert "cleared 1 stale entry " in capsys.readouterr().err


def test_stage_mode_prints_the_counts(tmp_path, capsys):
    run_dir = make_run_dir(tmp_path)
    assert publish_glue.main(["stage-artifacts", str(run_dir), str(tmp_path / "out")]) == 0
    assert capsys.readouterr().out.strip() == "staged=4 skipped=0"


@pytest.mark.parametrize(
    ("target", "reason"),
    [("/", "unsafe"), ("/tmp", "unsafe"), ("/etc", "unsafe")],
)
def test_a_system_root_is_refused_before_anything_is_removed(target, reason):
    with pytest.raises(ValueError, match=reason):
        publish_glue._resolve_safe_staging_dir(Path(target))


def test_a_repository_checkout_is_refused_this_is_what_catches_a_stray_dot(tmp_path):
    (tmp_path / ".git").mkdir()
    with pytest.raises(ValueError, match="repo checkout"):
        publish_glue._resolve_safe_staging_dir(tmp_path)


def test_a_symlinked_staging_dir_is_refused_its_contents_live_elsewhere(tmp_path):
    (tmp_path / "real").mkdir()
    link = tmp_path / "link"
    link.symlink_to(tmp_path / "real")
    with pytest.raises(ValueError, match="symlinked"):
        publish_glue._resolve_safe_staging_dir(link)


def test_home_and_its_ancestors_are_refused(tmp_path, monkeypatch):
    home = tmp_path / "home" / "operator"
    home.mkdir(parents=True)
    monkeypatch.setattr(publish_glue.Path, "home", classmethod(lambda cls: home))
    with pytest.raises(ValueError, match="home/ancestor"):
        publish_glue._resolve_safe_staging_dir(home)
    with pytest.raises(ValueError, match="home/ancestor"):
        publish_glue._resolve_safe_staging_dir(home.parent)


def test_clearing_a_directory_that_does_not_exist_removes_nothing(tmp_path):
    assert publish_glue._clear_entries(tmp_path / "absent") == 0
