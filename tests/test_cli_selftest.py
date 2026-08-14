"""``cv-infra selftest`` — the built-in stub round-trip entry point (M8, DoD-P5-07).

CPU-only; no GitHub, no GPU, no sockets: the orchestrator is faked with an
``httpx.MockTransport`` injected at the ``batch._make_client`` seam (the same seam
the batch/report suites use). What is under test is the M8 half ONLY — argparse
surface, HTTP call, output, exit code. The submitted DOCUMENT is M3/M7's
(``orchestrator.selftest.build_self_test_submission``, covered by
``tests/test_orchestrator_selftest.py``); this file asserts the CLI carries it
VERBATIM rather than rebuilding it.

Contract under test:
* exit codes — no new fold is invented (LOCKED §9 / D-I, DoD-P5-13 single table):
  ``SelfTestNotConfigured`` -> 3, server 422 -> 2, report_outcome pass/fail/errored
  -> 0/1/3, unreachable/non-202 -> 3;
* NFR-SELFTEST-001 — zero consumer input: the command takes no positional
  argument, reads no file, and runs from an empty working directory;
* REQ-SELFTEST-002 — the SAME submit+wait machinery as an ordinary envelope (the
  shared ``_post_envelope`` / ``_poll_until_terminal``), not a parallel path.
"""

from __future__ import annotations

import json

import httpx
import pytest

from cv_infra.cli import batch
from cv_infra.cli.exit_codes import exit_code_for_report_outcome
from cv_infra.cli.main import EXIT_CONTRACT, EXIT_FAIL, EXIT_INFRA, EXIT_PASS, _build_parser, main
from cv_infra.orchestrator.api import create_app
from cv_infra.orchestrator.fake_runner import FakeRunner
from cv_infra.orchestrator.models import JobState, Verdict
from cv_infra.orchestrator.selftest import SUT_IMAGE_ENV, build_self_test_submission
from cv_infra.orchestrator.store import Store

STUB_SUT = "cv-infra-selftest-stub:test"
ENVELOPE_ID = "env-selftest-1"


@pytest.fixture(autouse=True)
def _no_ambient_stub_image(monkeypatch):
    """Every test states its own stub handle — never inherit the operator's env."""
    monkeypatch.delenv(SUT_IMAGE_ENV, raising=False)


# --- orchestrator fake --------------------------------------------------------


def _wire(monkeypatch, handler) -> None:
    monkeypatch.setattr(
        batch,
        "_make_client",
        lambda api_base: httpx.AsyncClient(
            transport=httpx.MockTransport(handler), base_url="http://cv-infra.test"
        ),
    )


def _orchestrator(report_outcome: str = "pass", *, submit_status: int = 202, calls: list = None):
    """POST /envelopes -> 202 + id; GET /envelopes/{id} -> completed + outcome."""

    def handler(request: httpx.Request) -> httpx.Response:
        if calls is not None:
            calls.append((request.method, request.url.path, request.read()))
        if request.method == "POST":
            assert request.url.path == "/envelopes"  # SAME endpoint as `submit`
            if submit_status == 422:
                return httpx.Response(
                    422,
                    json={
                        "detail": {
                            "errors": [
                                {
                                    "field_path": "requests[0].scenario.robot",
                                    "expected": "a declared robot",
                                    "got": "'nova_carter'",
                                }
                            ]
                        }
                    },
                )
            if submit_status != 202:
                return httpx.Response(submit_status, json={})
            return httpx.Response(202, json={"envelope_id": ENVELOPE_ID})
        assert request.url.path == f"/envelopes/{ENVELOPE_ID}"  # SAME poll as `wait`
        return httpx.Response(200, json={"status": "completed", "report_outcome": report_outcome})

    return handler


def _argv(*extra: str) -> list[str]:
    return ["selftest", "--sut-image", STUB_SUT, *extra]


# --------------------------------------------------------------------------- #
# (1) exit-code contract — the single table, no new fold (D-I / DoD-P5-13)
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize(
    "report_outcome,expected",
    [("pass", EXIT_PASS), ("fail", EXIT_FAIL), ("errored", EXIT_INFRA)],
)
def test_round_trip_outcome_drives_the_exit_code(monkeypatch, capsys, report_outcome, expected):
    _wire(monkeypatch, _orchestrator(report_outcome))
    rc = main(_argv())
    out = capsys.readouterr().out
    assert rc == expected
    # the fold IS the shared single source, not a local table (재정의 금지)
    assert rc == exit_code_for_report_outcome(report_outcome)
    assert f"report_outcome={report_outcome} exit={expected}" in out


