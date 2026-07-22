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
import re
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
import yaml

from cv_infra.cli import batch, publish_glue
from cv_infra.cli.main import _build_parser
from cv_infra.contract.errors import ContractError
from cv_infra.report import github

_ROOT = Path(__file__).resolve().parents[1]
_VERIFY_WORKFLOW = _ROOT / ".github/workflows/verify.yml"
_VERIFY_ACTION = _ROOT / "actions/verify/action.yml"

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
    return {
        p.relative_to(staging_dir).as_posix()
        for p in staging_dir.rglob("*")
        if p.is_file()
    }


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


@pytest.mark.parametrize("path", [_VERIFY_WORKFLOW, _VERIFY_ACTION])
def test_no_consent_or_secret_value_injection(path):
    # G-21 (전 구문형): consent/secret VALUES are never injected as literals — only
    # env-key names are forwarded. No `ACCEPT_EULA=Y` / `{"ACCEPT_EULA": "Y"}` etc.
    text = path.read_text(encoding="utf-8")
    assert not re.search(r"(ACCEPT_EULA|PRIVACY_CONSENT)\s*[:=]\s*['\"]?(Y|yes|true|1)\b", text)
