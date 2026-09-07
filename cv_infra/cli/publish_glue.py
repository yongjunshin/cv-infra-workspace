"""Publish glue — the three things the workflow does with a finished run.

    python -m cv_infra.cli.publish_glue publish <report.json> <out-dir>
    python -m cv_infra.cli.publish_glue annotate <errors.json>
    python -m cv_infra.cli.publish_glue stage-artifacts <run-dir> <staging-dir>

It re-implements nothing: the markdown/Check payloads come from ``report.github`` and
the friendly error prose from ``contract.errors``. What it adds is the plumbing those
two cannot do — writing each payload to a FIXED file name the workflow's
``github-script`` step reads, turning an error object into the ``::error file,line,col::``
line the runner renders on the PR diff, and gathering the run's bytes into one directory
because ``upload-artifact``'s ``path:`` is static YAML and cannot read a dynamic list.

No token, no socket: the API calls are ``actions/github-script``'s job.
"""

from __future__ import annotations

import argparse
import json
import shutil
import sys
from pathlib import Path
from typing import Any

from cv_infra.contract.errors import ANNOTATION_KEYS, ContractError
from cv_infra.report import github

#: Fixed payload file names — the single source shared by the workflow steps that read
#: them and by the tests. JSON for the machine payload, markdown for the human bodies.
CHECK_RUN_FILE = "check-run.json"
STICKY_COMMENT_FILE = "sticky-comment.md"
STEP_SUMMARY_FILE = "step-summary.md"

#: What ``stage-artifacts`` collects out of a run dir, in order. The report first (it is
#: what a human opens), then the rendered payloads, then the per-case evidence.
STAGED_ENTRIES = ("report.json", "payloads", "zips", "logs")


# --- (1) publish — report JSON -> the payload files -----------------------------------


def render_payloads(report: dict[str, Any]) -> dict[str, Any]:
    """The three publish surfaces, keyed by the file name each is written under."""
    return {
        CHECK_RUN_FILE: github.render_check_run(report),
        STICKY_COMMENT_FILE: github.render_sticky_comment(report),
        STEP_SUMMARY_FILE: github.render_step_summary(report),
    }


def write_payloads(report: dict[str, Any], out_dir: Path) -> dict[str, Path]:
    """Write the payloads into ``out_dir`` under their fixed names; return the map."""
    out_dir.mkdir(parents=True, exist_ok=True)
    written: dict[str, Path] = {}
    for name, payload in render_payloads(report).items():
        path = out_dir / name
        if name.endswith(".json"):
            path.write_text(
                json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
                encoding="utf-8",
            )
        else:
            path.write_text(payload, encoding="utf-8")
        written[name] = path
    return written


# --- (2) annotate — errors.json -> ::error file,line,col:: ----------------------------


def _escape_data(value: str) -> str:
    """Escape a workflow-command message (data segment)."""
    return value.replace("%", "%25").replace("\r", "%0D").replace("\n", "%0A")


def _escape_property(value: str) -> str:
    """Escape a workflow-command property value (adds ``:`` and ``,``)."""
    return _escape_data(value).replace(":", "%3A").replace(",", "%2C")


def _friendly_message(entry: dict[str, Any]) -> str:
    """Rebuild the ``ContractError`` from its annotation dict and render its one-liner
    VERBATIM — the rejection reads identically on the console and on the PR line."""
    kwargs = {key: entry[key] for key in ANNOTATION_KEYS if entry.get(key) is not None}
    return str(ContractError(**kwargs))


def render_annotation(entry: dict[str, Any]) -> str:
    """One annotation dict -> a ``::error file=..,line=..,col=..::<msg>`` line.

    Each property is omitted when absent (no file -> a plain ``::error::``; a column
    only rides along with a line). ``source_path`` is already checkout-relative, which
    is exactly what the runner resolves an annotation ``file`` against.
    """
    props: list[str] = []
    source_path = entry.get("source_path")
    if source_path:
        props.append(f"file={_escape_property(str(source_path))}")
    source_line = entry.get("source_line")
    if source_line is not None:
        props.append(f"line={source_line}")
        source_col = entry.get("source_col")
        if source_col is not None:
            props.append(f"col={source_col}")
    head = f"::error {','.join(props)}::" if props else "::error::"
    return head + _escape_data(_friendly_message(entry))


def _error_entries(data: Any) -> list[dict[str, Any]]:
    """The annotation dicts in an ``errors.json``: a list of them, or a lone one."""
    entries = [data] if isinstance(data, dict) else data
    return [e for e in entries if isinstance(e, dict)] if isinstance(entries, list) else []


def render_annotations(data: Any) -> list[str]:
    """Every error entry -> its ``::error::`` line (empty in => empty out)."""
    return [render_annotation(entry) for entry in _error_entries(data)]


# --- (3) stage-artifacts — the run dir -> a directory upload-artifact can take --------


#: Absolute paths a staging dir may never resolve TO — emptying any of them would
#: destroy the host. Ancestors of ``$HOME`` and the filesystem root are additionally
#: rejected by the depth/containment checks in ``_prepare_staging_dir``.
_FORBIDDEN_STAGING_DIRS = frozenset(
    Path(p)
    for p in (
        "/",
        "/bin",
        "/boot",
        "/dev",
        "/etc",
        "/home",
        "/lib",
        "/media",
        "/mnt",
        "/opt",
        "/proc",
        "/root",
        "/run",
        "/sbin",
        "/srv",
        "/sys",
        "/tmp",
        "/usr",
        "/var",
    )
)


