"""YAML loader / validator / admit gate (M1 §3.3 — REQ-INTAKE-004/006/009).

The 6-stage pipeline IS the acceptance gate (NFR-INTAKE-003 — order matters,
any failing stage rejects BEFORE the execution plane ever sees the input):

    (1) safe parse (SafeLoader)      -> parse error   = friendly reject
    (2) apiVersion resolve           -> unknown/absent = reject / deprecated = warn
    (3) pydantic model_validate      -> schema error  = friendly reject
    (4) self-containedness           -> missing triad = reject   (REQ-INTAKE-006)
        + no platform stamp          -> submitted derivation = reject
    (5) ride-along artifacts         -> load failure  = reject   (REQ-INTAKE-007/008;
        - oracle load + bind            scenario dir on sys.path while binding — D-1)
        - sut.locomotion_policy      -> absent / escaping the scenario dir / digest
                                        mismatch = reject (D2 2026-08-31)
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

import hashlib
import io
import json
import sys
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml
from pydantic import ValidationError

from cv_infra.contract import errors as _errors
from cv_infra.contract import pict as _pict
from cv_infra.contract.errors import ContractError
from cv_infra.contract.profile import EmbodimentProfile
from cv_infra.contract.schema import EXAMPLE_IMAGE_REF, VerificationRequest
from cv_infra.contract.version import resolve_api_version
from cv_infra.oracles.base import load_oracle  # sanctioned edge (.importlinter ignore)

_DOC_LINK = "M1-contract-and-schema.md §3.3 (loader pipeline)"

#: Fixable example for every ``sut.locomotion_policy`` rejection — a copyable
#: block, not a value: the digest is the one thing only the consumer's own file
#: can answer, so the example names the COMMAND that prints it.
_POLICY_EXAMPLE = (
    "sut:\n  locomotion_policy:\n"
    "    file: policy.pt          # beside this scenario file\n"
    "    sha256: <sha256sum policy.pt>"
)


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
    #: Absolute path of the validated ``sut.locomotion_policy`` file — resolved
    #: ONCE here, at the only place that knows the anchor directory, so no
    #: consumer re-derives it (blueprint §8). ``None`` = the request declared no
    #: policy (every carter request).
    locomotion_policy_path: str | None = None
    #: Absolute paths of the validated ``sut.artifacts`` entries, keyed by the
    #: declared ``file`` — the v2 generalisation of ``locomotion_policy_path``.
    #: The platform records identity and infers nothing about what any entry is
    #: FOR; ``embodiment.robot.onboard.artifact`` says that, on the consumer side.
    artifact_paths: Mapping[str, str] = field(default_factory=dict)
    #: The loaded embodiment profile (``None`` = a v1 registry-form request).
    embodiment: EmbodimentProfile | None = None
    #: sha256 over the embodiment document with the SUT-owned entries removed —
    #: the identity axis for "same world and robot" (see ``embodiment_digest``).
    embodiment_digest: str | None = None
    #: Absolute paths of the ride-along input-space files (``space.model`` and the
    #: optional committed ``space.array``).
    space_model_path: str | None = None
    space_array_path: str | None = None


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
    doc = _safe_parse(text, source_path)
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
    _reject_platform_stamp(request, source_path)

    # (5) ride-along artifacts: oracles (REQ-INTAKE-007/008) + SUT policy (D2) #
    anchor = _plugin_anchor(source, plugin_dir)
    bound = _bind_oracles(request, anchor=anchor, source_path=source_path, locator=locator)
    policy_path = _check_locomotion_policy(
        request, anchor=anchor, source_path=source_path, locator=locator
    )
    artifact_paths = _check_artifacts(
        request, anchor=anchor, source_path=source_path, locator=locator
    )
    embodiment, embodiment_digest = _load_embodiment(
        request, anchor=anchor, source_path=source_path, locator=locator
    )
    space_model, space_array = _check_space(
        request, anchor=anchor, source_path=source_path, locator=locator
    )

    # (6) admit marking (REQ-INTAKE-009) ------------------------------------- #
    return AdmittedRequest(
        request=request,
        oracles=tuple(bound),
        warnings=tuple(warnings),
        source_path=source_path,
        locomotion_policy_path=policy_path,
        artifact_paths=artifact_paths,
        embodiment=embodiment,
        embodiment_digest=embodiment_digest,
        space_model_path=space_model,
        space_array_path=space_array,
    )


# --------------------------------------------------------------------------- #
# internals — one helper per stage that owns more than a call (stages 1 and 5),
# so ``load_request`` above reads as the 6-stage gate its docstring describes.
# --------------------------------------------------------------------------- #
def _safe_parse(text: str, source_path: str | None) -> dict:
    """Stage 1: SafeLoader parse -> the request MAPPING (anything else rejects)."""
    try:
        doc = yaml.safe_load(text)
    except yaml.YAMLError as exc:
        raise _parse_error(exc, source_path) from exc
    if not isinstance(doc, dict):
        raise ContractError(
            expected="a YAML mapping (scenario / sut / interface / acceptance_criteria)",
            got=repr(doc),
            example=f"sut:\n  image_ref: {EXAMPLE_IMAGE_REF}",
            doc_link=_DOC_LINK,
            source_path=source_path,
        )
    return doc


def _plugin_anchor(source: str | Path | io.TextIOBase, plugin_dir: str | None) -> str | None:
    """The stage-5 custom-oracle anchor directory, resolved — or ``None``.

    An explicit ``plugin_dir`` wins (stream submissions carry their anchor,
    p4c3); otherwise a file source anchors its parent dir, and anchor-less
    streams stay anchor-less.
    """
    if plugin_dir is not None:
        return str(Path(plugin_dir).resolve())
    if isinstance(source, (str, Path)):
        return str(Path(source).parent.resolve())
    return None


def _bind_oracles(
    request: VerificationRequest,
    *,
    anchor: str | None,
    source_path: str | None,
    locator: _Locator,
) -> list[str]:
    """Stage 5: load + bind every criterion's oracle, returning the bound names.

    D-1(a) submission plane (decision 2026-07-11 §D-1 wiring item 1):
    scenario-adjacent custom oracle modules ("module:Class" next to the YAML)
    resolve while binding — the ``anchor`` directory joins sys.path for stage 5
    ONLY, try/finally-restored.
    """
    bound: list[str] = []
    if anchor is not None:
        sys.path.insert(0, anchor)
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
        if anchor is not None and anchor in sys.path:
            sys.path.remove(anchor)
    return bound


def _check_locomotion_policy(
    request: VerificationRequest,
    *,
    anchor: str | None,
    source_path: str | None,
    locator: _Locator,
) -> str | None:
    """Stage 5, second ride-along: validate ``sut.locomotion_policy``, return its
    ABSOLUTE path (``None`` when the request declares none — the carter path,
    which then does nothing at all).

    Mirrors the custom-oracle ride-along above, deliberately: the artifact
    travels NEXT TO the scenario document, so it resolves against the SAME
    ``anchor`` directory and may not leave it. That directory is what the
    supervisor mounts read-only into the runner (``_runner_volumes``, D-1), so a
    path outside it is not merely untrusted — it does not exist over there.

    Three ways to be rejected, all friendly + exit-2-eligible (D2 2026-08-31 —
    the platform holds no policy, so it never fills one in): the path escapes
    the scenario directory, the file is not there, or its bytes hash to
    something other than the declared ``sha256``.
    """
    policy = request.sut.locomotion_policy
    if policy is None:
        return None
    field_file = "sut.locomotion_policy.file"
    if anchor is None:
        raise _policy_reject(
            field_file,
            "a submission that carries its scenario directory (the ride-along anchor) — a "
            "declared locomotion policy is resolved NEXT TO the scenario file, so an "
            "anchor-less submission has nowhere to look for it",
            repr(policy.file),
            source_path=source_path,
            locator=locator,
        )
    root = Path(anchor)  # already absolute+resolved (_plugin_anchor)
    resolved = (root / policy.file).resolve()
    if not resolved.is_relative_to(root):
        raise _policy_reject(
            field_file,
            f"a path INSIDE the scenario directory ({root}) — the policy file rides along "
            "with the request and only that directory reaches the runner, so '..' segments "
            "and absolute paths cannot be read",
            f"{policy.file!r} (resolved: {resolved})",
            source_path=source_path,
            locator=locator,
        )
    try:
        digest = hashlib.sha256(resolved.read_bytes()).hexdigest()
    except OSError as exc:
        raise _policy_reject(
            field_file,
            "an existing, readable file — the platform never supplies the policy (it is a "
            "SUT artifact: put it next to the scenario file and commit it, or publish it "
            "into the SUT image)",
            f"{policy.file!r} (resolved: {resolved})",
            source_path=source_path,
            locator=locator,
        ) from exc
    if digest != policy.sha256:
        raise _policy_reject(
            "sut.locomotion_policy.sha256",
            f"the sha256 of the declared file ({policy.file} hashes to {digest})",
            repr(policy.sha256),
            source_path=source_path,
            locator=locator,
        )
    return str(resolved)


def _ride_along(
    rel: str,
    *,
    field_path: str,
    anchor: str | None,
    what: str,
    source_path: str | None,
    locator: _Locator,
) -> Path:
    """Resolve ONE ride-along path against the request's directory, or reject.

    Every v2 ride-along (SUT artifact, embodiment profile, input-space model)
    obeys the same two rules the custom-oracle anchor already obeyed, for the
    same reason: the request's directory is what the supervisor mounts read-only
    into the runner, so a path outside it is not merely untrusted — over there it
    does not exist. Sharing one resolver is deliberate (G-25: three copies of an
    escape check drift, and the one that drifts is the one that stops checking).

    ``_check_locomotion_policy`` keeps its own copy on purpose: its rejection
    prose is pinned by tests and the whole field is deprecated by
    ``sut.artifacts``, so it is left untouched to be deleted with v1 rather than
    refactored twice.
    """
    if anchor is None:
        raise _ride_along_reject(
            field_path,
            f"a submission that carries its own directory (the ride-along anchor) — {what} "
            "is resolved NEXT TO the request document, so an anchor-less submission has "
            "nowhere to look for it",
            repr(rel),
            source_path=source_path,
            locator=locator,
        )
    root = Path(anchor)
    resolved = (root / rel).resolve()
    if not resolved.is_relative_to(root):
        raise _ride_along_reject(
            field_path,
            f"a path INSIDE the request directory ({root}) — {what} rides along with the "
            "request and only that directory reaches the runner, so '..' segments and "
            "absolute paths cannot be read",
            f"{rel!r} (resolved: {resolved})",
            source_path=source_path,
            locator=locator,
        )
    if not resolved.is_file():
        raise _ride_along_reject(
            field_path,
            f"an existing, readable file — {what} is yours to supply and the platform "
            "never fills one in",
            f"{rel!r} (resolved: {resolved})",
            source_path=source_path,
            locator=locator,
        )
    return resolved


def _ride_along_reject(
    field_path: str,
    expected: str,
    got: str,
    *,
    source_path: str | None,
    locator: _Locator,
) -> ContractError:
    """One friendly ride-along rejection, located at the offending YAML key."""
    return _relocated(
        ContractError(field_path=field_path, expected=expected, got=got, doc_link=_DOC_LINK),
        source_path=source_path,
        line_col=locator(tuple(field_path.split("."))),
    )


def _check_artifacts(
    request: VerificationRequest,
    *,
    anchor: str | None,
    source_path: str | None,
    locator: _Locator,
) -> dict[str, str]:
    """Stage 5: resolve + digest-verify every ``sut.artifacts`` entry.

    The digest is the whole point of the block. The platform cannot know what a
    file does, so the only property it can guarantee is that the bytes the
    verdict was computed against are the bytes the request named — which is also
    what makes the SUT axis of a regression comparison mean something.
    """
    resolved: dict[str, str] = {}
    for index, artifact in enumerate(request.sut.artifacts):
        base = f"sut.artifacts.{index}"
        path = _ride_along(
            artifact.file,
            field_path=f"{base}.file",
            anchor=anchor,
            what="a SUT artifact",
            source_path=source_path,
            locator=locator,
        )
        digest = hashlib.sha256(path.read_bytes()).hexdigest()
        if digest != artifact.sha256:
            raise _ride_along_reject(
                f"{base}.sha256",
                f"the sha256 of the declared file ({artifact.file} hashes to {digest})",
                repr(artifact.sha256),
                source_path=source_path,
                locator=locator,
            )
        resolved[artifact.file] = str(path)
    return resolved


def _load_embodiment(
    request: VerificationRequest,
    *,
    anchor: str | None,
    source_path: str | None,
    locator: _Locator,
) -> tuple[EmbodimentProfile | None, str | None]:
    """Stage 5: load + validate the embodiment document, and digest it for identity.

    The digest EXCLUDES the SUT-owned entries (``robot.onboard.artifact``) and
    nothing else. That split is the contract's answer to "is the profile part of
    the request's identity or part of its SUT": moving the camera is a DIFFERENT
    TEST and must invalidate the baseline, while swapping the policy file is the
    SAME test against a different SUT and must not. Both halves live in one
    document because they describe one robot; only the identity projection
    separates them.
    """
    if request.embodiment is None:
        return None, None
    path = _ride_along(
        request.embodiment,
        field_path="embodiment",
        anchor=anchor,
        what="the embodiment profile",
        source_path=source_path,
        locator=locator,
    )
    raw = _safe_parse(path.read_text(encoding="utf-8"), str(path))
    try:
        profile = EmbodimentProfile.model_validate(raw)
    except ValidationError as exc:
        raise _errors.from_validation_error(
            exc,
            model=EmbodimentProfile,
            source_path=str(path),
            locator=_Locator(path.read_text(encoding="utf-8")),
        )[0] from exc
    return profile, embodiment_digest(profile)


def embodiment_digest(profile: EmbodimentProfile) -> str:
    """sha256 of the profile's TEST-CONDITION content (SUT-owned entries removed).

    Canonical JSON (sorted keys, no whitespace) so the digest depends on the
    values and not on how the consumer laid out the YAML — reformatting a
    document must not invalidate its baselines.
    """
    projection = profile.model_dump(mode="json")
    onboard = projection.get("robot", {}).get("onboard")
    if isinstance(onboard, dict):
        onboard.pop("artifact", None)
    payload = json.dumps(projection, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _check_space(
    request: VerificationRequest,
    *,
    anchor: str | None,
    source_path: str | None,
    locator: _Locator,
) -> tuple[str | None, str | None]:
    """Stage 5: resolve the input-space model and the committed array beside it.

    The model's SYNTAX is validated here too, at admit, because an unparseable
    space must cost zero GPU seconds (NFR-INTAKE-003) — the alternative is
    discovering it after the scheduler has already paid for a runner.
    """
    if request.space is None:
        return None, None
    model = _ride_along(
        request.space.model,
        field_path="space.model",
        anchor=anchor,
        what="the input-space model",
        source_path=source_path,
        locator=locator,
    )
    _pict.validate_model(model.read_text(encoding="utf-8"), source_path=str(model))
    array = (
        _ride_along(
            request.space.array,
            field_path="space.array",
            anchor=anchor,
            what="the committed covering array",
            source_path=source_path,
            locator=locator,
        )
        if request.space.array is not None
        else None
    )
    return str(model), (str(array) if array is not None else None)


def _policy_reject(
    field_path: str,
    expected: str,
    got: str,
    *,
    source_path: str | None,
    locator: _Locator,
) -> ContractError:
    """One friendly policy rejection — the 8 annotation keys filled exactly the
    way stage 5's oracle rejection fills them (``_relocated`` attaches the YAML
    line/col of the offending key)."""
    return _relocated(
        ContractError(
            field_path=field_path,
            expected=expected,
            got=got,
            example=_POLICY_EXAMPLE,
            doc_link=_DOC_LINK,
        ),
        source_path=source_path,
        line_col=locator(tuple(field_path.split("."))),
    )


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
    keeps the acceptance gate explicit if the schema ever loosens.)

    "Scenario" is the SELF-CONTAINEDNESS that matters here, not any one spelling
    of it: a v1 document names a scene the platform ships, a v2 document ships
    the assets itself. Both answer "what world does this run in" without the
    platform reaching outside the request, which is what the requirement is
    about. The schema's own validator is what forbids saying neither or both.
    """
    triad = {
        "sut.image_ref": request.sut.image_ref,
        "scenario.scene": request.scenario.scene or request.embodiment,
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


def _reject_platform_stamp(request: VerificationRequest, source_path: str | None) -> None:
    """A submitted ``scenario.derivation`` is a lie — reject it (p6 §0-2).

    The block records WHICH derivation rule produced a sample and WHICH sample
    it is (``derive.materialize_request`` writes it). A consumer cannot know
    either: whatever they typed would ride the execution plane and the stored
    result as provenance the platform never gave (G-79 — the owner of a state
    is the only honest producer of the string that describes it). Rejecting at
    admit keeps it out of the execution plane entirely (NFR-INTAKE-003), which
    is why this sits with the stage-4 gate and not in the schema: the SHAPE is
    legal (the platform stamps it), the SUBMISSION is not.
    """
    if request.scenario.derivation is not None:
        raise ContractError(
            field_path="scenario.derivation",
            expected=(
                "no 'derivation' block in a submitted document — the platform stamps "
                "sample provenance when it materializes a sample"
            ),
            got=repr(request.scenario.derivation.model_dump()),
            example=(
                "delete the 'derivation:' block; to randomize a value declare it inline, "
                "e.g. goal:\n    x: {uniform: [-6.5, -5.5]}"
            ),
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
        except yaml.YAMLError:  # stage 1 already rejected such text — degrade, never raise
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