def test_unconfigured_stub_is_infra_3_and_never_contacts_the_orchestrator(monkeypatch, capsys):
    """No stub SUT handle = a deployment-configuration condition (3), refused
    BEFORE any submission — never a SUT verdict (1) and never a guessed image."""
    contacted: list = []
    _wire(monkeypatch, _orchestrator(calls=contacted))
    rc = main(["selftest"])  # no --sut-image, env cleared by the fixture
    err = capsys.readouterr().err
    assert rc == EXIT_INFRA
    assert contacted == []  # nothing was submitted
    assert SUT_IMAGE_ENV in err  # says exactly which knob to set
    assert "not a SUT verdict" in err
    assert "Traceback" not in err  # friendly refusal, raw traceback 0 (NFR-INTAKE-001)


def test_server_rejection_is_contract_2_with_the_m1_prose(monkeypatch, capsys):
    """A 422 is the M1 admit gate rejecting the envelope — the SAME fold as
    ``submit`` (a contract/validation rejection is 2 wherever it happens); the
    friendly M1 prose is rendered, never a traceback."""
    _wire(monkeypatch, _orchestrator(submit_status=422))
    rc = main(_argv())
    err = capsys.readouterr().err
    assert rc == EXIT_CONTRACT
    assert "requests[0].scenario.robot" in err  # M1 field path, rendered by the shared helper
    assert "Traceback" not in err


@pytest.mark.parametrize("status", [200, 500, 503])
def test_non_202_submission_is_infra_3(monkeypatch, capsys, status):
    _wire(monkeypatch, _orchestrator(submit_status=status))
    assert main(_argv()) == EXIT_INFRA
    assert "not a SUT verdict" in capsys.readouterr().err


