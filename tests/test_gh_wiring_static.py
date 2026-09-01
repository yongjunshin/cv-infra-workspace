"""M8 GitHub-wiring STATIC gate (p5c3) — REQ-INTAKE-003/005, REQ-REPORT-007, R10/D-J, §2-7.

These tests statically verify the AUTHORED plumbing (no live GitHub run — that is
p5c4). Three planes:

* CLI ``--trigger-source`` (REQ-INTAKE-003): flag choices/default + the wire fold
  (default human-manual is OMITTED so the server default applies; only ci-cd rides
  the POST body) — a plain human ``submit`` stays byte-identical to the pre-p5c3 wire.
* publish glue (``cv_infra.cli.publish_glue``): it IMPORTS the four M4 ``github.py``
  renderers (재구현 0 — asserted by delegation identity) and maps an M1 error object
  1:1 to a ``::error file,line,col::`` annotation (D-L).
* the reusable workflow + composite action YAML: ``workflow_call`` / composite shape,
  minimal permissions, self-hosted-by-label, ``--trigger-source ci-cd --wait``,
  immutable-SHA action pins, and the R10/D-J security invariants (no
  ``pull_request_target``, no PR-head checkout on the GPU job, SUT ref-only).

Stdlib + pyyaml + pytest — parses the YAML with ``yaml.safe_load`` (never executes it).
"""

from __future__ import annotations

import json
import os
import re
import subprocess
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
import yaml

from cv_infra.cli import batch, publish_glue
from cv_infra.cli.main import _build_parser
from cv_infra.contract.errors import ContractError
from cv_infra.report import github
from tests.negative.test_eula_gate import baked_consent_bindings

_ROOT = Path(__file__).resolve().parents[1]
_VERIFY_WORKFLOW = _ROOT / ".github/workflows/verify.yml"
_VERIFY_ACTION = _ROOT / "actions/verify/action.yml"
_PLATFORM_CI = _ROOT / ".github/workflows/ci.yml"

#: 40-hex immutable commit SHA pin (the trailing ``# vX.Y.Z`` tag comment is
#: stripped by the YAML parser, so the parsed ``uses`` value ends at the SHA).
_SHA_PINNED = re.compile(r"^[\w.\-]+/[\w.\-]+@[0-9a-f]{40}$")
_FLOATING = re.compile(r"@(main|master|latest)\b")


# --------------------------------------------------------------------------- #
# helpers
# --------------------------------------------------------------------------- #
def _load(path: Path) -> dict[str, Any]:
    return yaml.safe_load(path.read_text(encoding="utf-8"))


def _trigger(doc: dict[str, Any]) -> Any:
    """YAML 1.1 parses the ``on:`` key as the boolean ``True`` — read either form."""
    return doc.get("on", doc.get(True))


def _uses(steps: list[dict[str, Any]]) -> list[str]:
    return [step["uses"] for step in steps if isinstance(step, dict) and "uses" in step]


def _runs(steps: list[dict[str, Any]]) -> str:
    return "\n".join(step["run"] for step in steps if isinstance(step, dict) and "run" in step)


def _steps(doc: dict[str, Any]) -> list[dict[str, Any]]:
    """The step list for either entry topology (reusable workflow job / composite)."""
    return doc["jobs"]["verify"]["steps"] if "jobs" in doc else doc["runs"]["steps"]


def _upload_step(doc: dict[str, Any]) -> dict[str, Any]:
    return next(
        s
        for s in _steps(doc)
        if isinstance(s, dict) and str(s.get("uses", "")).startswith("actions/upload-artifact")
    )


def _stub_envelope(docs: list[dict[str, Any]]) -> SimpleNamespace:
    reqs = [
        SimpleNamespace(raw_doc=doc, oracle_plugin_dir=f"/abs/consumer/scenarios{i}")
        for i, doc in enumerate(docs)
    ]
    return SimpleNamespace(requests=reqs)


# --------------------------------------------------------------------------- #
# (A) CLI --trigger-source (REQ-INTAKE-003)
# --------------------------------------------------------------------------- #
def test_submit_trigger_source_flag_choices_and_default():
    parser = _build_parser()
    assert parser.parse_args(["submit", "e.yaml"]).trigger_source == "human-manual"
    assert (
        parser.parse_args(["submit", "e.yaml", "--trigger-source", "ci-cd"]).trigger_source
        == "ci-cd"
    )
    with pytest.raises(SystemExit):  # not a valid choice
        parser.parse_args(["submit", "e.yaml", "--trigger-source", "cron"])


def test_wire_trigger_source_omits_default_only():
    # default (human-manual) folds to None = OMITTED (server default applies); a
    # non-default (ci-cd, set by the Action) rides verbatim (REQ-INTAKE-003).
    assert batch._wire_trigger_source("human-manual") is None
    assert batch._wire_trigger_source("ci-cd") == "ci-cd"


def test_wire_body_carries_trigger_source_only_when_provided():
    env = _stub_envelope([{"scenario": "s0"}, {"scenario": "s1"}])
    # default path (None) — byte-identical to the pre-p5c3 wire (regression guard for
    # the existing strict body-key assertion in test_cli_batch.py).
    assert "trigger_source" not in batch._wire_body(env)
    assert "trigger_source" not in batch._wire_body(env, None)
    # ci-cd provided -> the key rides at top level, verbatim.
    body = batch._wire_body(env, "ci-cd")
    assert body["trigger_source"] == "ci-cd"
    assert body["requests"] == [{"scenario": "s0"}, {"scenario": "s1"}]


# --------------------------------------------------------------------------- #
# (B) publish glue — 4-surface payloads (github.py IMPORTED, 재구현 0)
# --------------------------------------------------------------------------- #
# A minimal report dict. Its rendering CORRECTNESS is not asserted here (that is
# test_report_github_renderer.py's job with real build_report fixtures) — these
# tests assert the glue DELEGATES to github.py (identity), so the report shape is
# irrelevant to the property under test.
_REPORT: dict[str, Any] = {
    "envelope_id": "env-1",
    "trigger_source": "ci-cd",
    "generated_at": "2026-07-20T00:00:00+00:00",
    "summary": {
        "verdict": "fail",
        "report_outcome": "fail",
        "total": 1,
        "passed": 0,
        "failed": 1,
        "errored": 0,
    },
    "baseline_summary": {"absent": 1, "regressed": 0, "improved": 0},
    "matrix": [
        {
            "request_id": "req-0",
            "sut_ref": "carter-sut:b",
            "rollup": {"verdict": "fail", "verdicts": ["fail"], "repeats": 1, "flaky": False},
            "metrics": {"time_to_goal_s": 12.0},
            "regression": {"status": "absent"},
            "artifacts": {
                "policy": "failing-all + one-representative-pass",
                "selected": [
                    {
                        "repeat_index": 0,
                        "role": "failing",
                        "verdict": "fail",
                        "result_json": "r.json",
                        "rosbag_mcap": "r.mcap",
                        "recording_mp4": None,
                        "excluded": [],
                        "warnings": [],
                    }
                ],
            },
        }
    ],
}


def test_render_payloads_delegates_to_github_renderers():
    payloads = publish_glue.render_payloads(_REPORT)
    assert payloads == {
        publish_glue.CHECK_RUN_FILE: github.render_check_run(_REPORT),
        publish_glue.STICKY_COMMENT_FILE: github.render_sticky_comment(_REPORT),
        publish_glue.STEP_SUMMARY_FILE: github.render_step_summary(_REPORT),
        publish_glue.ARTIFACT_MANIFEST_FILE: github.render_artifact_manifest(_REPORT),
    }


def test_write_payloads_emits_four_named_files(tmp_path):
    paths = publish_glue.write_payloads(_REPORT, tmp_path / "payloads")
    assert {p.name for p in paths.values()} == {
        "check-run.json",
        "sticky-comment.md",
        "step-summary.md",
        "artifact-manifest.json",
    }
    check = json.loads(paths[publish_glue.CHECK_RUN_FILE].read_text(encoding="utf-8"))
    # CHECK_RUN_NAME 확정 소비 (CEO 비준 2026-07-20): the payload name IS the github.py
    # constant, and that constant IS the confirmed value.
    assert check["name"] == github.CHECK_RUN_NAME == "CV-Infra Verification"
    sticky = paths[publish_glue.STICKY_COMMENT_FILE].read_text(encoding="utf-8")
    assert sticky.startswith(github.STICKY_COMMENT_MARKER)
    manifest = json.loads(paths[publish_glue.ARTIFACT_MANIFEST_FILE].read_text(encoding="utf-8"))
    assert set(manifest) == {"policy", "uploads", "missing", "excluded"}


