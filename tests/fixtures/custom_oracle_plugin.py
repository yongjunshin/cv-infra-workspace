"""Consumer-style custom oracle plugin fixture (D-1 (a): scenario-adjacent .py).

Import TARGET of the runner-side plugin-dir tests
(tests/test_runner_oracle_plugins.py). This directory stands in for the
consumer's scenario dir — the .py lives NEXT TO the scenario YAML, is
ro-mounted by M3 at the same absolute path, and reaches sys.path through the
runner's ``CV_ORACLE_PLUGIN_DIR`` consumption — so the module imports as the
top-level ``custom_oracle_plugin`` (NOT ``tests.*``), exactly like a
consumer's ``my_checks.py``. It is our OWN plugin, not an external-shape
fixture (G-28 anchor rules do not apply); the tests drive it through the REAL
``cv_infra.oracles.base.load_oracle`` — the loader is never mocked.
"""

from __future__ import annotations

from cv_infra.oracles.base import OracleBase
from cv_infra.runner.evaluate import OracleOutcome, read_field


class ParamVerdictOracle(OracleBase):
    """Passes iff the merged criteria carry a truthy ``custom_should_pass``.

    Deterministic and param-driven so the tests steer the verdict through the
    criteria alone (proving a plugin-dir oracle's outcome reaches the fold),
    returning the same ``OracleOutcome`` shape the MVP oracles do.
    """

    name = "param_verdict"
    version = "0.0.1"

    def validate_params(self, criteria: object) -> None:
        return None

    def evaluate(self, telemetry: object, criteria: object) -> OracleOutcome:
        passed = bool(read_field(criteria, "custom_should_pass", False))
        return OracleOutcome(self.name, passed=passed, detail="plugin-dir fixture oracle")
