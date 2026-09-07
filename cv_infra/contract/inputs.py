"""Admit-time input contract for ``cv-infra verify`` — every rejection in one place.

One request = the CLI flags (1:1 with the reusable workflow's ``with:`` inputs) plus
the operator environment. This module is the ONLY gate between them and the run, and
it is where the GPU cost of a bad request is decided: everything it rejects costs
**zero GPU seconds**, so the checks are deliberately eager (read the model, stat the
paths, resolve the binary) rather than discovered a container later.

Two exception classes, because the developer's next action differs:

* ``ContractError`` (exit 2) — the REQUEST is wrong. Friendly field/expected/got with a
  fixable example, so the message names the flag to change (NFR-INTAKE-001).
* ``InfraError`` (exit 3) — the RUNNER is wrong: operator consent env is absent, or the
  pinned PICT binary is not installed. Blaming the consumer's document for either would
  send a developer to fix a file that is not broken (D-I, exit 3 is never a SUT verdict).

Order is a decision, not an accident: consent is checked BEFORE anything else (Isaac
Sim's EULA/telemetry consent is the operator's, never baked into this repository, so a
run without it must not even parse its way toward a container), and the PICT binary is
resolved LAST (only a request whose model was accepted needs it).

The headless check is a WARNING, never a rejection: the container has no display, so a
GUI boot hangs or dies — but "the text mentions headless=False" is a grep, not proof
(the flag may be behind a ``--gui`` toggle that CI never passes). A grep-strength
signal earns a warning; the ERROR/timeout lane already covers the real failure.

Stdlib only, ``cv_infra.contract`` siblings aside: the contract stays the foundational
layer (``.importlinter``).
"""

from __future__ import annotations

import argparse
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import NoReturn

from cv_infra.contract import pict
from cv_infra.contract.errors import ContractError

# The pinned stock image. DUPLICATED (not imported) from ``cv_infra.execution``: the
# contract is the lowest layer and must import no sibling module, so the two literals
# are held equal by a test instead of by an import (tests/test_inputs.py).
DEFAULT_SIM_IMAGE = (
    "nvcr.io/nvidia/isaac-sim:5.1.0"
    "@sha256:f3563cb2ba0c18af0b2fb321360dcb73a917b899f879e3213623d6bee484fa54"
)

DEFAULT_CHECKOUT = "."
DEFAULT_RUN_DIR = "./.cv-infra-run"
DEFAULT_PICT_K = 2
DEFAULT_REPEATS = 1
DEFAULT_CONCURRENCY = 1
DEFAULT_CASE_TIMEOUT_S = 1800.0
DEFAULT_ORACLE_TIMEOUT_S = 300.0
DEFAULT_SHM_SIZE = "8g"
DEFAULT_MAX_ZIP_MB = 512

#: Operator consent (exit-3 gate). Never defaulted, never inferred — see the module doc.
CONSENT_ENV_KEYS: tuple[str, ...] = ("ACCEPT_EULA", "PRIVACY_CONSENT")

BASELINE_DB_ENV = "CV_BASELINE_DB"
DEFAULT_BASELINE_DB = "~/.cv-infra/baselines.sqlite3"

#: GitHub stamps the verified commit here; it labels the report and any baseline row.
COMMIT_SHA_ENV = "GITHUB_SHA"

#: An axis becomes ``--<name>=<value>`` in the sim script's own argv, so a name that is
#: not a legal long flag produces an unrunnable command line, and ``help``/``h`` collide
#: with the argparse every standard script has.
RESERVED_AXIS_NAMES = frozenset({"help", "h"})
_FLAG_SAFE_AXIS = re.compile(r"^[A-Za-z][A-Za-z0-9_-]*$")

#: Static headless heuristic (warning only — see the module doc).
_HEADLESS_FALSE = re.compile(r"headless.*False")


class InfraError(RuntimeError):
    """A precondition of the RUN, not of the request — exit 3, never exit 2.

    Carries a plain operator-facing message: unlike a ``ContractError`` there is no
    field to point at, because nothing in the consumer's document is wrong.
    """


