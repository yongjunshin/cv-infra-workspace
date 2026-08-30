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
stderr and the run continues (M8-D4/D5). Phase 4 added the batch surface
``submit``/``status``/``wait`` (``cv_infra/cli/batch.py``, lazily imported —
envelope submit to the M3 REST surface + terminal aggregated-verdict exit,
M8-D11); Phase 5 wires ``report`` (informational review — a thin client over
the M4 VerificationReport the orchestrator serves, D-O) and ``selftest`` (the
built-in stub round-trip — the same submit/wait machinery over the M7 stub
envelope ``orchestrator.selftest`` builds, REQ-SELFTEST-001/002). The full
sub-command surface is now wired: no placeholder remains.

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

from cv_infra.cli.exit_codes import EXIT_CONTRACT, EXIT_FAIL, EXIT_INFRA, EXIT_PASS

# NO top-level cv_infra.contract / third-party imports: the --help and
# placeholder sub-command paths must stay dependency-free (REQ-INTAKE-003/005).
# ``cv_infra.cli.exit_codes`` (above) is the sole first-party import — a
# stdlib-only leaf holding the exit-code contract's single source (LOCKED §9);
# the ``EXIT_*`` constants are re-exported from here for back-compat (call
# sites still do ``from cv_infra.cli.main import EXIT_PASS``). The M1 loader
# (pydantic + pyyaml) is imported lazily inside the run path.

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
# EVERY sub-command below is implemented — the surface carries no placeholder
# (G-47: a reserved-placeholder note that outlives the wiring is a stale
# declaration). ``run`` = Phase 2 (D-2); ``submit``/``status``/``wait`` = Phase 4
# batch surface; ``monitor`` = Phase 4 operational view; ``report`` = Phase 5
# informational review; ``selftest`` = Phase 5 built-in stub round-trip. All but
# ``run`` live in the lazily imported cv_infra/cli/{batch,monitor}.py.
_SUBCOMMANDS: dict[str, str] = {
    "run": "Run a single scenario end-to-end (supervisor co-spawns SUT + runner; envelope-less).",
    "submit": "Submit a RequestEnvelope YAML or scenario paths/globs to the orchestrator [--wait].",
    "status": "Show progress of an async envelope by id (informational; never gates on verdict).",
    "wait": "Block until an envelope reaches a terminal aggregated verdict (exit 0/1/3).",
    "monitor": "Show the operational view (queue/resources/health + rollup); informational.",
    "report": "Print the aggregated report for an envelope id (informational; --json for raw).",
    "selftest": "Run the built-in stub round-trip (no external SUT); exits like submit --wait.",
}

#: Batch sub-commands dispatched to ``cv_infra.cli.batch`` (Phase 4).
_BATCH_COMMANDS = ("submit", "status", "wait")

#: Every sub-command whose body lives in ``cv_infra.cli.batch`` — the batch trio
#: plus the Phase-5 ``selftest`` (built-in stub submit+wait: the same machinery,
#: a different document — REQ-SELFTEST-002). ``report`` keeps its own dispatch
#: block (its argument schema and unavailable-message differ).
_BATCH_MODULE_COMMANDS = (*_BATCH_COMMANDS, "selftest")

#: The M1 ``RequestEnvelope.trigger_source`` Literal (contract/schema.py, the
#: SoT), hardcoded ONCE here because the --help path must stay dependency-free
#: (no contract import to build the parser). Shared by every command that
#: records trigger provenance (submit, selftest — REQ-INTAKE-003).
_TRIGGER_SOURCES = ("human-manual", "ci-cd")