# --------------------------------------------------------------------------- #
# (B2) stage-artifacts — manifest uploads[] -> staging dir (P5-02 완결)
# --------------------------------------------------------------------------- #
def _report_with_artifacts(tmp_path) -> dict[str, Any]:
    """A report whose manifest yields uploads (2 present paths) + missing (mp4 None)
    + excluded (size-capped mcap). Real files exist on disk for the upload paths so
    ``stage-artifacts`` can copy them (host-resolvable absolute paths, T2 contract)."""
    src = tmp_path / "src"
    src.mkdir()
    result0 = src / "r0.json"
    result0.write_text("{}", encoding="utf-8")
    mcap0 = src / "r0.mcap"
    mcap0.write_bytes(b"\x00mcap")
    result2 = src / "r2.json"
    result2.write_text("{}", encoding="utf-8")
    return {
        "matrix": [
            {
                "request_id": "req-a",
                "artifacts": {
                    "policy": "failing-all + one-representative-pass",
                    "selected": [
                        {  # failure repeat 0: result.json + mcap uploaded, mp4 missing
                            "repeat_index": 0,
                            "role": "failure",
                            "verdict": "fail",
                            "result_json": str(result0),
                            "rosbag_mcap": str(mcap0),
                            "recording_mp4": None,
                            "excluded": [],
                            "warnings": [],
                        },
                        {  # rep-pass repeat 2: result.json uploaded, mcap size-excluded
                            "repeat_index": 2,
                            "role": "representative-pass",
                            "verdict": "pass",
                            "result_json": str(result2),
                            "rosbag_mcap": None,
                            "recording_mp4": None,
                            "excluded": ["rosbag_mcap"],
                            "warnings": ["MCAP 상한 초과 — 업로드 제외"],
                        },
                    ],
                },
            }
        ],
    }


def _staged_relpaths(staging_dir) -> set[str]:
    return {p.relative_to(staging_dir).as_posix() for p in staging_dir.rglob("*") if p.is_file()}


def test_stage_artifacts_stages_only_uploads(tmp_path):
    report = _report_with_artifacts(tmp_path)
    staging = tmp_path / "artifacts"
    summary = publish_glue.stage_artifacts(report, staging)
    # (a) only the 3 uploads[] paths are staged, under the deterministic layout.
    assert _staged_relpaths(staging) == {
        "req-a/repeat-0/result_json.json",
        "req-a/repeat-0/rosbag_mcap.mcap",
        "req-a/repeat-2/result_json.json",
    }
    assert summary == {"staged": 3, "skipped": 0}
    # (b) missing (mp4) + excluded (size-capped mcap) are NOT staged (결정 #1/#2).
    assert not any("recording_mp4" in p for p in _staged_relpaths(staging))
    assert "req-a/repeat-2/rosbag_mcap.mcap" not in _staged_relpaths(staging)
    # bytes copied verbatim.
    assert (staging / "req-a/repeat-0/rosbag_mcap.mcap").read_bytes() == b"\x00mcap"


def test_stage_artifacts_layout_is_deterministic(tmp_path):
    report = _report_with_artifacts(tmp_path)
    first = tmp_path / "a"
    second = tmp_path / "b"
    publish_glue.stage_artifacts(report, first)
    publish_glue.stage_artifacts(report, second)
    assert _staged_relpaths(first) == _staged_relpaths(second)


def test_stage_uploads_skips_none_and_absent_path_non_fatal(tmp_path):
    # An upload entry with path=None and one whose absolute path does not resolve on
    # the host (container-internal / stale) are both SKIPPED with a warning — never
    # fatal (§5c defensive; T2 aligns the producer to host paths).
    present = tmp_path / "present.json"
    present.write_text("{}", encoding="utf-8")
    uploads = [
        {"request_id": "r", "repeat_index": 0, "kind": "result_json", "path": None},
        {"request_id": "r", "repeat_index": 1, "kind": "rosbag_mcap", "path": "/nonexist/x.mcap"},
        {"request_id": "r", "repeat_index": 2, "kind": "result_json", "path": str(present)},
    ]
    staging = tmp_path / "artifacts"
    summary = publish_glue.stage_uploads(uploads, staging)
    assert summary == {"staged": 1, "skipped": 2}
    assert _staged_relpaths(staging) == {"r/repeat-2/result_json.json"}


def test_stage_artifacts_empty_report_is_empty_dir(tmp_path):
    # No matrix -> no uploads -> an empty (created) staging dir, no error.
    staging = tmp_path / "artifacts"
    assert publish_glue.stage_artifacts({}, staging) == {"staged": 0, "skipped": 0}
    assert staging.is_dir()
    assert _staged_relpaths(staging) == set()


# --------------------------------------------------------------------------- #
# (B3) stage-artifacts PRE-CLEAN (p5c9 T1) — a previous run's tree must not ride
# --------------------------------------------------------------------------- #
# The self-hosted runner does not clean its workspace between jobs, so the staging
# dir survives from push to push. Before p5c9 the stager only mkdir(exist_ok=True)'d
# it, so ``upload-artifact`` re-uploaded whatever was left: p5c8 live measured
# staged=6 but 17->23 files uploaded, 92.9% of a GREEN PR's zip being off-policy
# bytes — mostly the PREVIOUS push's failure recordings.
#
# These tests therefore run against a PRE-POPULATED staging dir. A test that only
# stages into an empty tmp dir cannot see this defect at all (G-35) — every
# assertion below is paired with a planted byte that must disappear.


def _plant_previous_run_tree(staging: Path) -> dict[str, Path]:
    """Plant a PREVIOUS run's staging tree (the p5c8 live shape) + a stray file.

    Three top-level entries: an envelope dir the current run does NOT write, an
    envelope dir the current run DOES write into (so overwriting files is not
    enough — the extra repeat must go too), and a loose file.
    """
    planted = {
        "other_envelope": staging / "req-previous/repeat-0/rosbag_mcap.mcap",
        "same_envelope": staging / "req-a/repeat-9/recording_mp4.mp4",
        "loose_file": staging / "leftover-report.json",
    }
    for path in planted.values():
        path.parent.mkdir(parents=True, exist_ok=True)
    planted["other_envelope"].write_bytes(b"\x00previous push failure recording")
    planted["same_envelope"].write_bytes(b"\x00previous push failure video")
    planted["loose_file"].write_text('{"from": "previous run"}', encoding="utf-8")
    return planted


def test_stage_artifacts_clears_a_previous_runs_tree(tmp_path):
    report = _report_with_artifacts(tmp_path)
    staging = tmp_path / "artifacts"
    planted = _plant_previous_run_tree(staging)
    summary = publish_glue.stage_artifacts(report, staging)
    # (a) every planted stale byte is GONE — including the one under an envelope the
    # current run also writes into.
    for label, path in planted.items():
        assert not path.exists(), label
    assert not (staging / "req-previous").exists()
    # (b) the resulting tree is EXACTLY the manifest's uploads, nothing else.
    assert _staged_relpaths(staging) == {
        "req-a/repeat-0/result_json.json",
        "req-a/repeat-0/rosbag_mcap.mcap",
        "req-a/repeat-2/result_json.json",
    }
    assert summary == {"staged": 3, "skipped": 0}
    # (c) the curation contract still holds through the pre-clean (결정 #1/#2):
    # missing (mp4) / excluded (size-capped mcap) never appear.
    assert not any("recording_mp4" in p for p in _staged_relpaths(staging))
    assert "req-a/repeat-2/rosbag_mcap.mcap" not in _staged_relpaths(staging)


def test_staged_tree_equals_manifest_uploads_after_a_previous_run(tmp_path):
    # The upload-plane invariant, stated against the manifest rather than a literal:
    # what upload-artifact sees == what the manifest declares (QA-recommended form).
    report = _report_with_artifacts(tmp_path)
    staging = tmp_path / "artifacts"
    _plant_previous_run_tree(staging)
    publish_glue.stage_artifacts(report, staging)
    uploads = github.render_artifact_manifest(report)["uploads"]
    expected_envelopes = {publish_glue._fs_safe(u["request_id"]) for u in uploads}
    assert {p.name for p in staging.iterdir()} == expected_envelopes == {"req-a"}
    assert len(_staged_relpaths(staging)) == len(uploads) == 3


def test_relative_staging_dir_is_cleared_like_the_action_invokes_it(tmp_path, monkeypatch):
    # The Action runs ``publish_glue stage-artifacts report.json artifacts`` in the
    # step CWD, i.e. the target arrives RELATIVE — it must still resolve+clear.
    monkeypatch.chdir(tmp_path)
    stale = Path("artifacts/req-previous/repeat-0/rosbag_mcap.mcap")
    stale.parent.mkdir(parents=True)
    stale.write_bytes(b"\x00previous")
    assert publish_glue.stage_uploads([], Path("artifacts")) == {"staged": 0, "skipped": 0}
    assert not stale.exists()
    assert (tmp_path / "artifacts").is_dir()


def test_pre_clean_says_on_stderr_what_it_removed(tmp_path, capsys):
    # Honesty: this defect survived 12 days because staging was silent. The line is
    # also the runtime-plane deployment marker (G-43).
    staging = tmp_path / "artifacts"
    _plant_previous_run_tree(staging)
    publish_glue.stage_artifacts({}, staging)
    assert f"cleared 3 stale entries from {staging.resolve()}" in capsys.readouterr().err


