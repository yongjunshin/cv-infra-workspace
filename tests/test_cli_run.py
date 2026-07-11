"""``cv-infra run`` wiring tests (M8, DoD-P2-07 + P3 friendly errors) —
supervisor STUBBED (G-17).

The run path (scenario YAML -> M1 6-stage loader admit gate -> canonical
JOB_SPEC -> supervisor -> exit code) is proven against a stub of the pinned
M8->M3 seam (``cv_infra.orchestrator.supervisor.run_job`` / ``JobOutcome`` —
cycle p2-supervisor-min verbatim pin), injected into ``sys.modules`` via
monkeypatch — NOTHING is written into ``cv_infra/orchestrator/``. The loader
under the CLI is the REAL ``contract.loader.load_request`` (P3 cycle-2 wiring):
every rejection case doubles as an ADMIT-GATE SPY — ``stub.calls == []``
proves the supervisor is never invoked on rejected input (NFR-INTAKE-003).
CPU-only, stdlib + pytest (+ pyyaml/pydantic, the run path's own deps).

The scenario fixture is the platform copy of the real consumer instance
``cv-infra-user/scenarios/nova_carter_warehouse_goal.yaml`` (drift-guarded by
tests/test_fixture_canonical_guard.py), kept under ``tests/fixtures/`` because
tests must not reference the consumer repo at runtime (boundary rule).
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

import cv_infra.contract.version as version_mod
from cv_infra.cli.main import EXIT_CONTRACT, EXIT_FAIL, EXIT_INFRA, EXIT_PASS, main
from cv_infra.contract.version import DeprecatedVersion

RUNNER_IMAGE = "cv-infra-runner:p2"

# Platform-side copy of the consumer instance
# cv-infra-user/scenarios/nova_carter_warehouse_goal.yaml — cycle-5
# bringup-measured fill; sut.image_ref = carter-sut:p2. The body is
# semantically identical to the source; only the fixture's platform header
# differs (its SOURCE OF TRUTH anchor + re-sync policy live in that header —
# no hash is retyped here, G-25: the drift guard is
# tests/test_fixture_canonical_guard.py + the PM merge-gate cross-repo diff).
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

    The payload is the minimal canonical subset ``{job_id, verdict}`` (the CLI
    re-validates it with the REAL M1 ``schema.Result``, whose optional fields
    default; full-shape wire equivalence with the runner emission is guarded
    by tests/test_contract_result_equivalence.py). A raw dict — not a model —
    so the unknown-verdict fold case ("wat") can be exercised too.
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
        payload = {"job_id": job_spec["job_id"], "verdict": self.verdict}
        result_path.write_text(json.dumps(payload), encoding="utf-8")
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

    # Wire-freeze pin (P3 cycle-2, task data contract): the validated-model
    # mapping emits the SAME sections the pre-loader raw pass-through did for
    # the canonical fixture (which spells every adapter_config key). Only
    # validated-type materialization differs textually (timeout_s: 120 ->
    # 120.0 — python-equal; measured 2026-07-10 probe).
    doc = yaml.safe_load(CARTER_YAML)
    assert spec["scenario"] == doc["scenario"]
    assert spec["interface"] == doc["interface"]
    assert spec["acceptance_criteria"] == doc["acceptance_criteria"]

    # Contract-side fields stay OFF the frozen Phase-2 wire; None-valued
    # optional fields stay absent (a present-None would defeat the oracles'
    # ``read_field(name, default)`` fallback).
    assert "debug_obstacle" not in spec["scenario"]
    assert "goal_orientation_wxyz" not in spec["acceptance_criteria"][0]["params"]
    for off_wire in ("apiVersion", "api_version", "execution_settings"):
        assert off_wire not in spec

    # Pinned positional seam: (job_spec, out_dir, runner_image, sut_image).
    assert call["out_dir"] == out_dir
    assert call["runner_image"] == RUNNER_IMAGE
    assert call["sut_image"] == spec["sut_image_ref"]


def test_job_id_flag_overrides_generated(monkeypatch, scenario_file, tmp_path):
    stub = RecordingSupervisor("pass")
    _install_supervisor(monkeypatch, stub)

    assert _run_cli(scenario_file, tmp_path / "out", "--job-id", "my-job-001") == EXIT_PASS
    assert stub.calls[0]["job_spec"]["job_id"] == "my-job-001"


def test_debug_obstacle_rides_the_scenario_wire_only_when_declared(monkeypatch, tmp_path):
    """D-2': the fail-injection cuboid is scenario world-state — declared keys
    reach the runner via the JOB_SPEC wire; undeclared dims stay ABSENT
    (runner defaults apply; the canonical-fixture test pins the absent case)."""
    stub = RecordingSupervisor("pass")
    _install_supervisor(monkeypatch, stub)
    doc = yaml.safe_load(CARTER_YAML)
    doc["scenario"]["debug_obstacle"] = {"x": -6.0, "y": 2.0, "height": 0.15}
    path = tmp_path / "obstacle.yaml"
    path.write_text(yaml.safe_dump(doc), encoding="utf-8")

    assert _run_cli(path, tmp_path / "out") == EXIT_PASS
    wire = stub.calls[0]["job_spec"]["scenario"]["debug_obstacle"]
    assert wire == {"x": -6.0, "y": 2.0, "height": 0.15}  # width/depth: None -> off the wire


# --- (2) verdict -> exit code ------------------------------------------------


@pytest.mark.parametrize(
    ("verdict", "expected"),
    [
        ("pass", EXIT_PASS),
        ("fail", EXIT_FAIL),
        ("timeout", EXIT_FAIL),  # SUT verdict (schema.py Verdict fold), not infra
        ("error", EXIT_INFRA),  # runner-recorded platform error
        # Unknown verdict: schema.Result's Verdict Literal rejects it at parse
        # (non-canonical result.json branch) -> folds to INFRA, never FAIL.
        ("wat", EXIT_INFRA),
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


# --- (3) contract errors -> friendly stderr + exit 2, supervisor never invoked


def _assert_contract_error(capsys, stub, rc) -> str:
    """Common admit-gate assertions: exit 2, SPY (zero supervisor calls —
    NFR-INTAKE-003), M1 ``str(err)`` prose on stderr, raw traceback 0."""
    assert rc == EXIT_CONTRACT
    assert stub.calls == []  # ADMIT-GATE SPY: rejected input never spawns
    err = capsys.readouterr().err
    assert err.startswith("cv-infra run: ")
    assert "expected" in err  # M1 friendly shape: field path + expected + ...
    assert "Traceback" not in err  # raw traceback 0 (NFR-INTAKE-001)
    return err


def test_missing_file_exits_2(monkeypatch, tmp_path, capsys):
    stub = RecordingSupervisor()
    _install_supervisor(monkeypatch, stub)
    rc = _run_cli(tmp_path / "does-not-exist.yaml", tmp_path / "out")
    err = _assert_contract_error(capsys, stub, rc)
    assert "readable YAML request file" in err


def test_broken_yaml_exits_2_with_line_location(monkeypatch, tmp_path, capsys):
    stub = RecordingSupervisor()
    _install_supervisor(monkeypatch, stub)
    bad = tmp_path / "broken.yaml"
    bad.write_text("scenario: [unclosed\n", encoding="utf-8")
    err = _assert_contract_error(capsys, stub, _run_cli(bad, tmp_path / "out"))
    assert "well-formed YAML" in err
    assert f"at {bad}:" in err  # parse errors carry the YAML line/col mark


def test_non_mapping_top_level_exits_2(monkeypatch, tmp_path, capsys):
    stub = RecordingSupervisor()
    _install_supervisor(monkeypatch, stub)
    bad = tmp_path / "list.yaml"
    bad.write_text("- 1\n- 2\n", encoding="utf-8")
    err = _assert_contract_error(capsys, stub, _run_cli(bad, tmp_path / "out"))
    assert "a YAML mapping" in err


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
    err = _assert_contract_error(capsys, stub, _run_cli(bad, tmp_path / "out"))
    assert "sut.tag" in err  # dotted field path (M1 render_loc)


def test_schema_violation_missing_goal_exits_2(monkeypatch, tmp_path, capsys):
    stub = RecordingSupervisor()
    _install_supervisor(monkeypatch, stub)
    doc = yaml.safe_load(CARTER_YAML)
    del doc["scenario"]["goal"]
    bad = tmp_path / "no-goal.yaml"
    bad.write_text(yaml.safe_dump(doc), encoding="utf-8")
    err = _assert_contract_error(capsys, stub, _run_cli(bad, tmp_path / "out"))
    assert "scenario.goal" in err


def test_schema_violation_unknown_adapter_key_exits_2(monkeypatch, tmp_path, capsys):
    stub = RecordingSupervisor()
    _install_supervisor(monkeypatch, stub)
    doc = yaml.safe_load(CARTER_YAML)
    doc["interface"]["adapter_config"]["topic_map"] = {}  # retired key -> SEAM-2 loud-reject
    bad = tmp_path / "retired-key.yaml"
    bad.write_text(yaml.safe_dump(doc), encoding="utf-8")
    err = _assert_contract_error(capsys, stub, _run_cli(bad, tmp_path / "out"))
    assert "topic_map" in err


def test_self_containedness_empty_criteria_exits_2(monkeypatch, tmp_path, capsys):
    """REQ-INTAKE-006 triad: a request without >=1 acceptance criterion is not
    self-contained — rejected at the gate, never executed."""
    stub = RecordingSupervisor()
    _install_supervisor(monkeypatch, stub)
    doc = yaml.safe_load(CARTER_YAML)
    doc["acceptance_criteria"] = []
    bad = tmp_path / "no-criteria.yaml"
    bad.write_text(yaml.safe_dump(doc), encoding="utf-8")
    err = _assert_contract_error(capsys, stub, _run_cli(bad, tmp_path / "out"))
    assert "acceptance_criteria" in err


def test_multiple_violations_all_render_on_stderr(monkeypatch, tmp_path, capsys):
    """Task pin: several pydantic violations -> the CLI renders the FULL
    ``from_validation_error`` list (one ``str(err)`` line group per violation),
    not just the loader's first."""
    stub = RecordingSupervisor()
    _install_supervisor(monkeypatch, stub)
    doc = yaml.safe_load(CARTER_YAML)
    del doc["scenario"]["goal"]  # violation 1
    del doc["sut"]  # violation 2
    bad = tmp_path / "two-violations.yaml"
    bad.write_text(yaml.safe_dump(doc), encoding="utf-8")
    err = _assert_contract_error(capsys, stub, _run_cli(bad, tmp_path / "out"))
    assert "scenario.goal" in err
    assert "\ncv-infra run: sut:" in err  # the second violation got its own render