_EXIT_CODE_EPILOG = (
    "exit-code contract (LOCKED Sec.9):\n"
    "  0  PASS      all verifications passed\n"
    "  1  FAIL      verification failed or regression vs baseline (SUT verdict)\n"
    "  2  CONTRACT  contract/validation error (bad YAML, unsupported apiVersion)\n"
    "  3  INFRA     infrastructure error (orchestrator down, EULA not accepted)\n"
    "\n"
    "'run' (P2), 'submit'/'status'/'wait'/'monitor' (P4) and 'report'/'selftest' "
    "(P5) are implemented — every sub-command listed above is wired."
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


def _add_api_argument(sub: argparse.ArgumentParser) -> None:
    sub.add_argument(
        "--api",
        default=None,
        help="orchestrator base URL (default: $CV_INFRA_API, else http://127.0.0.1:8000)",
    )


def _add_trigger_source_argument(sub: argparse.ArgumentParser) -> None:
    """``--trigger-source`` (REQ-INTAKE-003) — shared by ``submit`` and ``selftest``.

    Provenance of the trigger, recorded verbatim on the wire: the GitHub Action
    (and the platform CI self-test tier) passes ``ci-cd``, humans keep the
    default ``human-manual``. CI and human runs are otherwise identical (same
    CLI, same semantics — M8 §3.1). One definition, so a second surface can
    never drift from the M1 Literal (``_TRIGGER_SOURCES``).
    """
    sub.add_argument(
        "--trigger-source",
        choices=_TRIGGER_SOURCES,
        default=_TRIGGER_SOURCES[0],
        help="who triggered this run (default: human-manual; the Action passes ci-cd). "
        "Recorded verbatim by the orchestrator (REQ-INTAKE-003).",
    )


def _add_selftest_arguments(sub: argparse.ArgumentParser) -> None:
    """Argument schema for the Phase-5 ``selftest`` command (cv_infra/cli/batch.py).

    No positional input by construction: the request IS the built-in stub the
    platform supplies to itself (REQ-SELFTEST-001), so a self-test needs no
    consumer file, repo or image (NFR-SELFTEST-001). It always waits — the
    round-trip verdict is the whole point (REQ-SELFTEST-003), so there is no
    ``--wait`` flag to forget.
    """
    sub.add_argument(
        # The ONE deployment-level knob: which platform-internal image plays the
        # SUT side of the stub (M7 §3.5). Priority flag > $CV_SELFTEST_SUT_IMAGE,
        # resolved by orchestrator.selftest — never guessed, never a consumer
        # image (unset => exit 3, image-as-artifact FU-10).
        "--sut-image",
        default=None,
        metavar="REF",
        help="platform-internal stub SUT image ref (priority: flag > $CV_SELFTEST_SUT_IMAGE; "
        "never defaulted or guessed — unresolved is an infrastructure error, exit 3)",
    )
    _add_trigger_source_argument(sub)
    sub.add_argument(
        "--timeout",
        type=float,
        default=None,
        metavar="S",
        help="max seconds to wait for the terminal verdict; exceeded => exit 3 "
        "(default: wait indefinitely)",
    )
    _add_api_argument(sub)


def _add_batch_arguments(name: str, sub: argparse.ArgumentParser) -> None:
    """Argument schema for the Phase-4 batch commands (cv_infra/cli/batch.py)."""
    if name == "submit":
        sub.add_argument(
            # D-K (M8 §3.1, p5c4 G1): ONE RequestEnvelope YAML (decision p4c3
            # D-2, unchanged surface), OR >=1 scenario YAML paths/globs the CLI
            # folds into a size-N envelope — deterministic lexicographic path
            # order (G-39-1: canonical at the generation point).
            "sources",
            nargs="+",
            metavar="<envelope.yaml | scenario.yaml ...>",
            help="RequestEnvelope YAML path, or >=1 scenario YAML paths/globs — the CLI "
            "synthesizes a size-N envelope from scenario paths (D-K; deterministic "
            "lexicographic path order)",
        )
        sub.add_argument(
            # G2 (p5c4): SUT image REF injection. Priority: this flag >
            # $CV_INFRA_SUT_IMAGE env (the workflows' hand-off) > the scenario
            # value. A ref STRING only — never pulled or inspected (R10).
            "--sut-image",
            default=None,
            metavar="REF",
            help="SUT image reference injected into every submitted scenario's "
            "sut.image_ref (priority: flag > $CV_INFRA_SUT_IMAGE > scenario value; "
            "ref string only — never pulled or inspected, R10 ref-only)",
        )
        sub.add_argument(
            # G3 (p5c4): machine-readable exit-2 errors for the composite
            # annotate step (8-key list, D-L 1:1). Standalone default: OFF.
            "--errors-json",
            default=None,
            metavar="PATH",
            help="on a contract error (exit 2) also write the machine-readable 8-key "
            "error list to PATH (default: ./errors.json only when $GITHUB_ACTIONS is "
            "set — the annotate step's consumption form; standalone default: off)",
        )
        sub.add_argument(
            "--wait",
            action="store_true",
            help="block until the terminal aggregated verdict and exit 0/1/3 (M8-D11)",
        )
        _add_trigger_source_argument(sub)
    else:
        sub.add_argument("envelope_id", help="envelope id printed by 'cv-infra submit'")
    if name in ("submit", "wait"):
        sub.add_argument(
            "--timeout",
            type=float,
            default=None,
            metavar="S",
            help="max seconds to wait for the terminal verdict; exceeded => exit 3 "
            "(submit: requires --wait; default: wait indefinitely)",
        )
    _add_api_argument(sub)


def _add_report_arguments(sub: argparse.ArgumentParser) -> None:
    """Argument schema for the Phase-5 ``report`` command (cv_infra/cli/batch.py)."""
    sub.add_argument("envelope_id", help="envelope id printed by 'cv-infra submit'")
    sub.add_argument(
        "--json",
        action="store_true",
        help="print the raw VerificationReport JSON to stdout (default: human-readable render)",
    )
    _add_api_argument(sub)


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
        elif name in _BATCH_COMMANDS:
            _add_batch_arguments(name, sub)
        elif name == "monitor":
            _add_api_argument(sub)  # operational-view read: only the orchestrator base URL
        elif name == "report":
            _add_report_arguments(sub)
        elif name == "selftest":
            _add_selftest_arguments(sub)
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

    Thin alias of the M1 definition (``contract.job_spec.build_job_spec``, which
    holds the frozen wire shape and the reasons for it) — this plane owns no
    second assembly of the spec (p8c1: the twin the M3 REST path used to carry
    became an alias of the same function). Kept as a module-level function so
    the run path's lazy-import discipline holds: the contract stays OFF this
    module's import surface (--help must not pull it), and call sites/tests keep
    the handle they already have.
    """
    from cv_infra.contract.job_spec import build_job_spec

    return build_job_spec(request, job_id)


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
        # An `extra_forbidden` here is not a corrupt file: it is PLANE SKEW (G-74).
        # The keys came from the runner image that WROTE this file, so name that
        # plane and the way to read it — without this line "non-canonical" gives the
        # operator no path to "my runner image is stale". Duck-typed on a callable
        # .errors exactly like _render_contract_errors (no pydantic import here).
        errors = getattr(exc, "errors", None)
        if callable(errors) and any(e.get("type") == "extra_forbidden" for e in errors()):
            print(
                "cv-infra run: those unknown key(s) came from the RUNNER IMAGE that wrote "
                "this file — its baked code and this CLI are from different source commits "
                "(usually a runner image older than your checkout). Compare the image stamp "
                "with your checkout: docker image inspect --format "
                "'{{index .Config.Labels \"org.opencontainers.image.revision\"}}' <runner-image> "
                "(or scripts/check_plane_skew.sh), then REBUILD/re-pull the runner image "
                "and re-run — docs/deploy/README.md §8.",
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


def _run_job_kwargs(admitted: Any, scenario_path: Path) -> dict[str, Any]:
    """Assemble the kw-only extras ``run_job`` takes for THIS admitted request.

    Every key here is CONDITIONAL-BY-CONTRACT except the identity key: an absent
    key means the supervisor's pinned kw-only default applies (no mount, no env),
    so "not passed" is a real value and must not become an explicit ``None``.

    * ``runner_env`` — consent pass-through (decision 2026-07-03): the
      operator-provided consent env keys forwarded VERBATIM, only when present.
      When absent the kwarg is not passed at all and the runner boot guard
      honestly refuses to start Isaac (FU-8 is P5). Bag sensor opt-in (p5c12)
      rides the same seam; the key NAME comes from ``recording.BAG_SENSORS_ENV``
      via lazy import (G-25 — keep ``--help`` free of runner imports).
    * ``oracle_plugin_dir`` — D-1 wiring contract #2 (decision 2026-07-11): an
      admitted ``CustomCriterion`` means consumer oracle plugin ``.py`` files live
      next to the scenario YAML, so that directory (resolved absolute) goes to the
      supervisor, which ro-mounts it into the runner at the SAME path + announces
      ``CV_ORACLE_PLUGIN_DIR`` (contract #3). Detection is ``isinstance`` on the
      ADMITTED model, never a string heuristic.
    * ``request_identity_key`` — p5c20 ③ (DoD-P2-06 ① / REQ-REPORT-002),
      UNCONDITIONAL: the single-run entrypoint hands the runner the SAME key the
      envelope/REST entrypoint does, so a ``result.json`` produced by ``cv-infra
      run`` names WHICH request produced it instead of reporting
      ``identity_key=none`` (the p5c18 T3 defect, one half of which stayed open on
      this path). The key is M4's 단일 정의 IMPORTED (G-56) and fed the SAME input
      M3 feeds it (``VerificationRequest`` wire dump) — deriving it here from the
      JOB_SPEC would mint *a different key wearing the same name* (p5c18 T4's
      mutation: well-formed, per-request distinct, and wrong).
    """
    from cv_infra.contract.schema import CustomCriterion
    from cv_infra.report.regression import identity_key
    from cv_infra.runner.recording import BAG_SENSORS_ENV

    runner_env = {k: os.environ[k] for k in _CONSENT_ENV_KEYS if k in os.environ}
    if BAG_SENSORS_ENV in os.environ:
        runner_env[BAG_SENSORS_ENV] = os.environ[BAG_SENSORS_ENV]
    kwargs: dict[str, Any] = {"runner_env": runner_env} if runner_env else {}
    if any(isinstance(c, CustomCriterion) for c in admitted.request.acceptance_criteria):
        kwargs["oracle_plugin_dir"] = str(scenario_path.parent.resolve())
    kwargs["request_identity_key"] = identity_key(
        admitted.request.model_dump(mode="json", by_alias=True)
    )
    return kwargs


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

    What rides ALONGSIDE the JOB_SPEC (operator consent, the custom-oracle plugin
    dir, ``request_identity_key``) is assembled by ``_run_job_kwargs`` — see its
    docstring for each key's contract. ``request_identity_key`` rides
    UNCONDITIONALLY here because this entrypoint always holds an ADMITTED request:
    the honest-absence branch (``run_job``'s ``None`` default) belongs to callers
    that have no request at all, so ``run`` and the REST envelope emit the same
    key for the same request document.
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
    # p6c3: `cv-infra run` executes exactly one job, so it runs SAMPLE 0 of a
    # randomized document (a preview of what the fan-out's first sample will be —
    # same seed, same index, same bytes). A static document is returned unchanged.
    from cv_infra.contract.derive import materialize_request

    job_spec = _job_spec_from_request(materialize_request(admitted.request, 0), job_id)

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

    outcome = run_job(
        job_spec,
        out_dir,
        args.runner_image,
        job_spec["sut_image_ref"],
        **_run_job_kwargs(admitted, scenario_path),
    )
    return _exit_from_outcome(outcome)


def main(argv: list[str] | None = None) -> int:
    """Parse arguments and dispatch.

    Returns the process exit code per the contract documented in the module
    docstring. ``--help`` and argparse usage errors exit directly via
    ``SystemExit`` (``cv-infra --help`` => 0; bad/missing run arguments => 2,
    matching EXIT_CONTRACT).
    """
    parser = _build_parser()
    # ``parse_known_args`` + the explicit leftover check below renders a stray
    # token as the friendly one-liner + exit 2, instead of argparse's own bare
    # usage dump. Every sub-command parses its real schema now (no placeholder
    # absorbs trailing tokens any more), so the check is unconditional.
    args, extra = parser.parse_known_args(argv)

    if args.command is None:
        # Incomplete invocation: surface usage as a contract/usage error (2),
        # matching argparse's own convention for usage errors.
        parser.print_help(sys.stderr)
        return EXIT_CONTRACT

    if extra:
        print(
            f"cv-infra {args.command}: unrecognized argument(s): {' '.join(extra)}",
            file=sys.stderr,
        )
        return EXIT_CONTRACT

    if args.command == "run":
        return _cmd_run(args)

    if args.command == "monitor":
        try:
            # Lazy import (mirrors the batch idiom below): the operational-view
            # surface pulls httpx — the --help and run paths must never load it.
            from cv_infra.cli import monitor
        except ImportError as exc:
            print(
                f"cv-infra monitor: operational-view surface unavailable ({_one_line(exc)}) — "
                "platform build incomplete; this is an infrastructure error, "
                "not a SUT verdict",
                file=sys.stderr,
            )
            return EXIT_INFRA
        return monitor.cmd_monitor(args)

    if args.command == "report":
        try:
            # Lazy import (mirrors the batch idiom below): the report surface is
            # a thin client over the orchestrator REST report endpoint and pulls
            # httpx — the --help and run paths must never load it.
            from cv_infra.cli import batch
        except ImportError as exc:
            print(
                f"cv-infra report: report surface unavailable ({_one_line(exc)}) — "
                "platform build incomplete; this is an infrastructure error, "
                "not a SUT verdict",
                file=sys.stderr,
            )
            return EXIT_INFRA
        return batch.cmd_report(args)

    if args.command in _BATCH_MODULE_COMMANDS:
        try:
            # Lazy import (mirrors the supervisor idiom above): the batch
            # surface pulls httpx + the M3 REST module — the --help and run
            # paths must never load them.
            from cv_infra.cli import batch
        except ImportError as exc:
            print(
                f"cv-infra {args.command}: batch surface unavailable ({_one_line(exc)}) — "
                "platform build incomplete; this is an infrastructure error, "
                "not a SUT verdict",
                file=sys.stderr,
            )
            return EXIT_INFRA
        dispatch = {
            "submit": batch.cmd_submit,
            "status": batch.cmd_status,
            "wait": batch.cmd_wait,
            # selftest is the SAME submit+wait machinery over the built-in stub
            # envelope (REQ-SELFTEST-002: no self-test-only execution path).
            "selftest": batch.cmd_selftest,
        }
        return dispatch[args.command](args)

    # UNREACHABLE by construction: every name in ``_SUBCOMMANDS`` is dispatched
    # above. Kept as the defensive floor so a future sub-command added to the
    # parser but not to a dispatch table fails LOUDLY as a platform/not-ready
    # condition (3) instead of falling off the end and returning a silent 0.
    print(
        f"cv-infra: '{args.command}' is declared on the CLI surface but has no dispatch "
        "— platform build incomplete; this is an infrastructure error, not a SUT verdict",
        file=sys.stderr,
    )
    return EXIT_INFRA


if __name__ == "__main__":  # pragma: no cover - process entrypoint (console script calls main())
    sys.exit(main())
