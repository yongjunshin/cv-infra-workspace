"""``cv-infra`` command-line entry point and exit-code contract (M8).

This module is the *single contract surface* that both CI/CD and humans drive
(REQ-INTAKE-003): a GitHub Action is only a thin wrapper over this CLI
(LOCKED Sec.10 — CLI-first, Action-after). Phase 0 reserved the surface;
Phase 2 wired ``run`` for real (decision 2026-07-07 D-2: CLI builds the
JOB_SPEC, the M3 supervisor co-spawns SUT + runner and recovers result.json,
the CLI maps the recovered verdict to an exit code). Phase 3 replaced the
``run`` input path with the M1 6-stage loader (``contract.loader.load_request``
— the acceptance gate, NFR-INTAKE-003): any stage-1..5 rejection renders the
M1 ``ContractError`` friendly prose on stderr (``str(err)`` verbatim — field
path + expected + example + YAML line/col; raw traceback 0) and exits 2
BEFORE the supervisor is ever invoked; a deprecated ``apiVersion`` warns on
stderr and the run continues (M8-D4/D5). The other sub-commands and GitHub
integration land in later Phases (M8 Sec.5).

Exit-code contract (LOCKED Sec.9 — exercised standalone at DoD-P2-07)::

    0  PASS      all verifications passed
    1  FAIL      verification failed *or* regression vs baseline (a SUT verdict)
    2  CONTRACT  contract/validation error: schema violation, unsupported
                 apiVersion, malformed YAML, or CLI usage error
    3  INFRA     infrastructure error: orchestrator unreachable, EULA not
                 accepted, runner crash — *not* a SUT verdict (CI Check
                 conclusion = neutral / action_required)

The 1-vs-2-vs-3 split is the core DX contract: "your YAML is wrong (2)",
"your robot failed (1)" and "our platform broke (3)" stay distinct so a
developer never mistakes an infrastructure problem for a self-regression
(exit 3 is never collapsed into failure — D-I).
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

# NO top-level cv_infra.contract / third-party imports: the --help and
# placeholder sub-command paths must stay dependency-free (REQ-INTAKE-003/005).
# The M1 loader (pydantic + pyyaml) is imported lazily inside the run path.

# --- exit-code contract slots (LOCKED Sec.9) -------------------------------
# The 0/1 verdict branching + 2 (bad input, M1 friendly prose) + 3 (infra)
# paths are all wired for ``run`` (DoD-P2-07 + DoD-P3 friendly errors).
EXIT_PASS = 0
EXIT_FAIL = 1
EXIT_CONTRACT = 2
EXIT_INFRA = 3

# result.json verdict -> exit code. The RECOVERED result.json verdict OUTRANKS
# the runner container's exit code (which is informational only) — same fold as
# the runner's own _VERDICT_EXIT (cv_infra/runner/main.py); the verdict domain
# is contract/schema.py ``Verdict``. Unknown verdicts fold to INFRA (3),
# never to FAIL (1).
_VERDICT_EXIT: dict[str, int] = {
    "pass": EXIT_PASS,
    "fail": EXIT_FAIL,
    "timeout": EXIT_FAIL,  # SUT missed the sim-time budget = SUT verdict, not infra
    "error": EXIT_INFRA,  # runner-recorded platform error (FU-8)
}

# Operator consent env keys forwarded verbatim to the runner via the
# supervisor's kw-only ``runner_env`` — pass-through happens only when the key
# exists in the CLI process environment. Consent VALUES are operator-provided
# at runtime and never committed anywhere (decision 2026-07-03; the formal
# consent gate is P5/M5, honest boot-guard refusal until then).
_CONSENT_ENV_KEYS = ("ACCEPT_EULA", "PRIVACY_CONSENT")

# --- sub-command surface (REQ-INTAKE-003) ----------------------------------
# ``run`` is implemented (Phase 2, D-2); the rest are reserved placeholders so
# the contract surface stays visible via ``cv-infra --help``.
_SUBCOMMANDS: dict[str, str] = {
    "run": "Run a single scenario end-to-end (supervisor co-spawns SUT + runner; envelope-less).",
    "submit": "Submit an envelope / scenario glob to the orchestrator [--wait].",
    "status": "Show progress of an async envelope by id (informational).",
    "wait": "Block until an envelope reaches a terminal aggregated verdict.",
    "report": "Print the aggregated report for an envelope (informational).",
    "selftest": "Run the built-in stub round-trip (no external SUT).",
}

_EXIT_CODE_EPILOG = (
    "exit-code contract (LOCKED Sec.9):\n"
    "  0  PASS      all verifications passed\n"
    "  1  FAIL      verification failed or regression vs baseline (SUT verdict)\n"
    "  2  CONTRACT  contract/validation error (bad YAML, unsupported apiVersion)\n"
    "  3  INFRA     infrastructure error (orchestrator down, EULA not accepted)\n"
    "\n"
    "Phase 2: 'run' is implemented; the other sub-commands are reserved (later Phases)."
)


def _add_run_arguments(sub: argparse.ArgumentParser) -> None:
    sub.add_argument(
        "scenario",
        help="scenario YAML path (consumer-owned instance of the M1 shape)",
    )
    sub.add_argument(
        "--runner-image",
        required=True,
        help="runner image ref (required — no hardcoded default; image-as-artifact pin, FU-10)",
    )
    sub.add_argument(
        "--out-dir",
        default="./cv-infra-out",
        help="job artifact root; the supervisor creates per-job subdirs (default: ./cv-infra-out)",
    )
    sub.add_argument(
        "--job-id",
        default=None,
        help="job id (default: <scenario stem>-<UTC timestamp>)",
    )


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="cv-infra",
        description=(
            "Continuous verification CLI — the single contract surface for CI/CD and "
            "humans (REQ-INTAKE-003). A GitHub Action is a thin wrapper over this CLI."
        ),
        epilog=_EXIT_CODE_EPILOG,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    subparsers = parser.add_subparsers(dest="command", metavar="<command>")
    for name, help_text in _SUBCOMMANDS.items():
        sub = subparsers.add_parser(name, help=help_text, description=help_text)
        if name == "run":
            _add_run_arguments(sub)
    return parser


def _one_line(exc: BaseException) -> str:
    """Render an exception as a single stderr-friendly line (no traceback)."""
    if isinstance(exc, KeyError) and exc.args:
        return f"missing required key {exc.args[0]!r}"
    text = " ".join(str(exc).split())
    return text or type(exc).__name__


def _default_job_id(scenario_path: Path) -> str:
    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    return f"{scenario_path.stem}-{stamp}"


def _job_spec_from_request(request: Any, job_id: str) -> dict[str, Any]:
    """Admitted M1 ``schema.VerificationRequest`` -> canonical JOB_SPEC dict.

    The wire shape is the frozen Phase-2 seam (supervisor JOB_SPEC file ->
    runner parse): exact top-level key set ``{job_id, scenario, sut_image_ref,
    interface, acceptance_criteria}`` with ``sut.image_ref`` flattened
    (REQ-INTAKE-006). Contract-side fields (``apiVersion`` /
    ``execution_settings`` / ``sut.image_id``) stay OFF the wire — consuming
    them is a later-cycle supervisor/runner surface.

    ``exclude_none=True`` keeps "None = downstream default applies" fields
    ABSENT exactly as the raw-YAML pass-through did: a present-but-``null``
    known-key param (e.g. ``goal_orientation_wxyz``) would defeat the oracle's
    ``read_field(name, default)`` fallback. Free-form dict values (custom
    criterion params) are NOT filtered by pydantic's ``exclude_none`` —
    explicit nulls a user wrote survive verbatim (measured 2026-07-10).
    ``scenario.debug_obstacle`` (D-2') rides the wire only when declared.
    """
    return {
        "job_id": job_id,
        "scenario": request.scenario.model_dump(exclude_none=True),
        "sut_image_ref": request.sut.image_ref,  # flattened canonical field (REQ-INTAKE-006)
        "interface": request.interface.model_dump(exclude_none=True),
        "acceptance_criteria": [
            criterion.model_dump(exclude_none=True) for criterion in request.acceptance_criteria
        ],
    }


def _render_contract_errors(err: Any) -> None:
    """Render a loader rejection to stderr — ``errors.py``'s ``str(err)`` format
    verbatim (M1 owns the friendly shape; the CLI invents no format of its own).

    The loader raises only the FIRST violation (location-enriched). When its
    cause is a pydantic ``ValidationError`` carrying several violations, the
    remaining ones are re-rendered via ``from_validation_error`` (list
    traversal — the first element IS ``err``, minus the loader's line/col
    enrichment) so one run surfaces every violation. Duck-typed on a callable
    ``.errors`` exactly like ``errors.py`` itself — no pydantic import here.
    """
    print(f"cv-infra run: {err}", file=sys.stderr)
    cause = err.__cause__
    if cause is None or not callable(getattr(cause, "errors", None)):
        return
    from cv_infra.contract.errors import from_validation_error
    from cv_infra.contract.schema import VerificationRequest

    rest = from_validation_error(cause, model=VerificationRequest, source_path=err.source_path)[1:]
    for extra in rest:
        print(f"cv-infra run: {extra}", file=sys.stderr)


def _exit_from_outcome(outcome: Any) -> int:
    """Fold a pinned ``JobOutcome`` into the exit-code contract (table in ``_cmd_run``)."""
    if outcome.infra_error:
        print(
            f"cv-infra run: infrastructure error: {outcome.infra_error} "
            f"(runner exit={outcome.runner_exit_code})",
            file=sys.stderr,
        )
        return EXIT_INFRA
    if outcome.result_path is None:
        print(
            "cv-infra run: result.json was not recovered — treating as infrastructure error",
            file=sys.stderr,
        )
        return EXIT_INFRA
    result_path = Path(outcome.result_path)
    # Lazy import (pydantic): the M1 canonical Result re-validates the recovered
    # payload at this trust boundary (the runner emission is wire-equal by the
    # equivalence guard; pydantic ValidationError is a ValueError subclass, so
    # the stdlib-only catch below already covers it).
    from cv_infra.contract.schema import Result

    try:
        result = Result.model_validate(json.loads(result_path.read_text(encoding="utf-8")))
    except (OSError, json.JSONDecodeError, ValueError, KeyError, TypeError) as exc:
        print(
            f"cv-infra run: result.json at {result_path} is unreadable or non-canonical: "
            f"{_one_line(exc)}",
            file=sys.stderr,
        )
        return EXIT_INFRA
    code = _VERDICT_EXIT.get(result.verdict, EXIT_INFRA)
    if result.verdict not in _VERDICT_EXIT:
        print(
            f"cv-infra run: unknown verdict {result.verdict!r} in result.json — "
            "treating as infrastructure error",
            file=sys.stderr,
        )
    print(
        f"cv-infra run: job {outcome.job_id} verdict={result.verdict} exit={code} "
        f"result={result_path}"
    )
    return code


def _cmd_run(args: argparse.Namespace) -> int:
    """``cv-infra run``: M1 6-stage admit gate -> JOB_SPEC -> supervisor -> exit code.

    The input path IS ``contract.loader.load_request`` (parse -> apiVersion
    resolve -> pydantic validate -> self-containedness -> oracle bind -> admit,
    NFR-INTAKE-003): rejected input never reaches the supervisor — this
    function only consumes the loader's verdict, it re-validates nothing.
    Exit-code mapping (DoD-P2-07/P3-02..06); the RECOVERED result.json verdict
    outranks the runner container's exit code (informational only)::

        condition                                              exit
        -----------------------------------------------------  ----
        loader stage 1-5 reject: file missing / parse error /
          absent|unknown apiVersion / schema violation /
          non-self-contained / oracle unbindable                  2
        deprecated apiVersion                                     (stderr WARNING, run continues)
        supervisor unavailable / out-dir not creatable            3
        outcome.infra_error set                                   3
        result.json not recovered (result_path is None)           3
        result.json unreadable / non-canonical                    3
        verdict "pass"                                            0
        verdict "fail" / "timeout"                                1
        verdict "error" / unknown                                 3

    Operator consent (``ACCEPT_EULA``/``PRIVACY_CONSENT``) is forwarded from
    the CLI process env to ``runner_env`` only when present — when absent the
    runner boot guard honestly refuses (surfaces on the infra path, exit 3).
    """
    scenario_path = Path(args.scenario)
    job_id = args.job_id or _default_job_id(scenario_path)

    # Lazy loader import: the 6-stage gate pulls pydantic + pyyaml, which the
    # --help / placeholder paths must never load (REQ-INTAKE-003/005).
    from cv_infra.contract.errors import ContractError
    from cv_infra.contract.loader import load_request

    try:
        admitted = load_request(scenario_path)
    except ContractError as err:
        _render_contract_errors(err)
        return EXIT_CONTRACT

    # Deprecated apiVersion (and any future stage-2 warning): warn, continue —
    # the exit code stays the verdict's (M8-D5 / NFR-INTAKE-002).
    for warning in admitted.warnings:
        print(f"cv-infra run: WARNING: {warning}", file=sys.stderr)

    # Canonical JOB_SPEC from the ADMITTED model (G-17: the model, not prose,
    # is SoT) — wire shape frozen at the Phase-2 seam (_job_spec_from_request).
    job_spec = _job_spec_from_request(admitted.request, job_id)

    out_dir = Path(args.out_dir)
    try:
        out_dir.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        print(f"cv-infra run: cannot create --out-dir {out_dir}: {_one_line(exc)}", file=sys.stderr)
        return EXIT_INFRA

    try:
        # Lazy import (pinned M8->M3 seam, cycle p2-supervisor-min): the
        # supervisor is the sole docker.sock holder — non-run CLI paths must
        # never pull the docker dependency.
        from cv_infra.orchestrator.supervisor import run_job
    except ImportError as exc:
        print(
            f"cv-infra run: supervisor unavailable ({_one_line(exc)}) — platform build "
            "incomplete; this is an infrastructure error, not a SUT verdict",
            file=sys.stderr,
        )
        return EXIT_INFRA

    # Consent pass-through (decision 2026-07-03): forward the operator-provided
    # consent env keys verbatim, only when present. When absent, ``runner_env``
    # is NOT passed — the runner boot guard refuses to start Isaac (FU-8 is P5).
    consent_env = {k: os.environ[k] for k in _CONSENT_ENV_KEYS if k in os.environ}
    kwargs: dict[str, Any] = {"runner_env": consent_env} if consent_env else {}
    outcome = run_job(job_spec, out_dir, args.runner_image, job_spec["sut_image_ref"], **kwargs)
    return _exit_from_outcome(outcome)


def main(argv: list[str] | None = None) -> int:
    """Parse arguments and dispatch.

    Returns the process exit code per the contract documented in the module
    docstring. ``--help`` and argparse usage errors exit directly via
    ``SystemExit`` (``cv-infra --help`` => 0; bad/missing run arguments => 2,
    matching EXIT_CONTRACT).
    """
    parser = _build_parser()
    # The placeholder sub-commands take no real arguments yet, so we absorb
    # (and ignore) their trailing tokens with ``parse_known_args`` — this keeps
    # e.g. ``cv-infra submit x.yaml`` on the honest "not implemented (3)" path.
    # ``run`` parses its real schema and treats leftovers as usage errors (2).
    args, extra = parser.parse_known_args(argv)

    if args.command is None:
        # Incomplete invocation: surface usage as a contract/usage error (2),
        # matching argparse's own convention for usage errors.
        parser.print_help(sys.stderr)
        return EXIT_CONTRACT

    if args.command == "run":
        if extra:
            print(
                f"cv-infra run: unrecognized argument(s): {' '.join(extra)}",
                file=sys.stderr,
            )
            return EXIT_CONTRACT
        return _cmd_run(args)

    # Reserved surface: not wired yet. Report as an infrastructure / not-ready
    # condition (3), never as a SUT FAIL (1) — see the 1-vs-3 rationale in the
    # module docstring.
    print(
        f"cv-infra: '{args.command}' is not implemented yet "
        "(arrives in a later Phase — see M8 Sec.5).",
        file=sys.stderr,
    )
    return EXIT_INFRA


if __name__ == "__main__":
    sys.exit(main())
