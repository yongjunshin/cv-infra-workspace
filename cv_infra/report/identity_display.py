"""How a ``case_id`` is DISPLAYED — one definition, every surface.

The id itself is derived in one place (``contract/cases.py::case_id_for``); this leaf
owns the other single definition: how that 71-character string is shown to a human. It
lives in its own module so a second surface (a future text table, another renderer)
adopts the rule by importing it — a display rule that gets copied is a display rule
that diverges.

Rule: keep ``sha256:`` plus the first ``ABBREVIATED_HEX`` hex digits and mark the cut
with ``…``. Only a SUFFIX is dropped, so what a human sees stays a literal PREFIX of
the stored key (the baseline's primary key): it can be pasted into a prefix lookup and
can never be mistaken for a different case. Absence renders with the CALLER's own null
idiom — never a fabricated id.
"""

from __future__ import annotations

from typing import Any

#: Hex digits kept after the ``sha256:`` prefix. 12 hex = 48 bits: far beyond
#: collision range for one report (≤ tens of requests) and enough for a unique
#: prefix lookup, while keeping the cell at 20 chars.
ABBREVIATED_HEX = 12

#: Total characters kept from the key before the truncation mark.
CELL_CHARS = len("sha256:") + ABBREVIATED_HEX

#: Appended when (and only when) characters were actually dropped.
TRUNCATION_MARK = "…"


def identity_cell(key: Any, *, absent: str) -> str:
    """Render one ``request_identity_key`` table cell.

    ``key`` present -> abbreviated prefix + ``TRUNCATION_MARK`` (a key shorter
    than ``CELL_CHARS`` renders verbatim — nothing was dropped, so nothing is
    marked). ``key`` absent (``None`` / missing field) -> ``absent``, the
    caller's own null idiom.
    """
    if not key:
        return absent
    text = str(key)
    if len(text) <= CELL_CHARS:
        return text
    return text[:CELL_CHARS] + TRUNCATION_MARK


def was_abbreviated(cell: str) -> bool:
    """True when ``cell`` (an ``identity_cell`` result) actually lost characters —
    the condition for showing an abbreviation legend. A surface must not claim it
    abbreviated something when it did not."""
    return cell.endswith(TRUNCATION_MARK)
