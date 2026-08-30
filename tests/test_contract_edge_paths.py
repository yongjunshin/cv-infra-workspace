"""M1 contract edge/defensive paths (p8c2 T4 — branch coverage, contract/*).

The paths here are the ones the happy-path suites never walk, yet each one is a
contract promise in its own right:

* ``contract/__init__.py`` PEP 562 lazy exports — the mechanism that keeps
  ``import cv_infra.contract`` stdlib-only (the runner wheel installs
  ``--no-deps``, D-C/R20). Both arms: an advertised name resolves to the
  submodule's definition, an unknown name is a plain ``AttributeError``
  (never a speculative import).
* ``envelope.py`` envelope-FILE level rejects (unreadable / malformed /
  non-mapping) and the DEPRECATED apiVersion warn — the envelope half of the
  two-file attribution and of the 3-state version policy (NFR-INTAKE-002).
* ``loader.py`` stage-4 triad gate (REQ-INTAKE-006) re-asserted independently of
  the schema, plus the locator's degrade-don't-raise walks.
* ``errors.py`` example lookup for a ``loc`` that names no field — the friendly
  error must lose its example, not crash (NFR-INTAKE-001).

Everything runs in-process; the stdlib-only invariant itself is asserted by the
subprocess guard in ``test_contract_schema_p3.py`` (untouched here).
"""

from __future__ import annotations

import importlib
from pathlib import Path

import pytest
from pydantic import TypeAdapter, ValidationError

import cv_infra.contract as contract_pkg
import cv_infra.contract.version as version_mod
from cv_infra.contract import _LAZY
from cv_infra.contract.envelope import load_envelope
from cv_infra.contract.errors import ContractError, from_validation_error
from cv_infra.contract.loader import _check_self_contained, _Locator, load_request
from cv_infra.contract.schema import SutRef
from cv_infra.contract.version import DeprecatedVersion

FIXTURE = Path(__file__).parent / "fixtures" / "nova_carter_warehouse_goal.yaml"


# --------------------------------------------------------------------------- #
# contract/__init__.py — PEP 562 lazy export table (both arms)
# --------------------------------------------------------------------------- #
def test_every_lazy_export_resolves_to_its_submodule_definition():
    # The package advertises third-party-backed symbols WITHOUT importing them
    # at package-import time; each must still be the very object the submodule
    # defines (one definition, no re-export copy — blueprint §8).
    for name, module_path in _LAZY.items():
        assert getattr(contract_pkg, name) is getattr(importlib.import_module(module_path), name)
    # ``__all__`` = the two stdlib-safe eager names + exactly the lazy table.
    assert set(contract_pkg.__all__) == {"API_VERSION", "ContractError"} | set(_LAZY)


def test_unknown_attribute_raises_attributeerror_without_importing_anything():
    # The fallback arm: a name outside the table is a normal missing attribute.
    # (Importing on any name would let a typo drag a third-party module into the
    # runner's stdlib-only import surface.)
    with pytest.raises(AttributeError, match=r"cv_infra\.contract.*no attribute.*no_such_symbol"):
        contract_pkg.no_such_symbol


# --------------------------------------------------------------------------- #
# envelope.py — envelope-FILE level rejects + deprecation warn
# --------------------------------------------------------------------------- #
def _envelope_tree(tmp_path: Path, envelope_text: str) -> Path:
    """batch.yaml (envelope) + scenarios/warehouse.yaml (the canonical fixture)."""
    (tmp_path / "scenarios").mkdir(exist_ok=True)
    (tmp_path / "scenarios" / "warehouse.yaml").write_text(
        FIXTURE.read_text(encoding="utf-8"), encoding="utf-8"
    )
    envelope = tmp_path / "batch.yaml"
    envelope.write_text(envelope_text, encoding="utf-8")
    return envelope


def test_unreadable_envelope_file_rejects_friendly(tmp_path):
    missing = tmp_path / "batch.yaml"
    with pytest.raises(ContractError) as exc_info:
        load_envelope(missing)
    err = exc_info.value
    assert err.source_path == str(missing)  # the ENVELOPE file is the one named
    assert str(missing) in err.got
    assert "Traceback" not in str(err)  # NFR-INTAKE-001 — never a raw traceback


def test_malformed_envelope_yaml_rejects_with_the_envelope_files_line(tmp_path):
    envelope = _envelope_tree(tmp_path, "apiVersion: cv-infra/v1\nrequests: [unclosed\n  - nope\n")
    with pytest.raises(ContractError) as exc_info:
        load_envelope(envelope)
    err = exc_info.value
    assert err.source_path == str(envelope)  # two-file attribution: envelope, not scenario
    assert err.source_line is not None and err.source_col is not None
    assert "Traceback" not in str(err)


