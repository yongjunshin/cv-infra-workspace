"""How ``request_identity_key`` is DISPLAYED (M4, p5c20 ⑦) — one definition, all surfaces.

The key itself is derived in ONE place (``report/regression.py::identity_key``);
this leaf owns the other single definition: how that 71-char string is shown to a
human. Both human surfaces import it — the CLI text table (``report/matrix.py``)
and the GitHub markdown surfaces (``report/github.py``) — so the two can never
drift into two different truncations (G-56: a display rule copied is a display
rule that diverges).

Deliberately **stdlib-only and dependency-free**: ``report/github.py`` rests the
M4-09 portability negative on importing nothing but the stdlib + the M8
``exit_codes`` leaf, and ``report/matrix.py`` pulls the M3 orchestrator models —
so the shared rule cannot live in ``matrix.py`` without dragging that graph into
the renderer. Hence this file.

Rule: keep ``sha256:`` + the first ``ABBREVIATED_HEX`` hex digits and mark the cut
with ``…``. Only a SUFFIX is dropped, so what a human sees stays a literal PREFIX
of the stored key (``request_baselines.request_identity_key`` — the C-1 baseline
PK): it can be pasted straight into a prefix lookup and can never be mistaken for
a different key. Absence is rendered with the CALLER's existing null idiom (the
text table's ``-``, the markdown's ``n/a``) — never a fabricated key (§2-4).
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
