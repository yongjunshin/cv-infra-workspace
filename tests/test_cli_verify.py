"""``cv-infra verify`` end to end, against a fake docker daemon.

This is the test that proves the pipeline is wired: a real PICT expansion, real case
ids and seeds, the real execution seam (talking to a duck-typed client), the real
verdict typing, the real baseline store and the real renderers — everything except the
container itself. What it pins is the OBSERVABLE contract of a run: the exit code, the
files left in the run dir, and the fact that a truncated run loses whole cases off the
end of the array.
"""

from __future__ import annotations

import json
import os
import shutil
import types

import pytest

from cv_infra.cli import main as cli
from cv_infra.contract import inputs, pict
from tests.conftest import FakeClient, FakeContainer

MODEL = "lighting: bright, dim\nspeed:    slow, fast\n"
CASES = 4  # pairwise over two binary axes

CONSENT = {"ACCEPT_EULA": "Y", "PRIVACY_CONSENT": "Y"}
PASSING = b'{"upright": true, "z_final": 0.12}\n'
FAILING = b'{"upright": false, "z_final": 0.31}\n'

needs_pict = pytest.mark.skipif(
    not (os.environ.get(pict.PICT_BIN_ENV) or shutil.which("pict")),
    reason=f"${pict.PICT_BIN_ENV} not set",
)


@pytest.fixture(autouse=True)
def no_host_cache(monkeypatch):
    """The cache tiers are read from the PROCESS environment, so a developer whose shell
    exports them would otherwise have these tests seeding a real cache tree."""
    monkeypatch.delenv("CV_ISAAC_CACHE_ROOT", raising=False)
    monkeypatch.delenv("CV_ISAAC_CACHE_SCRATCH_ROOT", raising=False)


def checkout(tmp_path, *, oracle=True, sim_text="# sim\n"):
    root = tmp_path / "checkout"
    (root / "verify" / "out").mkdir(parents=True)
    (root / "verify" / "sim.py").write_text(sim_text, encoding="utf-8")
    (root / "verify" / "param_space.pict").write_text(MODEL, encoding="utf-8")
    if oracle:
        (root / "verify" / "oracle.py").write_text("# oracle\n", encoding="utf-8")
    return root


def environ(tmp_path, **extra):
    env = {**CONSENT, "CV_BASELINE_DB": str(tmp_path / "baselines.sqlite3")}
    if os.environ.get(pict.PICT_BIN_ENV):
        env[pict.PICT_BIN_ENV] = os.environ[pict.PICT_BIN_ENV]
    env.update(extra)
    return env


def argv(tmp_path, *, oracle=True, **flags):
    args = [
        "--checkout",
        str(tmp_path / "checkout"),
        "--run-dir",
        str(tmp_path / "run"),
        "--sim-script",
        "verify/sim.py",
        "--input-space",
        "verify/param_space.pict",
        "--output-dir",
        "verify/out",
    ]
    if oracle:
        args += ["--oracle-script", "verify/oracle.py"]
    for flag, value in flags.items():
        args.append(f"--{flag.replace('_', '-')}")
        if value is not True:
            args.append(str(value))
    return args


def spec_for(tmp_path, *, oracle=True, env=None, sim_text="# sim\n", **flags):
    checkout(tmp_path, oracle=oracle, sim_text=sim_text)
    return inputs.parse(argv(tmp_path, oracle=oracle, **flags), env or environ(tmp_path))


def client_for(stdout=PASSING, **overrides):
    """A daemon whose containers exit immediately — the supervision loop's first poll
    sees ``exited``, so the run costs no sleep."""
    return FakeClient(statuses=("exited",), stdout_logs=stdout, **overrides)


def report_of(tmp_path):
    return json.loads((tmp_path / "run" / "report.json").read_text(encoding="utf-8"))


# --- the happy path -------------------------------------------------------------------


@needs_pict
def test_a_passing_gate_exits_0_and_leaves_a_complete_run_dir(tmp_path, capsys):
    spec = spec_for(tmp_path)
    assert cli.run_verify(spec, client_for(), environ=environ(tmp_path)) == 0

    report = report_of(tmp_path)
    assert report["mode"] == "gate"
    assert len(report["matrix"]) == CASES
    assert report["summary"]["runs_total"] == CASES
    assert {row["result"] for row in report["matrix"]} == {"pass"}
    assert report["matrix"][0]["checks"]["upright"] == {"pass_ratio": 1.0, "n": 1}
    assert report["matrix"][0]["metrics"]["z_final"]["mean"] == 0.12

    run_dir = tmp_path / "run"
    for name in ("check-run.json", "sticky-comment.md", "step-summary.md"):
        assert (run_dir / "payloads" / name).is_file()
    assert not (run_dir / "errors.json").exists()
    assert len(list((run_dir / "zips").glob("*.zip"))) == CASES
    assert len(list((run_dir / "logs").glob("*.sim.log"))) == CASES
    # every per-case host output tree is dropped once it has been zipped
    assert list((run_dir / "cases").iterdir()) == []
    assert "gate: pass (exit 0)" in capsys.readouterr().out


