"""Admit-gate tests — contract/inputs.py. Every rejection here costs zero GPU seconds.

Two properties are asserted about EVERY rejection, because they are what make the
message actionable: it names the FLAG to change (``field_path``) and it says what was
expected next to what arrived (``expected`` / ``got``). And the 2-vs-3 split is
asserted by TYPE: a wrong request raises ``ContractError`` (exit 2, the developer's
file), a missing consent env or an uninstalled PICT raises ``InfraError`` (exit 3, the
runner's provisioning) — collapsing them would send a developer to fix a file that is
not broken.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from cv_infra import execution
from cv_infra.contract import inputs, pict
from cv_infra.contract.errors import ContractError
from cv_infra.contract.inputs import InfraError

MODEL = "lighting: bright, dim\nspeed:    0.2, 0.4\n"
BASE_ARGS = (
    "--sim-script",
    "verify/sim.py",
    "--input-space",
    "verify/param_space.pict",
    "--output-dir",
    "verify/out",
)


def make_checkout(tmp_path: Path, *, model: str = MODEL, sim: str = "# sim\n") -> Path:
    """A consumer checkout: sim script, input space, oracle, committed output dir."""
    checkout = tmp_path / "checkout"
    (checkout / "verify" / "out").mkdir(parents=True)
    (checkout / "verify" / "sim.py").write_text(sim, encoding="utf-8")
    (checkout / "verify" / "param_space.pict").write_text(model, encoding="utf-8")
    (checkout / "verify" / "oracle.py").write_text("# oracle\n", encoding="utf-8")
    return checkout


def make_env(tmp_path: Path, **overrides: str) -> dict[str, str]:
    """A provisioned runner environment: consent given, PICT installed."""
    binary = tmp_path / "pict"
    binary.write_text("#!/bin/sh\n", encoding="utf-8")
    binary.chmod(0o755)
    env = {"ACCEPT_EULA": "Y", "PRIVACY_CONSENT": "Y", pict.PICT_BIN_ENV: str(binary)}
    env.update(overrides)
    return env


def parse_args(tmp_path, *extra, checkout=None, env=None, base=None):
    checkout = make_checkout(tmp_path) if checkout is None else checkout
    return inputs.parse(
        [*BASE_ARGS, *extra],
        make_env(tmp_path) if env is None else env,
        checkout_base=checkout if base is None else base,
    )


def reject(tmp_path, *extra, **kwargs) -> ContractError:
    with pytest.raises(ContractError) as excinfo:
        parse_args(tmp_path, *extra, **kwargs)
    error = excinfo.value
    assert error.field_path and error.expected and error.got  # actionable, always
    return error


# --- (1) the accepted request ---------------------------------------------------------


def test_a_complete_request_becomes_a_spec_with_container_relative_paths(tmp_path):
    spec = parse_args(tmp_path, "--oracle-script", "verify/oracle.py", "--repeats", "3")

    assert spec.mode == "gate"
    assert (spec.sim_script, spec.sim_output_dir) == ("verify/sim.py", "verify/out")
    assert spec.oracle_script == "verify/oracle.py"
    assert spec.checkout == (tmp_path / "checkout").resolve()
    assert spec.input_space_text == MODEL  # the bytes admit validated, not a re-read
    assert (spec.pict_k, spec.repeats, spec.concurrency) == (2, 3, 1)
    assert spec.budget_s is None and spec.report_only is False
    assert spec.update_baseline is False and spec.checkout_sha is None
    assert spec.sim_image == inputs.DEFAULT_SIM_IMAGE
    assert spec.run_dir == (tmp_path / "checkout" / ".cv-infra-run").resolve()
    assert spec.baseline_db == Path("~/.cv-infra/baselines.sqlite3").expanduser()
    assert spec.pict_bin == str(tmp_path / "pict")
    assert spec.warnings == ()


def test_without_an_oracle_the_run_is_a_sweep(tmp_path):
    assert parse_args(tmp_path).mode == "sweep"


def test_the_operational_flags_are_all_honoured(tmp_path):
    spec = parse_args(
        tmp_path,
        "--pict-k",
        "1",
        "--budget-s",
        "10800",
        "--concurrency",
        "4",
        "--report-only",
        "--update-baseline",
        "--sim-image",
        "acme/isaac@sha256:dead",
        "--case-timeout-s",
        "60",
        "--oracle-timeout-s",
        "30",
        "--shm-size",
        "16g",
        "--max-zip-mb",
        "8",
        "--run-dir",
        str(tmp_path / "run"),
        env=make_env(tmp_path, GITHUB_SHA="abc123", CV_BASELINE_DB=str(tmp_path / "b.sqlite3")),
    )

    assert (spec.pict_k, spec.budget_s, spec.concurrency) == (1, 10800.0, 4)
    assert spec.report_only is True and spec.update_baseline is True
    assert spec.sim_image == "acme/isaac@sha256:dead"
    assert (spec.case_timeout_s, spec.oracle_timeout_s) == (60.0, 30.0)
    assert (spec.shm_size, spec.max_zip_mb) == ("16g", 8)
    assert spec.run_dir == (tmp_path / "run").resolve()
    assert spec.baseline_db == tmp_path / "b.sqlite3"
    assert spec.checkout_sha == "abc123"


def test_an_absolute_checkout_needs_no_base_directory(tmp_path):
    checkout = make_checkout(tmp_path)
    spec = inputs.parse(
        [*BASE_ARGS, "--checkout", str(checkout)], make_env(tmp_path), checkout_base=None
    )

    assert spec.checkout == checkout.resolve()


def test_the_spec_carries_every_field_the_execution_seam_reads(tmp_path):
    """The duck-typed surface ``cv_infra.execution`` documents (M1 landed first)."""
    spec = parse_args(tmp_path, "--oracle-script", "verify/oracle.py")

    for name in (
        "checkout",
        "sim_output_dir",
        "sim_image",
        "oracle_script",
        "case_timeout_s",
        "oracle_timeout_s",
        "shm_size",
        "max_zip_mb",
    ):
        assert hasattr(spec, name)


def test_the_pinned_image_is_the_same_literal_the_execution_seam_uses():
    """The contract may not import a sibling, so the duplicate pin is held equal here."""
    assert inputs.DEFAULT_SIM_IMAGE == execution.DEFAULT_SIM_IMAGE
    assert inputs.DEFAULT_CASE_TIMEOUT_S == execution.DEFAULT_CASE_TIMEOUT_S
    assert inputs.DEFAULT_ORACLE_TIMEOUT_S == execution.DEFAULT_ORACLE_TIMEOUT_S
    assert inputs.DEFAULT_SHM_SIZE == execution.DEFAULT_SHM_SIZE


# --- (2) exit-2: missing / malformed flags --------------------------------------------


def test_a_missing_required_flag_names_itself(tmp_path):
    checkout = make_checkout(tmp_path)
    with pytest.raises(ContractError) as excinfo:
        inputs.parse(
            ["--input-space", "verify/param_space.pict", "--output-dir", "verify/out"],
            make_env(tmp_path),
            checkout_base=checkout,
        )

    assert excinfo.value.field_path == "--sim-script"
    assert excinfo.value.got == "(missing)"
    assert "verify/sim.py" in excinfo.value.example


def test_an_unknown_flag_is_a_friendly_rejection_not_an_argparse_dump(tmp_path):
    error = reject(tmp_path, "--not-a-flag", "1")

    assert error.field_path == "(arguments)"
    assert "--not-a-flag" in error.got


def test_a_checkout_that_is_not_a_directory_is_refused(tmp_path):
    error = reject(tmp_path, "--checkout", str(tmp_path / "nope"), base=tmp_path)

    assert error.field_path == "--checkout"


# --- (3) exit-2: paths that the container could not see --------------------------------


@pytest.mark.parametrize(
    ("value", "why"),
    [
        ("/verify/out", "absolute"),
        ("../out", "escapes the checkout"),
        ("./out", "'.' segment"),
        ("", "empty"),
    ],
)
def test_an_output_dir_that_is_not_a_strict_subpath_is_refused(tmp_path, value, why):
    error = reject(tmp_path, "--output-dir", value)

    assert error.field_path == "--output-dir", why


def test_an_output_dir_that_is_not_committed_says_to_commit_a_gitkeep(tmp_path):
    error = reject(tmp_path, "--output-dir", "verify/missing")

    assert ".gitkeep" in error.expected  # git does not track empty directories


def test_an_output_dir_that_is_a_file_is_refused(tmp_path):
    checkout = make_checkout(tmp_path)
    (checkout / "verify" / "notadir").write_text("", encoding="utf-8")

    assert reject(tmp_path, "--output-dir", "verify/notadir", checkout=checkout)


def test_a_sim_script_absent_from_the_checkout_is_refused(tmp_path):
    error = reject(tmp_path, "--sim-script", "verify/ghost.py")

    assert error.field_path == "--sim-script" and "not a file" in error.got


def test_an_absolute_sim_script_is_refused_before_it_reaches_the_container(tmp_path):
    checkout = make_checkout(tmp_path)

    assert reject(tmp_path, "--sim-script", str(checkout / "verify" / "sim.py"), checkout=checkout)


def test_an_oracle_script_absent_from_the_checkout_is_refused(tmp_path):
    assert reject(tmp_path, "--oracle-script", "verify/ghost.py").field_path == "--oracle-script"


# --- (4) exit-2: numbers ---------------------------------------------------------------


@pytest.mark.parametrize(
    ("flag", "value"),
    [
        ("--pict-k", "0"),
        ("--pict-k", "two"),
        ("--repeats", "0"),
        ("--concurrency", "0"),
        ("--max-zip-mb", "0"),
    ],
)
def test_a_non_positive_or_non_numeric_count_is_refused(tmp_path, flag, value):
    assert reject(tmp_path, flag, value).field_path == flag


@pytest.mark.parametrize(
    ("flag", "value"),
    [
        ("--budget-s", "0"),
        ("--budget-s", "soon"),
        ("--case-timeout-s", "-1"),
        ("--oracle-timeout-s", "x"),
    ],
)
def test_a_non_positive_or_non_numeric_duration_is_refused(tmp_path, flag, value):
    error = reject(tmp_path, flag, value)

    assert error.field_path == flag and "positive" in error.expected


def test_an_order_wider_than_the_model_is_refused_before_pict_is_invoked(tmp_path):
    """PICT itself would fail with "Order cannot be larger than number of parameters"
    only after the binary runs; the model declares 2 axes, so k=3 is unanswerable."""
    error = reject(tmp_path, "--pict-k", "3")

    assert error.field_path == "--pict-k" and "2 axes" in error.expected


# --- (5) exit-2: the input-space model -------------------------------------------------


def test_an_invalid_model_is_rejected_with_its_line(tmp_path):
    checkout = make_checkout(tmp_path, model="lighting: bright\nlighting: dim\n")

    error = reject(tmp_path, checkout=checkout)

    assert error.source_line == 2 and "twice" in error.got


@pytest.mark.parametrize("name", ["2lighting", "light ing", "--lighting"])
def test_an_axis_name_that_is_not_a_usable_flag_is_refused(tmp_path, name):
    checkout = make_checkout(tmp_path, model=f"speed: 0.2\n{name}: bright, dim\n")

    error = reject(tmp_path, "--pict-k", "1", checkout=checkout)

    assert error.source_line == 2 and "long-flag" in error.got


@pytest.mark.parametrize("name", ["help", "h"])
def test_an_axis_named_after_the_scripts_own_help_is_refused(tmp_path, name):
    checkout = make_checkout(tmp_path, model=f"{name}: on, off\n")

    error = reject(tmp_path, "--pict-k", "1", checkout=checkout)

    assert "--help" in error.got and error.source_line == 1


# --- (6) exit-3: the runner's own provisioning -----------------------------------------


@pytest.mark.parametrize("missing", ["ACCEPT_EULA", "PRIVACY_CONSENT"])
def test_a_missing_consent_env_is_infra_not_a_contract_error(tmp_path, missing):
    env = make_env(tmp_path)
    del env[missing]

    with pytest.raises(InfraError) as excinfo:
        parse_args(tmp_path, env=env)

    assert missing in str(excinfo.value)
    assert not isinstance(excinfo.value, ContractError)  # exit 3, never exit 2


def test_a_blank_consent_env_does_not_count_as_consent(tmp_path):
    with pytest.raises(InfraError):
        parse_args(tmp_path, env=make_env(tmp_path, ACCEPT_EULA="  "))


def test_an_uninstalled_pict_binary_is_infra_not_a_bad_model(tmp_path, monkeypatch):
    monkeypatch.delenv(pict.PICT_BIN_ENV, raising=False)
    monkeypatch.setattr(pict.shutil, "which", lambda _name: None)
    env = make_env(tmp_path)
    del env[pict.PICT_BIN_ENV]

    with pytest.raises(InfraError) as excinfo:
        parse_args(tmp_path, env=env)

    assert pict.PICT_BIN_ENV in str(excinfo.value)


# --- (7) the headless warning (never a rejection) --------------------------------------


def test_a_gui_looking_sim_script_warns_with_its_line(tmp_path):
    checkout = make_checkout(tmp_path, sim='import x\nSimulationApp({"headless": False})\n')

    spec = parse_args(tmp_path, checkout=checkout)

    assert len(spec.warnings) == 1
    assert "verify/sim.py:2" in spec.warnings[0] and "no display" in spec.warnings[0]


def test_a_gui_toggle_is_still_admitted(tmp_path):
    """A ``--gui`` toggle mentions headless but never asks CI for a window."""
    checkout = make_checkout(tmp_path, sim='SimulationApp({"headless": not args.gui})\n')

    assert parse_args(tmp_path, checkout=checkout).warnings == ()
