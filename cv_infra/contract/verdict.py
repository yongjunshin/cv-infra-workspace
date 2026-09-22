"""Verdict typing — the oracle's flat dict becomes checks, metrics, nulls and notes.

There are ZERO reserved keys: the consumer names its own verdict keys and the VALUE'S
TYPE says what the key is. That is the whole schema:

===========  ==========================================================================
``bool``     a CHECK. The case passes when every check is true; a check's pass ratio is
             what the baseline compares across commits (a false check gates, exit 1).
number       a METRIC. Reported and compared, never gating (a threshold is the
             consumer's own check to write).
``null``     UNJUDGEABLE — the oracle could not decide this key for this run. It is
             excluded from the ratio; it is NOT a false check (false is a judgement,
             absence is not, and folding them together fabricates regressions).
``str``      a NOTE. Carried into the report so a human reads why.
===========  ==========================================================================

``isinstance(value, bool)`` is tested BEFORE the numeric test and this order is load
bearing: in Python ``bool`` IS a subclass of ``int``, so the natural order silently
files every check as a metric — the gate would then pass everything forever.

Anything else (a list, a nested dict) is that CASE's ERROR naming the key, not a
silently dropped value: flat dict only, because a nested verdict has no obvious
baseline key and would be compared against nothing.

Three lanes, from the run's exit codes alone (never from the sim's own exit status —
``SimulationApp.close()`` exits 0 whatever happened, and the stock ``python.sh``
squashes non-zero to 1, so ``rc_sim`` only ever separates "died badly" from "ran"):
``rc_sim`` bad -> ERROR, ``rc_oracle`` bad -> ERROR, otherwise parse the stdout.
"""

from __future__ import annotations

import json
from collections.abc import Iterable
from dataclasses import dataclass, field
from typing import Any

LANE_OK = "ok"
LANE_ERROR = "error"

#: How much of a rejected stdout is quoted back. Enough to see the last line that was
#: supposed to be JSON, short enough not to paste a boot log into a CI annotation.
TAIL_CHARS = 300


@dataclass(frozen=True)
class CaseRunResult:
    """One run, judged. ``lane`` is ERROR exactly when ``error`` is set.

    An ERROR run carries no checks/metrics: it is excluded from ratios and from
    regression comparison entirely (an infrastructure fault must not be recorded as the
    robot regressing), while still being counted and shown loudly in the report.
    """

    lane: str
    error: str | None = None
    checks: dict[str, bool] = field(default_factory=dict)
    metrics: dict[str, float] = field(default_factory=dict)
    nulls: list[str] = field(default_factory=list)
    notes: dict[str, str] = field(default_factory=dict)
    verdict: dict[str, Any] | None = None


def classify(
    rc_sim: int | None,
    rc_oracle: int | None,
    oracle_stdout: str | None,
    gate: bool,
) -> CaseRunResult:
    """Fold one run's exit codes + oracle stdout into its typed result.

    ``rc`` is None exactly when the container never exited on its own (timeout or an
    infrastructure fault before it could). ``gate`` is False in sweep mode (no oracle
    was declared): the run still succeeds or ERRORs, it just judges nothing.
    """
    if rc_sim != 0:
        return CaseRunResult(lane=LANE_ERROR, error=_rc_error("sim", rc_sim))
    if not gate:
        return CaseRunResult(lane=LANE_OK)
    if rc_oracle != 0:
        return CaseRunResult(lane=LANE_ERROR, error=_rc_error("oracle", rc_oracle))
    verdict = _last_json_object(oracle_stdout or "")
    if verdict is None:
        return CaseRunResult(
            lane=LANE_ERROR,
            error="the oracle printed no JSON object on stdout — the verdict is one "
            f"flat JSON dict on its own line. stdout tail: {_tail(oracle_stdout)}",
        )
    return _fold_types(verdict)


def has_boolean_check(results: Iterable[CaseRunResult]) -> bool:
    """Did ANY judged run produce a check?

    A gate whose verdicts are all metrics and notes cannot fail, so it would report a
    green check while asserting nothing. The caller rejects that (exit 2) unless
    ``--report-only`` says the emptiness is intended.

    The pipeline itself asks this question of the finished REPORT
    (``report.aggregate.exit_code_of``), where the ratios already exist — this is the
    same rule for a caller holding results in hand.
    """
    return any(result.checks for result in results)


# --- internals ------------------------------------------------------------------------


def _rc_error(what: str, rc: int | None) -> str:
    if rc is None:
        return f"the {what} container never exited (timed out, or it could not be run)"
    return f"the {what} exited rc={rc} — this case produced no trustworthy verdict"


def _last_json_object(stdout: str) -> dict[str, Any] | None:
    """The LAST line that parses as a JSON object.

    Last, because Isaac and Kit print to stdout long after the script's own output, and
    an oracle may echo progress before its verdict. Object, because a bare number or
    list on the final line is not a verdict — the search keeps walking backwards.
    """
    for line in reversed(stdout.splitlines()):
        text = line.strip()
        if not text:
            continue
        try:
            parsed = json.loads(text)
        except ValueError:
            continue
        if isinstance(parsed, dict):
            return parsed
    return None


def _tail(stdout: str | None) -> str:
    text = (stdout or "").strip()
    if not text:
        return "(empty)"
    return repr(text[-TAIL_CHARS:])


def _fold_types(verdict: dict[str, Any]) -> CaseRunResult:
    checks: dict[str, bool] = {}
    metrics: dict[str, float] = {}
    nulls: list[str] = []
    notes: dict[str, str] = {}
    for key, value in verdict.items():
        if isinstance(value, bool):  # BEFORE the numeric test — bool is a subclass of int
            checks[key] = value
        elif isinstance(value, (int, float)):
            metrics[key] = float(value)
        elif value is None:
            nulls.append(key)
        elif isinstance(value, str):
            notes[key] = value
        else:
            return CaseRunResult(
                lane=LANE_ERROR,
                error=f"verdict key '{key}' holds {type(value).__name__} — the verdict is "
                "a FLAT dict (bool=check, number=metric, null=unjudgeable, string=note)",
                verdict=verdict,
            )
    return CaseRunResult(
        lane=LANE_OK,
        checks=checks,
        metrics=metrics,
        nulls=nulls,
        notes=notes,
        verdict=verdict,
    )