# --- safety guards: the pre-clean must refuse dangerous targets, and refuse them
# --- BEFORE removing anything (each case plants a canary that must survive).
@pytest.mark.parametrize("unsafe", ["/", "/tmp", "/usr", "/home"])
def test_pre_clean_refuses_system_paths(unsafe):
    with pytest.raises(ValueError):
        publish_glue._prepare_staging_dir(Path(unsafe))


def test_pre_clean_refuses_home_and_its_ancestor(tmp_path, monkeypatch):
    fake_home = tmp_path / "home/etri"
    fake_home.mkdir(parents=True)
    canary = fake_home / "do-not-delete.txt"
    canary.write_text("survives", encoding="utf-8")
    monkeypatch.setattr(publish_glue.Path, "home", classmethod(lambda cls: fake_home))
    for unsafe in (fake_home, fake_home.parent):
        with pytest.raises(ValueError):
            publish_glue._prepare_staging_dir(unsafe)
    assert canary.read_text(encoding="utf-8") == "survives"


def test_pre_clean_refuses_a_repository_checkout(tmp_path):
    # This is what catches a stray ``.`` — on the runner the step CWD IS the checkout.
    checkout = tmp_path / "cv-infra-user"
    (checkout / ".git").mkdir(parents=True)
    canary = checkout / "verify.yml"
    canary.write_text("name: verify\n", encoding="utf-8")
    with pytest.raises(ValueError):
        publish_glue._prepare_staging_dir(checkout)
    assert canary.read_text(encoding="utf-8") == "name: verify\n"


def test_pre_clean_refuses_a_symlinked_target(tmp_path):
    outside = tmp_path / "outside"
    outside.mkdir()
    canary = outside / "keep.mcap"
    canary.write_bytes(b"outside bytes")
    link = tmp_path / "artifacts"
    link.symlink_to(outside, target_is_directory=True)
    with pytest.raises(ValueError):
        publish_glue._prepare_staging_dir(link)
    assert canary.read_bytes() == b"outside bytes"


def test_pre_clean_unlinks_symlinked_entries_without_following_them(tmp_path):
    # A symlink INSIDE the staging dir is removed as a link; its referent (outside
    # the staging dir) is never touched.
    outside = tmp_path / "outside"
    outside.mkdir()
    keep = outside / "keep.mcap"
    keep.write_bytes(b"outside bytes")
    staging = tmp_path / "artifacts"
    staging.mkdir()
    (staging / "link-to-dir").symlink_to(outside, target_is_directory=True)
    (staging / "link-to-file").symlink_to(keep)
    assert publish_glue.stage_uploads([], staging) == {"staged": 0, "skipped": 0}
    assert list(staging.iterdir()) == []
    assert not (staging / "link-to-dir").is_symlink()
    assert outside.is_dir() and keep.read_bytes() == b"outside bytes"


# --------------------------------------------------------------------------- #
# (C) M1 error object -> ::error file,line,col:: annotation (D-L 1:1)
# --------------------------------------------------------------------------- #
def _sample_error() -> ContractError:
    return ContractError(
        field_path="requests[0].scenario",
        expected="an existing scenario file",
        got="'missing.yaml'",
        example="scenario: scenarios/a.yaml",
        source_path="scenarios/a.yaml",
        source_line=3,
        source_col=15,
    )


def test_annotation_maps_source_fields_one_to_one():
    entry = _sample_error().to_annotation_dict()
    line = publish_glue.render_annotation(entry)
    # 1:1: source_path->file, source_line->line, source_col->col (D-L).
    assert line.startswith("::error ")
    assert "file=scenarios/a.yaml" in line
    assert "line=3" in line
    assert "col=15" in line
    # message = the M1 friendly prose (field path + expected + got), not invented.
    assert "requests[0].scenario" in line
    assert "expected an existing scenario file" in line
    assert "got 'missing.yaml'" in line


def test_annotation_without_location_is_bare_error():
    line = publish_glue.render_annotation({"field_path": "doc", "expected": "a mapping"})
    assert line.startswith("::error::")  # no file/line/col properties


def test_annotation_line_without_col_omits_col():
    entry = {"field_path": "x", "expected": "y", "source_path": "s.yaml", "source_line": 4}
    line = publish_glue.render_annotation(entry)
    assert "line=4" in line
    assert "col=" not in line


def test_annotation_escapes_message_and_property():
    entry = {
        "field_path": "a",
        "expected": "x\ny",  # newline in message
        "got": "50%",  # percent in message
        "source_path": "a:b,c.yaml",  # ':' and ',' in a property value
        "source_line": 1,
        "source_col": 2,
    }
    line = publish_glue.render_annotation(entry)
    assert "%0A" in line  # newline data-escaped
    assert "%25" in line  # percent data-escaped
    assert "file=a%3Ab%2Cc.yaml" in line  # ':' -> %3A and ',' -> %2C in the property


def test_render_annotations_accepts_list_and_422_body():
    entry = _sample_error().to_annotation_dict()
    one = [publish_glue.render_annotation(entry)]
    assert publish_glue.render_annotations([entry]) == one
    assert publish_glue.render_annotations({"errors": [entry]}) == one
    assert publish_glue.render_annotations({"detail": {"errors": [entry]}}) == one  # M3 422 shape
    assert publish_glue.render_annotations([]) == []
    assert publish_glue.render_annotations({"nonsense": 1}) == []


# --------------------------------------------------------------------------- #
# (D) reusable workflow verify.yml — workflow_call / D-H / security
# --------------------------------------------------------------------------- #
def test_verify_workflow_is_workflow_call_with_inputs_contract():
    doc = _load(_VERIFY_WORKFLOW)
    trigger = _trigger(doc)
    assert "workflow_call" in trigger  # reusable (D-H)
    inputs = trigger["workflow_call"]["inputs"]
    assert inputs["sut_image"]["required"] is True  # ref-only, required (§7.2)
    for name in ("scenarios", "runner_label", "scenarios_artifact", "api", "timeout_s"):
        assert name in inputs


def test_verify_workflow_permissions_are_least_privilege():
    doc = _load(_VERIFY_WORKFLOW)
    assert doc["permissions"] == {
        "checks": "write",
        "pull-requests": "write",
        "contents": "read",
    }


def test_verify_workflow_job_runs_self_hosted_by_label():
    doc = _load(_VERIFY_WORKFLOW)
    job = doc["jobs"]["verify"]
    assert job["runs-on"] == ["self-hosted", "${{ inputs.runner_label }}"]


def test_verify_workflow_submits_ci_cd_and_waits():
    doc = _load(_VERIFY_WORKFLOW)
    runs = _runs(doc["jobs"]["verify"]["steps"])
    assert "cv-infra submit" in runs
    assert "--trigger-source ci-cd" in runs
    assert "--wait" in runs


def test_verify_workflow_publishes_via_stock_actions():
    doc = _load(_VERIFY_WORKFLOW)
    uses = _uses(doc["jobs"]["verify"]["steps"])
    families = {u.split("@", 1)[0] for u in uses}
    assert "actions/github-script" in families  # Checks/Comments API client
    assert "actions/upload-artifact" in families  # artifact publish
    assert "actions/download-artifact" in families  # scenarios (no PR-head checkout)


def test_verify_workflow_gpu_job_has_no_pr_head_checkout():
    # R10/D-J: the GPU job must NOT check out PR-head source (SUT is ref-only).
    doc = _load(_VERIFY_WORKFLOW)
    families = {u.split("@", 1)[0] for u in _uses(doc["jobs"]["verify"]["steps"])}
    assert "actions/checkout" not in families
    assert "docker/build-push-action" not in families  # no SUT build on the GPU box


# --------------------------------------------------------------------------- #
# (E) composite action actions/verify/action.yml
# --------------------------------------------------------------------------- #
def test_composite_action_shape_and_inputs():
    doc = _load(_VERIFY_ACTION)
    assert doc["runs"]["using"] == "composite"
    inputs = doc["inputs"]
    assert inputs["sut_image"]["required"] is True  # ref-only, required
    for name in ("scenarios", "api", "timeout_s", "github_token"):
        assert name in inputs


def test_composite_action_submits_ci_cd_and_reflects_verdict():
    doc = _load(_VERIFY_ACTION)
    steps = doc["runs"]["steps"]
    runs = _runs(steps)
    assert "cv-infra submit" in runs
    assert "--trigger-source ci-cd" in runs
    assert "--wait" in runs
    # the verdict rides back out as the composite's step status (M8-D11).
    assert "exit ${{ steps.verify.outputs.code }}" in runs


def test_composite_action_has_no_pr_head_checkout():
    doc = _load(_VERIFY_ACTION)
    families = {u.split("@", 1)[0] for u in _uses(doc["runs"]["steps"])}
    assert "actions/checkout" not in families
    assert "docker/build-push-action" not in families


# --------------------------------------------------------------------------- #
# (F) cross-file pin / security invariants (§2-7, R10/D-J)
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("path", [_VERIFY_WORKFLOW, _VERIFY_ACTION])
def test_every_action_use_is_sha_pinned(path):
    doc = _load(path)
    steps = doc["jobs"]["verify"]["steps"] if "jobs" in doc else doc["runs"]["steps"]
    uses = _uses(steps)
    assert uses  # the file DOES pull stock actions (non-vacuous)
    for ref in uses:
        assert _SHA_PINNED.match(ref), f"{ref} is not pinned to an immutable 40-hex SHA"


