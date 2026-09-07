"""``cv-infra verify`` — one command, and the order it does things in is the contract.

    parse → expand the input space → run each case → judge it → compare to baseline →
    report → exit code

Three properties are worth stating, because each is a decision that could reasonably
have gone the other way:

* **Everything refusable is refused before a container starts.** ``contract.inputs``
  reads the model, stats the paths and resolves the PICT binary at admit, so a bad
  request costs ZERO GPU seconds and says which flag to change. Exit 2 also writes
  ``errors.json``, which the workflow turns into an inline annotation on the offending
  line — a rejection a developer reads in the diff, not in a log.
* **The budget is checked immediately before a case is launched**, never mid-case. The
  case list is case-major, so a run that hits the wall loses whole cases off the END of
  the array and keeps the coverage the executed prefix earned. Truncating a case's
  repeats instead would publish a pass ratio computed from half its samples.
* **The exit code comes out of the report, not out of the control flow.** Every judged
  fact lands in the report dict first (``report.aggregate``), and the code is folded
  from it — so the JSON, the process status and the CI conclusion cannot disagree, and
  a human can reproduce the verdict from the artifact alone.

Parallelism is one thread per CASE (``concurrency``), each running that case's repeats
in sequence: one container at a time per worker, all of them time-sharing the GPU.
"""

from __future__ import annotations

import json
import os
import shutil
import sys
import time
import traceback
from collections.abc import Mapping, Sequence
from itertools import groupby
from pathlib import Path
from typing import Any

from cv_infra import baselines, execution
from cv_infra.cli import publish_glue
from cv_infra.cli.exit_codes import EXIT_CONTRACT, EXIT_INFRA, EXIT_PASS
from cv_infra.contract import cases as case_expansion
from cv_infra.contract import inputs, pict, verdict
from cv_infra.contract.errors import ContractError
from cv_infra.report import aggregate

USAGE = (
    "usage: cv-infra verify --sim-script <path> --input-space <path> --output-dir <path>\n"
    "                       --sim-image <name>@sha256:<64 hex>\n"
    "                       [--oracle-script <path>] [--pict-k K] [--repeats N]\n"
    "                       [--budget-s S] [--concurrency K]\n"
    "                       [--report-only] [--update-baseline] [--run-dir DIR]\n"
    "       cv-infra selftest [any `verify` flag]\n"
    "\n"
    "exit: 0 pass · 1 a check failed or regressed · 2 the request was refused"
    " · 3 the platform could not judge"
)

#: Where the run's own files land, relative to ``--run-dir``.
REPORT_FILE = "report.json"
ERRORS_FILE = "errors.json"
PAYLOADS_DIR = "payloads"

#: The bundled example, checkout-relative (``examples/selftest`` in THIS repository).
SELFTEST_DIR = "examples/selftest"

#: ``selftest`` = ``verify`` with the bundled example filled in. Presets come FIRST so a
#: later flag of the operator's own wins (argparse keeps the last occurrence).
#:
#: This is the ONLY caller that passes ``DEFAULT_SIM_IMAGE``: ``--sim-image`` is required
#: and un-defaulted for a consumer (the image their script was developed against — see
#: ``contract.inputs._digest_pinned_image``), and the bundled example's is this one.
SELFTEST_PRESET: tuple[str, ...] = (
    "--checkout",
    inputs.DEFAULT_CHECKOUT,
    "--sim-image",
    inputs.DEFAULT_SIM_IMAGE,
    "--sim-script",
    f"{SELFTEST_DIR}/sim.py",
    "--input-space",
    f"{SELFTEST_DIR}/param_space.pict",
    "--output-dir",
    f"{SELFTEST_DIR}/out",
    "--oracle-script",
    f"{SELFTEST_DIR}/oracle.py",
)

COMMANDS = ("verify", "selftest")


def main(argv: Sequence[str] | None = None) -> int:
    """Console entry point. Never lets an unexpected fault exit 1: an uncaught Python
    exception would otherwise reach CI as "your robot failed" (exit 1), when what
    happened is that OUR code broke (exit 3)."""
    args = list(sys.argv[1:] if argv is None else argv)
    if not args or args[0] not in COMMANDS:
        print(USAGE, file=sys.stderr)
        return EXIT_PASS if args[:1] in (["-h"], ["--help"]) else EXIT_CONTRACT
    try:
        if args[0] == "selftest":
            return selftest(args[1:], os.environ)
        return verify(args[1:], os.environ)
    except Exception:  # noqa: BLE001 - see the docstring: a platform fault is exit 3
        traceback.print_exc()
        return EXIT_INFRA