@needs_pict
def test_the_container_command_is_the_case_argv_with_the_seed_in_the_env(tmp_path):
    spec = spec_for(tmp_path)
    client = client_for()
    cli.run_verify(spec, client, environ=environ(tmp_path))
    sim_image, sim_kwargs = client.run_calls[0]
    assert sim_image == spec.sim_image
    assert sim_kwargs["command"][0] == "verify/sim.py"
    flags = dict(part.lstrip("-").split("=", 1) for part in sim_kwargs["command"][1:])
    assert set(flags) == {"lighting", "speed"}
    assert flags["lighting"] in {"bright", "dim"} and flags["speed"] in {"slow", "fast"}
    assert sim_kwargs["environment"]["ACCEPT_EULA"] == "Y"
    assert sim_kwargs["environment"]["CV_SEED"].isdigit()
    # the oracle re-runs the SAME argv with the oracle script and no GPU
    _, oracle_kwargs = client.run_calls[1]
    assert oracle_kwargs["command"][0] == "verify/oracle.py"
    assert oracle_kwargs["command"][1:] == sim_kwargs["command"][1:]
    assert "device_requests" not in oracle_kwargs


@needs_pict
def test_a_headless_false_sim_script_warns_but_still_runs(tmp_path, capsys):
    spec = spec_for(tmp_path, sim_text='SimulationApp({"headless": False})\n')
    assert cli.run_verify(spec, client_for(), environ=environ(tmp_path)) == 0
    assert "warning:" in capsys.readouterr().err


# --- the failing and errored lanes ----------------------------------------------------


@needs_pict
def test_a_false_check_exits_1(tmp_path):
    spec = spec_for(tmp_path)
    assert cli.run_verify(spec, client_for(FAILING), environ=environ(tmp_path)) == 1
    report = report_of(tmp_path)
    assert report["summary"]["checks_failed"] == CASES
    assert report["matrix"][0]["result"] == "fail"


@needs_pict
def test_every_case_erroring_exits_3_and_never_runs_the_oracle(tmp_path):
    """A dead sim is an INCOMPLETE run, not a robot verdict — and there is nothing for
    an oracle to judge, so it is not started."""
    spec = spec_for(tmp_path)
    client = client_for(exit_code=1)
    assert cli.run_verify(spec, client, environ=environ(tmp_path)) == 3
    report = report_of(tmp_path)
    assert report["summary"]["cases_errored"] == CASES
    assert report["matrix"][0]["runs"][0]["error"].startswith("the sim exited rc=1")
    assert len(client.run_calls) == CASES  # sim only


@needs_pict
def test_a_container_that_cannot_start_errors_the_case_with_the_seams_own_message(tmp_path):
    """The execution seam knows what went wrong; the report shows ITS message rather
    than a generic "no verdict" re-derived from a missing exit code."""
    spec = spec_for(tmp_path)
    client = client_for(raise_on_run=RuntimeError("no space left on device"))
    assert cli.run_verify(spec, client, environ=environ(tmp_path)) == 3
    error = report_of(tmp_path)["matrix"][0]["runs"][0]["error"]
    assert "no space left on device" in error


@needs_pict
def test_an_oracle_that_dies_mid_collection_errors_that_case_too(tmp_path):
    """The sim ran; the oracle could not be read — so the case has no verdict, and that
    is an ERROR, never a false check."""
    queued = []
    for _ in range(CASES):
        queued.append(FakeContainer(statuses=("exited",)))
        queued.append(FakeContainer(statuses=("exited",), logs_error=RuntimeError("stream closed")))
    spec = spec_for(tmp_path)
    assert cli.run_verify(spec, FakeClient(queued=queued), environ=environ(tmp_path)) == 3
    assert "stream closed" in report_of(tmp_path)["matrix"][0]["runs"][0]["error"]


