"""YAML loader / validator / admit gate (M1 §3.3 — REQ-INTAKE-004/006/009).

The 6-stage pipeline IS the acceptance gate (NFR-INTAKE-003 — order matters,
any failing stage rejects BEFORE the execution plane ever sees the input):

    (1) safe parse (SafeLoader)      -> parse error   = friendly reject
    (2) apiVersion resolve           -> unknown/absent = reject / deprecated = warn
    (3) pydantic model_validate      -> schema error  = friendly reject
    (4) self-containedness           -> missing triad = reject   (REQ-INTAKE-006)
    (5) oracle load + bind           -> load failure  = reject   (REQ-INTAKE-007/008;
                                        scenario dir on sys.path while binding — D-1)
    (6) admit marking                -> AdmittedRequest           (REQ-INTAKE-009)

Rejection = a raised ``ContractError`` (friendly: field path + expected +
example + YAML line/col when locatable — NFR-INTAKE-001). The consumer maps it
to exit 2 / HTTP 422 (LOCKED §7-9) — this module never calls ``sys.exit`` and
never leaks a raw traceback into its message. When pydantic reports several
violations the FIRST is raised; consumers that want the full list post-process
via ``errors.from_validation_error`` directly (the loader's YAML locator is
remembered on the exception, so those re-renders keep line/col on EVERY
violation, not just the first — p3c3).

Inputs are a file path or an open text stream — nothing else (no URL, no
inline-string convenience). This loader admits ONE request document; the
user-facing RequestEnvelope (N>=1 scenario file references) is envelope.py's,
built ON this gate (D-2 p4c3).

Host/control-plane module: imports pydantic + yaml freely (the runner image
never imports the loader — D-C/R20).
"""

from __future__ import annotations

import io
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml
from pydantic import ValidationError

from cv_infra.contract import errors as _errors
from cv_infra.contract.errors import ContractError
from cv_infra.contract.schema import VerificationRequest
from cv_infra.contract.version import resolve_api_version
from cv_infra.oracles.base import load_oracle  # sanctioned edge (.importlinter ignore)

_DOC_LINK = "M1-contract-and-schema.md §3.3 (loader pipeline)"


@dataclass(frozen=True)
class AdmittedRequest:
    """Stage-6 admit marking (REQ-INTAKE-009): the ONLY object handed to the
    execution plane (M3 ``admit_envelope`` receives it downstream). Its
    existence == the request passed stages 1-5; rejected input raises before
    this is ever constructed (NFR-INTAKE-003 — nothing to propagate)."""

    request: VerificationRequest
    oracles: tuple[str, ...]  # bound plugin names (stage-5 proof, REQ-INTAKE-007)
    warnings: tuple[str, ...]  # e.g. apiVersion deprecation (stage 2)
    source_path: str | None
    admitted: bool = field(default=True, init=False)


def load_request(
    source: str | Path | io.TextIOBase,
    *,
    source_path: str | None = None,
    plugin_dir: str | None = None,
) -> AdmittedRequest:
    """Run one YAML request document through the 6-stage gate.

    Args:
        source: path to a YAML file, or an open text stream.
        source_path: consumer-repo-relative path recorded into errors/annotations
            (defaults to the file path when ``source`` is one; M8 owns the
            host->checkout path translation, D-L).
        plugin_dir: explicit stage-5 custom-oracle anchor directory (p4c3).
            When given it is used for scenario-adjacent ``module:Class``
            resolution even for STREAM sources (envelope.py / M3 api.py pass
            the scenario file's parent dir); when omitted, a file source keeps
            the existing parent-dir auto-anchor unchanged.

    Returns:
        ``AdmittedRequest`` — admitted, executable, with bound oracle names.

    Raises:
        ContractError: on any stage-1..5 violation (reject; exit-2-eligible).
    """
    text, source_path = _read(source, source_path)

    # (1) safe parse -------------------------------------------------------- #
    try:
        doc = yaml.safe_load(text)
    except yaml.YAMLError as exc:
        raise _parse_error(exc, source_path) from exc
    if not isinstance(doc, dict):
        raise ContractError(
            expected="a YAML mapping (scenario / sut / interface / acceptance_criteria)",
            got=repr(doc),
            example="sut:\n  image_ref: carter-sut:p2",
            doc_link=_DOC_LINK,
            source_path=source_path,
        )
    locator = _Locator(text)

    # (2) apiVersion resolve (version.py — 3-state) -------------------------- #
    warnings: list[str] = []
    resolution = resolve_api_version(doc.get("apiVersion"), source_path=source_path)
    if resolution.state == "reject":
        assert resolution.error is not None
        raise _relocated(resolution.error, line_col=locator(("apiVersion",)))
    if resolution.state == "warn" and resolution.warning:
        warnings.append(resolution.warning)

    # (3) pydantic model_validate -> friendly errors ------------------------- #
    try:
        request = VerificationRequest.model_validate(doc)
    except ValidationError as exc:
        raise _errors.from_validation_error(
            exc,
            model=VerificationRequest,
            source_path=source_path,
            locator=locator,
        )[0] from exc

    # (4) self-containedness (REQ-INTAKE-006) — explicit gate re-assertion --- #
    _check_self_contained(request, source_path)

    # (5) oracle load + bind (REQ-INTAKE-007/008) ---------------------------- #
    # D-1(a) submission plane (decision 2026-07-11 §D-1 wiring item 1):
    # scenario-adjacent custom oracle modules ("module:Class" next to the YAML)
    # resolve while binding — the anchor directory joins sys.path for stage 5
    # ONLY, try/finally-restored. An explicit ``plugin_dir`` wins (stream
    # submissions carry their anchor, p4c3); otherwise a file source anchors
    # its parent dir, and anchor-less streams stay anchor-less.
    if plugin_dir is not None:
        plugin_dir = str(Path(plugin_dir).resolve())
    elif isinstance(source, (str, Path)):
        plugin_dir = str(Path(source).parent.resolve())
    bound: list[str] = []
    if plugin_dir is not None:
        sys.path.insert(0, plugin_dir)
    try:
        for i, criterion in enumerate(request.acceptance_criteria):
            try:
                oracle = load_oracle(criterion.oracle)
            except ContractError as err:
                raise _relocated(
                    err,
                    field_path=f"acceptance_criteria[{i}].oracle",
                    source_path=source_path,
                    line_col=locator(("acceptance_criteria", i, "oracle")),
                ) from err
            bound.append(oracle.name)
    finally:
        if plugin_dir is not None and plugin_dir in sys.path:
            sys.path.remove(plugin_dir)

    # (6) admit marking (REQ-INTAKE-009) ------------------------------------- #
    return AdmittedRequest(
        request=request,
        oracles=tuple(bound),
        warnings=tuple(warnings),
        source_path=source_path,
    )