def selftest(argv: Sequence[str], environ: Mapping[str, str]) -> int:
    """Run the bundled example through the ordinary ``verify`` pipeline.

    This is the runner's own smoke test: it needs no consumer repository, no cloud
    asset and no robot, so a broken image, GPU, mount or PICT install is diagnosed HERE
    instead of in someone's pull request.

    The example is not part of the installed wheel (only ``cv_infra`` is packaged), so
    its absence means cv-infra is being invoked from outside its own checkout — a
    provisioning fact about the runner, hence exit 3 and not a request rejection.
    """
    checkout = _path_flag(argv, flag="--checkout", default=inputs.DEFAULT_CHECKOUT)
    if not (checkout / SELFTEST_DIR).is_dir():
        print(
            f"cv-infra: {SELFTEST_DIR}/ is not in {checkout} — `selftest` runs the example"
            " that ships with the cv-infra source tree. Run it from a checkout of this"
            " repository, or point --checkout at one.",
            file=sys.stderr,
        )
        return EXIT_INFRA
    return verify([*SELFTEST_PRESET, *argv], environ)


def verify(argv: Sequence[str], environ: Mapping[str, str]) -> int:
    """Admit the request, then run it. The two failure shapes are kept apart: a
    ``ContractError`` is the REQUEST's problem (exit 2, annotated), an ``InfraError``
    is the RUNNER's (exit 3, no annotation — nothing in the consumer's repo is wrong)."""
    try:
        spec = inputs.parse(argv, environ)
    except ContractError as err:
        return _reject(err, _path_flag(argv, flag="--run-dir", default=inputs.DEFAULT_RUN_DIR))
    except inputs.InfraError as err:
        print(f"cv-infra: {err}", file=sys.stderr)
        return EXIT_INFRA
    return run_verify(spec, environ=environ)


def run_verify(
    spec: Any,
    docker_client: Any = None,
    environ: Mapping[str, str] | None = None,
) -> int:
    """Run an ADMITTED request end to end; return its exit code.

    ``docker_client`` is the injection seam: production passes nothing and the real
    client is resolved once here, tests pass a duck-typed fake and the whole pipeline
    runs on a CPU host.
    """
    environ = os.environ if environ is None else environ
    for warning in spec.warnings:
        print(f"cv-infra: warning: {warning}", file=sys.stderr)
    try:
        array = pict.generate(
            spec.input_space_text,
            order=spec.pict_k,
            pict_bin=spec.pict_bin,
            source_path=spec.sim_input_space,
        )
    except ContractError as err:  # PictError — the model is the request's problem
        return _reject(err, spec.run_dir)
    try:
        client = execution.resolve_docker_client(docker_client)
    except Exception as exc:  # noqa: BLE001 - any docker-side failure is the same fault
        print(
            f"cv-infra: docker is not available ({type(exc).__name__}: {exc}) — the runner"
            " needs a reachable docker daemon and the NVIDIA container runtime.",
            file=sys.stderr,
        )
        return EXIT_INFRA

    plan_cases = _cases_of(array, spec)
    ran, truncated_after = _run_cases(spec, plan_cases, client, environ)
    plan = aggregate.PlanInfo(
        requested_k=spec.pict_k,
        cases_planned=len(plan_cases),
        cases_run=len(ran),
        coverage_achieved=pict.coverage_of_prefix(array, len(ran), spec.pict_k),
        truncated_after_case=truncated_after,
    )

    observations = aggregate.observations(ran)
    outcome = baselines.compare_best_effort(spec.baseline_db, observations)
    updated = spec.update_baseline and baselines.upsert_best_effort(
        spec.baseline_db,
        baselines.rows_for(observations),
        source=baselines.source_for(environ),
    )
    report = aggregate.build_report(spec, plan, ran, outcome, baseline_updated=updated)
    _write_run_dir(spec.run_dir, report)

    code = report["summary"]["exit_code"]
    if code == EXIT_CONTRACT:  # the only post-run rejection: a gate that asserts nothing
        _write_errors(spec.run_dir, _empty_gate_error(spec, report))
    print(_summary_line(report))
    return code


