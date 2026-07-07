"""MVP oracle: no_collision (M2 impl of M1 OracleBase — REQ-EXEC-011, D-E).

Passes iff the *chassis-only, ground/self-filtered* collision count is zero. This
is NOT global ``contact_events == 0``: the robot's wheels are always in contact
with the warehouse floor, so an unfiltered check false-FAILs every normal run.
The chassis prim and excluded actor-paths (ground/floor/self-bodies) are supplied
by adapter_config / measured at runtime, never scene-path hardcoded (R7). Filter
math lives in ``telemetry.count_real_collisions`` and is unit-tested on CPU.
"""

from __future__ import annotations

from cv_infra.oracles.base import OracleBase
from cv_infra.runner.evaluate import OracleOutcome, read_field
from cv_infra.runner.telemetry import TelemetryRecord, count_real_collisions


class NoCollisionOracle(OracleBase):
    name = "no_collision"
    version = "0.1.0"

    def validate_params(self, criteria: object) -> None:
        if read_field(criteria, "chassis_path") is None:
            raise ValueError("no_collision criteria require a chassis_path (D-E, R7)")

    def evaluate(self, telemetry: TelemetryRecord, criteria: object) -> OracleOutcome:
        chassis_path = read_field(criteria, "chassis_path")
        excluded_paths = read_field(criteria, "collision_excluded_paths", []) or []

        if chassis_path is None:
            return OracleOutcome(
                self.name,
                passed=False,
                reason="bad_criteria",
                detail="missing chassis_path (cannot filter D-E)",
            )

        count = count_real_collisions(telemetry.contact_events, chassis_path, list(excluded_paths))
        if count > 0:
            return OracleOutcome(
                self.name,
                passed=False,
                reason="collision",
                detail=f"{count} chassis collision(s) after ground/self filter",
            )
        return OracleOutcome(self.name, passed=True, detail="no chassis collisions")
