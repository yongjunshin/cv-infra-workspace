"""PICT covering-array case planning — the declared input space -> the case list.

The consumer declares its input space as a Microsoft PICT model file that rides
along with the request (same ride-along rule as a custom oracle module). This
module turns that file into the CASES a request fans out to, under a wallclock
budget. It is the ONLY place that knows what a covering array is; everything
downstream (fan-out, identity, rollup, report) sees a plain list of cases and is
unchanged.

**do-not-reinvent** (CLAUDE.md §3): PICT itself is REUSED, never reimplemented —
it is MIT-licensed, dependency-free C++11, and builds to a single ~380 KB binary
that the images pin by commit. This module shells out to it and owns only the
three things PICT does not do:

* **budget -> order.** PICT answers "how many cases for order k", never "what k
  fits in 3 hours". ``plan`` generates the k it was ASKED for and truncates the
  array when the budget cannot hold it; walking k downward is the opt-in
  ``orders="auto"`` mode, because a silently downgraded k is a coverage claim
  the report would still print as satisfied.
* **achieved coverage.** PICT's ``/s`` reports its own combination count, not
  the coverage of a TRUNCATED prefix. A budget-cut suite must report what it
  actually covered, so ``coverage_of_prefix`` recomputes it from the rows the
  suite actually ran, normalised against what the FULL array realises.
* **friendly errors.** PICT rejects with terse prose and NO line number
  ("Input Error: Parameter/value type mismatch: ..."), and it has no opinion at
  all about a parameter name that cannot become ``--<name>=<value>`` on the sim
  script's command line. The request surface owes the file/line/column
  treatment every other stage gives, so ``validate_model``
  pre-checks the shape and locates PICT's own complaint.

MEASURED anchors for the numbers in these docstrings (2026-09-07, the go2 patrol
space: 8 parameters, 2 constraints, 1,296-cell grid):

    k=2 -> 14 rows   k=3 -> 42 rows   k=4 -> 112 rows

and the prefix-truncation curve that makes ``plan``'s truncation honest rather
than arbitrary — PICT generates greedily, so a prefix is a real partial covering
array: 6/14 rows = 76.0 %, 10/14 = 94.2 %, 14/14 = 100 %.

Stdlib only (subprocess + tempfile + itertools + hashlib): the contract is the
foundational layer and must import no sibling package (``.importlinter``). The
binary is resolved lazily, so importing this module on a host without PICT is
fine — only ``generate`` needs it.
"""

from __future__ import annotations

import itertools
import os
import re
import shutil
import subprocess
import tempfile
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from cv_infra.contract.errors import ContractError

#: Env override for the pinned binary (deployment resolves it; tests inject it).
PICT_BIN_ENV = "CV_PICT_BIN"

#: The orders ``plan(orders="auto")`` will try, richest first. 2 = pairwise is the
#: floor: below it there is no combinatorial claim left to make, only a
#: value-coverage one.
DEFAULT_ORDERS: tuple[int, ...] = (3, 2)

#: Repeats floor for the ``orders="auto"`` branch ONLY. This project MEASURED
#: flakiness 0.333 on the batch path (2026-09-01: one document 1/3 then 3/3
#: sixteen minutes apart on the same image and host), so a suite that spends its
#: whole budget on distinct cases run ONCE is a row of coin flips. When the caller
#: hands ``plan`` a budget to reduce against, cases are cut before repeats fall
#: below this — but a DECLARED ``repeats`` is honoured verbatim (the workflow's
#: ``repeats:`` input means what it says; the operator owns that trade).
MIN_REPEATS = 3

#: An axis becomes ``--<name>=<value>`` in the sim script's own argv, so a name that
#: is not a legal long flag produces an unrunnable command line, and ``help``/``h``
#: collide with the argparse every standard script has.
RESERVED_AXIS_NAMES = frozenset({"help", "h"})
_FLAG_SAFE_AXIS = re.compile(r"^[A-Za-z][A-Za-z0-9_-]*$")

_PARAM_LINE = re.compile(r"^\s*([^#:{}][^:]*?)\s*:\s*(.+?)\s*$")
_CONSTRAINT_START = re.compile(r"^\s*(IF|#\s*constraint|\[)", re.IGNORECASE)
_BRACKETED = re.compile(r"\[([^\]]+)\]")


