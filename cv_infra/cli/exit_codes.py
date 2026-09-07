"""The exit-code contract, and the two tables that read off it.

Four codes, and the split between them is the whole developer experience::

    0  PASS      every judged check was true, and none regressed
    1  FAIL      a judged check was false, or a check regressed vs its baseline
    2  CONTRACT  the REQUEST was refused (bad flag, bad model, empty gate) — 0 GPU seconds
    3  INFRA     the PLATFORM could not judge (no consent, no PICT, no docker, all cases ERROR)

"your request is wrong" (2), "your robot failed" (1) and "our platform broke" (3) stay
distinct so a developer never reads an infrastructure fault as a self-regression — which
is also why exit 3 maps to a NEUTRAL Check conclusion and never to a failing one.

Dependency-0 leaf (stdlib only): the report renderer and the CLI both import these
tables instead of re-deriving them, so the exit code, the Check conclusion and the
report's own outcome label can never disagree about one run.
"""

from __future__ import annotations

EXIT_PASS = 0
EXIT_FAIL = 1
EXIT_CONTRACT = 2
EXIT_INFRA = 3

#: exit code -> GitHub Check Run conclusion. THE single source: ``report/github.py``
#: imports this rather than mapping a verdict itself. Exit 3 -> ``neutral`` (GitHub's
#: not-a-pass-not-a-fail state): an infra failure is never collapsed into "failure".
CHECK_CONCLUSION_BY_EXIT: dict[int, str] = {
    EXIT_PASS: "success",
    EXIT_FAIL: "failure",
    EXIT_CONTRACT: "failure",
    EXIT_INFRA: "neutral",
}

#: exit code -> ``summary.report_outcome`` in the report JSON. A label for readers;
#: ``summary.exit_code`` remains the single machine-readable source (report schema 1).
REPORT_OUTCOME_BY_EXIT: dict[int, str] = {
    EXIT_PASS: "pass",
    EXIT_FAIL: "fail",
    EXIT_CONTRACT: "errored",
    EXIT_INFRA: "errored",
}

#: Surfaced at the top of an exit-3 Check so the developer reads it as "our problem,
#: retry", not as their robot regressing.
INFRA_INCOMPLETE_MESSAGE = (
    "verification did not complete — platform/infrastructure problem, not a robot verdict"
)
