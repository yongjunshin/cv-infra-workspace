"""The wiring between the workflows and the code they invoke.

Nothing here runs a workflow; these are the three facts about the YAML that only break in
production, where the feedback loop is a pushed commit and a self-hosted runner:

* the reusable workflow's inputs and ``cv-infra verify``'s flags are the SAME set — an
  input nobody passes on is silently ignored, and a flag nobody exposes is unreachable;
* CI builds PICT from the pinned commit, and the pin agrees with the one the workstation
  installer uses (two PICTs = two different case arrays for one input space);
* every ``uses:`` is an immutable commit SHA (CLAUDE.md §2-7) — a tag ref can move under
  a green build.

The workflows are read as TEXT: there is no YAML parser in the dependency set (one
runtime dependency, ``docker``), and adding one to assert on YAML would be a strange
trade. The parsing below is therefore deliberately shallow and anchored on indentation.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from cv_infra.contract import inputs as contract_inputs

ROOT = Path(__file__).resolve().parents[1]
WORKFLOWS = ROOT / ".github" / "workflows"
VERIFY_YML = WORKFLOWS / "verify.yml"
CI_YML = WORKFLOWS / "ci.yml"
INSTALL_PICT = ROOT / "scripts" / "workstation_setup" / "install_pict.sh"

#: The contract, spelled out here so a change to either side has to be made twice on
#: purpose: workflow input -> the CLI flag it becomes.
INPUT_TO_FLAG = {
    "sim_script": "--sim-script",
    "sim_input_space": "--input-space",
    "sim_output_dir": "--output-dir",
    "oracle_script": "--oracle-script",
    "pict_k": "--pict-k",
    "repeats": "--repeats",
    "budget": "--budget-s",
    "sim_image": "--sim-image",
    "concurrency": "--concurrency",
    "report_only": "--report-only",
}

#: Inputs that steer the WORKFLOW rather than the run (they reach no flag).
WORKFLOW_ONLY_INPUTS = {"runner_label"}

#: Flags the workflow computes instead of exposing: where the run's files go, and the
#: baseline update that only a non-pull-request event is allowed to make.
OPERATIONAL_FLAGS = {"--run-dir", "--update-baseline"}


def workflow_call_inputs(text: str) -> list[str]:
    """The names declared under ``on.workflow_call.inputs`` (indent 6, one per key)."""
    lines = text.splitlines()
    start = lines.index("    inputs:") + 1
    names = []
    for line in lines[start:]:
        if line.strip() and not line.startswith("      "):
            break
        found = re.fullmatch(r"      ([A-Za-z_][A-Za-z0-9_]*):", line)
        if found:
            names.append(found.group(1))
    return names


def run_scripts(text: str) -> str:
    """Every ``run: |`` block's body, joined — the shell the workflow actually executes.

    Comment lines are dropped: a flag NAMED in a comment is documentation, and only a
    flag that is passed counts as wiring.
    """
    lines = text.splitlines()
    bodies = []
    for index, line in enumerate(lines):
        opener = re.fullmatch(r"(\s*)run: \|\s*", line)
        if not opener:
            continue
        indent = len(opener.group(1))
        for following in lines[index + 1 :]:
            if following.strip() and len(following) - len(following.lstrip()) <= indent:
                break
            if not following.strip().startswith("#"):
                bodies.append(following)
    return "\n".join(bodies)


@pytest.fixture(scope="module")
def verify_yml() -> str:
    return VERIFY_YML.read_text(encoding="utf-8")


@pytest.fixture(scope="module")
def ci_yml() -> str:
    return CI_YML.read_text(encoding="utf-8")


def test_declared_inputs_are_exactly_the_contract(verify_yml: str) -> None:
    assert set(workflow_call_inputs(verify_yml)) == set(INPUT_TO_FLAG) | WORKFLOW_ONLY_INPUTS


def test_every_input_is_read_somewhere(verify_yml: str) -> None:
    for name in workflow_call_inputs(verify_yml):
        assert f"inputs.{name} }}}}" in verify_yml, f"input '{name}' is declared but never used"


def test_run_block_passes_exactly_the_mapped_flags(verify_yml: str) -> None:
    used = set(re.findall(r"--[a-z][a-z0-9-]*", run_scripts(verify_yml)))
    assert used == set(INPUT_TO_FLAG.values()) | OPERATIONAL_FLAGS


def test_every_passed_flag_exists_on_the_cli(verify_yml: str) -> None:
    """The other half of 1:1: the flags are the parser's, not a plausible spelling."""
    known = set(contract_inputs._parser()._option_string_actions)
    assert set(re.findall(r"--[a-z][a-z0-9-]*", run_scripts(verify_yml))) <= known


def test_baseline_is_updated_only_off_pull_requests(verify_yml: str) -> None:
    """A PR must be judged against a baseline it cannot move."""
    script = run_scripts(verify_yml)
    guard = re.search(
        r'if \[ "\$\{GITHUB_EVENT_NAME:-\}" != "pull_request" \]; then\s*\n'
        r"\s*args\+=\(--update-baseline\)",
        script,
    )
    assert guard, "--update-baseline is not guarded by a non-pull_request event check"


def test_ci_builds_pict_from_the_pinned_commit(ci_yml: str) -> None:
    pin = re.search(r'CV_PICT_COMMIT="([0-9a-f]{40})"', INSTALL_PICT.read_text(encoding="utf-8"))
    assert pin, "install_pict.sh does not pin a PICT commit"
    assert "https://github.com/microsoft/pict" in ci_yml
    assert pin.group(1) in ci_yml, "CI and install_pict.sh build different PICT commits"
    assert 'CV_PICT_BIN=$HOME/.cache/cv-infra/pict/pict" >> "$GITHUB_ENV' in ci_yml


@pytest.mark.parametrize("workflow", [VERIFY_YML, CI_YML], ids=lambda path: path.name)
def test_every_action_is_pinned_to_a_commit_sha(workflow: Path) -> None:
    # Anchored at the start of the line so the header comment's consumer TEMPLATE
    # (`uses: …/verify.yml@v2` — a tag ref, and correct there) is not read as wiring.
    used = re.findall(r"^\s*(?:- )?uses: (\S+)", workflow.read_text(encoding="utf-8"), re.M)
    assert used, f"{workflow.name} declares no actions — the pin check would be vacuous"
    for reference in used:
        assert re.fullmatch(r"[^@]+@[0-9a-f]{40}", reference), f"not SHA-pinned: {reference}"