class PictError(ContractError):
    """A rejected input-space model (exit-2-eligible, like any stage-1..5 reject).

    Adapts this module's (problem, hint, line) shape onto the canonical
    ``ContractError`` fields so a bad model renders through the SAME friendly
    surface as every other stage — one-liner on the CLI, inline annotation in
    CI. ``hint`` is what the document should have said, so it
    lands in ``expected``; ``problem`` is what it did say, so it lands in ``got``.
    """

    def __init__(
        self,
        problem: str,
        *,
        hint: str = "",
        source_path: str | None = None,
        line: int | None = None,
        field_path: str = "space.model",
    ) -> None:
        super().__init__(
            field_path=field_path,
            expected=hint or "a valid PICT input-space model",
            got=problem,
            source_path=source_path,
            source_line=line,
        )


@dataclass(frozen=True)
class CoveringArray:
    """One generated array: the parameter names and the rows PICT emitted."""

    parameters: tuple[str, ...]
    rows: tuple[tuple[str, ...], ...]
    order: int

    def __len__(self) -> int:
        return len(self.rows)

    def as_dicts(self) -> list[dict[str, str]]:
        """Rows as ``{parameter: value}`` — the shape ``cases.expand`` substitutes from."""
        return [dict(zip(self.parameters, row, strict=True)) for row in self.rows]

    def to_tsv(self) -> str:
        """PICT's own TSV, byte-identical to its stdout — the form ``/e`` seeds from."""
        return "\n".join("\t".join(r) for r in (self.parameters, *self.rows)) + "\n"


@dataclass(frozen=True)
class Budget:
    """What the consumer is willing to spend. Time is the real constraint.

    ``repeats`` defaults to the contract's own default (the workflow's ``repeats:``
    input is 1), NOT to ``MIN_REPEATS``: a floor hidden in a dataclass default is the
    same silent second-guessing the default ``plan`` branch stopped doing. The floor
    lives in the ``orders="auto"`` budget-reduction branch and nowhere else.
    """

    wallclock_s: float
    repeats: int = 1
    max_cases: int | None = None


@dataclass(frozen=True)
class CasePlan:
    """The chosen suite plus the honest account of what it gave up to fit."""

    array: CoveringArray
    requested_order: int
    truncated_from: int | None
    coverage: float
    repeats: int
    est_wallclock_s: float

    @property
    def cases(self) -> int:
        return len(self.array)

    @property
    def runs(self) -> int:
        return self.cases * self.repeats

    def summary(self) -> str:
        """One line for the CI check surface. Truncation is never silent."""
        head = (
            f"{self.cases} cases x {self.repeats} repeats = {self.runs} runs, "
            f"{self.requested_order}-wise"
        )
        if self.truncated_from is not None:
            head += f" TRUNCATED from {self.truncated_from} rows, {self.coverage:.1%} coverage"
        else:
            head += f", {self.coverage:.1%} coverage"
        return f"{head}, est {self.est_wallclock_s / 3600:.1f} h"


def resolve_binary(pict_bin: str | os.PathLike[str] | None = None) -> str:
    """The pinned PICT binary, or a loud reject naming how to supply it."""
    candidate = pict_bin or os.environ.get(PICT_BIN_ENV) or shutil.which("pict")
    if not candidate:
        raise PictError(
            "PICT binary not found — the input-space model cannot be expanded.",
            hint=(
                f"set ${PICT_BIN_ENV} to the pinned binary, or put `pict` on PATH. "
                "The images vendor it by commit (do-not-reinvent: PICT is reused, "
                "not reimplemented)."
            ),
        )
    resolved = Path(candidate)
    if not resolved.is_file() or not os.access(resolved, os.X_OK):
        raise PictError(
            f"PICT binary {resolved} is not an executable file.",
            hint=f"${PICT_BIN_ENV} must point at the built pict executable.",
        )
    return str(resolved)


