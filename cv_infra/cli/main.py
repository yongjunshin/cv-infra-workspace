"""``cv-infra`` command-line entry point and exit-code contract (M8, Phase 0 skeleton).

This module is the *single contract surface* that both CI/CD and humans drive
(REQ-INTAKE-003): a GitHub Action is only a thin wrapper over this CLI
(LOCKED Sec.10 — CLI-first, Action-after). Phase 0 reserves and documents the
surface; real sub-command behaviour, GitHub integration and friendly-error
rendering land in later Phases (see M8 Sec.5).

Exit-code contract (LOCKED Sec.9 — reserved here, exercised standalone at
DoD-P2-07)::

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
import sys

# --- exit-code contract slots (LOCKED Sec.9) -------------------------------
# Reserved and documented in Phase 0. The verdict branching that actually
# returns 0/1 is implemented at Phase 2 (DoD-P2-07); friendly-error (2) and
# infra (3) paths land in Phase 3/5.
EXIT_PASS = 0
EXIT_FAIL = 1
EXIT_CONTRACT = 2
EXIT_INFRA = 3

# --- sub-command surface (REQ-INTAKE-003) ----------------------------------
# Real behaviour arrives in later Phases; Phase 0 only registers the
# placeholders so the contract surface is visible via ``cv-infra --help``.
_SUBCOMMANDS: dict[str, str] = {
    "run": "Run a single scenario standalone (Runner-only path; no orchestrator).",
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
    "Phase 0 skeleton: sub-commands are reserved but not yet implemented."
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
        subparsers.add_parser(name, help=help_text, description=help_text)
    return parser


def main(argv: list[str] | None = None) -> int:
    """Parse arguments and dispatch (Phase 0: skeleton).

    Returns the process exit code per the contract documented in the module
    docstring. ``--help`` and other argparse usage paths exit directly via
    ``SystemExit`` (``cv-infra --help`` => 0).
    """
    parser = _build_parser()
    # Phase 0 stub: the placeholder sub-commands take no real arguments yet, so we
    # absorb (and ignore) any trailing tokens with ``parse_known_args``. This keeps
    # ``cv-infra run scenario.yaml`` on the honest "not implemented (3)" path instead
    # of argparse's misleading "unrecognized arguments" usage error (2). Each
    # sub-command grows its real argument schema in a later Phase.
    args, _extra = parser.parse_known_args(argv)

    if args.command is None:
        # Incomplete invocation: surface usage as a contract/usage error (2),
        # matching argparse's own convention for usage errors.
        parser.print_help(sys.stderr)
        return EXIT_CONTRACT

    # Phase 0 skeleton: the surface is reserved but no sub-command is wired yet.
    # Report this as an infrastructure / not-ready condition (3), never as a SUT
    # FAIL (1) — see the 1-vs-3 rationale in the module docstring.
    print(
        f"cv-infra: '{args.command}' is not implemented in this Phase 0 skeleton "
        "(arrives in a later Phase — see M8 Sec.5).",
        file=sys.stderr,
    )
    return EXIT_INFRA


if __name__ == "__main__":
    sys.exit(main())
