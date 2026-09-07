"""The bundled example (``examples/selftest/``) and the ``cv-infra selftest`` preset.

The example is the runner's smoke test AND the reference `sim_script`, so what these
tests pin is the part a GPU run would discover far too late:

* the three contract-critical statics of a standalone script — stdlib-only imports
  before ``SimulationApp``, and the output path written checkout-root-relative;
* that the model's axes, sim.py's flags and oracle.py's flags are the SAME set (a
  drifted axis becomes an unrecognised flag inside a container, minutes in);
* that oracle.py really prints one flat JSON dict — run for real, in a subprocess,
  against a fixture trajectory (it is stdlib-only precisely so this is possible).

The simulation half cannot be tested here (it needs Isaac Sim and a GPU); it is
exercised by running ``cv-infra selftest`` on a provisioned runner.
"""

from __future__ import annotations

import ast
import json
import subprocess
import sys
from pathlib import Path

import pytest

from cv_infra.cli import main as cli
from cv_infra.contract import pict

ROOT = Path(__file__).resolve().parents[1]
EXAMPLE = ROOT / cli.SELFTEST_DIR
SIM = EXAMPLE / "sim.py"
ORACLE = EXAMPLE / "oracle.py"
MODEL = EXAMPLE / "param_space.pict"

#: Modules that only exist inside a booted SimulationApp.
ISAAC_PREFIXES = ("omni", "isaacsim", "pxr")

TRAJECTORY = {"seed": 7, "drop_height": 1.5, "cube_scale": 0.5, "z": [1.5, 1.1, 0.31, 0.25]}


def imports_of(tree: ast.Module, *, toplevel: bool) -> list[tuple[int, str]]:
    """``(lineno, root module)`` for every import — module level only, or all of them."""
    nodes = tree.body if toplevel else ast.walk(tree)
    found = []
    for node in nodes:
        if isinstance(node, ast.Import):
            found += [(node.lineno, alias.name.split(".")[0]) for alias in node.names]
        elif isinstance(node, ast.ImportFrom) and node.module:
            found.append((node.lineno, node.module.split(".")[0]))
    return found


def run_oracle(cwd: Path, *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, str(ORACLE), *args],
        cwd=cwd,
        capture_output=True,
        text=True,
        check=False,
    )


@pytest.fixture
def trajectory(tmp_path):
    """A checkout-shaped tree holding a sim output the oracle can judge."""
    out = tmp_path / cli.SELFTEST_DIR / "out"
    out.mkdir(parents=True)
    (out / "trajectory.json").write_text(json.dumps(TRAJECTORY), encoding="utf-8")
    return tmp_path


# --- (1) the example is complete -------------------------------------------------------


def test_the_example_ships_all_four_files():
    assert SIM.is_file() and ORACLE.is_file() and MODEL.is_file()
    # The output dir must be COMMITTED: the checkout is mounted read-only, so dockerd
    # cannot create the mount point for the case's output volume (contract/inputs.py).
    assert (EXAMPLE / "out" / ".gitkeep").is_file()


# --- (2) statics of a standalone script ------------------------------------------------


def test_sim_imports_nothing_isaac_before_simulationapp():
    tree = ast.parse(SIM.read_text(encoding="utf-8"))
    assert [name for _, name in imports_of(tree, toplevel=True) if name in ISAAC_PREFIXES] == []

    boot = [line for line, name in imports_of(tree, toplevel=False) if name == "isaacsim"]
    isaac = [line for line, name in imports_of(tree, toplevel=False) if name in ISAAC_PREFIXES]
    # `from isaacsim import SimulationApp` is the first Isaac import in the file; every
    # omni.*/isaacsim.* import below it runs only after the app was instantiated.
    assert min(boot) == min(isaac)