# --------------------------------------------------------------------------- #
# internals
# --------------------------------------------------------------------------- #
def _read(source: str | Path | io.TextIOBase, source_path: str | None) -> tuple[str, str | None]:
    if isinstance(source, (str, Path)):
        path = Path(source)
        try:
            text = path.read_text(encoding="utf-8")
        except OSError as exc:
            raise ContractError(
                expected="a readable YAML request file",
                got=str(path),
                example="cv-infra run scenarios/warehouse_goal.yaml",
                doc_link=_DOC_LINK,
                source_path=source_path or str(path),
            ) from exc
        return text, source_path or str(path)
    return source.read(), source_path


def _parse_error(exc: yaml.YAMLError, source_path: str | None) -> ContractError:
    mark = getattr(exc, "problem_mark", None)
    problem = getattr(exc, "problem", None) or str(exc).splitlines()[0]
    return ContractError(
        expected="well-formed YAML",
        got=str(problem),
        example="scenario:\n  scene: nova_carter_warehouse",
        doc_link=_DOC_LINK,
        source_path=source_path,
        source_line=mark.line + 1 if mark is not None else None,
        source_col=mark.column + 1 if mark is not None else None,
    )


def _check_self_contained(request: VerificationRequest, source_path: str | None) -> None:
    """REQ-INTAKE-006 triad, re-asserted independently of schema evolution:
    every request carries SUT image ref + scenario + >=1 acceptance criterion.
    (The schema's required fields make each branch unreachable today — this
    keeps the acceptance gate explicit if the schema ever loosens.)"""
    triad = {
        "sut.image_ref": request.sut.image_ref,
        "scenario.scene": request.scenario.scene,
        "acceptance_criteria": request.acceptance_criteria,
    }
    for path, value in triad.items():
        if not value:
            raise ContractError(
                field_path=path,
                expected="a self-contained request (SUT image ref + scenario + criteria)",
                example="acceptance_criteria:\n  - oracle: reached_goal",
                doc_link=_DOC_LINK,
                source_path=source_path,
            )


def _relocated(
    err: ContractError,
    *,
    field_path: str | None = None,
    source_path: str | None = None,
    line_col: tuple[int, int] | None = None,
) -> ContractError:
    """Copy a ``ContractError`` with source context filled in (the message is
    baked at construction, so enrichment builds a fresh object)."""
    return ContractError(
        field_path=field_path if field_path is not None else err.field_path,
        expected=err.expected,
        got=err.got,
        example=err.example,
        doc_link=err.doc_link,
        source_path=source_path if source_path is not None else err.source_path,
        source_line=line_col[0] if line_col else err.source_line,
        source_col=line_col[1] if line_col else err.source_col,
    )


class _Locator:
    """Map a pydantic ``loc`` path to the nearest YAML (line, col), 1-based.

    Walks the SafeLoader compose tree (marks preserved by PyYAML — no extra
    dependency). Best-effort: unknown segments (e.g. union discriminator tags)
    stop the walk and the nearest enclosing node's mark is returned; an
    unparseable document yields no locations. Feeds the M8 annotation
    ``source_line``/``source_col`` (D-L 1:1)."""

    def __init__(self, text: str) -> None:
        try:
            self._root = yaml.compose(text, Loader=yaml.SafeLoader)
        except yaml.YAMLError:  # pragma: no cover - stage 1 already rejected
            self._root = None

    def __call__(self, loc: tuple[Any, ...]) -> tuple[int, int] | None:
        node = self._root
        if node is None:
            return None
        for part in loc:
            child = _child(node, part)
            if child is None:  # tag segment / missing key -> nearest enclosing node
                break
            node = child
        mark = node.start_mark
        return (mark.line + 1, mark.column + 1)


def _child(node: yaml.nodes.Node, part: Any) -> yaml.nodes.Node | None:
    if isinstance(part, int) and isinstance(node, yaml.nodes.SequenceNode):
        return node.value[part] if 0 <= part < len(node.value) else None
    if isinstance(node, yaml.nodes.MappingNode):
        for key_node, value_node in node.value:
            if key_node.value == part:
                return value_node
    return None
