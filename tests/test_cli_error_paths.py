"""M8 CLI error / defensive paths — the branches the happy-path suites never take
(p8c2 브랜치 커버리지 close-out for ``cv_infra/cli/*``).

Everything here is an EDGE of the four CLI modules, not a second copy of their
main paths: the parser's undispatched-command floor, the "surface module missing"
folds, the HTTP-code / non-JSON-body folds, best-effort file writes that must not
mask a verdict, lenient render degradations, and the ``python -m
cv_infra.cli.publish_glue`` entry point the Action actually invokes. Each test
names the CONTRACT it pins (LOCKED §9 exit codes · D-O informational reads · G-17
lenient parsing · NFR-INTAKE-001 raw-traceback-0), never the current output text
for its own sake.

Seams are the ones the existing suites already use — ``batch._make_client``
(httpx ``MockTransport``) and the pinned supervisor stub — so nothing here opens a
socket, spawns a process or sleeps: every poll answered in this file is terminal
or fatal on the FIRST response, so the ``_POLL_INTERVAL_S`` sleep is never reached.
Stdlib + httpx + pytest, CPU-only.
"""

from __future__ import annotations

import importlib
import json
import sys
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest

from cv_infra.cli import batch, monitor, publish_glue
from cv_infra.cli import main as cli_main
from cv_infra.cli.main import EXIT_CONTRACT, EXIT_INFRA, EXIT_PASS, main

# The pinned supervisor stub + scenario fixture text live in the ``run`` suite;
# imported (not copied) so this file cannot drift from that seam pin — same idiom
# as ``tests/test_cli_batch.py``'s ``SuffixScriptedRunner`` import.
from tests.test_cli_run import CARTER_YAML, RecordingSupervisor, _install_supervisor

# The publish-plane report pin lives in the GitHub-wiring suite; reused verbatim so
# the entry-point tests below and the renderer tests share ONE report shape.
from tests.test_gh_wiring_static import _REPORT

ENVELOPE_ID = "env-1"


@pytest.fixture(autouse=True)
def _isolate_ci_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """The platform CI runs under ``GITHUB_ACTIONS=true``; a dev shell may export
    ``CV_INFRA_SUT_IMAGE``. Both change ``cmd_submit`` behaviour (G3 errors-JSON
    default / G2 wire injection), so they are dropped here exactly as
    ``tests/test_cli_batch.py`` does — no test may depend on its host."""
    monkeypatch.delenv("CV_INFRA_SUT_IMAGE", raising=False)
    monkeypatch.delenv("GITHUB_ACTIONS", raising=False)


