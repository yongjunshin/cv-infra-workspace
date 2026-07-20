"""Exit-code contract + CI Check-conclusion mapping — the M8 single source (LOCKED §9 / D-I).

Dependency-0 leaf module (stdlib only): every plane that needs the exit-code
contract or the exit→conclusion mapping imports FROM here and nothing here
imports back. ``cv_infra.cli.main`` re-exports the ``EXIT_*`` constants
(backward compat — older call sites do ``from cv_infra.cli.main import
EXIT_PASS`` etc.); ``cv_infra.cli.batch`` delegates ``exit_code_for_report_outcome``
/ ``REPORT_OUTCOME_EXIT`` here (재정의 금지); the M4 ``report/github.py`` renderer
imports ``CHECK_CONCLUSION_BY_EXIT`` to fold a verdict into a Check Run
conclusion WITHOUT re-deriving the table or pulling any heavy dependency
(M4-09 standalone: ``github.py`` stays token/network-free — keeping THIS module
free of ``cv_infra.orchestrator``/fastapi imports is exactly why it can).

Exit-code contract (LOCKED §9)::

    0  PASS      all verifications passed
    1  FAIL      verification failed OR regression vs baseline (a SUT verdict)
    2  CONTRACT  contract/validation error (bad YAML, unsupported apiVersion)
    3  INFRA     infrastructure error (orchestrator down, EULA not accepted,
                 runner crash) — NOT a SUT verdict

The 1-vs-2-vs-3 split is the core DX contract: "your YAML is wrong (2)", "your
robot failed (1)", "our platform broke (3)" stay distinct so a developer never
mistakes an infrastructure problem for a self-regression (exit 3 is never
collapsed into failure — D-I).
"""

from __future__ import annotations

from typing import Any

# --- exit-code contract (LOCKED §9) ----------------------------------------
EXIT_PASS = 0
EXIT_FAIL = 1
EXIT_CONTRACT = 2
EXIT_INFRA = 3

#: exit code -> GitHub Check Run conclusion (DoD-P5-13 verbatim, D-I). THE
#: single source: ``report/github.py`` imports this to set the conclusion — the
#: table is never re-derived anywhere else (enforced by a duplicate-literal
#: grep). Verbatim table: ``{0: "success", 1: "failure", 2: "failure",
#: 3: "neutral"}``. Exit 2 additionally drives a YAML-line inline annotation and
#: exit 3 additionally surfaces ``INFRA_INCOMPLETE_MESSAGE``; for the conclusion
#: FIELD itself the mapping is exactly this. Exit 3 -> "neutral" (GitHub's
#: not-a-pass-not-a-fail state = action_required to the developer): an infra
#: failure is NEVER collapsed into "failure" (D-I).
CHECK_CONCLUSION_BY_EXIT: dict[int, str] = {
    EXIT_PASS: "success",
    EXIT_FAIL: "failure",
    EXIT_CONTRACT: "failure",
    EXIT_INFRA: "neutral",
}

#: Surfaced with an exit-3 Check (neutral / action_required) so the developer
#: reads an infra failure as "our problem, retry", not a self-regression
#: (DoD-P5-13 verbatim).
INFRA_INCOMPLETE_MESSAGE = "플랫폼/인프라 문제로 검증 미완료 — SUT 판정 아님"

#: envelope-level ``report_outcome`` literal -> process exit code (D-I, M8-D11).
#: THE M8 single source for the report_outcome→exit fold — ``cli/batch.py``
#: imports this (재정의 금지). The literal keys mirror
#: ``orchestrator.api.REPORT_OUTCOME_{PASS,FAIL,ERRORED}`` (kept as bare strings
#: so this leaf stays free of the orchestrator/fastapi import graph); their
#: agreement with the api constants is cross-checked by
#: ``tests/test_cli_batch.py::test_report_outcome_exit_mapping_is_the_single_source``.
#: ``errored -> 3`` is where "exit 3 outranks 1" lands (the errored>fail
#: priority itself is folded upstream in ``api.report_outcome_of``).
REPORT_OUTCOME_EXIT: dict[str, int] = {
    "pass": EXIT_PASS,
    "fail": EXIT_FAIL,
    "errored": EXIT_INFRA,
}


def exit_code_for_report_outcome(outcome: Any) -> int:
    """Pure fold: terminal ``report_outcome`` -> process exit code.

    Unknown, non-str, or absent outcomes fold to ``EXIT_INFRA`` (3), NEVER to
    FAIL — the same rule ``main._VERDICT_EXIT`` applies to unknown verdicts (a
    value we cannot interpret is a platform problem, not a SUT judgement).
    """
    if isinstance(outcome, str) and outcome in REPORT_OUTCOME_EXIT:
        return REPORT_OUTCOME_EXIT[outcome]
    return EXIT_INFRA
