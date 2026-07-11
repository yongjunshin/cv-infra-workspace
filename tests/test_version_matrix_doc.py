"""``docs/version-compatibility.md`` <-> code-canon binding (M8-D5, R17 — G-25).

The matrix doc COPIES version values whose canon lives in code:

    axis 2 (CLI/package)   ``cv_infra/__init__.py`` ``__version__`` — the single
                           source ``pyproject.toml`` ``[tool.hatch.version]``
                           delegates to (both are asserted below)
    axis 3 (contract)      ``cv_infra/contract/apiversion.py`` ``API_VERSION``
                           + ``version.py`` ``SUPPORTED`` / ``DEPRECATED``
    axis 1 (Action tag)    git release tags — no in-repo code canon until the
                           Phase 5 surfaces exist, so the doc's "unpublished"
                           claim is guarded by the ABSENCE of the taggable
                           surfaces themselves (they land => this test forces
                           the doc row to move)

ONE test binds every copied value mechanically: edit either side alone and CI
fails — no hardcoded-version drift across the 3 independent axes (R17).
"""

from __future__ import annotations

import re
import tomllib
from pathlib import Path

import cv_infra.contract.version as version_mod
from cv_infra import __version__
from cv_infra.contract.apiversion import API_VERSION

_ROOT = Path(__file__).resolve().parents[1]
_DOC = _ROOT / "docs" / "version-compatibility.md"


def _rows(text: str) -> list[list[str]]:
    """Every markdown table line as a list of stripped cells."""
    return [
        [cell.strip() for cell in line.strip().strip("|").split("|")]
        for line in text.splitlines()
        if line.lstrip().startswith("|")
    ]


def _backticked(cell: str) -> set[str]:
    """The machine-checked value(s) of a cell = its backticked spans."""
    return set(re.findall(r"`([^`]+)`", cell))


def _the_row(rows: list[list[str]], predicate, what: str) -> list[str]:
    matches = [row for row in rows if predicate(row)]
    assert len(matches) == 1, f"expected exactly one {what} row in the doc, found {len(matches)}"
    return matches[0]


def test_version_matrix_doc_matches_code_canon():
    rows = _rows(_DOC.read_text(encoding="utf-8"))

    # -- axis 2: CLI/package cell == the single code canon --------------------
    axis2 = _the_row(rows, lambda r: len(r) == 3 and "CLI/패키지 버전" in r[0], "axis-2")
    assert _backticked(axis2[1]) == {
        __version__
    }, f"doc axis-2 says {axis2[1]!r} but cv_infra.__version__ is {__version__!r}"
    # ...and pyproject still delegates to that canon (doc cites the delegation).
    pyproject = tomllib.loads((_ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    assert "version" in pyproject["project"]["dynamic"]
    assert pyproject["tool"]["hatch"]["version"]["path"] == "cv_infra/__init__.py"

    # -- axis 3: contract apiVersion cell == API_VERSION ----------------------
    axis3 = _the_row(rows, lambda r: len(r) == 3 and "계약 `apiVersion`" in r[0], "axis-3")
    assert _backticked(axis3[1]) == {
        API_VERSION
    }, f"doc axis-3 says {axis3[1]!r} but contract API_VERSION is {API_VERSION!r}"

    # -- axis 1: the "unpublished" claim is guarded by surface absence --------
    axis1 = _the_row(rows, lambda r: len(r) == 3 and "Action 태그" in r[0], "axis-1")
    if "미발행" in axis1[1]:
        for surface in (".github/workflows/verify.yml", "actions/verify"):
            assert not (
                _ROOT / surface
            ).exists(), f"{surface} landed but the matrix still says 미발행 — update the doc"
    else:
        assert _backticked(axis1[1]), "a published Action-tag cell must name the tag in backticks"

    # -- compat-matrix row: bound to the resolver tables (SUPPORTED/DEPRECATED)
    compat = _the_row(
        rows, lambda r: len(r) == 5 and _backticked(r[1]) == {__version__}, "compat-matrix"
    )
    assert _backticked(compat[2]) == set(version_mod.SUPPORTED)
    assert _backticked(compat[3]) == set(version_mod.DEPRECATED)
    if not version_mod.DEPRECATED:
        assert "없음" in compat[3]  # honestly-empty table renders as an explicit "none"
    # the two Action-tag cells (axis table vs compat row) must agree.
    assert ("미발행" in compat[0]) == ("미발행" in axis1[1])