def test_absent_api_version_exits_2_with_add_the_key_guidance(monkeypatch, tmp_path, capsys):
    """D-1' strict: no apiVersion key -> friendly reject telling the user the
    exact line to add (never silently treated as current)."""
    stub = RecordingSupervisor()
    _install_supervisor(monkeypatch, stub)
    lines = [ln for ln in CARTER_YAML.splitlines() if not ln.startswith("apiVersion:")]
    assert len(lines) == len(CARTER_YAML.splitlines()) - 1  # exactly the key line removed
    bad = tmp_path / "no-apiversion.yaml"
    bad.write_text("\n".join(lines) + "\n", encoding="utf-8")
    err = _assert_contract_error(capsys, stub, _run_cli(bad, tmp_path / "out"))
    assert "apiVersion" in err
    assert "example: apiVersion: cv-infra/v1" in err


def test_unknown_api_version_exits_2_with_migration_pointer(monkeypatch, tmp_path, capsys):
    stub = RecordingSupervisor()
    _install_supervisor(monkeypatch, stub)
    bad = tmp_path / "future-apiversion.yaml"
    bad.write_text(
        CARTER_YAML.replace("apiVersion: cv-infra/v1", "apiVersion: cv-infra/v999"),
        encoding="utf-8",
    )
    err = _assert_contract_error(capsys, stub, _run_cli(bad, tmp_path / "out"))
    assert "cv-infra/v999" in err
    assert "apiVersion policy" in err  # doc_link pointer (M1 §3.1 citation anchor)


