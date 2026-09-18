"""The friendly rejection object — one violation, told so it can be fixed.

Every exit-2 rejection in this package is one of these. It carries the four things a
developer needs to repair the request without reading our source (field path, what was
expected, what arrived, a fixable example), plus an optional source location so CI can
put the same sentence on the offending line of the offending file
(``cli/publish_glue.py`` renders it as a ``::error file,line,col::`` annotation).

The eight field names are a contract with that renderer, not an implementation detail:
``ANNOTATION_KEYS`` is the single list both sides iterate, so the error object and the
annotation can never drift into two shapes.

A raw traceback is never the rejection surface: a stack trace names OUR file, and the
thing that needs changing is the caller's flag or model.

Stdlib only, and imported by every other contract module — this is the bottom of the
package (``.importlinter``).
"""

from __future__ import annotations

from typing import Any

#: Rendered into ``got`` when the value is simply absent — the default, because a
#: missing value is the most common rejection and has nothing to quote back.
_MISSING = "(missing)"

#: The machine-readable view, in order. Shared with ``cli.publish_glue`` (which
#: rehydrates an error from this dict), so both sides read one definition.
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
    """One rejected input. ``str()`` is the friendly one-liner; ``to_annotation_dict()``
    is the same violation in the shape CI annotates with."""

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
        """The violation as EXACTLY the eight ``ANNOTATION_KEYS`` — what ``errors.json``
        holds and what the annotation renderer reads back."""
        return {key: getattr(self, key) for key in ANNOTATION_KEYS}
