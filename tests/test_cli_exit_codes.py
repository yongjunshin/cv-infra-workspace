"""``cv_infra/cli/exit_codes.py`` — the M8 exit-code / Check-conclusion single
source (DoD-P5-13, LOCKED §9 / D-I). Stdlib + pytest.

This module is the dependency-0 leaf the M4 ``report/github.py`` renderer imports
for ``CHECK_CONCLUSION_BY_EXIT`` (the exit→conclusion half of the contract). Two
properties are pinned here: (1) the mapping literals are exactly the DoD-P5-13
table (and exit 3 is neutral, never failure — D-I), and (2) importing the leaf
pulls NO heavy dependency (so github.py stays token/network-free — M4-09).
"""

from __future__ import annotations

import os
import subprocess
import sys
import textwrap

from cv_infra.cli import exit_codes as ec


def test_exit_constants_are_the_0_1_2_3_contract():
    assert (ec.EXIT_PASS, ec.EXIT_FAIL, ec.EXIT_CONTRACT, ec.EXIT_INFRA) == (0, 1, 2, 3)


def test_check_conclusion_by_exit_is_p5_13_verbatim():
    # DoD-P5-13 verbatim: 0->success, 1->failure, 2->failure, 3->neutral.
    assert ec.CHECK_CONCLUSION_BY_EXIT == {0: "success", 1: "failure", 2: "failure", 3: "neutral"}
    # completeness: every exit code in the contract has a conclusion.
    assert set(ec.CHECK_CONCLUSION_BY_EXIT) == {
        ec.EXIT_PASS,
        ec.EXIT_FAIL,
        ec.EXIT_CONTRACT,
        ec.EXIT_INFRA,
    }


def test_infra_exit_is_neutral_never_collapsed_into_failure():
    # D-I: exit 3 (infra) is NEVER folded into failure — a developer must not
    # read a platform problem as a self-regression. exit 2 (contract) IS failure.
    assert ec.CHECK_CONCLUSION_BY_EXIT[ec.EXIT_INFRA] == "neutral"
    assert ec.CHECK_CONCLUSION_BY_EXIT[ec.EXIT_INFRA] != "failure"
    assert ec.CHECK_CONCLUSION_BY_EXIT[ec.EXIT_CONTRACT] == "failure"


def test_infra_incomplete_message_is_verbatim():
    assert ec.INFRA_INCOMPLETE_MESSAGE == "플랫폼/인프라 문제로 검증 미완료 — SUT 판정 아님"


def test_report_outcome_exit_fold():
    assert ec.REPORT_OUTCOME_EXIT == {"pass": 0, "fail": 1, "errored": 3}
    assert ec.exit_code_for_report_outcome("pass") == ec.EXIT_PASS
    assert ec.exit_code_for_report_outcome("fail") == ec.EXIT_FAIL
    # errored -> 3 (exit 3 outranks 1): infra noise never masquerades as regression.
    assert ec.exit_code_for_report_outcome("errored") == ec.EXIT_INFRA


def test_unknown_or_absent_outcome_folds_to_infra_never_fail():
    for bad in ("wat", "", None, 3, object()):
        assert ec.exit_code_for_report_outcome(bad) == ec.EXIT_INFRA


def test_leaf_import_pulls_no_heavy_dependency():
    """The M4 github.py renderer imports this leaf for the conclusion mapping and
    must NOT transitively pull the orchestrator/fastapi/httpx/pydantic graph
    (M4-09 standalone). A fresh interpreter that imports ONLY exit_codes proves
    the leaf is heavy-dep-free — mirrors the --help dependency-free probe."""
    probe = textwrap.dedent("""
        import sys
        import cv_infra.cli.exit_codes  # noqa: F401
        for mod in ("fastapi", "httpx", "pydantic", "cv_infra.orchestrator.api"):
            assert mod not in sys.modules, f"exit_codes leaf pulled {mod}"
        """)
    env = {k: v for k, v in os.environ.items() if k != "PYTHONPATH"}  # G-10 isolation
    proc = subprocess.run(
        [sys.executable, "-c", probe],
        capture_output=True,
        text=True,
        env=env,
    )
    assert proc.returncode == 0, proc.stderr
