"""``cv-infra`` command-line entry point and exit-code contract (M8).

This module is the *single contract surface* that both CI/CD and humans drive
(REQ-INTAKE-003): a GitHub Action is only a thin wrapper over this CLI
(LOCKED Sec.10 — CLI-first, Action-after). Phase 0 reserved the surface;
Phase 2 wires ``run`` for real (decision 2026-07-07 D-2: CLI builds the
JOB_SPEC, the M3 supervisor co-spawns SUT + runner and recovers result.json,
the CLI maps the recovered verdict to an exit code). The other sub-commands,
GitHub integration and friendly-error rendering land in later Phases (M8
Sec.5); the formal M1 loader (pydantic, field-path prose) is Phase 3 — the
YAML->JOB_SPEC mapping here is deliberately minimal.

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
import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from cv_infra.adapter.adapter_schema import Ros2AdapterConfig
from cv_infra.contract.models import VerificationRequest, VerificationResult

# --- exit-code contract slots (LOCKED Sec.9) -------------------------------
# The 0/1 verdict branching + 2 (bad input) + 3 (infra) paths are wired for
# ``run`` at Phase 2 (DoD-P2-07); friendly-error prose is Phase 3.
EXIT_PASS = 0
EXIT_FAIL = 1
EXIT_CONTRACT = 2
EXIT_INFRA = 3

# result.json verdict -> exit code. The RECOVERED result.json verdict OUTRANKS
# the runner container's exit code (which is informational only) — same fold as
# the runner's own _VERDICT_EXIT (cv_infra/runner/main.py) and the comment
# table in cv_infra/contract/models.py. Unknown verdicts fold to INFRA (3),
# never to FAIL (1).
_VERDICT_EXIT: dict[str, int] = {
    "pass": EXIT_PASS,
    "fail": EXIT_FAIL,
    "timeout": EXIT_FAIL,  # SUT missed the sim-time budget = SUT verdict, not infra
    "error": EXIT_INFRA,  # runner-recorded platform error (FU-8)
}

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

# scenario YAML -> canonical JOB_SPEC mapping surface (loud-reject, no silent
# drop — same contract as adapter_schema). The SUT image comes from the
# scenario's ``sut.image_ref`` (the scenario is the SoT), never a CLI flag.
_TOP_LEVEL_KEYS = frozenset({"scenario", "sut", "interface", "acceptance_criteria"})
_SUT_KEYS = frozenset({"image_ref"})


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


def _load_scenario_doc(path: Path) -> Any:
    # Lazy pyyaml import: non-run paths (--help, placeholders) must not pull
    # third-party deps (standalone surface stays cheap — REQ-INTAKE-003/005).
    import yaml

    text = path.read_text(encoding="utf-8")  # OSError -> contract error upstream
    try:
        return yaml.safe_load(text)
    except yaml.YAMLError as exc:  # normalize: upstream catch stays stdlib-only
        raise ValueError(f"YAML parse error: {_one_line(exc)}") from exc


def _scenario_doc_to_job_spec(doc: Any, job_id: str) -> dict[str, Any]:
    """Map a scenario YAML doc to the canonical VerificationRequest dict (JOB_SPEC).

    ``scenario`` / ``interface`` / ``acceptance_criteria`` pass through as-is;
    ``sut.image_ref`` flattens to ``sut_image_ref``; ``job_id`` is attached.
    Unknown keys (top level and under ``sut``) are LOUD-REJECTED — adapter_config
    unknown keys are rejected downstream by ``Ros2AdapterConfig.from_dict``.
    """
    if not isinstance(doc, dict):
        raise ValueError(
            "top level must be a mapping (scenario / sut / interface / acceptance_criteria)"
        )
    unknown = sorted(set(doc) - _TOP_LEVEL_KEYS)
    if unknown:
        raise ValueError(f"unknown top-level key(s) {unknown}; allowed: {sorted(_TOP_LEVEL_KEYS)}")
    missing = sorted(k for k in ("scenario", "sut") if k not in doc)
    if missing:
        raise ValueError(f"missing required top-level key(s) {missing}")
    sut = doc["sut"]
    if not isinstance(sut, dict):
        raise ValueError("'sut' must be a mapping with 'image_ref'")
    unknown_sut = sorted(set(sut) - _SUT_KEYS)
    if unknown_sut:
        raise ValueError(f"unknown key(s) under 'sut' {unknown_sut}; allowed: {sorted(_SUT_KEYS)}")
    image_ref = sut.get("image_ref")
    if not isinstance(image_ref, str) or not image_ref:
        raise ValueError("'sut.image_ref' must be a non-empty string (scenario is the SUT SoT)")
    return {
        "job_id": job_id,
        "scenario": doc["scenario"],
        "sut_image_ref": image_ref,  # flattened canonical field (REQ-INTAKE-006)
        "interface": doc.get("interface"),
        "acceptance_criteria": doc.get("acceptance_criteria") or [],
    }


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
    try:
        result = VerificationResult.from_dict(json.loads(result_path.read_text(encoding="utf-8")))
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
    """``cv-infra run``: scenario YAML -> JOB_SPEC -> supervisor -> exit code (D-2).

    Exit-code mapping (DoD-P2-07). The RECOVERED result.json verdict outranks
    the runner container's exit code — the runner code is informational only::

        condition                                              exit
        -----------------------------------------------------  ----
        file missing / YAML parse error / non-mapping doc         2
        unknown top-level or sut key (loud-reject)                2
        schema violation (VerificationRequest / adapter_config)   2
        supervisor unavailable / out-dir not creatable            3
        outcome.infra_error set                                   3
        result.json not recovered (result_path is None)           3
        result.json unreadable / non-canonical                    3
        verdict "pass"                                            0
        verdict "fail" / "timeout"                                1
        verdict "error" / unknown                                 3
    """
    scenario_path = Path(args.scenario)
    job_id = args.job_id or _default_job_id(scenario_path)
    try:
        doc = _load_scenario_doc(scenario_path)
        draft = _scenario_doc_to_job_spec(doc, job_id)
        # Only proceed once the real contract models accept the spec (task pin):
        # VerificationRequest for the top-level shape, Ros2AdapterConfig for the
        # adapter_config sub-schema (loud-rejects unknown keys — SEAM-2).
        request = VerificationRequest.from_dict(draft)
        if request.interface.type != "ros2":
            raise ValueError(
                f"interface.type {request.interface.type!r} is unsupported (MVP: 'ros2')"
            )
        Ros2AdapterConfig.from_dict(request.interface.adapter_config)
    except (OSError, ValueError, KeyError, TypeError) as exc:
        print(f"cv-infra run: invalid scenario {scenario_path}: {_one_line(exc)}", file=sys.stderr)
        return EXIT_CONTRACT

    # Canonical JOB_SPEC = round-trip through the real M1 model (defaults
    # materialized, exact canonical key set — G-17: the model, not prose, is SoT).
    job_spec = request.to_dict()

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

    outcome = run_job(job_spec, out_dir, args.runner_image, request.sut_image_ref)
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