@pytest.mark.parametrize("path", [_VERIFY_WORKFLOW, _VERIFY_ACTION])
def test_no_floating_tag_and_no_pull_request_target(path):
    text = path.read_text(encoding="utf-8")
    assert not _FLOATING.search(text)  # no @main/@master/@latest (§2-7)
    assert "pull_request_target" not in text  # R10/D-J: base-secret fork-PR RCE pattern


@pytest.mark.parametrize("path", [_VERIFY_WORKFLOW, _VERIFY_ACTION])
def test_stages_curated_artifacts_then_uploads_staging_dir(path):
    # P5-02 완결: both entry topologies stage the manifest uploads[] via the tested
    # Python glue, then upload artifacts/ ALONGSIDE report.json + payloads/ (retained).
    doc = _load(path)
    runs = _runs(_steps(doc))
    assert "publish_glue stage-artifacts report.json artifacts" in runs
    upload_path = _upload_step(doc)["with"]["path"]
    assert "report.json" in upload_path  # report retained
    assert "payloads/" in upload_path  # Check/comment/manifest payloads retained
    assert "artifacts/" in upload_path  # staged MCAP/mp4/result.json now uploaded


@pytest.mark.parametrize("path", [_VERIFY_WORKFLOW, _VERIFY_ACTION])
def test_stage_step_gated_on_have_report(path):
    doc = _load(path)
    stage = next(
        s for s in _steps(doc) if isinstance(s, dict) and "stage-artifacts" in str(s.get("run", ""))
    )
    assert stage["if"] == "always() && steps.verify.outputs.have_report == 'true'"


# --------------------------------------------------------------------------- #
# (G) platform CI 2-tier — self-test tier, 0 consumer dependency (NFR-SELFTEST-001)
# --------------------------------------------------------------------------- #
def test_platform_ci_has_a_self_test_tier_on_the_labelled_gpu_runner():
    job = _load(_PLATFORM_CI)["jobs"]["selftest"]
    assert job["runs-on"] == ["self-hosted", "cv-infra-gpu"]  # label, never an IP (LOCKED §16)
    assert "cv-infra selftest" in _runs(job["steps"])
    assert "--trigger-source ci-cd" in _runs(job["steps"])  # CI provenance (REQ-INTAKE-003)


def test_self_test_tier_reads_no_consumer_repo_or_asset():
    """NFR-SELFTEST-001 / boundary rule ③: the tier checks out THIS repo only,
    passes no scenario/envelope/SUT argument, and never pulls a consumer image."""
    job = _load(_PLATFORM_CI)["jobs"]["selftest"]
    runs = _runs(job["steps"])
    for checkout in (s for s in job["steps"] if "checkout" in str(s.get("uses", ""))):
        assert "repository" not in (checkout.get("with") or {})  # = the current repo
    # the selftest invocation carries NO positional input at all
    invocation = next(line for line in runs.splitlines() if "cv-infra selftest" in line)
    assert not re.search(r"cv-infra selftest\s+[^-]", invocation)
    assert "carter" not in _PLATFORM_CI.read_text(encoding="utf-8")  # no consumer fixture ref


def test_self_test_tier_is_gated_and_bakes_no_image_ref():
    """It has never run (measured 2026-08-19: 3 appearances in 41 CI runs, all
    `skipped`). The stub SUT image itself EXISTS since p5c15 — what is still
    missing is the operator-set configuration. The gate must be a repo VARIABLE
    and the image ref must arrive from it — a literal baked here would be exactly
    the guess the CLI refuses to make (FU-10 / NFR-SELFTEST-001)."""
    job = _load(_PLATFORM_CI)["jobs"]["selftest"]
    assert job["if"] == "${{ vars.CV_SELFTEST_ENABLED == 'true' }}"
    step = next(s for s in job["steps"] if "cv-infra selftest" in str(s.get("run", "")))
    assert step["env"]["CV_SELFTEST_SUT_IMAGE"] == "${{ vars.CV_SELFTEST_SUT_IMAGE }}"
    assert job["timeout-minutes"]  # a wedged GPU job never holds CI open forever


# --- the self-test tier's TIMEOUT LAYERING (p5c20 ⑥) ------------------------- #
# The job comment claims "a wedged round-trip is ended by the CLI (honest exit 3)
# rather than by a job cancellation". Until p5c20 that was PROSE ONLY, and it was
# FALSE: `timeout-minutes: 35` vs `--timeout 1800` (30m) compared two numbers on
# different clocks — the job clock also carries setup, which the CLI's does not.
# Run 32342573510 (2026-08-20) paid for it: setup 7m46s + a 27m21s queue wait hit
# the 35m job cap at 07:43:09Z, 47 s BEFORE the envelope completed successfully.
# The gate went red on a healthy product. So the relation is asserted here.

#: Setup measured on THIS tier (run 32342573510): job start -> selftest step start
#: = 7m46s (`Set up job` 1m38s + checkout/uv ~2s + `uv sync --frozen` 6m05s). One
#: observation, used as a floor for the setup budget — not an NFR (CLAUDE §2-4).
_MEASURED_SETUP_MINUTES = 7 + 46 / 60


def _layering_holds(job_cap_min, step_cap_min, cli_timeout_s) -> bool:
    """The two relations the comment promises, as one predicate.

    (1) the CLI's own budget expires INSIDE its step  -> the CLI reports, not the
        runner; (2) the job cap leaves the measured setup room ON TOP of that step
        -> setup never eats the round-trip's budget (the p5c20 ⑥ defect).
    """
    if step_cap_min is None or job_cap_min is None:
        return False
    return (
        step_cap_min * 60 > cli_timeout_s and job_cap_min - step_cap_min >= _MEASURED_SETUP_MINUTES
    )


def test_self_test_timeout_layering_lets_the_cli_end_a_wedged_round_trip():
    """★ The invariant, read off the FILE instead of asserted in a comment."""
    job = _load(_PLATFORM_CI)["jobs"]["selftest"]
    step = next(s for s in job["steps"] if "cv-infra selftest" in str(s.get("run", "")))
    match = re.search(r"--timeout\s+(\d+)", str(step["run"]))
    assert match, "the selftest invocation carries no --timeout to layer against"
    cli_timeout_s = int(match.group(1))

    job_cap = job.get("timeout-minutes")
    step_cap = step.get("timeout-minutes")
    assert _layering_holds(job_cap, step_cap, cli_timeout_s), (
        f"timeout layering broken: job={job_cap}m step={step_cap}m cli={cli_timeout_s}s "
        f"(need step*60 > cli AND job - step >= {_MEASURED_SETUP_MINUTES:.2f}m of setup)"
    )


def test_the_layering_predicate_rejects_the_configuration_that_actually_failed():
    """무장 실증 (G-35): the predicate above must be able to say NO.

    The historical configuration — one job cap, no step cap — is fed back in; it
    is rejected, and so is every near-miss (step cap at/below the CLI budget, job
    cap without room for the measured setup). Without this the assertion above
    could be satisfied by any pair of numbers.
    """
    assert not _layering_holds(35, None, 1800)  # ← the run that was cancelled
    assert not _layering_holds(45, 30, 1800)  # step cap == CLI budget: a tie loses
    assert not _layering_holds(45, 29, 1800)  # step cap below it: runner reports
    assert not _layering_holds(35, 31, 1800)  # no room for the 7m46s setup
    assert _layering_holds(45, 31, 1800)  # the shipped configuration


def test_platform_ci_actions_are_sha_pinned():
    doc = _load(_PLATFORM_CI)
    uses = [u for job in doc["jobs"].values() for u in _uses(job["steps"])]
    assert uses
    for ref in uses:
        assert _SHA_PINNED.match(ref), f"{ref} is not pinned to an immutable 40-hex SHA"
    assert not _FLOATING.search(_PLATFORM_CI.read_text(encoding="utf-8"))


@pytest.mark.parametrize("path", [_VERIFY_WORKFLOW, _VERIFY_ACTION])
def test_no_consent_or_secret_value_injection(path):
    # NEG-2: consent VALUES are never baked here — only env-key names are forwarded.
    # The predicate is IMPORTED from the NEG-2 suite, never re-stated (p5c11 F-1 /
    # G-56): this file used to carry its own value enumeration
    # (`Y|yes|true|1`), which was narrower than the boot guard's pure truthiness —
    # `ACCEPT_EULA=accepted-by-operator` sailed through both. Two copies of a
    # negative pattern drift, and the difference between them is the hole.
    text = path.read_text(encoding="utf-8")
    assert baked_consent_bindings(text) == []