@needs_pict
def test_a_gate_whose_verdict_holds_no_boolean_is_refused_with_an_annotation(tmp_path):
    spec = spec_for(tmp_path)
    assert cli.run_verify(spec, client_for(b'{"z_final": 0.12}\n'), environ=environ(tmp_path)) == 2
    errors = json.loads((tmp_path / "run" / "errors.json").read_text(encoding="utf-8"))
    assert errors[0]["field_path"] == "--oracle-script"
    assert errors[0]["source_path"] == "verify/oracle.py"


@needs_pict
def test_report_only_accepts_that_same_verdict(tmp_path):
    spec = spec_for(tmp_path, report_only=True)
    assert cli.run_verify(spec, client_for(b'{"z_final": 0.12}\n'), environ=environ(tmp_path)) == 0


@needs_pict
def test_a_sweep_runs_every_case_judges_nothing_and_never_gates(tmp_path):
    spec = spec_for(tmp_path, oracle=False)
    client = client_for(FAILING)
    assert cli.run_verify(spec, client, environ=environ(tmp_path)) == 0
    report = report_of(tmp_path)
    assert report["mode"] == "sweep"
    assert len(client.run_calls) == CASES  # no oracle container
    assert report["matrix"][0]["checks"] == {}


@needs_pict
def test_a_sweep_whose_every_case_errored_exits_3(tmp_path):
    """A sweep does not gate, but it does have to be COMPLETE: every container dying is
    an infrastructure fault, and publishing it as a pass would hide a broken image."""
    spec = spec_for(tmp_path, oracle=False)
    assert cli.run_verify(spec, client_for(exit_code=1), environ=environ(tmp_path)) == 3
    report = report_of(tmp_path)
    assert report["summary"]["cases_errored"] == CASES
    assert report["summary"]["report_outcome"] == "errored"


@needs_pict
def test_report_only_whose_every_case_errored_exits_3_too(tmp_path):
    """``--report-only`` waives the CHECK requirement, not the requirement that the run
    actually happened."""
    spec = spec_for(tmp_path, report_only=True)
    assert cli.run_verify(spec, client_for(exit_code=1), environ=environ(tmp_path)) == 3
    assert report_of(tmp_path)["summary"]["report_outcome"] == "errored"


# --- the budget -----------------------------------------------------------------------


@needs_pict
def test_the_budget_cuts_whole_cases_off_the_end_and_reports_the_coverage_kept(
    tmp_path, monkeypatch
):
    """The clock is injected so the cut is deterministic: the deadline is read once per
    case, at the moment that case would start."""
    ticks = iter([0.0, 1.0, 2.0, 3.0, 4.0, 5.0])
    monkeypatch.setattr(cli, "time", types.SimpleNamespace(monotonic=lambda: next(ticks)))
    spec = spec_for(tmp_path, budget_s=2.5)
    assert cli.run_verify(spec, client_for(), environ=environ(tmp_path)) == 0

    report = report_of(tmp_path)
    assert report["summary"]["cases_run"] == 2
    assert report["summary"]["cases_planned"] == CASES
    assert report["summary"]["coverage"]["truncated_after_case"] == 1
    assert 0.0 < report["summary"]["coverage"]["achieved"] < 1.0
    assert len(report["matrix"]) == 2


@needs_pict
def test_a_budget_that_is_already_spent_runs_nothing_and_says_so(tmp_path):
    spec = spec_for(tmp_path, budget_s=0.000001)
    assert cli.run_verify(spec, client_for(), environ=environ(tmp_path)) == 3
    report = report_of(tmp_path)
    assert report["summary"]["cases_run"] == 0
    assert report["summary"]["coverage"]["achieved"] == 0.0
    # -1, not None: "everything was cut" must not be indistinguishable from "nothing was
    # cut", or the published summary cannot explain why zero cases ran.
    assert report["summary"]["coverage"]["truncated_after_case"] == -1
    published = (tmp_path / "run" / "payloads" / "step-summary.md").read_text(encoding="utf-8")
    assert f"budget reached: ran 0/{CASES} cases" in published


# --- baselines ------------------------------------------------------------------------


@needs_pict
def test_update_baseline_writes_the_reference_and_the_next_run_is_compared_to_it(tmp_path):
    env = environ(tmp_path, GITHUB_SHA="deadbeef")
    first = spec_for(tmp_path, update_baseline=True, env=env)
    assert cli.run_verify(first, client_for(), environ=env) == 0
    assert report_of(tmp_path)["baseline"]["updated"] is True

    second = inputs.parse(argv(tmp_path), env)
    assert cli.run_verify(second, client_for(FAILING), environ=env) == 1
    report = report_of(tmp_path)
    assert report["summary"]["regressions"] == CASES
    assert report["baseline"]["updated"] is False
    assert report["matrix"][0]["regression"]["status"] == "regressed"
    assert report["matrix"][0]["regression"]["details"][0]["baseline"] == 1.0