def test_both_scripts_use_the_same_checkout_relative_output_path():
    expected = f"{cli.SELFTEST_DIR}/out/trajectory.json"
    assert expected in SIM.read_text(encoding="utf-8")
    assert expected in ORACLE.read_text(encoding="utf-8")


def test_the_model_axes_are_exactly_the_flags_both_scripts_accept():
    text = MODEL.read_text(encoding="utf-8")
    pict.validate_model(text, source_path=str(MODEL))
    axes = set(pict._declared_parameters(text))
    assert axes == {"drop_height", "cube_scale"}
    for script in (SIM, ORACLE):
        flags = {
            node.args[0].value.lstrip("-")
            for node in ast.walk(ast.parse(script.read_text(encoding="utf-8")))
            if isinstance(node, ast.Call)
            and getattr(node.func, "attr", None) == "add_argument"
            and node.args
            and isinstance(node.args[0], ast.Constant)
        }
        assert axes <= flags, f"{script.name} does not accept every declared axis"


# --- (3) the oracle, run for real ------------------------------------------------------


def test_the_oracle_prints_one_flat_json_dict_with_the_contract_types(trajectory):
    proc = run_oracle(trajectory, "--drop_height=1.5", "--cube_scale=0.5")

    assert proc.returncode == 0, proc.stderr
    lines = [line for line in proc.stdout.splitlines() if line.strip()]
    assert len(lines) == 1
    verdict = json.loads(lines[0])
    assert verdict["fell"] is True  # 1.5 m -> 0.25 m
    assert isinstance(verdict["z_final"], float) and verdict["z_final"] == 0.25
    assert isinstance(verdict["note"], str) and "seed 7" in verdict["note"]
    assert not any(isinstance(value, (dict, list)) for value in verdict.values())


def test_the_oracle_tolerates_axes_it_does_not_read(trajectory):
    """The platform replays the SIM's argv verbatim — every axis, read or not."""
    proc = run_oracle(trajectory, "--drop_height=1.5", "--cube_scale=0.5", "--lighting=dim")

    assert proc.returncode == 0, proc.stderr
    assert json.loads(proc.stdout.strip())["fell"] is True


def test_a_cube_that_never_fell_is_a_failed_check(trajectory):
    """The drop height the oracle judges against comes from the ARGV, not the file."""
    proc = run_oracle(trajectory, "--drop_height=0.3", "--cube_scale=0.2")

    assert json.loads(proc.stdout.strip())["fell"] is False


def test_a_missing_trajectory_is_the_error_lane_not_a_verdict(tmp_path):
    proc = run_oracle(tmp_path, "--drop_height=1.5", "--cube_scale=0.5")

    assert proc.returncode == 1  # rc != 0 -> ERROR lane, never a false check
    assert proc.stdout.strip() == ""
    assert "trajectory.json" in proc.stderr


# --- (4) the `cv-infra selftest` preset -------------------------------------------------


def test_selftest_presets_the_example_and_lets_later_flags_win(monkeypatch):
    seen: dict[str, list[str]] = {}

    def fake_verify(argv, environ):
        seen["argv"] = list(argv)
        return 0

    monkeypatch.setattr(cli, "verify", fake_verify)
    assert cli.main(["selftest", "--checkout", str(ROOT), "--repeats", "3"]) == 0

    argv = seen["argv"]
    assert argv[: len(cli.SELFTEST_PRESET)] == list(cli.SELFTEST_PRESET)
    assert argv[-4:] == ["--checkout", str(ROOT), "--repeats", "3"]
    # the operator's --checkout is the LAST occurrence, so argparse keeps it
    assert argv.index("--checkout", len(cli.SELFTEST_PRESET)) > argv.index("--checkout")


def test_selftest_without_the_example_is_infra_not_a_rejection(tmp_path, capsys):
    assert cli.main(["selftest", "--checkout", str(tmp_path)]) == 3

    assert f"{cli.SELFTEST_DIR}/ is not in {tmp_path}" in capsys.readouterr().err