def validate_model(model_text: str, *, source_path: str | None = None) -> None:
    """Pre-check the model and reject with file/line — PICT itself gives neither.

    Catches the five shapes that actually bite (the first four measured against
    the real binary on 2026-09-07):

    1. a parameter declared AFTER the first constraint (PICT: "Missing opening
       bracket or misplaced keyword", no line);
    2. a duplicate parameter name (PICT silently keeps one);
    3. an empty value list;
    4. a constraint referencing a parameter that was never declared (PICT:
       "Input Error", no line);
    5. a parameter name that cannot become ``--<name>=<value>`` in the sim
       script's argv. PICT accepts anything here — the breakage surfaces much
       later as an unparseable command line inside the GPU container, so it is
       rejected at admit time with the line that declared it.

    Type mismatches (a quoted numeric in a constraint) are left to PICT — its
    own message names the offending clause, and ``generate`` locates the line.
    """
    declared: dict[str, int] = {}
    first_constraint: int | None = None
    for lineno, raw in enumerate(model_text.splitlines(), start=1):
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if _CONSTRAINT_START.match(line) and ":" not in line.split("]")[0]:
            first_constraint = first_constraint or lineno
            for name in _BRACKETED.findall(line):
                if name.strip() not in declared:
                    raise PictError(
                        f"constraint references undeclared parameter [{name.strip()}].",
                        source_path=source_path,
                        line=lineno,
                        hint=f"declared parameters: {', '.join(declared) or '(none)'}",
                    )
            continue
        match = _PARAM_LINE.match(line)
        if not match:
            continue
        name, values = match.group(1).strip(), match.group(2).strip()
        if not _FLAG_SAFE_AXIS.match(name) or name in RESERVED_AXIS_NAMES:
            reason = (
                "collides with the sim script's own --help/-h"
                if name in RESERVED_AXIS_NAMES
                else "is not a usable long-flag name"
            )
            raise PictError(
                f"axis '{name}' {reason}.",
                source_path=source_path,
                line=lineno,
                hint="each axis becomes `--<name>=<value>` in the sim script's argv, so a "
                "name must match [A-Za-z][A-Za-z0-9_-]* and must not be `help` or `h`",
            )
        if first_constraint is not None:
            raise PictError(
                f"parameter '{name}' is declared after the first constraint "
                f"(line {first_constraint}).",
                source_path=source_path,
                line=lineno,
                hint=(
                    "PICT requires every parameter BEFORE the constraint section — "
                    "move this line up."
                ),
            )
        if name in declared:
            raise PictError(
                f"parameter '{name}' is declared twice (first at line {declared[name]}).",
                source_path=source_path,
                line=lineno,
                hint="PICT keeps only one of them silently — rename or remove the duplicate.",
            )
        if not [v for v in values.split(",") if v.strip()]:
            raise PictError(
                f"parameter '{name}' declares no values.",
                source_path=source_path,
                line=lineno,
                hint="a parameter needs at least one value: `name: a, b, c`",
            )
        declared[name] = lineno
    if not declared:
        raise PictError(
            "the input-space model declares no parameters.",
            source_path=source_path,
            hint="one `name: value, value` line per axis; constraints follow them.",
        )


def generate(
    model_text: str,
    *,
    order: int = 2,
    seed_rows: CoveringArray | None = None,
    pict_bin: str | os.PathLike[str] | None = None,
    source_path: str | None = None,
) -> CoveringArray:
    """Expand one model into its covering array of the given order.

    ``seed_rows`` is passed to PICT's ``/e`` seeding, and it is not an
    optimization — it is what keeps a suite's regression history alive. MEASURED
    (2026-09-07): adding ONE parameter to an 8-parameter model regenerated 13
    rows of which **zero** matched the previous 14 on their shared columns; with
    the previous array seeded, all 14 survived and the new axis still got
    covered. A baseline keyed on a case that no longer exists is an absent
    baseline, and an absent baseline SKIPS (normal, never a failure) — i.e. without
    seeding the gate goes quiet on every model edit instead of failing loudly.
    """
    validate_model(model_text, source_path=source_path)
    binary = resolve_binary(pict_bin)
    with tempfile.TemporaryDirectory(prefix="cv-pict-") as tmp:
        model_path = Path(tmp) / "model.pict"
        model_path.write_text(model_text, encoding="utf-8")
        argv = [binary, str(model_path), f"/o:{order}"]
        if seed_rows is not None:
            seed_path = Path(tmp) / "seed.tsv"
            seed_path.write_text(seed_rows.to_tsv(), encoding="utf-8")
            argv.append(f"/e:{seed_path}")
        proc = subprocess.run(argv, capture_output=True, text=True, timeout=300)  # noqa: S603
    if proc.returncode != 0:
        raise PictError(
            _pict_message(proc),
            source_path=source_path,
            line=_locate(model_text, proc.stdout + proc.stderr),
            hint="the model is valid PICT syntax only if `pict <model>` accepts it standalone.",
        )
    return _parse(proc.stdout, order=order, source_path=source_path)


