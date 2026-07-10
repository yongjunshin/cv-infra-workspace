"""M1 P3 apiVersion 3-state resolver tests (version.py — NFR-INTAKE-002).

States: supported/current -> accept; supported/deprecated -> accept + WARNING
(sunset + migration link); unknown OR ABSENT (D-1' strict, 2026-07-10) ->
reject carrying an exit-2-ELIGIBLE friendly error object (``sys.exit`` is the
consumer's, never called here). ``cv-infra/v1`` has no deprecated
predecessors, so the warn path is exercised by INJECTING a deprecation table
— no fake version constant in the module.
"""

from __future__ import annotations

import dataclasses

import pytest

from cv_infra.contract.apiversion import API_VERSION
from cv_infra.contract.errors import ContractError
from cv_infra.contract.version import (
    DEPRECATED,
    SUPPORTED,
    DeprecatedVersion,
    resolve_api_version,
)

INJECTED_DEPRECATED = {
    "cv-infra/v0": DeprecatedVersion(
        sunset="2 releases after cv-infra/v1", migration_link="see contract changelog"
    )
}


def test_current_version_accepts():
    res = resolve_api_version(API_VERSION)
    assert (res.state, res.warning, res.error) == ("accept", None, None)
    assert res.api_version == API_VERSION


def test_absent_version_rejects_with_add_the_key_guidance():
    # D-1' strict (supersedes the cycle-1 absent-accept assumption): absence
    # must reject LOUDLY with the exact line to add — never silently resolve
    # to current (NFR-INTAKE-002, versioned contract).
    res = resolve_api_version(None, source_path="s.yaml")
    assert res.state == "reject" and res.warning is None
    err = res.error
    assert isinstance(err, ContractError)
    assert err.field_path == "apiVersion"
    assert err.got == "(missing)"
    assert err.example == f"apiVersion: {API_VERSION}"  # the exact line to add
    assert "required" in err.expected  # add-the-key guidance, not a bare reject
    assert err.source_path == "s.yaml"
    assert "Traceback" not in str(err)


def test_deprecated_version_accepts_with_sunset_warning():
    res = resolve_api_version("cv-infra/v0", deprecated=INJECTED_DEPRECATED)
    assert res.state == "warn" and res.error is None
    assert "DEPRECATED" in res.warning
    assert "2 releases after cv-infra/v1" in res.warning  # sunset surfaced
    assert "contract changelog" in res.warning  # migration link surfaced
    assert API_VERSION in res.warning  # migration target


def test_unknown_version_rejects_with_friendly_error():
    res = resolve_api_version("cv-infra/v99", source_path="s.yaml")
    assert res.state == "reject" and res.warning is None
    err = res.error
    assert isinstance(err, ContractError)
    assert err.field_path == "apiVersion"
    assert API_VERSION in err.example  # fixable example names the current version
    assert err.source_path == "s.yaml"
    assert "Traceback" not in str(err)


def test_non_string_version_rejects():
    assert resolve_api_version(1).state == "reject"
    assert resolve_api_version(["cv-infra/v1"]).state == "reject"


def test_module_tables_are_honest():
    # v1 window: exactly the current version supported, nothing deprecated yet.
    assert SUPPORTED == frozenset({API_VERSION})
    assert DEPRECATED == {}


def test_resolution_is_a_value_not_an_effect():
    # frozen dataclass: consumers cannot mutate a reject into an accept.
    res = resolve_api_version("cv-infra/v99")
    with pytest.raises(dataclasses.FrozenInstanceError):
        res.state = "accept"
