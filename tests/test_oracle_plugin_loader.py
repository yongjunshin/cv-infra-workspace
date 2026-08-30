"""M1 P3 oracle plugin loader tests (oracles/base.py — REQ-INTAKE-007/008).

Both discovery forms: the ``cv_infra.oracles`` entry-point group (REAL
pyproject registrations — reached_goal / no_collision resolve through
importlib.metadata, not a hand-kept map) and the explicit ``module:Class``
path (consumer custom oracle). Every failure mode rejects with a friendly
``ContractError`` (exit-2-eligible; the loader never sys.exits).
"""

from __future__ import annotations

import pytest

from cv_infra.contract.errors import ContractError
from cv_infra.oracles.base import ENTRY_POINT_GROUP, OracleBase, load_oracle


def test_entry_point_group_name_is_the_contract():
    assert ENTRY_POINT_GROUP == "cv_infra.oracles"


@pytest.mark.parametrize("name", ["reached_goal", "no_collision"])
def test_mvp_oracles_load_via_real_entry_points(name):
    oracle = load_oracle(name)
    assert isinstance(oracle, OracleBase)
    assert oracle.name == name  # bound, evaluatable instance (REQ-INTAKE-007)


def test_custom_oracle_loads_via_explicit_path():
    oracle = load_oracle("tests.oracle_plugin_fixture:CustomOracle")
    assert isinstance(oracle, OracleBase)
    assert oracle.name == "custom_fixture"
    assert oracle.validate_params({}) is None  # contract-time hook callable


def test_unknown_entry_point_rejects_and_lists_registered():
    with pytest.raises(ContractError) as exc_info:
        load_oracle("does_not_exist")
    err = exc_info.value
    assert "'does_not_exist'" == err.got
    assert "reached_goal" in err.expected  # registered oracles surfaced for self-correction
    assert "Traceback" not in str(err)


def test_unimportable_module_path_rejects():
    with pytest.raises(ContractError):
        load_oracle("tests.does_not_exist_module:Nope")


def test_missing_attribute_rejects():
    with pytest.raises(ContractError):
        load_oracle("tests.oracle_plugin_fixture:Nope")


def test_non_oraclebase_class_rejects():
    with pytest.raises(ContractError) as exc_info:
        load_oracle("tests.oracle_plugin_fixture:NotAnOracle")
    assert "OracleBase" in exc_info.value.expected


def test_abstract_oracle_rejects_at_bind_time():
    with pytest.raises(ContractError):
        load_oracle("tests.oracle_plugin_fixture:AbstractOracle")


def test_an_entry_point_that_explodes_on_import_rejects_instead_of_crashing(monkeypatch):
    """A THIRD-PARTY oracle's own import error must land as the same friendly
    rejection as a typo'd name (REQ-INTAKE-008): the registration is discovered,
    so the "no such entry point" arm never runs, and without this guard the
    plugin's traceback would escape ``load_oracle`` and reach the operator as a
    platform crash instead of a contract error they can fix.

    The failure is injected at the ``importlib.metadata`` seam — a real broken
    registration would have to be installed into the environment.
    """
    import cv_infra.oracles.base as base_mod

    class _BrokenEntryPoint:
        name = "broken_oracle"

        def load(self):
            raise ImportError("no module named 'the_plugins_own_dependency'")

    class _Metadata:
        @staticmethod
        def entry_points(group=None, name=None):
            assert group == ENTRY_POINT_GROUP
            return [_BrokenEntryPoint()]

    monkeypatch.setattr(base_mod, "metadata", _Metadata)

    with pytest.raises(ContractError) as exc_info:
        load_oracle("broken_oracle")
    err = exc_info.value
    assert "entry point failed to load" in err.expected
    assert "the_plugins_own_dependency" in err.expected  # the cause is not swallowed
    assert "'broken_oracle'" == err.got
    assert "Traceback" not in str(err)
