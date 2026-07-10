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