# --------------------------------------------------------------------------- #
# (H) CI INPUT plane — workspace pre-clean + residue guard (p7c3 T7, v1.2.1)
# --------------------------------------------------------------------------- #
# The GPU job does NO `actions/checkout` (R10) and checkout was the only step that
# ever emptied its workspace, so `download-artifact` MERGED onto the previous run's
# tree: consumer PR #4 (run 33230008911) delivered 4 scenarios, `scenarios/*.yaml`
# selected 5, and the 5th was a document that push had DELETED (same identity key
# as the run before). The repair is two steps in the workflow, and it is TESTED
# THE WAY IT SHIPS: the assertions below both (a) read the workflow text and
# (b) EXECUTE the shell it embeds against a planted residue tree, because a repair
# that only exists in prose is not a repair (G-100). YAML-plane, not CLI-plane, on
# purpose — the release tag moves only this plane (G-43).

_CLEAN_STEP_ID = "clean-workspace"
_GUARD_STEP_ID = "input-guard"
#: the file the PR had removed and that got verified anyway (the actual residue).
_RESIDUE = "scenarios/nova_carter_warehouse_obstacles_random.yaml"
_DELIVERED = [
    "scenarios/nova_carter_warehouse_goal.yaml",
    "scenarios/nova_carter_warehouse_goal_b.yaml",
    "scenarios/nova_carter_warehouse_goal_random.yaml",
    "scenarios/nova_carter_warehouse_obstacles_low_random.yaml",
]


def _step_index(doc: dict[str, Any], predicate) -> int:
    for i, step in enumerate(_steps(doc)):
        if predicate(step):
            return i
    raise AssertionError("no step matched")


def _by_id(step_id: str):
    return lambda s: s.get("id") == step_id


def _by_uses(family: str):
    return lambda s: str(s.get("uses", "")).startswith(family)


def _script(doc: dict[str, Any], step_id: str) -> str:
    return next(s for s in _steps(doc) if s.get("id") == step_id)["run"]


def _plant(ws: Path, relpaths: list[str]) -> None:
    for rel in relpaths:
        path = ws / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(f"# {rel}\n", encoding="utf-8")


def _runner_layout(tmp_path: Path) -> tuple[Path, Path]:
    """The measured runner layout: `<runner>/_work/{_temp,<repo>/<repo>}`."""
    work = tmp_path / "runner" / "_work"
    ws = work / "cv-infra-user" / "cv-infra-user"
    temp = work / "_temp"
    ws.mkdir(parents=True)
    temp.mkdir(parents=True)
    return ws, temp


def _workflow_input_default(name: str) -> str:
    """Read an input default out of the workflow itself — the test must not carry a
    second copy of the consumer contract (G-25/G-56)."""
    return _trigger(_load(_VERIFY_WORKFLOW))["workflow_call"]["inputs"][name]["default"]


def _exec(script: str, ws: Path, temp: Path, *, cwd: Path | None = None, **overrides):
    env = {
        "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
        "HOME": str(temp.parent.parent),  # <runner>, never the developer's $HOME
        "GITHUB_WORKSPACE": str(ws),
        "RUNNER_TEMP": str(temp),
        "GITHUB_RUN_ID": "33230008911",
        "GITHUB_RUN_ATTEMPT": "1",
        # what the workflow's `env:` block binds for the guard step, at their
        # DECLARED defaults (the consumer-facing glob included).
        "CV_SCENARIOS": _workflow_input_default("scenarios"),
        "CV_SCENARIOS_ARTIFACT": _workflow_input_default("scenarios_artifact"),
        # the runner file a step writes its outputs into (`echo k=v >> $GITHUB_OUTPUT`).
        "GITHUB_OUTPUT": str(temp / "github-output.txt"),
    }
    env.update({k: v for k, v in overrides.items() if v is not None})
    for key, value in overrides.items():
        if value is None:
            env.pop(key, None)
    return subprocess.run(
        ["bash", "-c", script],
        capture_output=True,
        text=True,
        env=env,
        cwd=str(cwd or ws if (cwd or ws).is_dir() else temp),
    )


# --- (H1) workflow TEXT: the clean happens first, and only in the workspace ----
def test_workspace_is_cleaned_before_any_input_is_downloaded():
    doc = _load(_VERIFY_WORKFLOW)
    clean = _step_index(doc, _by_id(_CLEAN_STEP_ID))
    download = _step_index(doc, _by_uses("actions/download-artifact"))
    submit = _step_index(doc, _by_id("verify"))
    assert clean == 0, "the clean must be the FIRST step (nothing may precede the emptying)"
    assert clean < download < submit


def test_workspace_clean_is_scoped_to_the_workspace_and_refuses_by_control_flow():
    script = _script(_load(_VERIFY_WORKFLOW), _CLEAN_STEP_ID)
    # (a) the deletion target comes ONLY from $GITHUB_WORKSPACE — never an input,
    # never a literal path.
    deletions = [line.strip() for line in script.splitlines() if "-delete" in line]
    assert deletions == ['find "${ws}" -mindepth 1 -delete'], deletions
    assert 'ws="${GITHUB_WORKSPACE:-}"' in script
    assert "${{" not in script  # no expression interpolation into a deleting shell
    # (b) each refusal is control flow, not an echo (G-93).
    assert script.count("die ") >= 6
    assert 'die() { echo "::error::' in script
    assert "exit 2; }" in script
    # (c) the post-condition that makes the clean provable, not merely attempted.
    assert 'left="$(find "${ws}" -mindepth 1 | wc -l)"' in script
    assert '[ "${left}" -eq 0 ] || die' in script


def test_residue_guard_sits_between_download_and_submit_and_keeps_the_glob_contract():
    doc = _load(_VERIFY_WORKFLOW)
    download = _step_index(doc, _by_uses("actions/download-artifact"))
    guard = _step_index(doc, _by_id(_GUARD_STEP_ID))
    submit = _step_index(doc, _by_id("verify"))
    assert download < guard < submit
    # the consumer contract is untouched: same input, same default glob, and the
    # guard reads the SAME input the submit step expands (no second source).
    inputs = _trigger(doc)["workflow_call"]["inputs"]
    assert inputs["scenarios"]["default"] == "scenarios/*.yaml"
    step = next(s for s in _steps(doc) if s.get("id") == _GUARD_STEP_ID)
    assert step["env"]["CV_SCENARIOS"] == "${{ inputs.scenarios }}"
    assert "cv-infra submit ${{ inputs.scenarios }}" in _script(doc, "verify")


# --- (H2) the shell ITSELF, executed against a planted residue tree ------------
def test_the_clean_script_removes_the_previous_runs_residue_and_says_what_it_removed(
    tmp_path,
):
    ws, temp = _runner_layout(tmp_path)
    _plant(ws, [_RESIDUE, "report.json", "payloads/check-run.json", "artifacts/req-a/x.mcap"])
    result = _exec(_script(_load(_VERIFY_WORKFLOW), _CLEAN_STEP_ID), ws, temp)
    assert result.returncode == 0, result.stderr
    assert list(ws.iterdir()) == []
    # G-26: the operator must be able to READ what disappeared.
    assert "4 top-level entries BEFORE:" in result.stdout
    assert "- d scenarios" in result.stdout
    assert "- f report.json" in result.stdout
    assert "workspace AFTER = 0 entries" in result.stdout
    # the attestation the guard consumes (and nothing written into the workspace).
    assert (temp / "cv-infra-workspace-clean.stamp").read_text().split() == [
        "33230008911",
        "1",
        str(ws),
    ]


def test_the_clean_script_never_reaches_outside_the_workspace(tmp_path):
    ws, temp = _runner_layout(tmp_path)
    canaries = [ws.parent / "sibling-checkout.txt", temp / "other-job.txt"]
    for canary in canaries:
        canary.write_text("must survive", encoding="utf-8")
    _plant(ws, [_RESIDUE])
    result = _exec(_script(_load(_VERIFY_WORKFLOW), _CLEAN_STEP_ID), ws, temp)
    assert result.returncode == 0, result.stderr
    assert all(c.exists() for c in canaries)


def test_the_clean_script_unlinks_a_symlinked_entry_without_following_it(tmp_path):
    ws, temp = _runner_layout(tmp_path)
    outside = tmp_path / "production-store"
    outside.mkdir()
    (outside / "cv-infra.sqlite3").write_text("production", encoding="utf-8")
    (ws / "link-to-store").symlink_to(outside, target_is_directory=True)
    result = _exec(_script(_load(_VERIFY_WORKFLOW), _CLEAN_STEP_ID), ws, temp)
    assert result.returncode == 0, result.stderr
    assert not (ws / "link-to-store").exists()
    assert (outside / "cv-infra.sqlite3").read_text() == "production"  # referent untouched


@pytest.mark.parametrize(
    "label,overrides",
    [
        ("unset", {"GITHUB_WORKSPACE": None}),
        ("empty", {"GITHUB_WORKSPACE": ""}),
        ("relative", {"GITHUB_WORKSPACE": "workspace"}),
        ("shallow", {"GITHUB_WORKSPACE": "/tmp"}),
        ("root", {"GITHUB_WORKSPACE": "/"}),
    ],
)
def test_the_clean_script_refuses_a_workspace_it_cannot_vouch_for(tmp_path, label, overrides):
    ws, temp = _runner_layout(tmp_path)
    _plant(ws, [_RESIDUE])
    result = _exec(
        _script(_load(_VERIFY_WORKFLOW), _CLEAN_STEP_ID), ws, temp, cwd=temp, **overrides
    )
    assert result.returncode != 0, f"{label} was accepted"
    assert "::error::cv-infra workspace pre-clean:" in result.stdout
    assert (ws / _RESIDUE).exists()  # refused BEFORE deleting anything


