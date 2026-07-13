"""User-facing RequestEnvelope contract (M1 — D-2 p4c3, REQ-INTAKE-001/004/005).

The envelope YAML is the batch submission unit (M8 ``cv-infra submit``): an
``apiVersion`` plus ``requests:`` = a list of scenario **file path references**
with an optional per-request ``repeats`` override — nothing else (D-2 option a:
inline request documents are NOT part of this contract; the REST JSON wire
keeps them as an internal representation, D-1 p4c2). Baseline shape (D-2):

    apiVersion: cv-infra/v1
    requests:
      - scenario: scenarios/nova_carter_warehouse_goal.yaml  # relative to THIS file
        repeats: 3        # optional — overrides execution_settings.repeats
      - scenario: scenarios/goal_b.yaml

Each referenced scenario file is admitted through the EXISTING 6-stage
``loader.load_request`` gate (zero re-implementation) with the scenario's
parent directory as the stage-5 custom-oracle anchor — scenario-adjacent
``module:Class`` oracles resolve naturally. Error attribution is two-file:
envelope-level violations carry the ENVELOPE file's ``source_path`` +
line/col; a referenced scenario's violations propagate the loader's error
untouched (the SCENARIO file's ``source_path`` + line/col) — the failing file
is always distinguishable by ``source_path``.

``LoadedRequestRef`` / ``LoadedEnvelope`` / ``load_envelope`` below are the
cross-team VERBATIM contract (T2 CLI / T3 consume in parallel — field names
and the signature are frozen, G-17). ``raw_doc`` is the wire-submission
canonical form: the scenario YAML parsed verbatim, with an envelope
``repeats`` override already applied (M3's JSON wire takes it as-is).

Host/control-plane module (imports pydantic + yaml freely, like loader.py);
an envelope-level DEPRECATED apiVersion warns via the stdlib ``warnings``
channel — the frozen ``LoadedEnvelope`` shape carries no warnings field, and
per-scenario deprecation warnings ride ``admitted.warnings`` as before.
"""

from __future__ import annotations

import io
import warnings
from dataclasses import dataclass, replace
from pathlib import Path

import yaml
from pydantic import Field, ValidationError

from cv_infra.contract import errors as _errors
from cv_infra.contract.apiversion import API_VERSION
from cv_infra.contract.errors import ContractError

# Same-package private reuse (single definition of the parse-error/locator/
# enrichment idioms — loader.py owns them, this module builds ON the gate).
from cv_infra.contract.loader import (
    AdmittedRequest,
    _Locator,
    _parse_error,
    _relocated,
    load_request,
)
from cv_infra.contract.schema import _ForbidExtra
from cv_infra.contract.version import resolve_api_version

_DOC_LINK = "M1-contract-and-schema.md §3.3 (envelope loading — D-2 p4c3 file refs)"

_EXAMPLE = "requests:\n  - scenario: scenarios/nova_carter_warehouse_goal.yaml"


# --------------------------------------------------------------------------- #
# envelope document schema (D-2 baseline — nothing beyond it)
# --------------------------------------------------------------------------- #
class EnvelopeRequestRef(_ForbidExtra):
    """One ``requests:`` entry: a scenario file reference (+ optional repeats).

    ``scenario`` is a path — relative paths resolve against the envelope
    file's own directory. ``repeats`` (>=1) overrides the scenario's
    ``execution_settings.repeats`` for this submission only (the scenario
    file itself is never rewritten).
    """

    scenario: str = Field(min_length=1, examples=["scenarios/nova_carter_warehouse_goal.yaml"])
    repeats: int | None = Field(default=None, ge=1, examples=[3])


class EnvelopeDocument(_ForbidExtra):
    """The envelope YAML shape (D-2 baseline): apiVersion + >=1 file reference.

    ``apiVersion`` semantics (accept/warn/reject, strict absent-reject) are
    version.py's — resolved BEFORE this model validates, same as the request
    loader's stage 2 (single definition of the version table).
    """

    api_version: str = Field(default=API_VERSION, alias="apiVersion", examples=[API_VERSION])
    requests: list[EnvelopeRequestRef] = Field(
        min_length=1, examples=[[{"scenario": "scenarios/nova_carter_warehouse_goal.yaml"}]]
    )


# --------------------------------------------------------------------------- #
# loaded (admitted) view — cross-team VERBATIM contract (T2/T3, G-17)
# --------------------------------------------------------------------------- #
@dataclass(frozen=True)
class LoadedRequestRef:
    """One admitted scenario reference (envelope entry -> 6-stage evidence)."""

    admitted: AdmittedRequest  # 6-stage pass evidence (existing type, reused)
    raw_doc: dict  # scenario YAML parsed verbatim + repeats override applied (wire canonical)
    scenario_path: str  # resolved absolute path
    oracle_plugin_dir: str  # = the scenario file's parent dir (absolute, always exists)


@dataclass(frozen=True)
class LoadedEnvelope:
    """The fully admitted envelope: every reference passed the 6-stage gate."""

    api_version: str
    requests: tuple[LoadedRequestRef, ...]  # envelope-file order