@dataclass(frozen=True)
class VerifySpec:
    """One accepted request: every value the pipeline needs, already validated.

    Paths that travel INTO the container (``sim_script`` / ``sim_input_space`` /
    ``sim_output_dir`` / ``oracle_script``) stay checkout-relative strings, because the
    container's working dir is the checkout mount — the same string means the same file
    in CI and in a consumer's local ``./python.sh verify/sim.py --x=1`` (local parity).
    ``checkout`` / ``run_dir`` / ``baseline_db`` are resolved host paths.

    ``input_space_text`` is the model as READ AT ADMIT, so the plan expands exactly the
    bytes that were validated (a re-read could pick up a different file).
    """

    checkout: Path
    sim_script: str
    sim_input_space: str
    sim_output_dir: str
    oracle_script: str | None
    input_space_text: str
    pict_k: int
    repeats: int
    budget_s: float | None
    sim_image: str
    concurrency: int
    report_only: bool
    update_baseline: bool
    run_dir: Path
    case_timeout_s: float
    oracle_timeout_s: float
    shm_size: str
    max_zip_mb: int
    baseline_db: Path
    pict_bin: str
    checkout_sha: str | None
    warnings: tuple[str, ...] = ()

    @property
    def mode(self) -> str:
        """``gate`` when an oracle can judge, else ``sweep`` (runs, never gates)."""
        return "gate" if self.oracle_script else "sweep"


def parse(
    argv: Sequence[str],
    environ: Mapping[str, str],
    checkout_base: Path | str | None = None,
) -> VerifySpec:
    """Flags + environment -> a validated ``VerifySpec``, or a loud rejection.

    ``checkout_base`` is the directory a RELATIVE ``--checkout`` / ``--run-dir``
    resolves against (default: the process cwd) — it exists so tests and callers that
    already know the repo root do not have to chdir.
    """
    _require_consent(environ)
    args = _parser().parse_args(list(argv))
    base = Path.cwd() if checkout_base is None else Path(checkout_base)
    checkout = _resolve_under(base, args.checkout)
    if not checkout.is_dir():
        raise ContractError(
            field_path="--checkout",
            expected="an existing directory (the consumer checkout root)",
            got=str(checkout),
            example="--checkout .",
        )

    sim_script = _checkout_file(
        args.sim_script,
        checkout,
        flag="--sim-script",
        what="the Isaac standalone sim script",
        example="verify/sim.py",
    )
    sim_input_space = _checkout_file(
        args.input_space,
        checkout,
        flag="--input-space",
        what="the PICT input-space model",
        example="verify/param_space.pict",
    )
    oracle_script = (
        None
        if args.oracle_script is None
        else _checkout_file(
            args.oracle_script,
            checkout,
            flag="--oracle-script",
            what="the oracle script (omit it to run a non-gating sweep)",
            example="verify/oracle.py",
        )
    )
    sim_output_dir = _checkout_output_dir(args.output_dir, checkout)

    input_space_text = (checkout / sim_input_space).read_text(encoding="utf-8")
    pict.validate_model(input_space_text, source_path=sim_input_space)
    axes = pict._declared_parameters(input_space_text)
    _reject_unsafe_axis_names(axes, input_space_text, source_path=sim_input_space)

    pict_k = _bounded_int(args.pict_k, flag="--pict-k", minimum=1, example="2")
    if pict_k > len(axes):
        raise ContractError(
            field_path="--pict-k",
            expected=f"at most {len(axes)} (the model declares {len(axes)} axes)",
            got=str(pict_k),
            example=f"--pict-k {len(axes)}",
            source_path=sim_input_space,
        )

    return VerifySpec(
        checkout=checkout,
        sim_script=sim_script,
        sim_input_space=sim_input_space,
        sim_output_dir=sim_output_dir,
        oracle_script=oracle_script,
        input_space_text=input_space_text,
        pict_k=pict_k,
        repeats=_bounded_int(args.repeats, flag="--repeats", minimum=1, example="3"),
        budget_s=(
            None
            if args.budget_s is None
            else _bounded_float(args.budget_s, flag="--budget-s", example="10800")
        ),
        sim_image=args.sim_image,
        concurrency=_bounded_int(args.concurrency, flag="--concurrency", minimum=1, example="2"),
        report_only=args.report_only,
        update_baseline=args.update_baseline,
        run_dir=_resolve_under(base, args.run_dir),
        case_timeout_s=_bounded_float(args.case_timeout_s, flag="--case-timeout-s", example="1800"),
        oracle_timeout_s=_bounded_float(
            args.oracle_timeout_s, flag="--oracle-timeout-s", example="300"
        ),
        shm_size=args.shm_size,
        max_zip_mb=_bounded_int(args.max_zip_mb, flag="--max-zip-mb", minimum=1, example="512"),
        baseline_db=Path(environ.get(BASELINE_DB_ENV) or DEFAULT_BASELINE_DB).expanduser(),
        pict_bin=_resolve_pict_binary(environ),
        checkout_sha=environ.get(COMMIT_SHA_ENV) or None,
        warnings=_headless_warnings(checkout / sim_script, sim_script),
    )


