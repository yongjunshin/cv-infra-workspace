"""p5c4 CLI gap tests — G1 glob→envelope synthesis, G2 SUT-image injection,
G3 exit-2 errors-JSON emission (M8, tasks/dx-2026-07-21-cli-e2e-gaps).

The three gaps are exactly the submit surface the p5c3 workflows consume at
run time (measured forms, quoted in the assertions below):

* G1: ``cv-infra submit ${{ inputs.scenarios }} --trigger-source ci-cd --wait``
  — a glob / N scenario paths must fold into ONE size-N envelope (D-K), in
  deterministic lexicographic path order (G-39-1).
* G2: the submit step carries ``CV_INFRA_SUT_IMAGE: ${{ inputs.sut_image }}``
  as env — the ref must land in every request's ``sut.image_ref`` on the wire
  (flag > env > scenario value; ref STRING only, R10).
* G3: the annotate step runs ``if [ -f errors.json ]; then python -m
  cv_infra.cli.publish_glue annotate errors.json; fi`` in the step CWD — an
  exit-2 submit must have produced that file (8-key list, D-L 1:1) under
  GitHub Actions, and must stay side-effect free standalone.

Wiring reuses the ``tests/test_cli_batch`` idioms (real M3 app over a spying
ASGITransport at the ``batch._make_client`` seam; the envelope adapter stays
REAL on the synthesis paths).
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
import yaml

from cv_infra.cli import batch, publish_glue
from cv_infra.cli.main import EXIT_CONTRACT, EXIT_PASS, _build_parser, main
from cv_infra.contract.errors import ANNOTATION_KEYS
from cv_infra.orchestrator.api import create_app
from cv_infra.orchestrator.fake_runner import FakeRunner
from cv_infra.orchestrator.store import Store
from tests.test_cli_batch import (
    SpyASGITransport,
    _request_doc,
    _stub_envelope,
    _wire_cli,
    _wire_transport,
)

_CANONICAL_TEXT = (
    Path(__file__).parent / "fixtures" / "nova_carter_warehouse_goal.yaml"
).read_text(encoding="utf-8")


@pytest.fixture(autouse=True)
def _isolate_ci_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """The platform CI itself runs under GITHUB_ACTIONS=true and a dev shell
    may export CV_INFRA_SUT_IMAGE — neither may leak into these assertions
    (each test opts in explicitly where the behavior IS the subject)."""
    monkeypatch.delenv("CV_INFRA_SUT_IMAGE", raising=False)
    monkeypatch.delenv("GITHUB_ACTIONS", raising=False)


def _scenario_with_ref(image_ref: str) -> str:
    """Canonical fixture text with a distinguishing ``sut.image_ref`` — the
    marker that makes request ORDER observable on the wire."""
    doc = yaml.safe_load(_CANONICAL_TEXT)
    doc["sut"]["image_ref"] = image_ref
    return yaml.safe_dump(doc)


def _write_scenarios(tmp_path: Path, names_to_text: dict[str, str]) -> Path:
    scenarios = tmp_path / "scenarios"
    scenarios.mkdir(exist_ok=True)
    for name, text in names_to_text.items():
        (scenarios / name).write_text(text, encoding="utf-8")
    return scenarios


def _wire_refs(spy: SpyASGITransport) -> tuple[dict, list[str]]:
    """The one POST body + its per-request image refs (order-observable)."""
    (post,) = [r for r in spy.requests if r.method == "POST"]
    body = json.loads(post.content)
    return body, [doc["sut"]["image_ref"] for doc in body["requests"]]


# --------------------------------------------------------------------------- #
# G1 — glob / multi-path -> ONE size-N envelope, lexicographic path order
# --------------------------------------------------------------------------- #


def test_submit_glob_synthesizes_size_n_envelope_in_path_order(monkeypatch, tmp_path, capsys):
    """A QUOTED glob (no shell expansion — the CLI expands it, D-K) folds into
    ONE size-3 envelope whose requests ride in lexicographic path order
    regardless of file-creation order (G-39-1 canonical at generation)."""
    scenarios = _write_scenarios(
        tmp_path,
        {  # created deliberately out of order
            "c.yaml": _scenario_with_ref("sut-c"),
            "a.yaml": _scenario_with_ref("sut-a"),
            "b.yaml": _scenario_with_ref("sut-b"),
        },
    )
    with Store(tmp_path / "cv.sqlite3") as store:
        app = create_app(store, FakeRunner(), k=2)
        spy = _wire_transport(monkeypatch, app)  # envelope adapter stays REAL

        assert main(["submit", str(scenarios / "*.yaml"), "--wait"]) == EXIT_PASS

        out_lines = capsys.readouterr().out.strip().splitlines()
        assert out_lines[0].startswith("env-")  # stdout line 1 = the bare id
        assert "report_outcome=pass" in out_lines[-1]
        body, refs = _wire_refs(spy)
        assert refs == ["sut-a", "sut-b", "sut-c"]  # size 3, sorted by path
        anchor = str(scenarios.resolve())
        assert body["oracle_plugin_dirs"] == [anchor, anchor, anchor]  # 등길이 anchors


def test_submit_multiple_paths_canonical_order_and_dedup(monkeypatch, tmp_path, capsys):
    """The shell-expanded form: N explicit paths, unsorted and one duplicated,
    still synthesize the SAME sorted envelope (exact-duplicate paths dropped —
    repeats are the envelope file's ``repeats`` field, never accidental)."""
    scenarios = _write_scenarios(
        tmp_path,
        {
            "c.yaml": _scenario_with_ref("sut-c"),
            "a.yaml": _scenario_with_ref("sut-a"),
            "b.yaml": _scenario_with_ref("sut-b"),
        },
    )
    with Store(tmp_path / "cv.sqlite3") as store:
        app = create_app(store, FakeRunner(), k=1)
        spy = _wire_transport(monkeypatch, app)

        argv = ["submit"] + [str(scenarios / n) for n in ("c.yaml", "a.yaml", "b.yaml", "a.yaml")]
        assert main(argv + ["--wait"]) == EXIT_PASS

        _, refs = _wire_refs(spy)
        assert refs == ["sut-a", "sut-b", "sut-c"]
        capsys.readouterr()


def test_submit_single_scenario_path_synthesizes_size_1(monkeypatch, tmp_path, capsys):
    scenarios = _write_scenarios(tmp_path, {"only.yaml": _scenario_with_ref("sut-only")})
    with Store(tmp_path / "cv.sqlite3") as store:
        app = create_app(store, FakeRunner(), k=1)
        spy = _wire_transport(monkeypatch, app)

        assert main(["submit", str(scenarios / "only.yaml"), "--wait"]) == EXIT_PASS

        body, refs = _wire_refs(spy)
        assert refs == ["sut-only"]
        assert body["oracle_plugin_dirs"] == [str(scenarios.resolve())]
        capsys.readouterr()


def test_load_submission_dispatch_envelope_vs_synthesis(monkeypatch, tmp_path):
    """The D-K dispatch: ONE non-glob arg whose doc carries ``requests`` -> the
    unchanged M1 envelope-loader surface; a scenario doc / two args / a glob ->
    the synthesis route."""
    envelope = tmp_path / "batch.yaml"
    envelope.write_text(
        "apiVersion: cv-infra/v1\nrequests:\n  - scenario: s.yaml\n", encoding="utf-8"
    )
    scenario = tmp_path / "sc.yaml"
    scenario.write_text(_CANONICAL_TEXT, encoding="utf-8")

    seen: list[str] = []
    monkeypatch.setattr(
        batch, "_load_envelope", lambda source: seen.append(source) or "envelope-route"
    )
    monkeypatch.setattr(batch, "_synthesize_envelope", lambda sources: "synthesis-route")

    assert batch._load_submission([str(envelope)]) == "envelope-route"
    assert seen == [str(envelope)]
    assert batch._load_submission([str(scenario)]) == "synthesis-route"
    assert batch._load_submission([str(envelope), str(envelope)]) == "synthesis-route"
    assert batch._load_submission([str(tmp_path / "*.yaml")]) == "synthesis-route"


def test_submit_unmatched_glob_is_friendly_exit_2_server_untouched(monkeypatch, tmp_path, capsys):
    """bash passes a no-match glob through LITERALLY — the CLI must reject it
    friendly (exit 2, no traceback) before the orchestrator is contacted."""
    with Store(tmp_path / "cv.sqlite3") as store:
        app = create_app(store, FakeRunner(), k=1)
        spy = _wire_transport(monkeypatch, app)

        rc = main(["submit", str(tmp_path / "nope" / "*.yaml")])

        captured = capsys.readouterr()
        assert rc == EXIT_CONTRACT
        assert spy.requests == []  # never reached the server
        assert captured.out == ""  # no envelope_id
        assert "a glob matching at least one scenario YAML file" in captured.err
        assert "Traceback" not in captured.err


def test_submit_missing_scenario_file_is_friendly_exit_2(tmp_path, capsys):
    rc = main(["submit", str(tmp_path / "ghost.yaml")])
    captured = capsys.readouterr()
    assert rc == EXIT_CONTRACT
    assert "an existing scenario YAML file" in captured.err
    assert "Traceback" not in captured.err


def test_submit_parser_surface_accepts_globs_and_the_new_flags():
    """Static parser pin (the ``test_gh_wiring_static`` idiom): the measured
    workflow invocation parses, and the envelope form stays one positional."""
    parser = _build_parser()
    ns = parser.parse_args(
        ["submit", "s1.yaml", "s2.yaml", "--sut-image", "r", "--errors-json", "e.json"]
    )
    assert ns.sources == ["s1.yaml", "s2.yaml"]
    assert ns.sut_image == "r"
    assert ns.errors_json == "e.json"
    ns = parser.parse_args(["submit", "envelope.yaml"])
    assert ns.sources == ["envelope.yaml"]
    assert ns.sut_image is None and ns.errors_json is None


# --------------------------------------------------------------------------- #
# G2 — SUT image injection: flag > env > scenario value; wire-only; ref-only
# --------------------------------------------------------------------------- #


def test_resolve_sut_image_priority_flag_env_none(monkeypatch):
    assert batch._resolve_sut_image(None) is None  # no override: scenario value stands
    monkeypatch.setenv("CV_INFRA_SUT_IMAGE", "ghcr.io/acme/robot@sha256:aaa")
    assert batch._resolve_sut_image(None) == "ghcr.io/acme/robot@sha256:aaa"
    assert batch._resolve_sut_image("flag-ref") == "flag-ref"  # flag outranks env
    monkeypatch.setenv("CV_INFRA_SUT_IMAGE", "")
    assert batch._resolve_sut_image(None) is None  # empty env = unset (G-26), never ""


def test_submit_env_injects_sut_image_ref_only_into_the_wire(monkeypatch, tmp_path, capsys):
    """CV_INFRA_SUT_IMAGE (the workflows' measured hand-off env) overrides
    EVERY request's ``sut.image_ref`` on the wire; the ref string travels
    verbatim (never pulled/inspected/normalized — R10 ref-only) and the
    scenario FILES are never rewritten."""
    injected = "ghcr.io/acme/robot@sha256:deadbeefcafe"
    scenarios = _write_scenarios(
        tmp_path,
        {"a.yaml": _scenario_with_ref("sut-a"), "b.yaml": _scenario_with_ref("sut-b")},
    )
    on_disk_before = {p.name: p.read_text(encoding="utf-8") for p in scenarios.iterdir()}
    monkeypatch.setenv("CV_INFRA_SUT_IMAGE", injected)
    with Store(tmp_path / "cv.sqlite3") as store:
        app = create_app(store, FakeRunner(), k=2)
        spy = _wire_transport(monkeypatch, app)

        assert main(["submit", str(scenarios / "*.yaml"), "--wait"]) == EXIT_PASS

        _, refs = _wire_refs(spy)
        assert refs == [injected, injected]  # byte-identical ref, both requests
    assert {p.name: p.read_text(encoding="utf-8") for p in scenarios.iterdir()} == on_disk_before
    capsys.readouterr()


def test_submit_flag_outranks_env_and_covers_envelope_files_too(monkeypatch, tmp_path, capsys):
    """--sut-image outranks the env, and the injection applies to the
    envelope-file input mode too — ONE override surface for both submit forms
    (the same CLI serves CI and humans, REQ-INTAKE-003)."""
    _write_scenarios(tmp_path, {"a.yaml": _scenario_with_ref("sut-a")})
    envelope = tmp_path / "batch.yaml"
    envelope.write_text(
        "apiVersion: cv-infra/v1\nrequests:\n  - scenario: scenarios/a.yaml\n", encoding="utf-8"
    )
    monkeypatch.setenv("CV_INFRA_SUT_IMAGE", "env-ref")
    with Store(tmp_path / "cv.sqlite3") as store:
        app = create_app(store, FakeRunner(), k=1)
        spy = _wire_transport(monkeypatch, app)

        assert main(["submit", str(envelope), "--sut-image", "flag-ref", "--wait"]) == EXIT_PASS

        _, refs = _wire_refs(spy)
        assert refs == ["flag-ref"]
        capsys.readouterr()


def test_wire_body_injection_is_a_copy_and_default_off():
    """The injection never mutates the loaded envelope (wire-plane copy only)
    and ``None`` keeps the pre-p5c4 wire byte-identical."""
    envelope = _stub_envelope([_request_doc(), _request_doc()])
    original_refs = [ref.raw_doc["sut"]["image_ref"] for ref in envelope.requests]

    body = batch._wire_body(envelope, None, "injected-ref")
    assert [doc["sut"]["image_ref"] for doc in body["requests"]] == ["injected-ref"] * 2
    # the loaded envelope's raw docs stayed untouched (copy, not mutation)
    assert [ref.raw_doc["sut"]["image_ref"] for ref in envelope.requests] == original_refs
    # no override -> the raw docs ride verbatim (identity, not a rewrite)
    assert batch._wire_body(envelope)["requests"] == [ref.raw_doc for ref in envelope.requests]


# --------------------------------------------------------------------------- #
# G3 — exit-2 errors JSON: the composite annotate step's exact consumption form
# --------------------------------------------------------------------------- #


def test_resolve_errors_json_flag_ci_default_standalone_off(monkeypatch):
    assert batch._resolve_errors_json(None) is None  # standalone default: OFF
    monkeypatch.setenv("GITHUB_ACTIONS", "true")
    assert batch._resolve_errors_json(None) == Path("errors.json")  # measured CWD name
    assert batch._resolve_errors_json("custom.json") == Path("custom.json")  # flag outranks


def test_submit_exit2_under_github_actions_emits_annotatable_errors_json(
    monkeypatch, tmp_path, capsys
):
    """THE consumption round-trip: exit 2 -> ``./errors.json`` appears in the
    CWD (the annotate step's ``[ -f errors.json ]`` probe) -> feeding that
    exact file shape to ``publish_glue.render_annotations`` yields the
    ``::error file=..,line=..::`` lines GitHub surfaces on the PR diff.
    The submit uses the CI shape — a checkout-RELATIVE path (the workflows'
    ``scenarios/*.yaml`` glob expands relative) — so ``source_path`` must stay
    relative for ``file=`` to land on the diff's file (D-L)."""
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("GITHUB_ACTIONS", "true")
    (tmp_path / "broken.yaml").write_text("scenario: [unclosed\n", encoding="utf-8")

    rc = main(["submit", "broken.yaml"])  # relative, exactly the CI spelling

    captured = capsys.readouterr()
    assert rc == EXIT_CONTRACT
    assert "Traceback" not in captured.err
    entries = json.loads((tmp_path / "errors.json").read_text(encoding="utf-8"))
    assert isinstance(entries, list) and entries
    assert set(entries[0]) == set(ANNOTATION_KEYS)  # exactly the 8 keys (D-L 1:1)
    assert entries[0]["source_path"] == "broken.yaml"  # as-given, repo-root relative
    lines = publish_glue.render_annotations(entries)  # the annotate step's renderer
    assert lines and lines[0].startswith("::error file=broken.yaml,line=")


def test_submit_exit2_standalone_leaves_no_errors_file(monkeypatch, tmp_path, capsys):
    """Standalone (no GITHUB_ACTIONS, no flag): exit 2 leaves the CWD clean —
    the emission is harmless-by-default outside CI (LOCKED §10 정신)."""
    monkeypatch.chdir(tmp_path)
    broken = tmp_path / "broken.yaml"
    broken.write_text("scenario: [unclosed\n", encoding="utf-8")

    assert main(["submit", str(broken)]) == EXIT_CONTRACT
    assert not (tmp_path / "errors.json").exists()
    capsys.readouterr()


def test_submit_errors_json_flag_is_the_standalone_equivalent(monkeypatch, tmp_path, capsys):
    """--errors-json PATH gives a human the SAME machine-readable list without
    any CI env — the capability is CLI-first, not CI-only (LOCKED §10)."""
    monkeypatch.chdir(tmp_path)
    target = tmp_path / "my-errors.json"
    broken = tmp_path / "broken.yaml"
    broken.write_text("scenario: [unclosed\n", encoding="utf-8")

    assert main(["submit", str(broken), "--errors-json", str(target)]) == EXIT_CONTRACT
    entries = json.loads(target.read_text(encoding="utf-8"))
    assert entries and set(entries[0]) == set(ANNOTATION_KEYS)
    assert not (tmp_path / "errors.json").exists()  # the default name is untouched
    capsys.readouterr()


def test_submit_server_422_entries_land_in_errors_json(monkeypatch, tmp_path, capsys):
    """Server-side re-rejection: the 422's 8-key entries are written VERBATIM —
    the same dicts the stderr prose rendered (one parse, two views)."""
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("GITHUB_ACTIONS", "true")
    envelope = tmp_path / "batch.yaml"
    envelope.write_text(
        "apiVersion: cv-infra/v1\nrequests:\n  - scenario: s0.yaml\n", encoding="utf-8"
    )
    bad = _request_doc()
    del bad["sut"]  # violates the REQ-INTAKE-006 triad -> server admit gate rejects
    with Store(tmp_path / "cv.sqlite3") as store:
        app = create_app(store, FakeRunner(), k=1)
        # Stubbed client-side loader ACCEPTS — the server stays authoritative.
        _wire_cli(monkeypatch, app, docs=[bad])

        rc = main(["submit", str(envelope), "--wait"])

    captured = capsys.readouterr()
    assert rc == EXIT_CONTRACT
    assert "cv-infra submit: " in captured.err
    entries = json.loads((tmp_path / "errors.json").read_text(encoding="utf-8"))
    assert entries
    assert all(set(ANNOTATION_KEYS) <= set(entry) for entry in entries)
    assert any("sut" in (entry.get("field_path") or "") for entry in entries)
    assert publish_glue.render_annotations(entries)  # annotate-step consumable


def test_submit_clears_stale_errors_json_before_any_outcome(monkeypatch, tmp_path, capsys):
    """Self-hosted workspace reuse (no checkout-clean on the GPU job — R10):
    a previous run's errors.json must never be replayed. Both a PASSING submit
    and a usage-error submit (exit 2 with no annotations) remove it first."""
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("GITHUB_ACTIONS", "true")
    stale = tmp_path / "errors.json"
    scenarios = _write_scenarios(tmp_path, {"a.yaml": _scenario_with_ref("sut-a")})
    with Store(tmp_path / "cv.sqlite3") as store:
        app = create_app(store, FakeRunner(), k=1)
        _wire_transport(monkeypatch, app)

        stale.write_text("[]", encoding="utf-8")
        assert main(["submit", str(scenarios / "a.yaml"), "--wait"]) == EXIT_PASS
        assert not stale.exists()  # a pass leaves no errors file behind

        stale.write_text("[]", encoding="utf-8")
        assert main(["submit", str(scenarios / "a.yaml"), "--timeout", "5"]) == EXIT_CONTRACT
        assert not stale.exists()  # usage-error exit 2 never replays stale annotations
    capsys.readouterr()