def test_the_clean_script_refuses_home_its_ancestor_and_a_path_outside_the_work_root(tmp_path):
    ws, temp = _runner_layout(tmp_path)
    _plant(ws, [_RESIDUE])
    script = _script(_load(_VERIFY_WORKFLOW), _CLEAN_STEP_ID)
    # $HOME itself / an ancestor of $HOME
    for home in (str(ws), str(ws / "nested" / "deeper")):
        result = _exec(script, ws, temp, HOME=home)
        assert result.returncode != 0, home
        assert "HOME" in result.stdout
    # the runner work root itself, and a path outside it (production data lives on
    # the same host — this is the guard that keeps a mis-set workspace off it).
    for target in (str(temp.parent), str(tmp_path / "cv-infra-prod" / "store" / "db")):
        result = _exec(script, ws, temp, GITHUB_WORKSPACE=target, cwd=temp)
        assert result.returncode != 0, target
        assert "work root" in result.stdout
    assert (ws / _RESIDUE).exists()


def test_the_clean_script_is_a_no_op_on_a_fresh_workspace(tmp_path):
    ws, temp = _runner_layout(tmp_path)
    result = _exec(_script(_load(_VERIFY_WORKFLOW), _CLEAN_STEP_ID), ws, temp)
    assert result.returncode == 0, result.stderr
    assert "(already empty)" in result.stdout


# --- (H3) the guard shell, executed ------------------------------------------
def _clean_then_deliver(tmp_path, delivered=None):
    """Run the real clean step, then plant what the download step would deliver."""
    ws, temp = _runner_layout(tmp_path)
    _plant(ws, [_RESIDUE])  # ← yesterday's run, exactly as measured on etri6000
    result = _exec(_script(_load(_VERIFY_WORKFLOW), _CLEAN_STEP_ID), ws, temp)
    assert result.returncode == 0, result.stderr
    _plant(ws, _DELIVERED if delivered is None else delivered)
    return ws, temp


def test_clean_then_download_submits_exactly_what_this_run_delivered(tmp_path):
    """The defect, end to end: the residue is gone and the glob selects only the 4."""
    ws, temp = _clean_then_deliver(tmp_path)
    assert not (ws / _RESIDUE).exists()
    result = _exec(_script(_load(_VERIFY_WORKFLOW), _GUARD_STEP_ID), ws, temp)
    assert result.returncode == 0, result.stdout + result.stderr
    assert "4 scenario file(s) selected, all delivered by this run." in result.stdout
    selected = {line[4:] for line in result.stdout.splitlines() if line.startswith("  + ")}
    assert selected == set(_DELIVERED)


def test_without_the_clean_the_residue_would_ride_and_the_guard_says_so(tmp_path):
    """편측 변이 (G-59): remove ONLY the clean step and the pair must go loud.

    This is the pre-v1.2.1 workflow: download-artifact merges onto the surviving
    tree. The glob then selects 5 documents for a push that delivered 4 — silently,
    before this release. The guard's attestation check is what turns that into a
    stop, so it must fire here and name the missing pre-clean.
    """
    ws, temp = _runner_layout(tmp_path)
    _plant(ws, [_RESIDUE])
    _plant(ws, _DELIVERED)  # the merge, with NO clean before it
    assert len(list((ws / "scenarios").iterdir())) == 5  # ← the defect, reproduced
    result = _exec(_script(_load(_VERIFY_WORKFLOW), _GUARD_STEP_ID), ws, temp)
    assert result.returncode == 2
    assert "no workspace-clean attestation" in result.stdout


def test_the_guard_rejects_an_attestation_from_another_run(tmp_path):
    ws, temp = _clean_then_deliver(tmp_path)
    result = _exec(_script(_load(_VERIFY_WORKFLOW), _GUARD_STEP_ID), ws, temp, GITHUB_RUN_ID="1")
    assert result.returncode == 2
    assert "attestation is from another run/workspace" in result.stdout


def test_the_guard_rejects_a_glob_that_selects_nothing(tmp_path):
    ws, temp = _clean_then_deliver(tmp_path, delivered=["inputs/goal.yaml"])
    result = _exec(_script(_load(_VERIFY_WORKFLOW), _GUARD_STEP_ID), ws, temp)
    assert result.returncode == 2
    # the message must point at the delivery, not at a stack trace (NFR-INTAKE-001)
    assert "matches no file at 'scenarios/*.yaml'" in result.stdout
    assert "  - inputs/goal.yaml" in result.stdout  # what WAS delivered, listed


def test_the_guard_rejects_a_selection_from_outside_the_delivered_tree(tmp_path):
    ws, temp = _clean_then_deliver(tmp_path)
    outside = tmp_path / "elsewhere"
    outside.mkdir()
    (outside / "smuggled.yaml").write_text("# not from this artifact\n", encoding="utf-8")
    result = _exec(
        _script(_load(_VERIFY_WORKFLOW), _GUARD_STEP_ID),
        ws,
        temp,
        CV_SCENARIOS=f"{outside}/*.yaml",
    )
    assert result.returncode == 2
    assert "is NOT one of this run's delivered inputs" in result.stdout


# --------------------------------------------------------------------------- #
# (I) CI INPUT VISIBILITY — staging into the deployment's root (go2 C6d, AR-33)
# --------------------------------------------------------------------------- #
# (H) proved WHAT the run delivers. This section is about WHERE it is readable
# from. The CLI admits the request on the runner; the orchestrator RE-ADMITS it in
# its own container, and a request that carries a ride-along file next to its
# scenario (`sut.locomotion_policy`, a `module:Class` oracle) is resolved against
# the scenario's directory THERE. That container does not mount the runner's
# `_work` tree, so the go2 consumer PR died at admit with "does not exist" —
# 5 files delivered, guarded, and invisible (2026-09-01, job 99702862827).
#
# The repair copies the guarded tree into `${CV_OUT_DIR}/ci-inputs/${RUN_ID}/`
# (a root both planes see at an identical host path) and submits from there,
# then gives the directory back. It is tested THE WAY IT SHIPS — the shell the
# workflow embeds is EXECUTED against a planted runner layout, including the
# submit step, whose `cv-infra` is stubbed so the test can read the CWD and the
# argv the CLI would have received (a repair that only exists in prose is not a
# repair — G-100). YAML plane on purpose: the release tag moves only this plane
# and the runtime `cv_infra` package does not travel with it (G-43).

_STAGE_STEP_ID = "stage-inputs"
_CLEANUP_STEP_ID = "cleanup-inputs"
#: The go2 delivery, as measured on the failing consumer run: two scenarios, two
#: scenario-adjacent oracle modules, and the locomotion policy (D2) — the second
#: SUT artifact, which travels in the REQUEST and not in the image.
_GO2_DELIVERED = [
    "scenarios/go2_t0_smoke.yaml",
    "scenarios/go2_ta_nav_random.yaml",
    "scenarios/hold_near_goal.py",
    "scenarios/upright.py",
    "scenarios/policy.pt",
]
_EXPR = re.compile(r"\$\{\{\s*([^}]+?)\s*\}\}")


def _interpolate(script: str) -> str:
    """Resolve the ``${{ inputs.X }}`` expressions GitHub substitutes before the
    shell ever runs — with the workflow's OWN declared defaults, so the test keeps
    no second copy of the consumer contract (G-25/G-56)."""

    def one(match: re.Match[str]) -> str:
        expr = match.group(1)
        assert expr.startswith("inputs."), f"unresolvable expression in a run: {expr}"
        return str(_workflow_input_default(expr.split(".", 1)[1]))

    return _EXPR.sub(one, script)


def _outputs(temp: Path) -> dict[str, str]:
    """The `$GITHUB_OUTPUT` file a step just wrote, parsed."""
    path = temp / "github-output.txt"
    if not path.exists():
        return {}
    return dict(
        line.split("=", 1) for line in path.read_text(encoding="utf-8").splitlines() if "=" in line
    )


def _guarded_delivery(tmp_path: Path, delivered: list[str] | None = None) -> tuple[Path, Path]:
    """clean -> deliver -> guard, all three as the workflow runs them."""
    ws, temp = _runner_layout(tmp_path)
    clean = _exec(_script(_load(_VERIFY_WORKFLOW), _CLEAN_STEP_ID), ws, temp)
    assert clean.returncode == 0, clean.stderr
    _plant(ws, _GO2_DELIVERED if delivered is None else delivered)
    guard = _exec(_script(_load(_VERIFY_WORKFLOW), _GUARD_STEP_ID), ws, temp)
    assert guard.returncode == 0, guard.stdout + guard.stderr
    return ws, temp