def test_deprecated_api_version_warns_and_continues(monkeypatch, tmp_path, capsys):
    """M8-D5 accept-with-WARNING leg: deprecated (via injected table — the real
    DEPRECATED table is honestly empty for cv-infra/v1) -> stderr WARNING with
    sunset + migration link, and the run PROCEEDS to its verdict exit."""
    stub = RecordingSupervisor("pass")
    _install_supervisor(monkeypatch, stub)
    monkeypatch.setattr(
        version_mod,
        "DEPRECATED",
        {"cv-infra/v0": DeprecatedVersion(sunset="removed after v3", migration_link="docs/mig.md")},
    )
    deprecated = tmp_path / "deprecated-apiversion.yaml"
    deprecated.write_text(
        CARTER_YAML.replace("apiVersion: cv-infra/v1", "apiVersion: cv-infra/v0"),
        encoding="utf-8",
    )

    assert _run_cli(deprecated, tmp_path / "out") == EXIT_PASS  # verdict exit, not 2
    assert len(stub.calls) == 1  # deprecated = accepted: the job actually ran
    err = capsys.readouterr().err
    assert "cv-infra run: WARNING:" in err
    assert "DEPRECATED" in err and "sunset" in err and "docs/mig.md" in err


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


def test_help_path_imports_no_yaml_pydantic_or_docker(tmp_path):
    """``cv-infra --help`` must exit 0 WITHOUT pulling pyyaml, pydantic (the M1
    loader) or docker — the loader import is lazy inside the run path."""
    probe = textwrap.dedent("""
        import sys
        from cv_infra.cli.main import main
        try:
            rc = main(["--help"])
        except SystemExit as e:
            rc = e.code or 0
        assert rc == 0, f"--help exit {rc}"
        assert "yaml" not in sys.modules, "pyyaml imported on --help path"
        assert "pydantic" not in sys.modules, "pydantic (M1 loader) imported on --help path"
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


# --- (6) operator consent env pass-through (decision 2026-07-03) --------------
# The CLI must forward ACCEPT_EULA / PRIVACY_CONSENT from its own process env
# to the supervisor's kw-only ``runner_env`` — verbatim, and only when present.
# Values below are OPAQUE test tokens: no consent-value literal is committed
# anywhere (repo-wide grep = 0); only pass-through is asserted.

CONSENT_KEYS = ("ACCEPT_EULA", "PRIVACY_CONSENT")


class ConsentRecordingSupervisor(RecordingSupervisor):
    """``RecordingSupervisor`` that also records the kw-only seam kwargs."""

    def __init__(self, verdict: str = "pass"):
        super().__init__(verdict)
        self.kwargs_calls: list[dict] = []

    def __call__(self, job_spec, out_dir, runner_image, sut_image, **kwargs):
        self.kwargs_calls.append(kwargs)
        return super().__call__(job_spec, out_dir, runner_image, sut_image)


@pytest.fixture()
def no_ambient_consent(monkeypatch: pytest.MonkeyPatch) -> None:
    """Hermetic baseline: strip any ambient consent env before each case."""
    for key in CONSENT_KEYS:
        monkeypatch.delenv(key, raising=False)


def test_consent_env_both_present_pass_through_verbatim(
    monkeypatch, scenario_file, tmp_path, no_ambient_consent
):
    stub = ConsentRecordingSupervisor("pass")
    _install_supervisor(monkeypatch, stub)
    monkeypatch.setenv("ACCEPT_EULA", "opaque-token-eula-7f3a")
    monkeypatch.setenv("PRIVACY_CONSENT", "opaque-token-privacy-91c2")

    assert _run_cli(scenario_file, tmp_path / "out") == EXIT_PASS
    # Exactly runner_env, exactly the two keys, values verbatim (no extras leak).
    assert stub.kwargs_calls == [
        {
            "runner_env": {
                "ACCEPT_EULA": "opaque-token-eula-7f3a",
                "PRIVACY_CONSENT": "opaque-token-privacy-91c2",
            }
        }
    ]


def test_consent_env_absent_passes_nothing(
    monkeypatch, scenario_file, tmp_path, no_ambient_consent
):
    stub = ConsentRecordingSupervisor("pass")
    _install_supervisor(monkeypatch, stub)

    assert _run_cli(scenario_file, tmp_path / "out") == EXIT_PASS
    # Absent => runner_env NOT passed at all (boot guard refuses honestly; FU-8 is P5).
    assert stub.kwargs_calls == [{}]


@pytest.mark.parametrize("present", CONSENT_KEYS)
def test_consent_env_partial_forwards_only_present_key(
    monkeypatch, scenario_file, tmp_path, no_ambient_consent, present
):
    stub = ConsentRecordingSupervisor("pass")
    _install_supervisor(monkeypatch, stub)
    monkeypatch.setenv(present, "opaque-token-partial-05d8")

    assert _run_cli(scenario_file, tmp_path / "out") == EXIT_PASS
    assert stub.kwargs_calls == [{"runner_env": {present: "opaque-token-partial-05d8"}}]


# --- (7) oracle_plugin_dir pass-through (D-1 wiring contract #2, 2026-07-11) --
# An admitted CustomCriterion -> the CLI hands the scenario's parent directory
# (resolved absolute) to the supervisor as kw-only ``oracle_plugin_dir`` so it
# can be ro-mounted into the runner (contract #3). MVP-only criteria -> None:
# the kwarg stays unpassed (= the pinned kw-only default, no mount, no env).
# Detection is ``isinstance(..., CustomCriterion)`` on the ADMITTED model, not
# a string heuristic — proven via a real module:Class oracle that stage-5 binds
# (tests.oracle_plugin_fixture, the loader tests' plugin stand-in).


def test_custom_criterion_passes_scenario_parent_as_oracle_plugin_dir(
    monkeypatch, tmp_path, no_ambient_consent
):
    stub = ConsentRecordingSupervisor("pass")
    _install_supervisor(monkeypatch, stub)
    doc = yaml.safe_load(CARTER_YAML)
    doc["acceptance_criteria"].append(
        {"oracle": "tests.oracle_plugin_fixture:CustomOracle", "params": {"anything": "goes"}}
    )
    nested = tmp_path / "scenarios"  # parent dir != cwd: the DIR must ride, not "."
    nested.mkdir()
    scenario = nested / "custom.yaml"
    scenario.write_text(yaml.safe_dump(doc), encoding="utf-8")

    assert _run_cli(scenario, tmp_path / "out") == EXIT_PASS
    assert stub.kwargs_calls == [{"oracle_plugin_dir": str(nested.resolve())}]


def test_mvp_only_criteria_pass_oracle_plugin_dir_none(
    monkeypatch, scenario_file, tmp_path, no_ambient_consent
):
    """No CustomCriterion (canonical fixture: reached_goal + no_collision only)
    -> the delivered seam value is the contract's ``None``: the kwarg is not
    passed, so the supervisor's pinned kw-only default ``None`` applies."""
    stub = ConsentRecordingSupervisor("pass")
    _install_supervisor(monkeypatch, stub)

    assert _run_cli(scenario_file, tmp_path / "out") == EXIT_PASS
    assert stub.kwargs_calls == [{}]  # no oracle_plugin_dir key -> default None