def load_envelope(source: str | Path) -> LoadedEnvelope:
    """Load + admit a RequestEnvelope YAML file (D-2 — file path references).

    Pipeline (mirrors the request loader's staging): read -> safe parse ->
    apiVersion 3-state resolve -> envelope schema validate -> per reference:
    resolve the scenario path (relative to the envelope file), admit it
    through the 6-stage ``load_request`` gate (scenario parent dir = stage-5
    oracle anchor), apply the ``repeats`` override.

    Raises:
        ContractError: on any envelope- or scenario-level violation
            (friendly, exit-2-eligible; the failing FILE is identified by
            ``source_path`` — envelope vs scenario).
    """
    env_path = Path(source)
    env_source_path = str(env_path)
    try:
        text = env_path.read_text(encoding="utf-8")
    except OSError as exc:
        raise ContractError(
            expected="a readable RequestEnvelope YAML file",
            got=str(env_path),
            example="cv-infra submit envelopes/batch.yaml",
            doc_link=_DOC_LINK,
            source_path=env_source_path,
        ) from exc

    # (1) safe parse — envelope-file line/col on malformed YAML ------------- #
    try:
        doc = yaml.safe_load(text)
    except yaml.YAMLError as exc:
        raise _parse_error(exc, env_source_path) from exc
    if not isinstance(doc, dict):
        raise ContractError(
            expected="a YAML mapping (apiVersion + requests: scenario file references)",
            got=repr(doc),
            example=_EXAMPLE,
            doc_link=_DOC_LINK,
            source_path=env_source_path,
        )
    locator = _Locator(text)

    # (2) apiVersion resolve — the SAME 3-state policy as a request document  #
    resolution = resolve_api_version(doc.get("apiVersion"), source_path=env_source_path)
    if resolution.state == "reject":
        assert resolution.error is not None
        raise _relocated(resolution.error, line_col=locator(("apiVersion",)))
    if resolution.state == "warn" and resolution.warning:
        warnings.warn(resolution.warning, stacklevel=2)

    # (3) envelope schema validate -> friendly errors (envelope line/col) --- #
    try:
        envelope = EnvelopeDocument.model_validate(doc)
    except ValidationError as exc:
        raise _errors.from_validation_error(
            exc, model=EnvelopeDocument, source_path=env_source_path, locator=locator
        )[0] from exc

    # (4) per reference: resolve -> admit (6-stage) -> repeats override ----- #
    refs = tuple(
        _load_ref(entry, i, env_path.parent, env_source_path, locator)
        for i, entry in enumerate(envelope.requests)
    )
    return LoadedEnvelope(api_version=resolution.api_version, requests=refs)


# --------------------------------------------------------------------------- #
# internals
# --------------------------------------------------------------------------- #
def _load_ref(
    entry: EnvelopeRequestRef,
    index: int,
    base_dir: Path,
    env_source_path: str,
    locator: _Locator,
) -> LoadedRequestRef:
    """Admit ONE envelope entry: the scenario file is read once and that
    snapshot feeds both the 6-stage gate (verbatim text -> the loader's
    scenario-file line/col stays exact) and ``raw_doc``."""
    scenario_path = (base_dir / entry.scenario).resolve()
    try:
        scenario_text = scenario_path.read_text(encoding="utf-8")
    except OSError as exc:
        loc = locator(("requests", index, "scenario"))
        raise ContractError(
            field_path=f"requests[{index}].scenario",
            expected=(
                "a path to an existing scenario YAML file "
                "(relative paths resolve against the envelope file's directory)"
            ),
            got=f"{entry.scenario!r} (resolved: {scenario_path})",
            example="scenario: scenarios/nova_carter_warehouse_goal.yaml",
            doc_link=_DOC_LINK,
            source_path=env_source_path,
            source_line=loc[0] if loc else None,
            source_col=loc[1] if loc else None,
        ) from exc

    # Scenario violations propagate the loader's ContractError UNTOUCHED —
    # scenario-file source_path + line/col (two-file attribution, D-2).
    admitted = load_request(
        io.StringIO(scenario_text),
        source_path=str(scenario_path),
        plugin_dir=str(scenario_path.parent),
    )

    raw_doc = yaml.safe_load(scenario_text)
    if entry.repeats is not None:
        # Override on BOTH views: raw_doc (the wire canonical M3 submits) and
        # the admitted model (what in-process consumers read). ``repeats`` was
        # already validated >=1 by the envelope schema above.
        raw_doc.setdefault("execution_settings", {})["repeats"] = entry.repeats
        settings = admitted.request.execution_settings.model_copy(update={"repeats": entry.repeats})
        admitted = replace(
            admitted, request=admitted.request.model_copy(update={"execution_settings": settings})
        )

    return LoadedRequestRef(
        admitted=admitted,
        raw_doc=raw_doc,
        scenario_path=str(scenario_path),
        oracle_plugin_dir=str(scenario_path.parent),
    )