def test_unreachable_orchestrator_is_infra_3(monkeypatch, capsys):
    def refuse(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("connection refused")

    _wire(monkeypatch, refuse)
    rc = main(_argv())
    err = capsys.readouterr().err
    assert rc == EXIT_INFRA
    assert "orchestrator unreachable" in err
    assert "Traceback" not in err


def test_selftest_never_returns_an_unmapped_code(monkeypatch):
    """Every branch lands inside the 0/1/2/3 contract (LOCKED §9)."""
    seen = set()
    for handler in (
        _orchestrator("pass"),
        _orchestrator("fail"),
        _orchestrator("errored"),
        _orchestrator(submit_status=422),
        _orchestrator(submit_status=500),
    ):
        _wire(monkeypatch, handler)
        seen.add(main(_argv()))
    _wire(monkeypatch, _orchestrator())
    seen.add(main(["selftest"]))  # unconfigured
    assert seen <= {EXIT_PASS, EXIT_FAIL, EXIT_CONTRACT, EXIT_INFRA}
    assert seen == {EXIT_PASS, EXIT_FAIL, EXIT_CONTRACT, EXIT_INFRA}  # all four exercised


# --------------------------------------------------------------------------- #
# (2) the CLI carries M3/M7's document VERBATIM (no re-implementation)
# --------------------------------------------------------------------------- #
def test_posted_body_is_the_producer_output_verbatim(monkeypatch):
    calls: list = []
    _wire(monkeypatch, _orchestrator(calls=calls))
    assert main(_argv()) == EXIT_PASS
    post = next(c for c in calls if c[0] == "POST")
    expected = build_self_test_submission(sut_image_ref=STUB_SUT).body
    assert json.loads(post[2]) == expected
    # the markers that make it identifiable as a self-test ride as produced
    assert expected["is_self_test"] is True and expected["origin"] == "built-in-stub"


def test_flag_beats_env_and_env_is_used_when_the_flag_is_absent(monkeypatch):
    calls: list = []
    monkeypatch.setenv(SUT_IMAGE_ENV, "from-env:1")
    _wire(monkeypatch, _orchestrator(calls=calls))
    assert main(_argv()) == EXIT_PASS  # --sut-image given
    assert json.loads(calls[0][2])["requests"][0]["sut"]["image_ref"] == STUB_SUT
    calls.clear()
    assert main(["selftest"]) == EXIT_PASS  # env fallback
    assert json.loads(calls[0][2])["requests"][0]["sut"]["image_ref"] == "from-env:1"


def test_trigger_source_default_is_omitted_and_ci_cd_rides(monkeypatch):
    """Same wire rule as ``submit`` (REQ-INTAKE-003): the default folds to
    OMITTED so the server default applies; ci-cd (the platform CI tier) rides."""
    calls: list = []
    _wire(monkeypatch, _orchestrator(calls=calls))
    assert main(_argv()) == EXIT_PASS
    assert "trigger_source" not in json.loads(calls[0][2])
    calls.clear()
    assert main(_argv("--trigger-source", "ci-cd")) == EXIT_PASS
    assert json.loads(calls[0][2])["trigger_source"] == "ci-cd"


def test_provenance_is_printed_before_the_verdict(monkeypatch, capsys):
    """REQ-SELFTEST-001: the operator sees WHICH stub ran (origin + handle)."""
    _wire(monkeypatch, _orchestrator("fail"))
    assert main(_argv()) == EXIT_FAIL
    lines = capsys.readouterr().out.splitlines()
    assert lines[0].startswith(
        f"cv-infra selftest: built-in stub origin=built-in-stub sut={STUB_SUT} -> "
    )
    assert ENVELOPE_ID in lines[1]  # the id is emitted for a follow-up `cv-infra report`
    assert "report_outcome=fail" in lines[2]


# --------------------------------------------------------------------------- #
# (3) NFR-SELFTEST-001 — zero consumer input
# --------------------------------------------------------------------------- #
def test_runs_from_an_empty_directory_with_no_arguments(monkeypatch, tmp_path):
    """No consumer repo, scenario file or image: an empty CWD is enough."""
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv(SUT_IMAGE_ENV, STUB_SUT)
    _wire(monkeypatch, _orchestrator("pass"))
    assert main(["selftest"]) == EXIT_PASS
    assert list(tmp_path.iterdir()) == []  # and it leaves nothing behind


def test_selftest_takes_no_positional_input(capsys):
    """A path argument is a usage error (2) — there is nothing for a user to
    supply: the request is the platform's own built-in stub."""
    assert main(["selftest", "scenarios/mine.yaml"]) == EXIT_CONTRACT
    assert "unrecognized argument(s): scenarios/mine.yaml" in capsys.readouterr().err


def test_the_consumer_sut_env_never_leaks_into_the_self_test(monkeypatch, capsys):
    """``CV_INFRA_SUT_IMAGE`` is the CONSUMER injection env ``submit`` reads (G2).
    It is set on any CI box that ran a verification, so a self-test that fell
    back to it would silently acquire the external-SUT dependency
    NFR-SELFTEST-001 forbids — the self-test reads ONLY its own env and refuses."""
    contacted: list = []
    monkeypatch.setenv("CV_INFRA_SUT_IMAGE", "ghcr.io/consumer/carter-sut:p2")
    _wire(monkeypatch, _orchestrator(calls=contacted))
    assert main(["selftest"]) == EXIT_INFRA
    assert contacted == []
    assert "carter-sut" not in capsys.readouterr().err


def test_no_host_path_rides_the_wire(monkeypatch):
    """G-69: the self-test sends no client-side host path (no oracle anchor) —
    so a containerised control plane can serve it with no bind-mount."""
    calls: list = []
    _wire(monkeypatch, _orchestrator(calls=calls))
    assert main(_argv()) == EXIT_PASS
    assert "oracle_plugin_dirs" not in json.loads(calls[0][2])


# --------------------------------------------------------------------------- #
# (4) REQ-SELFTEST-002 — the SAME machinery as an ordinary submission
# --------------------------------------------------------------------------- #
def test_selftest_uses_the_shared_submit_and_wait_helpers(monkeypatch):
    """Runtime guard (not prose): the command must travel through the ONE
    ``_post_envelope`` + ``_poll_until_terminal`` pair ``submit --wait`` uses. A
    self-test-only submit/poll path would leave these spies untouched."""
    used: list[str] = []
    real_post, real_poll = batch._post_envelope, batch._poll_until_terminal

    async def spy_post(*args, **kwargs):
        used.append("post")
        return await real_post(*args, **kwargs)

    async def spy_poll(*args, **kwargs):
        used.append("poll")
        return await real_poll(*args, **kwargs)

    monkeypatch.setattr(batch, "_post_envelope", spy_post)
    monkeypatch.setattr(batch, "_poll_until_terminal", spy_poll)
    _wire(monkeypatch, _orchestrator("pass"))
    assert main(_argv()) == EXIT_PASS
    assert used == ["post", "poll"]


def test_selftest_always_waits_there_is_no_fire_and_forget(monkeypatch):
    """REQ-SELFTEST-003 is a ROUND-trip: there is no ``--wait`` flag to forget
    (and no way to ask for a verdict-less submission)."""
    parser = _build_parser()
    args = parser.parse_args(["selftest"])
    assert not hasattr(args, "wait")
    with pytest.raises(SystemExit):
        parser.parse_args(["selftest", "--no-wait"])


# --------------------------------------------------------------------------- #
# (5) E2E over the REAL orchestrator app (CPU, no Isaac, no sockets)
# --------------------------------------------------------------------------- #
# The built-in stub travels the REAL FastAPI app: the M1 6-stage admit gate,
# fan-out, queue/scheduler, rollup, report_outcome — with the job execution
# faked (``FakeRunner``, the same seam the batch E2E tests use). This is the
# DoD-P5-07 round-trip MINUS the Isaac execution: a LIVE round-trip additionally
# needs the platform-internal stub SUT image, which does not exist yet
# (M7 §3.5 A/B open). Nothing here claims a live run.


def _wire_asgi(monkeypatch, app) -> None:
    monkeypatch.setattr(
        batch,
        "_make_client",
        lambda api_base: httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://cv-infra.test"
        ),
    )
    monkeypatch.setattr(batch, "_POLL_INTERVAL_S", 0.01)