# --- the flag surface -----------------------------------------------------------------


class _Parser(argparse.ArgumentParser):
    """argparse that rejects through the contract surface instead of ``sys.exit``.

    An unknown flag or a missing value is a request error like any other, so it must
    render as the same friendly one-liner (and become the same CI annotation) rather
    than as argparse's bare usage dump on stderr.
    """

    def error(self, message: str) -> NoReturn:
        raise ContractError(
            field_path="(arguments)",
            expected="the documented `cv-infra verify` flags",
            got=message,
            example="--sim-script verify/sim.py --input-space verify/param_space.pict "
            "--output-dir verify/out",
        )


def _parser() -> _Parser:
    """Every value arrives as a STRING: numeric conversion is ours, so a bad number is a
    friendly field/expected/got rather than argparse's ``invalid int value`` on stderr.
    """
    parser = _Parser(prog="cv-infra verify")
    parser.add_argument("--sim-script")
    parser.add_argument("--input-space")
    parser.add_argument("--output-dir")
    parser.add_argument("--oracle-script")
    parser.add_argument("--pict-k", default=str(DEFAULT_PICT_K))
    parser.add_argument("--repeats", default=str(DEFAULT_REPEATS))
    parser.add_argument("--budget-s")
    parser.add_argument("--sim-image", default=DEFAULT_SIM_IMAGE)
    parser.add_argument("--concurrency", default=str(DEFAULT_CONCURRENCY))
    parser.add_argument("--report-only", action="store_true")
    parser.add_argument("--update-baseline", action="store_true")
    parser.add_argument("--checkout", default=DEFAULT_CHECKOUT)
    parser.add_argument("--run-dir", default=DEFAULT_RUN_DIR)
    parser.add_argument("--case-timeout-s", default=str(DEFAULT_CASE_TIMEOUT_S))
    parser.add_argument("--oracle-timeout-s", default=str(DEFAULT_ORACLE_TIMEOUT_S))
    parser.add_argument("--shm-size", default=DEFAULT_SHM_SIZE)
    parser.add_argument("--max-zip-mb", default=str(DEFAULT_MAX_ZIP_MB))
    return parser


# --- the exit-3 gates -----------------------------------------------------------------


def _require_consent(environ: Mapping[str, str]) -> None:
    missing = [key for key in CONSENT_ENV_KEYS if not (environ.get(key) or "").strip()]
    if missing:
        raise InfraError(
            f"{', '.join(missing)} not set — Isaac Sim's EULA and telemetry consent "
            "belong to the OPERATOR of the runner and are never baked into this "
            "repository (a value written down here would be this project consenting "
            "on someone else's behalf). Export both variables in the runner's "
            "environment and re-run."
        )


def _resolve_pict_binary(environ: Mapping[str, str]) -> str:
    """The pinned PICT binary — absence is the runner's problem (exit 3), not the model's.

    ``pict.resolve_binary`` reports it as a ``PictError`` because inside that module the
    only caller is a model expansion; here it is a provisioning fact, so it is re-raised
    as ``InfraError`` (scripts/workstation_setup installs the pinned commit).
    """
    try:
        return pict.resolve_binary(environ.get(pict.PICT_BIN_ENV))
    except pict.PictError as exc:
        raise InfraError(str(exc)) from exc


# --- validation helpers ---------------------------------------------------------------


def _resolve_under(base: Path, raw: str) -> Path:
    path = Path(raw).expanduser()
    return (path if path.is_absolute() else base / path).resolve()


