"""Publish glue — the M8 Action-plane adapter that turns a report JSON into the
four ``cv_infra.report.github`` payloads a ``github-script`` step posts, and an
M1 error object into a ``::error file,line,col::`` workflow annotation (M8 §3.4/§3.5,
REQ-REPORT-007, REQ-INTAKE-005, NFR-INTAKE-001, LOCKED §14).

This module RE-IMPLEMENTS NOTHING: the four surface renderers live in
``cv_infra.report.github`` (owner = M4) and are IMPORTED, and the friendly
error-prose shape lives in ``cv_infra.contract.errors`` (owner = M1) and is
rehydrated verbatim (same idiom as ``cli/batch._render_rejection``). The glue
only (a) writes each rendered payload to a fixed-name file the composite action
hands to ``actions/github-script@v7`` / ``actions/upload-artifact@v4``, and
(b) maps the 8 machine-readable annotation keys (D-L 1:1) to the GitHub
workflow-command line the runner surfaces on the PR diff.

It holds NO GitHub token and opens NO socket — the real API calls / uploads are
``actions/github-script`` + ``actions/upload-artifact`` (LOCKED §14). Import-wise
it drags only ``github.py`` (stdlib + ``cli.exit_codes`` leaf) and ``errors.py``
(stdlib-only), so it runs on the GPU box without the server/network graph.

Invoked by ``actions/verify`` (composite) / the reusable ``verify.yml`` as::

    python -m cv_infra.cli.publish_glue publish <report.json> <out-dir>
    python -m cv_infra.cli.publish_glue annotate <errors.json>
    python -m cv_infra.cli.publish_glue stage-artifacts <report.json> <staging-dir>

``stage-artifacts`` copies the artifact manifest's ``uploads[]`` (the curated
per-run MCAP/mp4/result.json paths) into a staging dir ``actions/upload-artifact``
then uploads — the dynamic path list lives in JSON and cannot be a static YAML
``path:``, so a TESTED Python step gathers the bytes first (no brittle shell/jq).

The REAL trigger / posting is observed in p5c4 (this cycle authors + statically
verifies the plumbing — no live GitHub run is claimed).
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

#: Fixed payload file names (the single source shared by the composite action's
#: github-script / upload-artifact steps and the static test). JSON for the
#: machine payloads, markdown for the human bodies.
CHECK_RUN_FILE = "check-run.json"
STICKY_COMMENT_FILE = "sticky-comment.md"
STEP_SUMMARY_FILE = "step-summary.md"
ARTIFACT_MANIFEST_FILE = "artifact-manifest.json"


# --------------------------------------------------------------------------- #
# (1) publish — report JSON -> the four github.py payloads written to files
# --------------------------------------------------------------------------- #
def render_payloads(report: dict[str, Any]) -> dict[str, Any]:
    """Render all four publish surfaces from a report JSON (IMPORTED renderers).

    Returns a dict keyed by fixed file name so ``write_payloads`` and the tests
    share one mapping; the github.py functions are called, never reimplemented.
    """
    return {
        CHECK_RUN_FILE: github.render_check_run(report),
        STICKY_COMMENT_FILE: github.render_sticky_comment(report),
        STEP_SUMMARY_FILE: github.render_step_summary(report),
        ARTIFACT_MANIFEST_FILE: github.render_artifact_manifest(report),
    }


def write_payloads(report: dict[str, Any], out_dir: Path) -> dict[str, Path]:
    """Write the four payloads into ``out_dir`` under their fixed names.

    ``.json`` payloads are dumped as JSON (``ensure_ascii=False`` keeps the
    Korean C-1 / infra messaging intact); ``.md`` payloads are written verbatim.
    Returns the file-name -> path map the composite references.
    """
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


# --------------------------------------------------------------------------- #
# (2) annotate — M1 error object -> ::error file,line,col:: workflow command
# --------------------------------------------------------------------------- #
def _escape_data(value: str) -> str:
    """Escape a GitHub workflow-command message (data segment)."""
    return value.replace("%", "%25").replace("\r", "%0D").replace("\n", "%0A")


def _escape_property(value: str) -> str:
    """Escape a GitHub workflow-command property value (adds ``:`` and ``,``)."""
    return _escape_data(value).replace(":", "%3A").replace(",", "%2C")


def _friendly_message(entry: dict[str, Any]) -> str:
    """Rehydrate the M1 ``ContractError`` from the 8-key annotation dict and
    render its friendly one-liner VERBATIM (field path + expected + got + example
    — M1 owns the shape, mirrors ``batch._render_rejection``; no format invented
    here). Only the keys the entry actually carries are passed through."""
    kwargs = {key: entry[key] for key in ANNOTATION_KEYS if entry.get(key) is not None}
    return str(ContractError(**kwargs))


def render_annotation(entry: dict[str, Any]) -> str:
    """One 8-key annotation dict -> a ``::error file=..,line=..,col=..::<msg>`` line.

    Field mapping is 1:1 (D-L): ``source_path -> file``, ``source_line -> line``,
    ``source_col -> col`` (each omitted when absent — ``file``-less falls back to
    a plain ``::error::``, and ``col`` only rides when ``line`` does). The message
    is the M1 friendly prose. ``source_path`` is already consumer-repo-root
    relative (M1 §3.4 / D-L), so it maps straight to the annotation ``file``.
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
    """Extract the 8-key annotation dicts from either a bare list or the M3 422
    body shape (``{"detail": {"errors": [...]}}`` / ``{"errors": [...]}``)."""
    if isinstance(data, list):
        entries = data
    elif isinstance(data, dict):
        detail = data.get("detail", data)
        entries = detail.get("errors") if isinstance(detail, dict) else None
    else:
        entries = None
    return [e for e in entries if isinstance(e, dict)] if isinstance(entries, list) else []