# --- the case loop --------------------------------------------------------------------


def _cases_of(array: Any, spec: Any) -> list[list[Any]]:
    """The covering array as a list of CASES, each holding its repeats in order.

    ``contract.cases.expand`` is the single source of the run list (ids, seeds, argv);
    this only regroups its case-major output, so the parallel unit is the case and a
    case's repeats stay together (and stay whole when the budget cuts).
    """
    runs = case_expansion.expand(array, sim_script=spec.sim_script, repeats=spec.repeats)
    return [list(group) for _, group in groupby(runs, key=lambda run: run.case_index)]


def _run_cases(
    spec: Any, plan_cases: Sequence[Sequence[Any]], client: Any, environ: Mapping[str, str]
) -> tuple[list[aggregate.CaseRecord], int | None]:
    """Run the cases (``concurrency`` at a time) until the budget runs out.

    Returns the cases that RAN plus the index of the last one (``-1`` when the budget
    was gone before the first case even started — distinct from ``None``, "nothing was
    cut", so the report can say WHY zero cases ran). The deadline is read at the top of
    each case's task, i.e. at the moment
    that case would start: workers take the queue in array order, so what runs is the
    array's prefix — which is what makes the reported coverage meaningful.
    """
    deadline = None if spec.budget_s is None else time.monotonic() + spec.budget_s

    def run_one(case_runs: Sequence[Any]) -> aggregate.CaseRecord | None:
        if deadline is not None and time.monotonic() >= deadline:
            return None
        return _run_case(spec, case_runs, client, environ)

    # Imported here, not at module scope: `cv-infra --help` and every rejection path
    # must stay free of thread machinery they never use.
    from concurrent.futures import ThreadPoolExecutor  # noqa: PLC0415 - see above

    with ThreadPoolExecutor(max_workers=spec.concurrency) as pool:
        records = list(pool.map(run_one, plan_cases))
    ran = [record for record in records if record is not None]
    if len(ran) == len(plan_cases):
        return ran, None
    return ran, len(ran) - 1


def _run_case(
    spec: Any, case_runs: Sequence[Any], client: Any, environ: Mapping[str, str]
) -> aggregate.CaseRecord:
    """One case: its repeats, in order, each sim + oracle + verdict."""
    runs = [_run_once(spec, case, client, environ) for case in case_runs]
    first = case_runs[0]
    return aggregate.CaseRecord(
        case_id=first.case_id,
        axes=dict(first.axes),
        repeats_planned=spec.repeats,
        runs=tuple(runs),
    )


def _run_once(spec: Any, case: Any, client: Any, environ: Mapping[str, str]) -> aggregate.RunRecord:
    """One case+repeat: the sim container, then (in gate mode) the oracle container."""
    sim = execution.run_sim_case(spec, case, client, run_dir=spec.run_dir, operator_env=environ)
    rc_oracle: int | None = None
    stdout = ""
    oracle_error: str | None = None
    if spec.oracle_script and sim.error is None and sim.rc == 0:
        rc_oracle, stdout, oracle_error = execution.run_oracle(
            spec,
            case,
            client,
            run_dir=spec.run_dir,
            operator_env=environ,
            case_out=sim.out_dir,
        )
    _discard_case_dir(sim.out_dir)
    return aggregate.RunRecord(
        repeat=case.repeat,
        seed=case.seed,
        rc_sim=sim.rc,
        rc_oracle=rc_oracle,
        wall_s=sim.wall_s,
        result=_judge(spec, sim, rc_oracle, stdout, oracle_error),
        zip=_run_relative(sim.zip_path, spec.run_dir),
        log=_run_relative(sim.log_path, spec.run_dir),
        zip_truncated=sim.zip_truncated,
    )