def _checkout_relative(value: str | None, *, flag: str, what: str, example: str) -> str:
    """A strict relative subpath of the checkout — the only shape the container can see.

    Absolute paths, ``..`` and ``.`` are refused rather than normalised: the string is
    handed to the container verbatim (working dir = the checkout mount), so anything
    that escapes the checkout on the host would silently mean a different file there.
    """
    raw = (value or "").strip()
    segments = raw.rstrip("/").split("/")
    if not raw or raw.startswith("/") or any(seg in ("", ".", "..") for seg in segments):
        raise ContractError(
            field_path=flag,
            expected=f"{what}, as a path relative to the checkout root (no leading '/', "
            "no '..', no '.')",
            got=raw or "(missing)",
            example=f"{flag} {example}",
        )
    return raw.rstrip("/")


def _checkout_file(value: str | None, checkout: Path, *, flag: str, what: str, example: str) -> str:
    relative = _checkout_relative(value, flag=flag, what=what, example=example)
    if not (checkout / relative).is_file():
        raise ContractError(
            field_path=flag,
            expected=f"{what}; the path must exist in the checkout",
            got=f"{relative} (not a file under {checkout})",
            example=f"{flag} {example}",
        )
    return relative


def _checkout_output_dir(value: str | None, checkout: Path) -> str:
    """The output dir must EXIST in the checkout: the case's host dir is bind-mounted on
    top of it, and the checkout is mounted read-only — dockerd cannot create the mount
    point inside a ``:ro`` bind, so an uncommitted directory fails at container start
    with an unreadable docker error instead of here.
    """
    relative = _checkout_relative(
        value,
        flag="--output-dir",
        what="the directory the sim script writes its artifacts to",
        example="verify/out",
    )
    if not (checkout / relative).is_dir():
        raise ContractError(
            field_path="--output-dir",
            expected="a directory that exists in the checkout (commit a `.gitkeep` "
            "inside it — git does not track empty directories)",
            got=f"{relative} (not a directory under {checkout})",
            example="--output-dir verify/out",
        )
    return relative


def _bounded_int(raw: str, *, flag: str, minimum: int, example: str) -> int:
    try:
        value: int | None = int(raw)
    except (TypeError, ValueError):
        value = None
    if value is None or value < minimum:
        raise ContractError(
            field_path=flag,
            expected=f"an integer >= {minimum}",
            got=repr(raw),
            example=f"{flag} {example}",
        )
    return value


def _bounded_float(raw: str, *, flag: str, example: str) -> float:
    try:
        value: float | None = float(raw)
    except (TypeError, ValueError):
        value = None
    if value is None or value <= 0:
        raise ContractError(
            field_path=flag,
            expected="a positive number of seconds",
            got=repr(raw),
            example=f"{flag} {example}",
        )
    return value


def _reject_unsafe_axis_names(axes: Sequence[str], model_text: str, *, source_path: str) -> None:
    """Every axis name must survive becoming ``--<name>=<value>`` in the script's argv.

    M5 moves this into ``pict.validate_model`` (where the rest of the model shape is
    checked); it lives here for now so the rejection exists before that refactor.
    """
    for name in axes:
        if _FLAG_SAFE_AXIS.match(name) and name not in RESERVED_AXIS_NAMES:
            continue
        reason = (
            "collides with the sim script's own --help/-h"
            if name in RESERVED_AXIS_NAMES
            else "is not a usable long-flag name"
        )
        raise pict.PictError(
            f"axis '{name}' {reason}.",
            source_path=source_path,
            line=_line_of_declaration(model_text, name),
            hint="each axis becomes `--<name>=<value>` in the sim script's argv, so a "
            "name must match [A-Za-z][A-Za-z0-9_-]* and must not be `help` or `h`",
        )


def _line_of_declaration(model_text: str, name: str) -> int | None:
    """Line where ``name`` is declared — None when it is not in this text (no location)."""
    for lineno, raw in enumerate(model_text.splitlines(), start=1):
        if raw.split(":", 1)[0].strip() == name:
            return lineno
    return None


def _headless_warnings(script_path: Path, source_path: str) -> tuple[str, ...]:
    """Grep-strength GUI warning (never a rejection — see the module doc)."""
    text = script_path.read_text(encoding="utf-8", errors="replace")
    match = _HEADLESS_FALSE.search(text)
    if match is None:
        return ()
    lineno = text.count("\n", 0, match.start()) + 1
    return (
        f"{source_path}:{lineno}: `{match.group(0).strip()}` — the case container has no "
        "display, so a GUI boot hangs or crashes into the ERROR lane. Put it behind a "
        'flag CI never passes: SimulationApp({"headless": not args.gui}).',
    )