def _out_root(tmp_path: Path) -> Path:
    """A stand-in for the deployment's job-artifact root, with a job directory and
    another run's staged tree already in it — the canaries every deletion below
    must leave alone."""
    out = tmp_path / "cv-infra-prod" / "out"
    (out / "cvj-env-b56d38370060-r0" / "result").mkdir(parents=True)
    (out / "cvj-env-b56d38370060-r0" / "result" / "result.json").write_text("{}", encoding="utf-8")
    (out / "ci-inputs" / "99999999999" / "scenarios").mkdir(parents=True)
    (out / "ci-inputs" / "99999999999" / "scenarios" / "other.yaml").write_text(
        "# another run\n", encoding="utf-8"
    )
    return out


def _canaries(out: Path) -> dict[str, Path]:
    return {
        "job artifact": out / "cvj-env-b56d38370060-r0" / "result" / "result.json",
        "another run's inputs": out / "ci-inputs" / "99999999999" / "scenarios" / "other.yaml",
    }


def _stage(ws: Path, temp: Path, **overrides):
    return _exec(_script(_load(_VERIFY_WORKFLOW), _STAGE_STEP_ID), ws, temp, **overrides)


def _cleanup(ws: Path, temp: Path, **overrides):
    return _exec(_script(_load(_VERIFY_WORKFLOW), _CLEANUP_STEP_ID), ws, temp, **overrides)


def _relfiles(root: Path) -> set[str]:
    return {p.relative_to(root).as_posix() for p in root.rglob("*") if p.is_file()}


# --- (I1) workflow TEXT: where the two new steps sit, and what they may read ---
def test_staging_sits_between_the_guard_and_submit_and_cleanup_always_runs_last():
    doc = _load(_VERIFY_WORKFLOW)
    guard = _step_index(doc, _by_id(_GUARD_STEP_ID))
    stage = _step_index(doc, _by_id(_STAGE_STEP_ID))
    submit = _step_index(doc, _by_id("verify"))
    cleanup = _step_index(doc, _by_id(_CLEANUP_STEP_ID))
    # staging may only ever act on a tree the guard already vouched for.
    assert guard < stage < submit < cleanup
    steps = _steps(doc)
    # the cleanup runs on every outcome, and only the verdict follows it.
    assert steps[cleanup]["if"] == "always()"
    assert _runs(steps[cleanup + 1 :]).strip() == "exit ${{ steps.verify.outputs.code }}"


def test_the_staging_and_cleanup_shells_derive_their_target_and_refuse_by_control_flow():
    doc = _load(_VERIFY_WORKFLOW)
    stage = _script(doc, _STAGE_STEP_ID)
    cleanup = _script(doc, _CLEANUP_STEP_ID)
    for script in (stage, cleanup):
        # the path is DERIVED from two runner variables — never interpolated from a
        # workflow expression into a shell that deletes (the clean step's rule).
        assert "${{" not in script
        assert 'out="${CV_OUT_DIR:-}"' in script
        assert 'run_id="${GITHUB_RUN_ID:-}"' in script
        assert 'stage="${out}/ci-inputs/${run_id}"' in script
    # the only recursive deletes are inside that one directory.
    deletions = [
        line.strip() for line in (stage + "\n" + cleanup).splitlines() if "-delete" in line
    ]
    assert deletions == [
        'find "${stage}" -mindepth 1 -delete',
        'find "${stage}" -mindepth 1 -delete 2>/dev/null',
    ], deletions
    assert stage.count("die ") >= 8  # G-93: each refusal is control flow, not an echo
    assert 'die() { echo "::error::cv-infra input staging: ' in stage


def test_the_submit_step_reads_the_staged_directory_from_the_staging_step_only():
    doc = _load(_VERIFY_WORKFLOW)
    step = next(s for s in _steps(doc) if s.get("id") == "verify")
    assert step["env"]["CV_SUBMIT_DIR"] == "${{ steps.stage-inputs.outputs.dir }}"
    script = step["run"]
    # the glob keeps its delivered spelling (D-L annotations) — the CWD moves, not
    # the paths (this is also the assertion H asserts from the other side).
    assert 'cd "${submit_dir}" && cv-infra submit ${{ inputs.scenarios }}' in script
    assert 'submit_dir="${CV_SUBMIT_DIR:-}"' in script
    assert '[ -n "${submit_dir}" ] || submit_dir="${ws}"' in script  # unset = pre-AR-33


# --- (I2) the staging shell, executed --------------------------------------- #
def test_staging_copies_the_guarded_tree_with_every_ride_along_next_to_its_scenario(tmp_path):
    ws, temp = _guarded_delivery(tmp_path)
    out = _out_root(tmp_path)
    result = _stage(ws, temp, CV_OUT_DIR=str(out))
    assert result.returncode == 0, result.stdout + result.stderr

    stage = out / "ci-inputs" / "33230008911"
    assert _outputs(temp)["dir"] == str(stage)
    # (a) the staged tree IS the delivered tree — same relative layout, so the
    # policy file and both oracle modules still sit next to the scenarios (the
    # adjacency the loader's stage 5 resolves against).
    assert _relfiles(stage) == set(_GO2_DELIVERED)
    assert (stage / "scenarios" / "policy.pt").read_text(encoding="utf-8") == (
        ws / "scenarios" / "policy.pt"
    ).read_text(encoding="utf-8")
    # (b) a COPY: the workspace the publish steps still run in is untouched.
    assert _relfiles(ws) == set(_GO2_DELIVERED)
    # (c) the operator can read what moved where (G-26).
    assert f"staged 5 delivered file(s) -> {stage}" in result.stdout
    assert "  - scenarios/policy.pt" in result.stdout
    # (d) nothing else in the deployment root was touched.
    for label, canary in _canaries(out).items():
        assert canary.exists(), label


def test_staging_precreates_the_bytecode_cache_the_control_plane_would_own(tmp_path):
    """MEASURED 2026-09-01: admitting a scenario-adjacent oracle makes the control
    plane (root, in its container) write `__pycache__/` into the staged directory,
    and the runner user cannot unlink files inside a root-owned directory. The
    directory is therefore created HERE, by the runner user, so the cleanup can
    still empty it. Without this the residue is permanent — measured on the
    deployment host at `out/_c4c/__pycache__` (root:root 755)."""
    ws, temp = _guarded_delivery(tmp_path)
    out = _out_root(tmp_path)
    assert _stage(ws, temp, CV_OUT_DIR=str(out)).returncode == 0
    cache = out / "ci-inputs" / "33230008911" / "scenarios" / "__pycache__"
    assert cache.is_dir()
    assert os.access(cache, os.W_OK)  # ours, so a root-written .pyc can be removed


def test_staging_reuses_the_run_id_but_never_an_earlier_attempts_tree(tmp_path):
    ws, temp = _guarded_delivery(tmp_path)
    out = _out_root(tmp_path)
    stage = out / "ci-inputs" / "33230008911"
    (stage / "scenarios").mkdir(parents=True)
    (stage / "scenarios" / "attempt-1-leftover.yaml").write_text("# stale\n", encoding="utf-8")
    (stage / "junk.txt").write_text("stale\n", encoding="utf-8")
    result = _stage(ws, temp, CV_OUT_DIR=str(out), GITHUB_RUN_ATTEMPT="2")
    assert result.returncode == 0, result.stdout + result.stderr
    assert "cleared 3 entr(ies) left by an earlier attempt" in result.stdout
    assert _relfiles(stage) == set(_GO2_DELIVERED)


def test_staging_stops_when_the_staged_tree_is_not_what_the_guard_vouched_for(tmp_path):
    """편측 변이 (G-59): the set-equality check is what carries the guard's
    conclusion onto the new root. Plant a file AFTER the guard ran — exactly the
    shape of a residue the guard never saw — and the staging must refuse to submit
    from a tree it cannot attribute to this run."""
    ws, temp = _guarded_delivery(tmp_path)
    out = _out_root(tmp_path)
    _plant(ws, ["scenarios/snuck_in_after_the_guard.yaml"])
    result = _stage(ws, temp, CV_OUT_DIR=str(out))
    assert result.returncode == 2
    assert "::error::cv-infra input staging: the staged tree is not what this run delivered" in (
        result.stdout
    )
    assert "+scenarios/snuck_in_after_the_guard.yaml" in result.stdout  # the diff, shown


def test_staging_stops_without_the_guards_delivered_list(tmp_path):
    ws, temp = _guarded_delivery(tmp_path)
    (temp / "cv-infra-delivered-inputs.txt").unlink()
    result = _stage(ws, temp, CV_OUT_DIR=str(_out_root(tmp_path)))
    assert result.returncode == 2
    assert "the input guard did not run" in result.stdout


