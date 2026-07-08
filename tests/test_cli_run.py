"""``cv-infra run`` wiring tests (M8, DoD-P2-07) — supervisor STUBBED (G-17).

The run path (scenario YAML -> canonical JOB_SPEC -> supervisor -> exit code,
D-2) is proven against a stub of the pinned M8->M3 seam
(``cv_infra.orchestrator.supervisor.run_job`` / ``JobOutcome`` — cycle
p2-supervisor-min verbatim pin). The real supervisor lands in a parallel M3
worktree, so these tests inject a fake module into ``sys.modules`` via
monkeypatch — NOTHING is written into ``cv_infra/orchestrator/``; the
real-import round-trip is the PM merge gate. CPU-only, stdlib + pytest
(+ pyyaml, which is the CLI's own dependency under test).

The scenario fixture is a VERBATIM copy (byte-identical, sha256-checked at
copy time) of the real consumer instance
``cv-infra-user/scenarios/nova_carter_warehouse_goal.yaml``, kept under
``tests/fixtures/`` because tests must not reference the consumer repo at
runtime (boundary rule) and ruff must not lint its long comment lines.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import textwrap
import types
from dataclasses import dataclass
from pathlib import Path

import pytest
import yaml

from cv_infra.cli.main import EXIT_CONTRACT, EXIT_FAIL, EXIT_INFRA, EXIT_PASS, main
from cv_infra.contract.models import VerificationRequest, VerificationResult

RUNNER_IMAGE = "cv-infra-runner:p2"

# Verbatim copy (sha256 3b328ed4…302a, identical to the consumer instance) of
# cv-infra-user/scenarios/nova_carter_warehouse_goal.yaml — cycle-3
# measured-aligned; sut.image_ref = carter-sut:p2.
CARTER_YAML = (Path(__file__).parent / "fixtures" / "nova_carter_warehouse_goal.yaml").read_text(
    encoding="utf-8"
)


@dataclass
class FakeJobOutcome:
    """Field-exact stand-in for the pinned ``JobOutcome`` (seam contract, verbatim)."""

    job_id: str
    result_path: Path | None
    runner_exit_code: int | None
    infra_error: str | None


class RecordingSupervisor:
    """Stub of the pinned ``run_job`` seam: records the call, writes result.json.

    The result dict is built with the REAL M1 ``VerificationResult`` so the
    payload the CLI parses is canonical by construction (G-17).
    """

    def __init__(self, verdict: str = "pass"):
        self.verdict = verdict
        self.calls: list[dict] = []

    def __call__(self, job_spec, out_dir, runner_image, sut_image):
        self.calls.append(
            {
                "job_spec": job_spec,
                "out_dir": out_dir,
                "runner_image": runner_image,
                "sut_image": sut_image,
            }
        )
        job_dir = out_dir / job_spec["job_id"]
        job_dir.mkdir(parents=True, exist_ok=True)
        result_path = job_dir / "result.json"
        result = VerificationResult(job_id=job_spec["job_id"], verdict=self.verdict)
        result_path.write_text(json.dumps(result.to_dict()), encoding="utf-8")
        return FakeJobOutcome(
            job_id=job_spec["job_id"],
            result_path=result_path,
            runner_exit_code=0,
            infra_error=None,
        )


def _install_supervisor(monkeypatch: pytest.MonkeyPatch, run_job) -> None:
    """Inject a fake ``cv_infra.orchestrator.supervisor`` module (no repo file)."""
    mod = types.ModuleType("cv_infra.orchestrator.supervisor")
    mod.run_job = run_job
    mod.JobOutcome = FakeJobOutcome
    monkeypatch.setitem(sys.modules, "cv_infra.orchestrator.supervisor", mod)


@pytest.fixture()
def scenario_file(tmp_path: Path) -> Path:
    path = tmp_path / "nova_carter_warehouse_goal.yaml"
    path.write_text(CARTER_YAML, encoding="utf-8")
    return path


def _run_cli(scenario: Path, out_dir: Path, *extra: str) -> int:
    argv = ["run", str(scenario), "--runner-image", RUNNER_IMAGE, "--out-dir", str(out_dir)]
    return main([*argv, *extra])


# --- (1) canonical JOB_SPEC reaches the stub --------------------------------


def test_job_spec_reaches_stub_in_canonical_shape(monkeypatch, scenario_file, tmp_path):
    stub = RecordingSupervisor("pass")
    _install_supervisor(monkeypatch, stub)
    out_dir = tmp_path / "out"

    assert _run_cli(scenario_file, out_dir) == EXIT_PASS
    assert len(stub.calls) == 1
    call = stub.calls[0]
    spec = call["job_spec"]

    # Canonical VerificationRequest dict: exact key set, sut.image_ref flattened.
    assert set(spec) == {"job_id", "scenario", "sut_image_ref", "interface", "acceptance_criteria"}
    assert "sut" not in spec
    assert spec["sut_image_ref"] == "carter-sut:p2"
    assert spec["job_id"].startswith("nova_carter_warehouse_goal-")  # stem + UTC stamp

    # scenario / interface / acceptance_criteria pass through as-is.
    assert spec["scenario"]["scene"] == "nova_carter_warehouse"
    assert spec["scenario"]["goal"] == {"x": -6.0, "y": 5.0, "yaw": 1.5708, "frame": "map"}
    assert spec["interface"]["type"] == "ros2"
    assert spec["interface"]["adapter_config"]["cmd_vel"]["topic"] == "/cmd_vel"
    assert spec["interface"]["adapter_config"]["odom_topics"] == ["/odom", "/chassis/odom"]
    assert [c["oracle"] for c in spec["acceptance_criteria"]] == ["reached_goal", "no_collision"]

    # Round-trips through the real M1 model without loss (G-17).
    assert VerificationRequest.from_dict(spec).to_dict() == spec

    # Pinned positional seam: (job_spec, out_dir, runner_image, sut_image).
    assert call["out_dir"] == out_dir
    assert call["runner_image"] == RUNNER_IMAGE
    assert call["sut_image"] == spec["sut_image_ref"]


def test_job_id_flag_overrides_generated(monkeypatch, scenario_file, tmp_path):
    stub = RecordingSupervisor("pass")
    _install_supervisor(monkeypatch, stub)

    assert _run_cli(scenario_file, tmp_path / "out", "--job-id", "my-job-001") == EXIT_PASS
    assert stub.calls[0]["job_spec"]["job_id"] == "my-job-001"


# --- (2) verdict -> exit code ------------------------------------------------


@pytest.mark.parametrize(
    ("verdict", "expected"),
    [
        ("pass", EXIT_PASS),
        ("fail", EXIT_FAIL),
        ("timeout", EXIT_FAIL),  # SUT verdict (models.py fold), not infra
        ("error", EXIT_INFRA),  # runner-recorded platform error
        ("wat", EXIT_INFRA),  # unknown verdict folds to INFRA, never FAIL
    ],
)
def test_recovered_verdict_maps_to_exit_code(
    monkeypatch, scenario_file, tmp_path, verdict, expected
):
    _install_supervisor(monkeypatch, RecordingSupervisor(verdict))
    assert _run_cli(scenario_file, tmp_path / "out") == expected


def test_recovered_verdict_outranks_runner_exit_code(monkeypatch, scenario_file, tmp_path):
    # Runner exit code says 1, recovered result.json says pass -> exit 0.
    stub = RecordingSupervisor("pass")

    def run_job(job_spec, out_dir, runner_image, sut_image):
        outcome = stub(job_spec, out_dir, runner_image, sut_image)
        outcome.runner_exit_code = 1
        return outcome

    _install_supervisor(monkeypatch, run_job)
    assert _run_cli(scenario_file, tmp_path / "out") == EXIT_PASS


# --- (3) contract errors -> exit 2, no spawn ---------------------------------


def _assert_contract_error(capsys, stub, rc) -> str:
    assert rc == EXIT_CONTRACT
    assert stub.calls == []  # contract errors must precede any spawn
    err = capsys.readouterr().err
    assert "cv-infra run: invalid scenario" in err
    assert "Traceback" not in err
    assert len(err.strip().splitlines()) == 1  # one-line cause (raw traceback 0)
    return err


def test_missing_file_exits_2(monkeypatch, tmp_path, capsys):
    stub = RecordingSupervisor()
    _install_supervisor(monkeypatch, stub)
    rc = _run_cli(tmp_path / "does-not-exist.yaml", tmp_path / "out")
    _assert_contract_error(capsys, stub, rc)


def test_broken_yaml_exits_2(monkeypatch, tmp_path, capsys):
    stub = RecordingSupervisor()
    _install_supervisor(monkeypatch, stub)
    bad = tmp_path / "broken.yaml"
    bad.write_text("scenario: [unclosed\n", encoding="utf-8")
    _assert_contract_error(capsys, stub, _run_cli(bad, tmp_path / "out"))


def test_non_mapping_top_level_exits_2(monkeypatch, tmp_path, capsys):
    stub = RecordingSupervisor()
    _install_supervisor(monkeypatch, stub)
    bad = tmp_path / "list.yaml"
    bad.write_text("- 1\n- 2\n", encoding="utf-8")
    _assert_contract_error(capsys, stub, _run_cli(bad, tmp_path / "out"))


def test_unknown_top_level_key_loud_rejects_exit_2(monkeypatch, tmp_path, capsys):
    stub = RecordingSupervisor()
    _install_supervisor(monkeypatch, stub)
    bad = tmp_path / "extra.yaml"
    bad.write_text(CARTER_YAML + "\nextra_top: 1\n", encoding="utf-8")
    err = _assert_contract_error(capsys, stub, _run_cli(bad, tmp_path / "out"))
    assert "extra_top" in err  # loud-reject names the offending key (no silent drop)


def test_unknown_sut_key_loud_rejects_exit_2(monkeypatch, tmp_path, capsys):
    stub = RecordingSupervisor()
    _install_supervisor(monkeypatch, stub)
    doc = yaml.safe_load(CARTER_YAML)
    doc["sut"]["tag"] = "oops"
    bad = tmp_path / "sut-extra.yaml"
    bad.write_text(yaml.safe_dump(doc), encoding="utf-8")
    _assert_contract_error(capsys, stub, _run_cli(bad, tmp_path / "out"))


def test_schema_violation_missing_goal_exits_2(monkeypatch, tmp_path, capsys):
    stub = RecordingSupervisor()
    _install_supervisor(monkeypatch, stub)
    doc = yaml.safe_load(CARTER_YAML)
    del doc["scenario"]["goal"]
    bad = tmp_path / "no-goal.yaml"
    bad.write_text(yaml.safe_dump(doc), encoding="utf-8")
    _assert_contract_error(capsys, stub, _run_cli(bad, tmp_path / "out"))


def test_schema_violation_unknown_adapter_key_exits_2(monkeypatch, tmp_path, capsys):
    stub = RecordingSupervisor()
    _install_supervisor(monkeypatch, stub)
    doc = yaml.safe_load(CARTER_YAML)
    doc["interface"]["adapter_config"]["topic_map"] = {}  # retired key -> SEAM-2 loud-reject
    bad = tmp_path / "retired-key.yaml"
    bad.write_text(yaml.safe_dump(doc), encoding="utf-8")
    _assert_contract_error(capsys, stub, _run_cli(bad, tmp_path / "out"))


def test_missing_runner_image_is_usage_error_2(monkeypatch, scenario_file):
    stub = RecordingSupervisor()
    _install_supervisor(monkeypatch, stub)
    with pytest.raises(SystemExit) as excinfo:  # argparse required-option error
        main(["run", str(scenario_file)])
    assert excinfo.value.code == EXIT_CONTRACT
    assert stub.calls == []


def test_unrecognized_extra_argument_exits_2(monkeypatch, scenario_file, tmp_path):
    stub = RecordingSupervisor()
    _install_supervisor(monkeypatch, stub)
    assert _run_cli(scenario_file, tmp_path / "out", "--bogus", "x") == EXIT_CONTRACT
    assert stub.calls == []


# --- (4) infra outcomes -> exit 3 --------------------------------------------


def test_infra_error_exits_3(monkeypatch, scenario_file, tmp_path, capsys):
    def run_job(job_spec, out_dir, runner_image, sut_image):
        return FakeJobOutcome(
            job_id=job_spec["job_id"],
            result_path=None,
            runner_exit_code=None,
            infra_error="docker daemon unreachable",
        )

    _install_supervisor(monkeypatch, run_job)
    assert _run_cli(scenario_file, tmp_path / "out") == EXIT_INFRA
    assert "docker daemon unreachable" in capsys.readouterr().err


def test_result_not_recovered_exits_3(monkeypatch, scenario_file, tmp_path):
    def run_job(job_spec, out_dir, runner_image, sut_image):
        return FakeJobOutcome(
            job_id=job_spec["job_id"], result_path=None, runner_exit_code=3, infra_error=None
        )

    _install_supervisor(monkeypatch, run_job)
    assert _run_cli(scenario_file, tmp_path / "out") == EXIT_INFRA


@pytest.mark.parametrize(
    "payload",
    [
        "not-json{{{",  # unparseable JSON
        '{"job_id": "x"}',  # parses, but non-canonical (verdict missing)
    ],
)
def test_bad_result_json_exits_3(monkeypatch, scenario_file, tmp_path, payload):
    def run_job(job_spec, out_dir, runner_image, sut_image):
        out_dir.mkdir(parents=True, exist_ok=True)
        result_path = out_dir / "result.json"
        result_path.write_text(payload, encoding="utf-8")
        return FakeJobOutcome(
            job_id=job_spec["job_id"], result_path=result_path, runner_exit_code=0, infra_error=None
        )

    _install_supervisor(monkeypatch, run_job)
    assert _run_cli(scenario_file, tmp_path / "out") == EXIT_INFRA


def test_supervisor_import_failure_exits_3(monkeypatch, scenario_file, tmp_path, capsys):
    # None in sys.modules => deterministic ImportError, pre- AND post-merge.
    monkeypatch.setitem(sys.modules, "cv_infra.orchestrator.supervisor", None)
    assert _run_cli(scenario_file, tmp_path / "out") == EXIT_INFRA
    assert "supervisor unavailable" in capsys.readouterr().err


# --- (5) --help path stays dependency-free -----------------------------------


def test_help_path_imports_no_yaml_or_docker(tmp_path):
    """``cv-infra --help`` must exit 0 WITHOUT pulling pyyaml or docker (lazy imports)."""
    probe = textwrap.dedent("""
        import sys
        from cv_infra.cli.main import main
        try:
            rc = main(["--help"])
        except SystemExit as e:
            rc = e.code or 0
        assert rc == 0, f"--help exit {rc}"
        assert "yaml" not in sys.modules, "pyyaml imported on --help path"
        assert "docker" not in sys.modules, "docker imported on --help path"
        """)
    env = {k: v for k, v in os.environ.items() if k != "PYTHONPATH"}  # G-10 isolation
    proc = subprocess.run(
        [sys.executable, "-c", probe],
        capture_output=True,
        text=True,
        env=env,
        cwd=tmp_path,  # neutral cwd: import must come from the installed package
    )
    assert proc.returncode == 0, proc.stderr
