"""MVP oracle: no_collision (M2 impl of M1 OracleBase — REQ-EXEC-011, D-E).

Passes iff the *scoped, ground/self-filtered* collision count is zero. This is
NOT global ``contact_events == 0``: the robot's wheels are always in contact
with the warehouse floor, so an unfiltered check false-FAILs every normal run.
The chassis prim, the excluded actor-paths (ground/floor/self-bodies) and the
scope are supplied by the scenario / measured at runtime, never scene-path
hardcoded (R7). Filter math lives in ``telemetry.count_real_collisions`` and is
unit-tested on CPU.

``collision_scope`` has ONE home here (``resolve_collision_scope``): both this
oracle's verdict and the ``collision_count`` metric in ``runner.main`` /
``runner.batch`` take it from that function, so the number in ``result.json``
and the verdict beside it cannot be taken with two different meanings (G-25).
"""

from __future__ import annotations

from cv_infra.oracles.base import OracleBase
from cv_infra.runner.evaluate import OracleOutcome, read_field
from cv_infra.runner.telemetry import (
    COLLISION_SCOPES,
    SCOPE_CHASSIS,
    TelemetryRecord,
    count_real_collisions,
)

#: Scope applied when the scenario declares none — the pre-AR-12 meaning
#: (chassis prim only). The DEFAULT VALUE is oracle-owned (M2); the contract
#: (M1 ``NoCollisionParams``) only says the key may be written and spells the
#: absent case ``None``, which is what keeps every existing request's
#: ``request_identity_key`` where it is.
DEFAULT_COLLISION_SCOPE = SCOPE_CHASSIS


def resolve_collision_scope(criteria: object) -> str:
    """Decide WHAT counts as "the robot" for this run — the only place it is decided.

    Absent / null -> ``DEFAULT_COLLISION_SCOPE``. A declared value must be one of
    ``COLLISION_SCOPES``; anything else raises ``ValueError``, which on the
    runner path lands pre-boot through ``validate_params`` -> exit 2 (usage)
    instead of silently judging the mission with the default meaning. The
    contract's ``Literal`` already rejects a typo at admit time — this is the
    same rule stated where the value is USED, for criteria that arrive by any
    other path (hand-built dict, custom entrypoint).
    """
    scope = read_field(criteria, "collision_scope")
    if scope is None:
        return DEFAULT_COLLISION_SCOPE
    if scope not in COLLISION_SCOPES:
        raise ValueError(
            f"no_collision 'collision_scope' must be one of {list(COLLISION_SCOPES)} "
            f"(got {scope!r}) — example: collision_scope: robot"
        )
    return str(scope)


class NoCollisionOracle(OracleBase):
    name = "no_collision"
    version = "0.1.0"

    def validate_params(self, criteria: object) -> None:
        if read_field(criteria, "chassis_path") is None:
            raise ValueError("no_collision criteria require a chassis_path (D-E, R7)")
        # Pre-boot (exit 2), same stance as reached_goal's tolerance resolution:
        # an unusable scope must be refused before the GPU spends a mission.
        resolve_collision_scope(criteria)

    def evaluate(self, telemetry: TelemetryRecord, criteria: object) -> OracleOutcome:
        chassis_path = read_field(criteria, "chassis_path")
        excluded_paths = read_field(criteria, "collision_excluded_paths", []) or []
        scope = resolve_collision_scope(criteria)

        if chassis_path is None:
            return OracleOutcome(
                self.name,
                passed=False,
                reason="bad_criteria",
                detail="missing chassis_path (cannot filter D-E)",
            )

        count = count_real_collisions(
            telemetry.contact_events, chassis_path, list(excluded_paths), scope
        )
        # The scope IS the noun in the detail: under the default it reads exactly
        # as it always did ("...chassis collision(s)..."), and a scenario that
        # opted into the subtree gets a sentence that is true of what was judged.
        if count > 0:
            return OracleOutcome(
                self.name,
                passed=False,
                reason="collision",
                detail=f"{count} {scope} collision(s) after ground/self filter",
            )
        return OracleOutcome(self.name, passed=True, detail=f"no {scope} collisions")
