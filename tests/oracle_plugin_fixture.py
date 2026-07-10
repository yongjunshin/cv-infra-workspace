"""Test-only oracle plugin fixtures for the M1 explicit-path loader
(tests/test_oracle_plugin_loader.py / test_contract_loader_p3.py).

NOT a test module (no ``test_`` prefix): it is the import TARGET of the
``module:Class`` explicit-path form — a stand-in for a consumer-authored
custom oracle (REQ-INTAKE-007) plus the negative shapes the loader must
reject (REQ-INTAKE-008).
"""

from __future__ import annotations

from cv_infra.oracles.base import OracleBase


class CustomOracle(OracleBase):
    """A well-formed consumer custom oracle (loadable + bindable)."""

    name = "custom_fixture"
    version = "0.0.1"

    def validate_params(self, criteria: object) -> None:
        return None

    def evaluate(self, telemetry: object, criteria: object) -> object:
        return {"passed": True}


class NotAnOracle:
    """Importable, but not an OracleBase subclass — must be rejected."""


class AbstractOracle(OracleBase):
    """OracleBase subclass that never implemented the interface — must be
    rejected at instantiation (bind) time."""

    name = "abstract_fixture"
    version = "0.0.1"