@pytest.mark.parametrize(
    "runner_kwargs,expected",
    [({}, EXIT_PASS), ({"verdict": Verdict.FAIL}, EXIT_FAIL)],
)
def test_round_trip_through_the_real_orchestrator(
    monkeypatch, tmp_path, capsys, runner_kwargs, expected
):
    """The stub is ADMITTED by the real gate and its terminal verdict becomes
    the process exit code — the platform proving itself with zero consumer input."""
    with Store(tmp_path / "cv.sqlite3") as store:
        app = create_app(store, FakeRunner(state=JobState.COMPLETED, **runner_kwargs), k=1)
        _wire_asgi(monkeypatch, app)

        rc = main(["selftest", "--sut-image", STUB_SUT, "--trigger-source", "ci-cd"])

        out = capsys.readouterr().out
        assert rc == expected
        envelope_id = out.splitlines()[1]
        # the envelope is identifiable as a self-test on the operational plane
        # (REQ-SELFTEST-004) and its provenance survives in the store
        stored = store.load_envelope(envelope_id)
        assert stored.is_self_test is True
        assert stored.origin == "built-in-stub"


def test_the_built_in_stub_is_accepted_by_the_real_admit_gate(monkeypatch, tmp_path, capsys):
    """Non-vacuity guard for the round-trip above: a 422 would ALSO be a
    deterministic exit (2), so assert the stub actually passed the M1 gate —
    the platform's own document satisfies the platform's own contract."""
    with Store(tmp_path / "cv.sqlite3") as store:
        _wire_asgi(monkeypatch, create_app(store, FakeRunner(state=JobState.COMPLETED), k=1))
        assert main(["selftest", "--sut-image", STUB_SUT]) == EXIT_PASS
        err = capsys.readouterr().err
        assert err == ""  # no rejection prose, no infra note


# --------------------------------------------------------------------------- #
# (6) G-47 — the surface declaration must not outlive the wiring
# --------------------------------------------------------------------------- #
def test_help_no_longer_advertises_a_reserved_placeholder(capsys):
    with pytest.raises(SystemExit) as excinfo:
        main(["--help"])
    assert excinfo.value.code == 0
    out = capsys.readouterr().out
    assert "selftest" in out
    assert "reserved" not in out  # the P5 placeholder wording is gone (G-47)
    assert "not implemented" not in out