def plan(
    model_text: str,
    *,
    budget: Budget,
    cost_s_per_run: float,
    concurrency: int = 1,
    order: int = 2,
    orders: Sequence[int] | Literal["auto"] | None = None,
    seed_rows: CoveringArray | None = None,
    pict_bin: str | os.PathLike[str] | None = None,
    source_path: str | None = None,
) -> CasePlan:
    """Fit the REQUESTED order into ``budget``, truncating only as a last resort.

    Default (``orders=None``): generate exactly ``order``-wise and, if the array
    does not fit, cut the array — never the order. A silently downgraded k is
    the worst of the failure modes available here, because the report keeps
    printing "requested k" while covering less than it claims, and nothing in CI
    is loud about it. Truncation is the honest alternative: the prefix is a real
    partial covering array and ``coverage_of_prefix`` says exactly how partial.

    ``orders="auto"`` opts back into the old walk-down (``DEFAULT_ORDERS``) for
    callers who would rather trade k than rows, and **only that branch** keeps
    the one design opinion this module ever had — repeats are cut last, never
    below ``MIN_REPEATS``: with MEASURED per-case flakiness 0.333, a 76 %-coverage
    suite whose verdicts are statistics beats a 100 %-coverage suite whose
    verdicts are single draws. Every other call honours ``budget.repeats``
    verbatim, an explicit ``orders=[...]`` sequence included: that sequence buys
    the walk-down and nothing else, because a caller that named a repeats number
    is not asking to be second-guessed by the shape of a different argument.

    Assumption, surfaced because the signature cannot: with ``orders="auto"`` the
    ``order`` argument is unused (the walk is ``DEFAULT_ORDERS``). Pass one or the
    other, not both.

    Cost per run comes from the caller, so this function stays pure and
    CPU-testable: no clock, no store, no probe.
    """
    if concurrency < 1:
        raise ValueError("concurrency must be >= 1")
    if budget.repeats < 1:
        raise ValueError("budget.repeats must be >= 1")
    repeats = max(budget.repeats, MIN_REPEATS) if orders == "auto" else budget.repeats
    per_case_s = cost_s_per_run * repeats / concurrency
    if per_case_s <= 0:
        raise ValueError("cost_s_per_run must be > 0")
    affordable = int(budget.wallclock_s // per_case_s)
    if budget.max_cases is not None:
        affordable = min(affordable, budget.max_cases)

    ordered = _orders_to_try(model_text, order=order, orders=orders)
    smallest: CoveringArray | None = None
    for candidate in ordered:
        array = generate(
            model_text,
            order=candidate,
            seed_rows=seed_rows,
            pict_bin=pict_bin,
            source_path=source_path,
        )
        smallest = array
        if len(array) <= affordable:
            return CasePlan(
                array=array,
                requested_order=candidate,
                truncated_from=None,
                coverage=1.0,
                repeats=repeats,
                est_wallclock_s=len(array) * per_case_s,
            )

    assert smallest is not None  # noqa: S101 - `ordered` is non-empty, so the loop ran
    if affordable < 1:
        raise PictError(
            f"the budget affords no cases at all: one case costs "
            f"{per_case_s / 3600:.2f} h ({repeats} repeats / concurrency {concurrency}) "
            f"but the budget is {budget.wallclock_s / 3600:.2f} h.",
            source_path=source_path,
            hint="raise budget.wallclock_s, lower repeats, or make a case cheaper.",
        )
    kept = CoveringArray(
        parameters=smallest.parameters, rows=smallest.rows[:affordable], order=ordered[-1]
    )
    return CasePlan(
        array=kept,
        requested_order=ordered[-1],
        truncated_from=len(smallest),
        coverage=coverage_of_prefix(smallest, affordable, ordered[-1]),
        repeats=repeats,
        est_wallclock_s=len(kept) * per_case_s,
    )


def coverage_of_prefix(array: CoveringArray, keep: int, order: int) -> float:
    """Order-t coverage a greedy PREFIX of ``array`` retains (see module doc)."""
    width = len(array.parameters)
    full = _combinations(array.rows, width, order)
    if not full:
        return 1.0
    return len(_combinations(array.rows[:keep], width, order)) / len(full)


# --- internals ------------------------------------------------------------------------


def _orders_to_try(
    model_text: str, *, order: int, orders: Sequence[int] | Literal["auto"] | None
) -> list[int]:
    """The order(s) ``plan`` will generate, richest first.

    ``None`` means the single requested order and no clamping: an order wider
    than the space is PICT's own loud reject ("Order cannot be larger than
    number of parameters"), which ``inputs`` pre-empts with a friendlier exit-2
    from ``_declared_parameters``. The walk-down branch clamps instead, because
    there the whole point is to land on SOME order that a 2-axis space can hold.

    A string other than ``"auto"`` is a typo, not a sequence of orders: name it
    here rather than let ``int(o)`` die on one of its characters.
    """
    if orders is None:
        return [int(order)]
    if isinstance(orders, str) and orders != "auto":
        raise ValueError(f'orders must be "auto" or a sequence of ints, not {orders!r}')
    wanted = DEFAULT_ORDERS if orders == "auto" else orders
    ordered = sorted({int(o) for o in wanted}, reverse=True)
    if not ordered:
        raise ValueError("orders must not be empty")
    width = len(_declared_parameters(model_text))
    return [o for o in ordered if o <= width] or [min(ordered[-1], width)]


def _declared_parameters(model_text: str) -> list[str]:
    """Parameter names the model declares, in order — read without invoking PICT.

    Used to clamp a requested order to what the space can hold; the shape rules
    it relies on are the ones ``validate_model`` has already enforced.
    """
    names: list[str] = []
    for raw in model_text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or _CONSTRAINT_START.match(line):
            continue
        match = _PARAM_LINE.match(line)
        if match:
            names.append(match.group(1).strip())
    return names


def _combinations(rows: Iterable[Sequence[str]], width: int, order: int) -> set[tuple]:
    """The set of order-t (position, value) tuples the rows realise."""
    seen: set[tuple] = set()
    axes = list(itertools.combinations(range(width), order))
    for row in rows:
        for combo in axes:
            seen.add((combo, tuple(row[i] for i in combo)))
    return seen


def _parse(stdout: str, *, order: int, source_path: str | None) -> CoveringArray:
    lines = [ln for ln in stdout.splitlines() if ln.strip()]
    if not lines:
        raise PictError(
            "PICT produced no rows for this model.",
            source_path=source_path,
            hint="constraints may have excluded every combination — relax them and retry.",
        )
    header = tuple(lines[0].split("\t"))
    rows = tuple(tuple(ln.split("\t")) for ln in lines[1:])
    bad = next((r for r in rows if len(r) != len(header)), None)
    if bad is not None:
        raise PictError(
            f"PICT emitted a row with {len(bad)} values for {len(header)} parameters.",
            source_path=source_path,
            hint="a value containing a TAB breaks the output format — remove it from the model.",
        )
    return CoveringArray(parameters=header, rows=rows, order=order)


def _pict_message(proc: subprocess.CompletedProcess[str]) -> str:
    """PICT's first line of prose, or the exit code when it rejected silently.

    The text is stripped first, so its first line is the first line that says
    anything — the old blank-line skip inside the loop was unreachable.
    """
    text = (proc.stdout + "\n" + proc.stderr).strip()
    if not text:
        return f"PICT exited {proc.returncode} without a message."
    return text.splitlines()[0].strip()


def _locate(model_text: str, output: str) -> int | None:
    """PICT quotes the offending clause but never its line — find it ourselves."""
    quoted = output.split(":")[-1].strip()
    if not quoted:
        return None
    for lineno, line in enumerate(model_text.splitlines(), start=1):
        if quoted and quoted in line:
            return lineno
    return None
