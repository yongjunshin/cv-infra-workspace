"""Friendly contract-error object (M1 §3.4 — REQ-INTAKE-005/008, NFR-INTAKE-001).

Carries the structured, self-correcting validation error surfaced on schema
violations: field path + expected + got + fixable example, plus machine-readable
source location for M8 GitHub-Actions inline annotations (M1 object fields <->
M8 file/line/col, 1:1 — D-L). The field set below is the cross-team VERBATIM
contract (blueprint §8 "error object shape M1<->M8"):

    field_path / expected / got / example / doc_link / source_path /
    source_line / source_col

Principle: a raw pydantic/Python traceback is NEVER leaked to stderr —
violations are rendered as this object and rejected with exit 2 by the
consumer (DoD-P3-02; exit-code contract LOCKED §7-9; ``sys.exit`` is the
consumer's job, never raised here).

This module stays STDLIB-ONLY at import time: the pydantic
``ValidationError.errors()`` post-processing below duck-types the exception
(``exc.errors()``) instead of importing pydantic, so the runner image (wheel
installed ``--no-deps``, no pydantic) can keep importing ``cv_infra.contract``
submodules (D-C/R20).
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from typing import Any

#: Sentinel rendered into ``got`` when the violation is a missing field (the
#: pydantic ``input`` for a "missing" error is the parent mapping — dumping it
#: would drown the actual problem).
_MISSING = "(missing)"

#: Module-INTERNAL attribute name under which a YAML locator passed to
#: ``from_validation_error`` is remembered on the ``ValidationError`` instance
#: itself, so a later locator-less re-render of the SAME exception (the CLI's
#: multi-violation list traversal over ``err.__cause__``) still attaches
#: line/col to the 2nd+ violations. Private (`_` prefix) — public promotion is
#: a PM merge-gate call. Attribute stash measured OK on the bundle-matched
#: pydantic 2.11.7 (pydantic_core ValidationError accepts setattr; weakref
#: does NOT — probed 2026-07-11).
_LOCATOR_ATTR = "_cv_infra_yaml_locator"

#: The 8 machine-readable annotation keys (M1 §3.4 <-> M8 file/line/col, 1:1).
ANNOTATION_KEYS = (
    "field_path",
    "expected",
    "got",
    "example",
    "doc_link",
    "source_path",
    "source_line",
    "source_col",
)


class ContractError(Exception):
    """Structured, friendly contract/validation error (M1 §3.4 shape).

    One object = one violation. ``str()`` renders the friendly one-liner
    (field path + expected + got + example — NFR-INTAKE-001, self-correcting);
    ``to_annotation_dict()`` is the machine-readable view M8 renders as a
    GitHub inline annotation and M3 REST returns as the 422 payload.
    """

    def __init__(
        self,
        *,
        field_path: str = "",
        expected: str = "",
        got: str = _MISSING,
        example: str = "",
        doc_link: str = "",
        source_path: str | None = None,
        source_line: int | None = None,
        source_col: int | None = None,
    ) -> None:
        self.field_path = field_path
        self.expected = expected
        self.got = got
        self.example = example
        self.doc_link = doc_link
        self.source_path = source_path
        self.source_line = source_line
        self.source_col = source_col
        super().__init__(self._friendly())

    def _friendly(self) -> str:
        where = self.field_path or "(document)"
        parts = [f"{where}: expected {self.expected}, got {self.got}"]
        if self.example:
            parts.append(f"example: {self.example}")
        if self.doc_link:
            parts.append(f"see: {self.doc_link}")
        loc = self._location()
        if loc:
            parts.append(loc)
        return " | ".join(parts)

    def _location(self) -> str:
        if self.source_path is None and self.source_line is None:
            return ""
        path = self.source_path or "<input>"
        if self.source_line is None:
            return f"at {path}"
        col = f":{self.source_col}" if self.source_col is not None else ""
        return f"at {path}:{self.source_line}{col}"

    def to_annotation_dict(self) -> dict[str, Any]:
        """Machine-readable dict with EXACTLY the 8 verbatim keys (M8 1:1)."""
        return {key: getattr(self, key) for key in ANNOTATION_KEYS}


def render_loc(loc: Iterable[Any]) -> str:
    """pydantic ``loc`` tuple -> dotted/indexed field path (M1 §3.4).

    ``("requests", 0, "acceptance_criteria", "timeout_s")`` becomes
    ``requests[0].acceptance_criteria.timeout_s``. Union/discriminator tags in
    the loc are kept as path segments (they name the branch that rejected).
    """
    out = ""
    for part in loc:
        if isinstance(part, int):
            out += f"[{part}]"
        else:
            out += f".{part}" if out else str(part)
    return out


def _render_got(err: Mapping[str, Any]) -> str:
    if err.get("type") == "missing":
        return _MISSING
    return repr(err.get("input"))


def _unwrap_annotation(annotation: Any, index_step: bool) -> Any:
    """Best-effort unwrap of ``list[X]`` / ``X | None`` / ``Annotated[X, ...]``."""
    import typing  # stdlib; local so module import stays trivially cheap

    origin = typing.get_origin(annotation)
    args = typing.get_args(annotation)
    if index_step:
        # stepping through a sequence index -> the element annotation
        if origin in (list, tuple) and args:
            return args[0]
        return None
    if origin is None:
        return annotation
    # Optional[X] / unions: only unambiguous (X | None) unwraps
    non_none = [a for a in args if a is not type(None)]
    if len(non_none) == 1:
        return non_none[0]
    return annotation


def _example_for(model: Any, loc: Iterable[Any]) -> str:
    """Best-effort fixable example ("<field>: <value>", M1 §3.4 shape) for the
    field named by ``loc``, from the schema's ``Field(examples=[...])``.

    Walks ``model_fields`` down the loc path (skipping list indices via the
    element annotation). Returns "" when the path cannot be resolved (e.g.
    across an ambiguous union) — the error stays friendly without an example.
    """
    current = model
    field = None
    name = ""
    for part in loc:
        if isinstance(part, int):
            if field is None:
                return ""
            current = _unwrap_annotation(field.annotation, index_step=True)
            field = None
            continue
        fields = getattr(current, "model_fields", None)
        if fields is None and field is not None:
            current = _unwrap_annotation(field.annotation, index_step=False)
            fields = getattr(current, "model_fields", None)
        if not isinstance(fields, Mapping) or part not in fields:
            return ""
        field = fields[part]
        name = str(part)
        current = _unwrap_annotation(field.annotation, index_step=False)
    examples = getattr(field, "examples", None) if field is not None else None
    return f"{name}: {examples[0]}" if examples else ""


def from_validation_error(
    exc: Any,
    *,
    model: Any = None,
    source_path: str | None = None,
    locator: Any = None,
    doc_link: str = "",
) -> list[ContractError]:
    """pydantic v2 ``ValidationError`` -> list of friendly ``ContractError``.

    Duck-typed post-processing of ``exc.errors()`` (loc/msg/type/input — M1 §2
    reuse rule: pydantic is the validation engine, this only reshapes its
    output). ``model`` enables ``examples=[...]`` lookup; ``locator`` is an
    optional ``callable(loc) -> (line, col) | None`` (see ``loader.py`` — YAML
    node walk) filling the M8 annotation line/col when available.

    A passed ``locator`` is remembered on ``exc`` (``_LOCATOR_ATTR``,
    best-effort) so re-rendering the same exception WITHOUT one — the consumer
    idiom for surfacing violations beyond the loader's first — keeps line/col
    on every violation, not just the first.
    """
    if locator is None:
        locator = getattr(exc, _LOCATOR_ATTR, None)
    else:
        try:
            setattr(exc, _LOCATOR_ATTR, locator)
        except (AttributeError, TypeError):  # exotic exc types: stay best-effort
            pass
    out: list[ContractError] = []
    for err in exc.errors(include_url=False):
        loc = tuple(err.get("loc") or ())
        line_col = locator(loc) if locator is not None else None
        out.append(
            ContractError(
                field_path=render_loc(loc),
                expected=str(err.get("msg", "")),
                got=_render_got(err),
                example=_example_for(model, loc) if model is not None else "",
                doc_link=doc_link,
                source_path=source_path,
                source_line=line_col[0] if line_col else None,
                source_col=line_col[1] if line_col else None,
            )
        )
    return out