@needs_pict
def test_an_unreachable_baseline_db_never_stops_the_run(tmp_path, capsys):
    """Best effort by design: the baseline is an ADDED signal, never a precondition."""
    env = environ(tmp_path, CV_BASELINE_DB=str(tmp_path / "nope" / "db.sqlite3"))
    assert cli.run_verify(spec_for(tmp_path, env=env), client_for(), environ=env) == 0
    assert report_of(tmp_path)["baseline"]["available"] is False
    assert "unavailable" in capsys.readouterr().err


# --- admit and infrastructure failures ------------------------------------------------


@needs_pict
def test_a_rejected_request_exits_2_with_errors_json_where_the_workflow_looks(tmp_path):
    checkout(tmp_path)
    code = cli.verify(argv(tmp_path, pict_k=9), environ(tmp_path))
    assert code == 2
    errors = json.loads((tmp_path / "run" / "errors.json").read_text(encoding="utf-8"))
    assert errors[0]["field_path"] == "--pict-k"
    assert not (tmp_path / "run" / "report.json").exists()  # nothing ran


@needs_pict
def test_a_model_pict_itself_rejects_is_the_requests_problem_too(tmp_path, monkeypatch):
    spec = spec_for(tmp_path)

    def explode(*_args, **_kwargs):
        raise pict.PictError("undeclared parameter", source_path=spec.sim_input_space, line=3)

    monkeypatch.setattr(cli.pict, "generate", explode)
    assert cli.run_verify(spec, client_for(), environ=environ(tmp_path)) == 2
    errors = json.loads((tmp_path / "run" / "errors.json").read_text(encoding="utf-8"))
    assert errors[0]["source_line"] == 3


def test_missing_operator_consent_is_the_runners_problem_not_the_requests(tmp_path, capsys):
    checkout(tmp_path)
    assert cli.verify(argv(tmp_path), {"CV_BASELINE_DB": str(tmp_path / "db")}) == 3
    assert "ACCEPT_EULA, PRIVACY_CONSENT not set" in capsys.readouterr().err
    assert not (tmp_path / "run" / "errors.json").exists()  # nothing to annotate in the repo


@needs_pict
def test_an_unreachable_docker_daemon_exits_3_once_not_once_per_case(tmp_path, monkeypatch, capsys):
    spec = spec_for(tmp_path)

    def no_daemon(_client):
        raise RuntimeError("Error while fetching server API version")

    monkeypatch.setattr(cli.execution, "resolve_docker_client", no_daemon)
    assert cli.run_verify(spec, None, environ=environ(tmp_path)) == 3
    assert "docker is not available (RuntimeError:" in capsys.readouterr().err


# --- the console entry point ----------------------------------------------------------


@needs_pict
def test_main_dispatches_verify(tmp_path, monkeypatch):
    checkout(tmp_path)
    monkeypatch.setattr(os, "environ", environ(tmp_path))
    monkeypatch.setattr(cli, "run_verify", lambda spec, environ=None: 7)
    assert cli.main(["verify", *argv(tmp_path)]) == 7


def test_main_prints_usage_for_help_and_for_a_missing_subcommand(capsys):
    assert cli.main(["--help"]) == 0
    assert cli.main([]) == 2
    assert cli.main(["monitor"]) == 2
    assert capsys.readouterr().err.count("usage: cv-infra verify") == 3


def test_an_unexpected_fault_is_exit_3_never_exit_1(monkeypatch, capsys):
    """An uncaught exception would otherwise leave Python's status 1, which in this
    contract means "your robot failed" — the one lie the exit codes exist to prevent."""

    def explode(*_args, **_kwargs):
        raise RuntimeError("bug in the platform")

    monkeypatch.setattr(cli.inputs, "parse", explode)
    assert cli.main(["verify"]) == 3
    assert "RuntimeError: bug in the platform" in capsys.readouterr().err


def test_an_absent_artifact_path_stays_absent_in_the_report(tmp_path):
    """``SimExecution`` allows a missing zip/log; the report must not invent one."""
    assert cli._run_relative(None, tmp_path) is None