def test_non_mapping_envelope_document_rejects(tmp_path):
    # A bare list of scenario paths is the plausible mistake — the envelope is a
    # MAPPING (apiVersion + requests), so it rejects before any scenario is read.
    envelope = _envelope_tree(tmp_path, "- scenarios/warehouse.yaml\n")
    with pytest.raises(ContractError) as exc_info:
        load_envelope(envelope)
    err = exc_info.value
    assert err.source_path == str(envelope)
    assert "mapping" in err.expected and "requests" in err.example


def test_deprecated_envelope_api_version_warns_but_still_admits(tmp_path, monkeypatch):
    # NFR-INTAKE-002 warn state at the ENVELOPE level: accept + WARNING, never a
    # reject. ``DEPRECATED`` is honestly empty for cv-infra/v1, so the window is
    # injected (same idiom as the loader's stage-2 test).
    monkeypatch.setattr(
        version_mod,
        "DEPRECATED",
        {"cv-infra/v0": DeprecatedVersion(sunset="2 releases", migration_link="changelog")},
    )
    envelope = _envelope_tree(
        tmp_path, "apiVersion: cv-infra/v0\nrequests:\n  - scenario: scenarios/warehouse.yaml\n"
    )
    with pytest.warns(UserWarning, match="DEPRECATED"):
        loaded = load_envelope(envelope)
    assert loaded.api_version == "cv-infra/v0"  # resolved value, execution continues
    assert len(loaded.requests) == 1
    assert loaded.requests[0].admitted.admitted is True


# --------------------------------------------------------------------------- #
# loader.py — stage-4 triad gate (REQ-INTAKE-006) + locator degradation
# --------------------------------------------------------------------------- #
#: "if the schema ever loosens" (loader.py ``_check_self_contained`` docstring):
#: ``model_copy`` skips validation, so it produces exactly the request a looser
#: schema would admit. One entry per triad member -> its expected field path.
_TRIAD_LOOSENERS = {
    "acceptance_criteria": lambda r: r.model_copy(update={"acceptance_criteria": []}),
    "scenario.scene": lambda r: r.model_copy(
        update={"scenario": r.scenario.model_copy(update={"scene": ""})}
    ),
    "sut.image_ref": lambda r: r.model_copy(
        update={"sut": r.sut.model_copy(update={"image_ref": ""})}
    ),
}


@pytest.mark.parametrize("field_path", sorted(_TRIAD_LOOSENERS))
def test_stage4_triad_gate_rejects_each_missing_member(field_path):
    request = load_request(FIXTURE).request
    assert _check_self_contained(request, "s.yaml") is None  # control: the triad is intact

    with pytest.raises(ContractError) as exc_info:
        _check_self_contained(_TRIAD_LOOSENERS[field_path](request), "s.yaml")
    err = exc_info.value
    assert err.field_path == field_path  # the gate names WHICH member is missing
    assert err.source_path == "s.yaml"
    assert "Traceback" not in str(err)


def test_locator_yields_no_location_for_an_unparseable_document():
    # Stage 1 rejects malformed YAML before any locator lookup, but the locator
    # is built from the SAME text: it must degrade to "no location" (the M8
    # annotation simply carries no line/col) instead of raising.
    locator = _Locator("scenario: [unclosed\n  nope")
    assert locator(("scenario", "scene")) is None


def test_locator_falls_back_to_the_nearest_enclosing_node_past_a_scalar():
    # A pydantic ``loc`` can run DEEPER than the YAML tree (union/discriminator
    # tag segments — _Locator's docstring). Walking past a scalar returns that
    # scalar's own 1-based mark, so the annotation still points at the offending
    # value rather than at the document root.
    locator = _Locator("scenario:\n  scene: nova_carter_warehouse\n")
    scene_mark = locator(("scenario", "scene"))
    assert scene_mark == (2, 10)  # 1-based: the value starts after "  scene: "
    assert locator(("scenario", "scene", "static")) == scene_mark


# --------------------------------------------------------------------------- #
# errors.py — a loc that names no field loses its example, never crashes
# --------------------------------------------------------------------------- #
def test_example_lookup_degrades_when_the_loc_starts_at_an_index():
    # A list-rooted validation puts the INDEX first, so no field annotation has
    # been resolved when the index step runs. Without the guard the renderer
    # itself would raise (``None.annotation``) and leak a traceback out of the
    # code whose whole job is to prevent one (NFR-INTAKE-001).
    with pytest.raises(ValidationError) as exc_info:
        TypeAdapter(list[SutRef]).validate_python([{"image_ref": 5}])
    (indexed,) = from_validation_error(exc_info.value, model=SutRef)
    assert indexed.field_path == "[0].image_ref"
    assert indexed.example == ""  # no field resolved -> nothing to exemplify
    assert indexed.expected and indexed.got == "5"  # friendly otherwise

    # Contrast: the same model DOES exemplify the same field when the loc names
    # it — the empty example above is the guard, not a missing examples table.
    with pytest.raises(ValidationError) as direct_info:
        SutRef.model_validate({"image_ref": 5})
    (named,) = from_validation_error(direct_info.value, model=SutRef)
    assert named.field_path == "image_ref" and named.example