def _resolve_safe_staging_dir(staging_dir: Path) -> Path:
    """Resolve the staging target and REFUSE every dangerous shape; else return it.

    A path mistake here is unrecoverable, so each guard is explicit and there is no
    shell ``rm -rf``. The target ① is resolved to an absolute path (the workflow passes
    a relative ``artifacts``), ② must not be a system root, ``$HOME`` or an ancestor of
    it, nor a shallow (<2 component) path, ③ must not be a repository checkout (a
    ``.git`` entry — this is what catches a stray ``.``), ④ must not be a symlink (its
    contents live outside the named location, so emptying it would reach outside).

    Raising is the ONLY outcome besides a safe path — no caller may proceed on a
    refusal, and nothing is removed before every guard has passed.
    """
    raw = Path(staging_dir).expanduser()
    if raw.is_symlink():
        raise ValueError(f"stage-artifacts: refusing a symlinked staging dir: {raw}")
    target = raw.resolve()
    home = Path.home()
    if len(target.parts) < 3 or target in _FORBIDDEN_STAGING_DIRS:
        raise ValueError(f"stage-artifacts: refusing an unsafe staging dir: {target}")
    if target == home or target in home.parents:
        raise ValueError(f"stage-artifacts: refusing a home/ancestor staging dir: {target}")
    if (target / ".git").exists():
        raise ValueError(f"stage-artifacts: refusing a repo checkout as staging dir: {target}")
    return target


def _clear_entries(target: Path) -> int:
    """Remove the ALREADY-VALIDATED target's entries; return how many were removed.

    Only the target's ENTRIES go — never the target itself, never its parent — and
    removal never follows a symlink out: a symlinked entry is unlinked, its referent
    untouched. A non-existent target has nothing to clear (0).
    """
    if not target.is_dir():
        return 0
    cleared = 0
    for entry in sorted(target.iterdir()):
        if entry.is_dir() and not entry.is_symlink():
            shutil.rmtree(entry)
        else:
            entry.unlink()
        cleared += 1
    return cleared


def _prepare_staging_dir(staging_dir: Path) -> Path:
    """Resolve, safety-check and EMPTY the staging dir; return the resolved path.

    WHY empty it: a self-hosted runner does NOT clean its workspace between jobs, so a
    staging tree left by a PREVIOUS run survives and ``upload-artifact`` re-uploads it
    verbatim — measured once as a green PR's artifact being 92.9% the previous push's
    failure recordings. The target must START empty; this function is that guarantee.

    SAFETY = ``_resolve_safe_staging_dir`` (refuse) then ``_clear_entries`` (remove),
    strictly in that order: nothing is removed until every guard has passed. One stderr
    line always reports what was cleared — silence is how the defect above survived.
    """
    target = _resolve_safe_staging_dir(staging_dir)
    cleared = _clear_entries(target)
    target.mkdir(parents=True, exist_ok=True)
    plural = "y" if cleared == 1 else "ies"
    print(f"stage-artifacts: cleared {cleared} stale entr{plural} from {target}", file=sys.stderr)
    return target


def stage_artifacts(run_dir: Path, staging_dir: Path) -> dict[str, int]:
    """Copy the run's report, payloads and per-case evidence into ``staging_dir``.

    The run dir IS the curation: every zip and log in it belongs to a case of THIS run
    (``cases/`` is deliberately not staged — its contents were already zipped). An
    entry that does not exist is skipped with a stderr line rather than failing the
    upload: a missing log must never cost the operator the rest of the evidence.
    Returns ``{"staged", "skipped"}`` counted in entries, not bytes.
    """
    target = _prepare_staging_dir(staging_dir)
    source = Path(run_dir)
    staged = skipped = 0
    for name in STAGED_ENTRIES:
        entry = source / name
        if entry.is_dir():
            shutil.copytree(entry, target / name)
        elif entry.is_file():
            shutil.copy2(entry, target / name)
        else:
            print(f"stage-artifacts: skip (absent) {entry}", file=sys.stderr)
            skipped += 1
            continue
        staged += 1
    return {"staged": staged, "skipped": skipped}


# --- entry point (invoked by the reusable verify.yml) ---------------------------------


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="cv-infra-publish",
        description="report.json -> GitHub payloads · errors.json -> annotations · run dir"
        " -> upload staging",
    )
    sub = parser.add_subparsers(dest="mode", required=True, metavar="<mode>")
    pub = sub.add_parser("publish", help="render the payload files from a report JSON")
    pub.add_argument("report", help="path to the run's report.json")
    pub.add_argument("out_dir", help="directory the payload files are written into")
    ann = sub.add_parser("annotate", help="render error objects as ::error:: workflow commands")
    ann.add_argument("errors", help="path to the run's errors.json")
    stage = sub.add_parser(
        "stage-artifacts", help="copy the run's report/payloads/zips/logs into a staging dir"
    )
    stage.add_argument("run_dir", help="the run dir (cv-infra verify --run-dir)")
    stage.add_argument("staging_dir", help="directory the artifact files are staged into")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)
    if args.mode == "publish":
        report = json.loads(Path(args.report).read_text(encoding="utf-8"))
        for name, path in write_payloads(report, Path(args.out_dir)).items():
            print(f"{name}={path}", file=sys.stderr)  # provenance only; stdout stays clean
        return 0
    if args.mode == "stage-artifacts":
        summary = stage_artifacts(Path(args.run_dir), Path(args.staging_dir))
        print(f"staged={summary['staged']} skipped={summary['skipped']}")
        return 0
    # annotate: emit each ::error:: to stdout so the runner surfaces it inline.
    data = json.loads(Path(args.errors).read_text(encoding="utf-8"))
    for line in render_annotations(data):
        print(line)
    return 0


if __name__ == "__main__":  # pragma: no cover - process entrypoint (python -m; main() is tested)
    sys.exit(main())