def render_annotations(data: Any) -> list[str]:
    """Every error entry -> its ``::error::`` line (empty in => empty out)."""
    return [render_annotation(entry) for entry in _error_entries(data)]


# --------------------------------------------------------------------------- #
# (3) stage-artifacts — manifest uploads[] -> a staging dir upload-artifact takes
# --------------------------------------------------------------------------- #
def _fs_safe(value: Any) -> str:
    """A single filesystem path segment (no separators; ``None`` -> ``unknown``)."""
    text = "unknown" if value is None else str(value)
    return text.replace("/", "_").replace("\\", "_") or "unknown"


def _entry_id(entry: dict[str, Any]) -> str:
    """A human label for a skip warning (request/repeat/kind)."""
    return f"{entry.get('request_id')}/repeat-{entry.get('repeat_index')}/{entry.get('kind')}"


def _staging_relpath(entry: dict[str, Any], src: Path) -> Path:
    """Deterministic, collision-free layout for one upload entry:
    ``<request_id>/repeat-<repeat_index>/<kind><suffix>``.

    ``(request_id, repeat_index, kind)`` is unique across ``uploads`` (each kind
    appears at most once per selected entry — ``github._classify_entry``), so the
    kind alone names the file (the source suffix is preserved). Path separators in
    the request id are neutralised so a slashy id can never escape the staging dir.
    """
    request = _fs_safe(entry.get("request_id"))
    kind = str(entry.get("kind") or "artifact")
    return Path(request) / f"repeat-{entry.get('repeat_index')}" / f"{kind}{src.suffix}"


def stage_uploads(uploads: list[dict[str, Any]], staging_dir: Path) -> dict[str, int]:
    """Copy each ``uploads[]`` entry's ``path`` into ``staging_dir`` under a stable
    layout. ONLY the curated ``uploads`` are staged — ``missing``/``excluded`` never
    reach here (결정 #1/#2 curation was already applied when the manifest was built).

    Defensive (T2 is aligning the producer to host-resolvable absolute paths): an
    entry whose ``path`` is absent/empty, or does not resolve to an existing file
    (a container-internal path unresolvable on the host, or a stray relative path),
    is SKIPPED with a stderr warning — a missing byte never fails the upload/job.
    Returns ``{"staged", "skipped"}``.
    """
    staging_dir.mkdir(parents=True, exist_ok=True)
    staged = 0
    skipped = 0
    for entry in uploads:
        path = entry.get("path")
        if not path:
            print(f"stage-artifacts: skip (no path) {_entry_id(entry)}", file=sys.stderr)
            skipped += 1
            continue
        src = Path(path)
        if not src.is_file():
            print(f"stage-artifacts: skip (unresolved path) {path}", file=sys.stderr)
            skipped += 1
            continue
        dest = staging_dir / _staging_relpath(entry, src)
        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(src, dest)
        staged += 1
    return {"staged": staged, "skipped": skipped}


def stage_artifacts(report: dict[str, Any], staging_dir: Path) -> dict[str, int]:
    """Stage the artifact-manifest ``uploads[]`` for ``actions/upload-artifact``.

    The manifest is the SINGLE SOURCE of what to upload — this IMPORTS
    ``github.render_artifact_manifest`` (never re-selects/re-classifies) and copies
    only its ``uploads`` into ``staging_dir``. The staging step exists because
    ``upload-artifact``'s ``path:`` is static YAML and cannot read the manifest's
    dynamic per-run path list; gathering the curated bytes into one dir first is
    the sanctioned pattern (no path is fetched from a socket — files only).
    """
    manifest = github.render_artifact_manifest(report)
    return stage_uploads(manifest.get("uploads") or [], staging_dir)


# --------------------------------------------------------------------------- #
# entry point (invoked by actions/verify composite / reusable verify.yml)
# --------------------------------------------------------------------------- #
def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="cv-infra-publish",
        description="M8 publish glue: report JSON -> github.py payloads / M1 errors -> annotations",
    )
    sub = parser.add_subparsers(dest="mode", required=True, metavar="<mode>")
    pub = sub.add_parser("publish", help="render the four payloads from a report JSON into a dir")
    pub.add_argument("report", help="path to the report JSON (cv-infra report <id> --json)")
    pub.add_argument("out_dir", help="directory the payload files are written into")
    ann = sub.add_parser("annotate", help="render M1 error objects as ::error:: workflow commands")
    ann.add_argument("errors", help="path to the errors JSON (list or M3 422 body)")
    stage = sub.add_parser(
        "stage-artifacts",
        help="copy the artifact manifest's uploads[] paths into a staging dir for upload-artifact",
    )
    stage.add_argument("report", help="path to the report JSON (cv-infra report <id> --json)")
    stage.add_argument("staging_dir", help="directory the curated artifact files are staged into")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)
    if args.mode == "publish":
        report = json.loads(Path(args.report).read_text(encoding="utf-8"))
        for name, path in write_payloads(report, Path(args.out_dir)).items():
            print(f"{name}={path}", file=sys.stderr)  # provenance only; stdout stays clean
        return 0
    if args.mode == "stage-artifacts":
        report = json.loads(Path(args.report).read_text(encoding="utf-8"))
        summary = stage_artifacts(report, Path(args.staging_dir))
        print(f"staged={summary['staged']} skipped={summary['skipped']}")
        return 0
    # annotate: emit each ::error:: to stdout so the runner surfaces it inline.
    data = json.loads(Path(args.errors).read_text(encoding="utf-8"))
    for line in render_annotations(data):
        print(line)
    return 0


if __name__ == "__main__":
    sys.exit(main())