def _wire(monkeypatch: pytest.MonkeyPatch, handler) -> list[httpx.Request]:
    """Patch the shared ``batch._make_client`` seam with a MockTransport (the idiom
    of tests/test_cli_{batch,monitor,report}.py). Returns the recorded requests."""
    seen: list[httpx.Request] = []

    def record(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return handler(request)

    monkeypatch.setattr(
        batch,
        "_make_client",
        lambda api_base: httpx.AsyncClient(
            transport=httpx.MockTransport(record), base_url="http://cv-infra.test"
        ),
    )
    return seen


def _always(response: httpx.Response):
    return lambda request: response


# --------------------------------------------------------------------------- #
# (1) cli/main.py — parser floors, one-line prose, unusable out-dir, missing
#     surface modules
# --------------------------------------------------------------------------- #


def test_no_subcommand_prints_usage_on_stderr_and_exits_2(capsys):
    """An incomplete invocation is a usage/contract error (2, argparse's own
    convention) and the help text goes to STDERR — stdout stays scriptable."""
    assert main([]) == EXIT_CONTRACT
    captured = capsys.readouterr()
    assert captured.out == ""
    assert "usage: cv-infra" in captured.err


def test_declared_but_undispatched_subcommand_fails_loudly_as_infra(monkeypatch, capsys):
    """The dispatch floor of ``main`` (unreachable while every ``_SUBCOMMANDS``
    name is dispatched): a name added to the SURFACE but not to a dispatch table
    must exit 3 loudly, never fall off the end as a silent 0. Adding the name is
    also all it takes for the parser to expose it (help-only schema)."""
    monkeypatch.setitem(cli_main._SUBCOMMANDS, "futurecmd", "declared on the surface only")

    parsed = cli_main._build_parser().parse_args(["futurecmd"])
    assert parsed.command == "futurecmd"

    assert main(["futurecmd"]) == EXIT_INFRA
    err = capsys.readouterr().err
    assert "'futurecmd' is declared on the CLI surface but has no dispatch" in err
    assert "not a SUT verdict" in err


def test_one_line_renders_a_keyerror_as_the_missing_key():
    """``_one_line`` is the no-traceback renderer every infra/usage message uses
    (NFR-INTAKE-001). A bare ``KeyError`` stringifies to just ``'k'``, which reads
    as noise in a sentence — it is named instead; an empty exception degrades to
    its type name rather than to nothing."""
    assert cli_main._one_line(KeyError("job_id")) == "missing required key 'job_id'"
    assert cli_main._one_line(RuntimeError()) == "RuntimeError"


def _write_scenario(tmp_path: Path) -> Path:
    path = tmp_path / "nova_carter_warehouse_goal.yaml"
    path.write_text(CARTER_YAML, encoding="utf-8")
    return path


def test_uncreatable_out_dir_is_infra_and_precedes_any_spawn(monkeypatch, tmp_path, capsys):
    """``--out-dir`` that cannot be created is a platform condition (3), and it is
    detected BEFORE the supervisor is imported/invoked — the stub's empty call log
    is the spy (nothing is spawned into a directory that does not exist)."""
    stub = RecordingSupervisor("pass")
    _install_supervisor(monkeypatch, stub)
    blocker = tmp_path / "not-a-dir"
    blocker.write_text("", encoding="utf-8")

    rc = main(
        [
            "run",
            str(_write_scenario(tmp_path)),
            "--runner-image",
            "cv-infra-runner:p2",
            "--out-dir",
            str(blocker / "out"),
        ]
    )

    assert rc == EXIT_INFRA
    assert stub.calls == []
    err = capsys.readouterr().err
    assert f"cannot create --out-dir {blocker / 'out'}" in err
    assert "Traceback" not in err


def test_unknown_verdict_folds_to_infra_never_to_fail(monkeypatch, tmp_path, capsys):
    """Module contract: "Unknown verdicts fold to INFRA (3), never to FAIL (1)".

    ``schema.Result``'s ``Verdict`` Literal and ``_VERDICT_EXIT``'s key set are
    identical today, so the defensive arm of the fold is reached by SHRINKING the
    table — i.e. simulating a Literal that grew a member the CLI does not map yet.
    ``timeout`` is the sharp case: its real fold is 1, so a table miss must move it
    to 3 (loudly), not leave it on a SUT verdict."""
    stub = RecordingSupervisor("timeout")
    _install_supervisor(monkeypatch, stub)
    monkeypatch.delitem(cli_main._VERDICT_EXIT, "timeout")

    rc = main(
        [
            "run",
            str(_write_scenario(tmp_path)),
            "--runner-image",
            "cv-infra-runner:p2",
            "--out-dir",
            str(tmp_path / "out"),
        ]
    )

    assert rc == EXIT_INFRA
    assert "unknown verdict 'timeout' in result.json" in capsys.readouterr().err


def _break_import(monkeypatch: pytest.MonkeyPatch, dotted: str) -> None:
    """Make ``from <package> import <name>`` raise ImportError in-process.

    ``None`` in ``sys.modules`` is the import machinery's own "halted" marker, and
    the already-bound package attribute has to go with it (the ``from`` form reads
    the attribute first). monkeypatch restores both."""
    package, _, name = dotted.rpartition(".")
    monkeypatch.delattr(importlib.import_module(package), name, raising=False)
    monkeypatch.setitem(sys.modules, dotted, None)


@pytest.mark.parametrize(
    ("argv", "surface"),
    [
        (["monitor"], "cv_infra.cli.monitor"),
        (["report", ENVELOPE_ID], "cv_infra.cli.batch"),
        (["status", ENVELOPE_ID], "cv_infra.cli.batch"),
    ],
)
def test_missing_surface_module_is_infra_not_a_verdict(monkeypatch, capsys, argv, surface):
    """Every non-``run`` command imports its body lazily (so ``--help`` stays
    dependency-free). An incomplete platform build must therefore surface as exit 3
    with a one-line diagnosis — never as a SUT verdict and never as a traceback."""
    _break_import(monkeypatch, surface)

    assert main(argv) == EXIT_INFRA
    err = capsys.readouterr().err
    assert err.startswith(f"cv-infra {argv[0]}: ")
    assert "unavailable" in err
    assert "infrastructure error, not a SUT verdict" in err
    assert "Traceback" not in err


# --------------------------------------------------------------------------- #
# (2) cli/batch.py — the HTTP-code / body folds and the best-effort side writes
# --------------------------------------------------------------------------- #


def test_make_client_targets_the_resolved_api_base():
    """The REAL client seam (every other test injects a transport in its place):
    the base URL is the resolved API and nothing is dialled at construction."""
    client = batch._make_client("http://cv-infra.test:8000")
    assert isinstance(client, httpx.AsyncClient)
    assert str(client.base_url) == "http://cv-infra.test:8000"


def test_status_lookup_folds_server_failures_to_infra(capsys):
    """``_status_lookup_exit`` is the ONE reading of the status endpoint's codes,
    shared by ``status`` and the poll loop: 500 (supervision crashed) and any other
    non-200 are infrastructure (3) — only 404 is a usage error (2)."""
    assert batch._status_lookup_exit("wait", ENVELOPE_ID, httpx.Response(500, text="boom")) == (
        EXIT_INFRA
    )
    assert batch._status_lookup_exit("status", ENVELOPE_ID, httpx.Response(503)) == EXIT_INFRA
    err = capsys.readouterr().err
    assert "envelope supervision crashed: boom" in err
    assert "unexpected orchestrator response 503" in err
    assert err.count("not a SUT verdict") == 2


def test_unknown_report_outcome_is_infra_and_says_so(capsys):
    """An outcome the single-source table does not know folds to 3 with a loud
    line; the KNOWN ``errored`` is 3 SILENTLY (it is the infra verdict, not a
    surprise) — the contrast is what makes the loud line meaningful."""
    assert batch._terminal_outcome_exit("wait", ENVELOPE_ID, {"report_outcome": "wat"}) == (
        EXIT_INFRA
    )
    assert "unknown report_outcome 'wat'" in capsys.readouterr().err

    assert batch._terminal_outcome_exit("wait", ENVELOPE_ID, {"report_outcome": "errored"}) == (
        EXIT_INFRA
    )
    assert capsys.readouterr().err == ""


def test_rejection_without_error_entries_still_shows_the_body(capsys):
    """A 422 whose body is not the M1 ``{"detail": {"errors": [...]}}`` shape (an
    unexpected rejection from a proxy or a future server) must still reach the
    operator verbatim, and the machine view stays EMPTY — the annotate step is fed
    only real 8-key entries."""
    response = httpx.Response(422, json={"detail": "rejected upstream"})
    assert batch._render_rejection("submit", response) == []
    assert "envelope rejected (422)" in capsys.readouterr().err


def test_rejection_renders_non_dict_entries_but_never_machine_feeds_them(capsys):
    """Mixed entries: a dict is rehydrated into the M1 friendly prose AND returned
    for the G3 errors-JSON; a non-dict is printed as-is and dropped from the
    machine view (``publish_glue.render_annotations`` consumes 8-key dicts only)."""
    entry = {"field_path": "requests[0].scenario", "expected": "an existing scenario file"}
    response = httpx.Response(422, json={"detail": {"errors": ["plain rejection text", entry]}})

    assert batch._render_rejection("submit", response) == [entry]
    err = capsys.readouterr().err
    assert "cv-infra submit: plain rejection text" in err
    assert "requests[0].scenario" in err


def test_unwritable_errors_json_warns_and_never_masks_the_verdict(tmp_path, capsys):
    """G3 emission is best-effort: the exit-2 verdict belongs to the rejection, so
    an unwritable target warns on stderr and raises nothing."""
    occupied = tmp_path / "errors.json"
    occupied.mkdir()

    batch._emit_errors_json(occupied, [{"field_path": "requests[0].scenario"}])

    assert f"could not write errors JSON to {occupied}" in capsys.readouterr().err


def test_wait_on_a_non_json_status_body_is_infra(monkeypatch, capsys):
    """The poll loop reads FIELDS of the status body, so a non-JSON one is an infra
    condition — not an absent verdict that could be read as a pass."""
    _wire(monkeypatch, _always(httpx.Response(200, text="<html>gateway</html>")))

    assert main(["wait", ENVELOPE_ID]) == EXIT_INFRA
    assert "orchestrator returned a non-JSON status body" in capsys.readouterr().err


def test_status_on_a_non_json_body_is_infra(monkeypatch, capsys):
    """``status`` never inspects a field (it dumps whatever JSON was served), so a
    non-JSON body is its ONLY infra condition on a 200."""
    _wire(monkeypatch, _always(httpx.Response(200, text="<html>gateway</html>")))

    assert main(["status", ENVELOPE_ID]) == EXIT_INFRA
    assert "orchestrator returned a non-JSON status body" in capsys.readouterr().err


def _envelope_file(tmp_path: Path) -> Path:
    path = tmp_path / "envelope.yaml"
    path.write_text(
        "apiVersion: cv-infra/v1\nrequests:\n  - scenario: scenarios/s0.yaml\n", encoding="utf-8"
    )
    return path


def _stub_loaded_envelope(monkeypatch: pytest.MonkeyPatch) -> None:
    """Field-exact stand-in for the pinned ``LoadedEnvelope``/``LoadedRequestRef``
    (``_wire_body`` reads only ``raw_doc`` + ``oracle_plugin_dir``)."""
    envelope = SimpleNamespace(
        api_version="cv-infra/v1",
        requests=(
            SimpleNamespace(
                admitted=object(),
                raw_doc={"scenario": {"scene": "s"}},
                scenario_path="scenarios/s0.yaml",
                oracle_plugin_dir="/abs/consumer/scenarios",
            ),
        ),
    )
    monkeypatch.setattr(batch, "_load_envelope", lambda source: envelope)


def test_submit_without_wait_prints_only_the_id_and_never_polls(monkeypatch, tmp_path, capsys):
    """D-O: submission accepted ≠ verdict. Without ``--wait`` the command exits 0
    as soon as the id is in hand, emits the BARE id on stdout (``ID=$(cv-infra
    submit ...)`` stays scriptable) and issues no status GET at all."""
    _stub_loaded_envelope(monkeypatch)
    seen = _wire(monkeypatch, _always(httpx.Response(202, json={"envelope_id": "env-9"})))

    assert main(["submit", str(_envelope_file(tmp_path))]) == EXIT_PASS
    assert capsys.readouterr().out == "env-9\n"
    assert [(r.method, r.url.path) for r in seen] == [("POST", "/envelopes")]


def test_submit_202_without_an_envelope_id_is_infra(monkeypatch, tmp_path, capsys):
    """An accepted submission the CLI cannot address afterwards is a platform
    condition (3) — never a silent 0 that would report "submitted" with no handle."""
    _stub_loaded_envelope(monkeypatch)
    _wire(monkeypatch, _always(httpx.Response(202, json={"accepted": True})))

    assert main(["submit", str(_envelope_file(tmp_path))]) == EXIT_INFRA
    assert "202 response carried no envelope_id" in capsys.readouterr().err


@pytest.mark.parametrize(
    ("response", "expected_message"),
    [
        (httpx.Response(500), "unexpected orchestrator response 500"),
        (httpx.Response(200, text="<html>not json</html>"), "non-JSON report body"),
    ],
)
def test_report_transport_failures_are_infra_never_a_verdict(
    monkeypatch, capsys, response, expected_message
):
    """``report`` is informational (D-O): exit 1/2 are structurally unreachable on
    the fetch path, so every transport-level failure lands on 3."""
    _wire(monkeypatch, _always(response))

    assert main(["report", ENVELOPE_ID]) == EXIT_INFRA
    assert expected_message in capsys.readouterr().err


def test_report_409_without_a_known_reason_still_surfaces_the_body(monkeypatch, capsys):
    """The 409 contract carries ``detail.reason`` (not-terminal / supervision-error).
    A 409 that carries neither (an older or proxied server) must not be silently
    reclassified: it stays infra and the body is shown verbatim."""
    _wire(monkeypatch, _always(httpx.Response(409, text="report not servable")))

    assert main(["report", ENVELOPE_ID]) == EXIT_INFRA
    err = capsys.readouterr().err
    assert "envelope report unavailable (409): report not servable" in err


def test_artifact_block_omits_the_policy_line_when_the_report_carries_none(capsys):
    """M4's ``_first_policy`` returns ``None`` when no row carries a selection
    policy; the CLI mirrors that by DROPPING the provenance paragraph rather than
    fabricating one — the artifact rows themselves still render."""
    report = {
        "matrix": [
            {
                "request_id": "req-0",
                "artifacts": {
                    "selected": [
                        {
                            "repeat_index": 0,
                            "role": "failing",
                            "verdict": "fail",
                            "result_json": "/abs/run/result.json",
                        }
                    ]
                },
            }
        ]
    }

    batch._render_artifacts(report)

    out = capsys.readouterr().out
    assert "artifacts (recording/telemetry review):" in out
    assert "selection policy:" not in out
    assert "[reviewable] req-0 repeat 0" in out


# --------------------------------------------------------------------------- #
# (3) cli/monitor.py — lenient render degradations (G-17)
# --------------------------------------------------------------------------- #


def test_non_dict_request_entries_are_skipped_not_crashed():
    """A monitor peek must never crash on a richer/odd projection: a non-dict
    request entry is skipped and the well-formed ones still render."""
    rendered = monitor.render_monitor({"requests": [{"request_id": "r0"}, "junk", None]})

    assert "requests (1):" in rendered
    assert "r0" in rendered


def test_broken_job_without_infra_error_gets_no_placeholder_line():
    """``infra_error`` absent = the server recorded none: the indented line is
    OMITTED rather than printed as ``n/a`` noise (the block itself still shows the
    server's own error category)."""
    record = {
        "requests": [
            {
                "request_id": "r0",
                "jobs": [{"job_id": "r0:0", "state": "FAILED", "error_category": "runner-crash"}],
            }
        ]
    }

    rendered = monitor.render_monitor(record)

    assert "category=runner-crash" in rendered
    assert "infra_error" not in rendered


def test_projection_without_broken_jobs_ends_at_the_none_marker():
    """Second early exit: requests exist but none is error-categorised — the view
    says so explicitly instead of leaving the operator to infer it from silence."""
    rendered = monitor.render_monitor({"requests": [{"request_id": "r0", "jobs": []}]})

    assert rendered.endswith("broken jobs: none")


# --------------------------------------------------------------------------- #
# (4) cli/publish_glue.py — the ``python -m`` entry point the Action invokes
# --------------------------------------------------------------------------- #
# Measured call sites (.github/workflows/verify.yml + actions/verify/action.yml):
#   python -m cv_infra.cli.publish_glue publish        report.json payloads
#   python -m cv_infra.cli.publish_glue annotate       errors.json
#   python -m cv_infra.cli.publish_glue stage-artifacts report.json artifacts


def _write_json(path: Path, payload) -> Path:
    path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    return path


def test_publish_mode_writes_the_four_payloads_and_keeps_stdout_clean(tmp_path, capsys):
    """The composite's publish step hands these four fixed names to
    ``github-script``/``upload-artifact``; the ``name=path`` lines are provenance
    and belong on stderr so stdout stays consumable."""
    report = _write_json(tmp_path / "report.json", _REPORT)
    out_dir = tmp_path / "payloads"

    assert publish_glue.main(["publish", str(report), str(out_dir)]) == 0

    captured = capsys.readouterr()
    assert captured.out == ""
    for name in (
        publish_glue.CHECK_RUN_FILE,
        publish_glue.STICKY_COMMENT_FILE,
        publish_glue.STEP_SUMMARY_FILE,
        publish_glue.ARTIFACT_MANIFEST_FILE,
    ):
        assert (out_dir / name).is_file()
        assert f"{name}={out_dir / name}" in captured.err
    # Delegation, not a second render: the written check-run IS github.py's.
    written = json.loads((out_dir / publish_glue.CHECK_RUN_FILE).read_text(encoding="utf-8"))
    assert written == publish_glue.render_payloads(_REPORT)[publish_glue.CHECK_RUN_FILE]


def test_annotate_mode_prints_one_workflow_command_per_entry(tmp_path, capsys):
    """The two halves of the G3 feed, in-process: the submit step WRITES
    errors.json and the annotate step reads THAT file back. One ``::error ...::``
    line per entry, in order, and nothing else on stdout — the runner turns each
    line into an inline PR annotation."""
    entries = [
        {
            "field_path": "scenario.goal.x",
            "expected": "a number",
            "got": "'far'",
            "source_path": "scenarios/s0.yaml",
            "source_line": 7,
            "source_col": 12,
        },
        {"field_path": "sut.image_ref", "expected": "an image reference"},
    ]
    errors = tmp_path / "errors.json"
    batch._emit_errors_json(errors, entries)  # the producing half, verbatim

    assert publish_glue.main(["annotate", str(errors)]) == 0

    assert capsys.readouterr().out.splitlines() == publish_glue.render_annotations(entries)


def test_stage_artifacts_mode_reports_the_staged_counts(tmp_path, capsys):
    """The stage step's stdout is the operator's only view of what
    ``upload-artifact`` will find; the staged file lands under the manifest's
    stable per-run layout."""
    source = tmp_path / "result.json"
    source.write_text("{}", encoding="utf-8")
    report = _write_json(
        tmp_path / "report.json",
        {
            "matrix": [
                {
                    "request_id": "req-0",
                    "artifacts": {
                        "policy": "failing-all + one-representative-pass",
                        "selected": [
                            {
                                "repeat_index": 0,
                                "role": "failing",
                                "verdict": "fail",
                                "result_json": str(source),
                            }
                        ],
                    },
                }
            ]
        },
    )
    staging = tmp_path / "artifacts"

    assert publish_glue.main(["stage-artifacts", str(report), str(staging)]) == 0

    assert capsys.readouterr().out.strip() == "staged=1 skipped=0"
    assert (staging / "req-0" / "repeat-0" / "result_json.json").is_file()


def test_annotation_extraction_ignores_a_document_that_is_neither_list_nor_mapping():
    """``annotate`` is fed whatever the submit step wrote; a scalar/garbage document
    yields NO annotations instead of raising — an annotation renderer must never be
    the thing that fails the job."""
    assert publish_glue.render_annotations("nonsense") == []
    assert publish_glue.render_annotations(None) == []
    assert publish_glue.render_annotations({"detail": {}}) == []