def _judge(
    spec: Any, sim: Any, rc_oracle: int | None, stdout: str, oracle_error: str | None
) -> verdict.CaseRunResult:
    """Fold one run into its result. An execution-seam ``error`` (a timeout, a docker
    fault) is carried VERBATIM instead of being re-derived from the exit codes: the
    seam knows what happened and its message is what the report shows."""
    if sim.error is not None:
        return verdict.CaseRunResult(lane=verdict.LANE_ERROR, error=sim.error)
    if oracle_error is not None:
        return verdict.CaseRunResult(lane=verdict.LANE_ERROR, error=oracle_error)
    return verdict.classify(sim.rc, rc_oracle, stdout, gate=bool(spec.oracle_script))


def _discard_case_dir(out_dir: Path) -> None:
    """Drop the case's host output tree once it has been zipped.

    The zip IS the collected form (including the oversize case, where it holds a
    manifest instead of the files — keeping the bytes there would defeat the very cap
    that replaced them). Best-effort: a directory we cannot remove is not worth failing
    a finished case over, it is a disk-space problem for the operator.
    """
    shutil.rmtree(out_dir.parent, ignore_errors=True)


# --- run-dir output -------------------------------------------------------------------


def _write_run_dir(run_dir: Path, report: dict[str, Any]) -> None:
    """``report.json`` plus the rendered payloads the workflow publishes."""
    run_dir.mkdir(parents=True, exist_ok=True)
    (run_dir / REPORT_FILE).write_text(
        json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    publish_glue.write_payloads(report, run_dir / PAYLOADS_DIR)


def _write_errors(run_dir: Path, error: ContractError) -> None:
    """``errors.json`` — the annotation dicts the workflow renders on the diff."""
    run_dir.mkdir(parents=True, exist_ok=True)
    (run_dir / ERRORS_FILE).write_text(
        json.dumps([error.to_annotation_dict()], ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )


def _reject(err: ContractError, run_dir: Path) -> int:
    """Refuse the request: the friendly one-liner on stderr, the machine-readable form
    in ``errors.json``, exit 2. No container was started."""
    print(f"cv-infra: {err}", file=sys.stderr)
    _write_errors(run_dir, err)
    return EXIT_CONTRACT


def _empty_gate_error(spec: Any, report: dict[str, Any]) -> ContractError:
    """The one rejection only a finished run can make: the oracle judged, and not one
    of its keys was a boolean, so the gate asserts nothing and would be green forever.
    ``--report-only`` is the opt-in for exactly this shape."""
    return ContractError(
        field_path="--oracle-script",
        expected="a verdict with at least one boolean key — a bool is a CHECK, and a "
        "gate with no check cannot fail (pass --report-only if that is intended)",
        got=f"{report['summary']['runs_total']} judged run(s), all keys numbers/strings/null",
        example='print(json.dumps({"fell": False, "z_final": 0.12}))',
        source_path=spec.oracle_script,
    )


def _path_flag(argv: Sequence[str], *, flag: str, default: str) -> Path:
    """One path flag, read off raw argv before (or instead of) a full parse.

    Two callers need this. A rejection still has to leave ``errors.json`` where the
    workflow's annotate step looks, so ``--run-dir`` is read even though no spec was
    ever built; and ``selftest`` must know the checkout before it can say whether the
    example is there. Both scan tolerantly — unparseable flags simply leave the default,
    since the real parser is the one that gets to complain about them.
    """
    import argparse  # noqa: PLC0415 - only these two paths need a second parser

    parser = argparse.ArgumentParser(add_help=False)
    parser.add_argument(flag, default=default)
    known, _ = parser.parse_known_args(list(argv))
    raw = getattr(known, flag.lstrip("-").replace("-", "_"))
    return Path(raw).expanduser().resolve()


def _run_relative(path: Path | None, run_dir: Path) -> str | None:
    """A run-dir-relative artifact path — the report travels to readers for whom this
    host's absolute paths mean nothing."""
    if path is None:
        return None
    return str(Path(path).relative_to(run_dir))


def _summary_line(report: dict[str, Any]) -> str:
    summary = report["summary"]
    return (
        f"cv-infra {report['mode']}: {summary['report_outcome']} (exit "
        f"{summary['exit_code']}) — {summary['cases_run']}/{summary['cases_planned']} cases,"
        f" {summary['runs_total']} runs, {summary['checks_failed']} check(s) failed,"
        f" {summary['cases_errored']} errored, {summary['regressions']} regression(s)"
    )


if __name__ == "__main__":  # pragma: no cover - process entrypoint (main() is tested)
    sys.exit(main())