@pytest.mark.parametrize(
    "label,overrides,expected",
    [
        ("relative root", {"CV_OUT_DIR": "cv-infra-prod/out"}, "not an absolute path"),
        ("absent root", {"CV_OUT_DIR": "/nonexistent/cv-infra-prod/out"}, "is not a directory"),
        ("run id unset", {"GITHUB_RUN_ID": None}, "GITHUB_RUN_ID is unset/empty"),
        ("run id not numeric", {"GITHUB_RUN_ID": "../../etc"}, "is not numeric"),
    ],
)
def test_staging_refuses_a_target_it_cannot_vouch_for(tmp_path, label, overrides, expected):
    ws, temp = _guarded_delivery(tmp_path)
    out = _out_root(tmp_path)
    result = _stage(ws, temp, **{"CV_OUT_DIR": str(out), **overrides})
    assert result.returncode == 2, label
    assert "::error::cv-infra input staging:" in result.stdout
    assert expected in result.stdout
    assert not (out / "ci-inputs" / "33230008911").exists()  # refused BEFORE creating
    for name, canary in _canaries(out).items():
        assert canary.exists(), name


def test_staging_refuses_a_symlinked_deployment_root(tmp_path):
    ws, temp = _guarded_delivery(tmp_path)
    out = _out_root(tmp_path)
    link = tmp_path / "out-link"
    link.symlink_to(out, target_is_directory=True)
    result = _stage(ws, temp, CV_OUT_DIR=str(link))
    assert result.returncode == 2
    assert "is a symlink" in result.stdout
    assert not (out / "ci-inputs" / "33230008911").exists()


# --- (I3) CV_OUT_DIR unset: the old behaviour, and it says what will break ----
def test_without_cv_out_dir_nothing_is_staged_and_the_warning_names_the_casualty(tmp_path):
    ws, temp = _guarded_delivery(tmp_path)
    result = _stage(ws, temp, CV_OUT_DIR=None)
    assert result.returncode == 0, result.stdout + result.stderr
    warning = next(line for line in result.stdout.splitlines() if line.startswith("::warning"))
    # a silent fallback is what cost the go2 PR a red run with no explanation:
    # the message must name the variable to set AND what fails without it.
    assert "CV_OUT_DIR" in warning
    assert "sut.locomotion_policy" in warning
    assert "exit 2" in warning
    assert _outputs(temp)["dir"] == ""  # -> the submit step falls back to the workspace


# --- (I4) the submit step, executed against a stub CLI ------------------------ #
def _stub_cli(tmp_path: Path) -> tuple[Path, Path]:
    """A `cv-infra` on PATH that records the directory it was invoked from and its
    argv. This is what lets the test read the request's ORACLE/POLICY ANCHOR (the
    scenario's parent dir, i.e. the CWD-resolved glob match) instead of asserting
    it in prose."""
    bin_dir = tmp_path / "stub-bin"
    bin_dir.mkdir()
    log = tmp_path / "cv-infra-invocations.txt"
    stub = bin_dir / "cv-infra"
    stub.write_text(
        "#!/bin/bash\n"
        f'{{ echo "cwd=$(pwd)"; for a in "$@"; do echo "arg=$a"; done; }} >> "{log}"\n'
        'if [ "$1" = "submit" ]; then echo "env-stub00000"; fi\n'
        "exit 0\n",
        encoding="utf-8",
    )
    stub.chmod(0o755)
    return bin_dir, log


def _submit(ws: Path, temp: Path, bin_dir: Path, **overrides):
    script = _interpolate(_script(_load(_VERIFY_WORKFLOW), "verify"))
    return _exec(
        script,
        ws,
        temp,
        PATH=f"{bin_dir}:{os.environ.get('PATH', '/usr/bin:/bin')}",
        **overrides,
    )


def test_submit_runs_from_the_staged_root_with_the_delivered_spelling(tmp_path):
    """★ The repair, end to end: same glob, same spelling, different anchor.

    The scenario paths the CLI receives stay consumer-repo-relative (so an exit-2
    annotation still lands on the PR diff — D-L), while the directory they resolve
    against is now one the control plane can read (AR-33).
    """
    ws, temp = _guarded_delivery(tmp_path)
    out = _out_root(tmp_path)
    assert _stage(ws, temp, CV_OUT_DIR=str(out)).returncode == 0
    stage = out / "ci-inputs" / "33230008911"

    bin_dir, log = _stub_cli(tmp_path)
    result = _submit(ws, temp, bin_dir, CV_SUBMIT_DIR=str(stage))
    assert result.returncode == 0, result.stdout + result.stderr

    calls = log.read_text(encoding="utf-8").splitlines()
    submit_cwd = calls[0]
    args = [line[4:] for line in calls if line.startswith("arg=")]
    assert submit_cwd == f"cwd={stage}"  # the anchor moved …
    assert args[:3] == [  # … and nothing else did (glob expanded in the staged root)
        "submit",
        "scenarios/go2_t0_smoke.yaml",
        "scenarios/go2_ta_nav_random.yaml",
    ]
    assert not any(a.startswith("/") for a in args), args  # no absolute path submitted
    assert "--trigger-source" in args and "ci-cd" in args and "--wait" in args
    # the report/publish plane still runs in the workspace, not in the staged tree.
    outputs = _outputs(temp)  # (the staging step's `dir` is in this file too)
    assert (outputs["code"], outputs["have_report"]) == ("0", "true")
    assert (ws / "report.json").exists()
    assert not (stage / "report.json").exists()


def test_submit_falls_back_to_the_workspace_when_nothing_was_staged(tmp_path):
    """The CV_OUT_DIR-unset path, paired with its warning (G-35): the job still
    submits — from the workspace, exactly as before AR-33."""
    ws, temp = _guarded_delivery(tmp_path)
    bin_dir, log = _stub_cli(tmp_path)
    result = _submit(ws, temp, bin_dir, CV_SUBMIT_DIR="")
    assert result.returncode == 0, result.stdout + result.stderr
    assert log.read_text(encoding="utf-8").splitlines()[0] == f"cwd={ws}"


# --- (I5) the cleanup shell, executed ---------------------------------------- #
def test_cleanup_removes_this_runs_staged_tree_and_only_that(tmp_path):
    ws, temp = _guarded_delivery(tmp_path)
    out = _out_root(tmp_path)
    assert _stage(ws, temp, CV_OUT_DIR=str(out)).returncode == 0
    stage = out / "ci-inputs" / "33230008911"
    assert stage.is_dir()

    result = _cleanup(ws, temp, CV_OUT_DIR=str(out))
    assert result.returncode == 0, result.stdout + result.stderr
    assert not stage.exists()
    assert f"{stage} is gone" in result.stdout
    for label, canary in _canaries(out).items():
        assert canary.exists(), label
    assert (out / "ci-inputs").is_dir()  # the shared parent survives for other runs


def test_cleanup_reports_a_leftover_it_cannot_own_without_reddening_the_job(tmp_path):
    """The root-owned `__pycache__` case, simulated with a directory this process
    cannot write into: the residue must be NAMED, and the job must still carry the
    CLI's verdict rather than a housekeeping failure."""
    ws, temp = _guarded_delivery(tmp_path)
    out = _out_root(tmp_path)
    assert _stage(ws, temp, CV_OUT_DIR=str(out)).returncode == 0
    locked = out / "ci-inputs" / "33230008911" / "scenarios" / "__pycache__"
    (locked / "hold_near_goal.cpython-311.pyc").write_bytes(b"\x00pyc")
    locked.chmod(0o500)
    try:
        result = _cleanup(ws, temp, CV_OUT_DIR=str(out))
        assert result.returncode == 0, result.stdout + result.stderr
        assert "::warning title=cv-infra::staged inputs could not be fully removed" in result.stdout
        assert "hold_near_goal.cpython-311.pyc" in result.stdout
    finally:
        locked.chmod(0o700)


@pytest.mark.parametrize(
    "label,overrides",
    [
        ("root unset", {"CV_OUT_DIR": None}),
        ("root relative", {"CV_OUT_DIR": "cv-infra-prod/out"}),
        ("run id unset", {"GITHUB_RUN_ID": None}),
        ("run id not numeric", {"GITHUB_RUN_ID": "../99999999999"}),
    ],
)
def test_cleanup_deletes_nothing_when_it_cannot_name_this_runs_directory(
    tmp_path, label, overrides
):
    ws, temp = _guarded_delivery(tmp_path)
    out = _out_root(tmp_path)
    assert _stage(ws, temp, CV_OUT_DIR=str(out)).returncode == 0
    result = _cleanup(ws, temp, **{"CV_OUT_DIR": str(out), **overrides})
    assert result.returncode == 0, label  # never a red job for a skipped cleanup
    assert (out / "ci-inputs" / "33230008911").is_dir(), label  # untouched, not deleted
    for name, canary in _canaries(out).items():
        assert canary.exists(), name


def test_cleanup_refuses_to_follow_a_symlinked_staging_directory(tmp_path):
    ws, temp = _guarded_delivery(tmp_path)
    out = _out_root(tmp_path)
    outside = tmp_path / "production-store"
    outside.mkdir()
    (outside / "cv-infra.sqlite3").write_text("production", encoding="utf-8")
    (out / "ci-inputs").mkdir(exist_ok=True)
    (out / "ci-inputs" / "33230008911").symlink_to(outside, target_is_directory=True)
    result = _cleanup(ws, temp, CV_OUT_DIR=str(out))
    assert result.returncode == 0
    assert "nothing to remove" in result.stdout
    assert (outside / "cv-infra.sqlite3").read_text(encoding="utf-8") == "production"
