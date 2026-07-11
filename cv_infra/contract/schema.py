"""Contract models (M1 §3.2) — Phase 3 pydantic v2 formalization.

Single definition of the verification contract (blueprint §8 — consumers
import, never redefine): ``RequestEnvelope`` / ``VerificationRequest`` /
``Result`` / ``ExecutionSettings`` / ``ResourceBudget`` + their sub-models.
Every model rejects unknown keys loudly (``extra="forbid"`` at EVERY nesting
level) — nothing is silently dropped (the G-25 ``goal_tolerance_m`` lesson).

Wire grounding (karpathy — only fields with a real basis exist):

* Request side = the canonical consumer scenario document
  (tests/fixtures/nova_carter_warehouse_goal.yaml @ cv-infra-user f1c9607):
  ``scenario`` / ``sut`` / ``interface`` / ``acceptance_criteria`` top level,
  plus the M1 §3.2 additions ``apiVersion`` (optional, resolver = version.py)
  and ``execution_settings`` (optional; ``repeats`` is consumed by M3 fan-out).
* Result side = the exact result.json the Phase-2 runner emits
  (``cv_infra.runner.evaluate.build_result_dict``). ``Result`` must pass that
  dict through UNMODIFIED — the wire is pinned against explicit literals by
  tests/test_result_emission_golden.py and bound to this model by the
  emission-binding tests in tests/test_contract_schema_p3.py (guard +
  positive control, G-25/G-17).

This pydantic canon is the ONLY definition since D-4' (2026-07-10): the
Phase-2 stdlib dataclasses (contract/models.py) are retired, all consumers
validate through here. This module is imported lazily from
``cv_infra.contract`` so the package import stays stdlib-only; the runner
executes it on the BUNDLE-SUPPLIED pydantic (D-4').
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Annotated, Any, Literal

from pydantic import (
    BaseModel,
    ConfigDict,
    Discriminator,
    Field,
    Tag,
    model_validator,
)

from cv_infra.contract.adapter_schema import Interface
from cv_infra.contract.apiversion import API_VERSION

# Verdict domain (REQ-EXEC-013). Kept as a Literal + tuple so the wire value is
# a plain string (result.json field ``verdict: str``).
#
# verdict -> CLI exit-code mapping (COMMENT ONLY; contract LOCK = cycle-6 P2-07,
# rendered in cv_infra/cli/main.py):
#   pass    -> 0
#   fail    -> 1
#   timeout -> 1   (SUT missed the sim-time budget = SUT verdict, not infra)
#   error   -> 3   (runner crash / Isaac unreached / EULA not agreed = platform, FU-8)
# (bad input, pre-sim -> exit 2 is raised on the CLI side, not carried in a Result.)
Verdict = Literal["pass", "fail", "timeout", "error"]
VERDICTS: tuple[str, ...] = ("pass", "fail", "timeout", "error")


class _ForbidExtra(BaseModel):
    """Shared config: unknown keys are a loud contract violation, never dropped."""

    model_config = ConfigDict(extra="forbid", populate_by_name=True)


# --------------------------------------------------------------------------- #
# Request side (REQ-INTAKE-001/002/006)
# --------------------------------------------------------------------------- #
class Goal(_ForbidExtra):
    """Navigation goal pose (REQ-EXEC-004). Coordinates are expressed in ``frame``."""

    x: float = Field(examples=[-6.0])
    y: float = Field(examples=[5.0])
    yaw: float = Field(examples=[1.5708])
    frame: str = "map"


class DebugObstacle(_ForbidExtra):
    """FAIL-injection cuboid dropped into the stage pre-reset (D-2' 2026-07-10).

    An obstacle is WORLD STATE, not a judging criterion — hence a ``Scenario``
    field (supersedes the P2 free-form criteria-params ride-along). Keys are
    1:1 with the runner's ``SimRuntime.spawn_debug_obstacle`` read set
    (cv_infra/runner/sim_runtime.py — bound mechanically in the schema tests).
    ``None`` on a dimension means "runner default applies" — the default
    VALUES stay runner-owned (M2), the shape is M1's (ReachedGoalParams
    pattern).
    """

    x: float = Field(examples=[-6.0])
    y: float = Field(examples=[2.0])
    height: float | None = Field(default=None, gt=0, examples=[0.15])
    width: float | None = Field(default=None, gt=0, examples=[1.2])
    depth: float | None = Field(default=None, gt=0, examples=[0.4])


class Scenario(_ForbidExtra):
    """Self-contained scene + goal + determinism inputs (REQ-INTAKE-006).

    ``timeout_s`` is a SIM-time (/clock) budget, NOT wall-clock — the
    wall-clock runaway watchdog is M3's (M1 §3.2, D-F). ``seed`` backs
    determinism (LOCKED §7-6).
    """

    scene: str = Field(min_length=1, examples=["nova_carter_warehouse"])
    robot: str = Field(min_length=1, examples=["nova_carter"])
    # Block-valued examples (here and in VerificationRequest below) exist so a
    # WHOLE-BLOCK-MISSING violation still gets a fixable example (DoD-P3-02
    # footnote, p3c3) — dicts render as valid YAML flow mappings.
    goal: Goal = Field(examples=[{"x": -6.0, "y": 5.0, "yaw": 1.5708}])
    seed: int = Field(examples=[42])
    timeout_s: float = Field(gt=0, examples=[120])
    debug_obstacle: DebugObstacle | None = None


class SutRef(_ForbidExtra):
    """SUT image reference (REQ-INTAKE-006 required element #1).

    ``image_id`` optionally pins the EXACT image (image-as-artifact, FU-10):
    local tags carry no RepoDigest, so the docker Image Id is the pin. Optional
    — when given it must be a full ``sha256:`` id (loud, friendly reject
    otherwise; example = the measured carter-sut:p2 Image Id).
    """

    image_ref: str = Field(min_length=1, examples=["carter-sut:p2"])
    image_id: str | None = Field(
        default=None,
        pattern=r"^sha256:[0-9a-f]{64}$",
        examples=["sha256:47aff5c993dac05b1664482e44af9401073336f142cb6d4919d81b47f8f9d48a"],
    )


class ExecutionSettings(_ForbidExtra):
    """Execution knobs (M1 §3.2). All optional — the canonical scenario omits it.

    ``repeats`` is the 2-axis fan-out input (M3 ``fanout.py`` — single
    definition, blueprint §8). ``fixed_dt`` expresses the determinism dt lock
    (LOCKED §7-6; the Phase-2 runner steps at 1/60 — enforcement is M2's).
    ``seed`` / mission ``timeout_s`` live in ``Scenario`` (canonical fixture),
    NOT here — one home per field.
    """

    repeats: int = Field(default=1, ge=1, examples=[3])
    fixed_dt: float | None = Field(default=None, gt=0, examples=[0.016667])


# --- acceptance criteria ("criteria are also input", REQ-INTAKE-007) -------- #
class ReachedGoalParams(_ForbidExtra):
    """Known-key params for the ``reached_goal`` oracle.

    Keys are the oracle's OWN read set (``read_field`` call sites in
    cv_infra/oracles/reached_goal.py; fixture-real: ``position_tolerance_m`` /
    ``yaw_tolerance_rad``). ``None`` means "oracle default applies" — the
    default VALUES stay oracle-owned (M2), the shape is M1's.
    """

    position_tolerance_m: float | None = Field(default=None, gt=0, examples=[0.75])
    yaw_tolerance_rad: float | None = Field(default=None, gt=0, examples=[0.26])
    goal_orientation_wxyz: list[float] | None = Field(
        default=None, min_length=4, max_length=4, examples=[[1.0, 0.0, 0.0, 0.0]]
    )

    @model_validator(mode="before")
    @classmethod
    def _reject_legacy_goal_tolerance(cls, data: Any) -> Any:
        """The cycle-3 draft key was silently ignored by the oracle (G-25 root
        cause) — reject it LOUDLY with the migration instead of re-swallowing."""
        if isinstance(data, Mapping) and "goal_tolerance_m" in data:
            raise ValueError(
                "legacy key 'goal_tolerance_m' is not read by the reached_goal "
                "oracle (it was silently ignored pre-P3) — use "
                "'position_tolerance_m' (example: position_tolerance_m: 0.75)"
            )
        return data


class NoCollisionParams(_ForbidExtra):
    """Known-key params for the ``no_collision`` oracle (keys = its read set).

    ``chassis_path`` is REQUIRED at contract time — absent, the runner's
    ``telemetry.bind()`` raises mid-mission (P2-13 precondition); rejecting
    here keeps bad input out of the execution plane (NFR-INTAKE-003, D-E/R7).
    """

    chassis_path: str = Field(min_length=1, examples=["/World/Nova_Carter_ROS/chassis_link"])
    collision_excluded_paths: list[str] = Field(
        default_factory=list, examples=[["/World/Nova_Carter_ROS"]]
    )


class ReachedGoalCriterion(_ForbidExtra):
    oracle: Literal["reached_goal"]
    params: ReachedGoalParams = Field(default_factory=ReachedGoalParams)


class NoCollisionCriterion(_ForbidExtra):
    oracle: Literal["no_collision"]
    params: NoCollisionParams


class CustomCriterion(_ForbidExtra):
    """Any non-MVP oracle: the plugin named here is loaded/bound at loader
    stage 5 (REQ-INTAKE-007/008) and validates its OWN params — the contract
    cannot know a plugin's key set, so ``params`` stays a free mapping."""

    oracle: str = Field(min_length=1, examples=["my_pkg.checks:MyOracle"])
    params: dict[str, Any] = Field(default_factory=dict)


def _criterion_tag(value: Any) -> str:
    """Discriminate on ``oracle``: MVP names get their known-key schema, every
    other name routes to ``CustomCriterion`` (plugin-validated)."""
    oracle = value.get("oracle") if isinstance(value, Mapping) else getattr(value, "oracle", None)
    return oracle if oracle in ("reached_goal", "no_collision") else "custom"


AcceptanceCriterion = Annotated[
    (
        Annotated[ReachedGoalCriterion, Tag("reached_goal")]
        | Annotated[NoCollisionCriterion, Tag("no_collision")]
        | Annotated[CustomCriterion, Tag("custom")]
    ),
    Discriminator(_criterion_tag),
]


class VerificationRequest(_ForbidExtra):
    """Self-contained verification instance (REQ-INTAKE-002/006) — the wire
    shape of one consumer scenario document.

    Required triad (REQ-INTAKE-006): ``sut`` (image ref) + ``scenario`` +
    ``acceptance_criteria`` (>=1). ``apiVersion`` is optional here as a FIELD;
    its semantics (accept/warn/reject) are version.py's — the schema does not
    duplicate the version table (single definition).
    """

    api_version: str = Field(default=API_VERSION, alias="apiVersion", examples=[API_VERSION])
    scenario: Scenario = Field(
        examples=[
            {
                "scene": "nova_carter_warehouse",
                "robot": "nova_carter",
                "goal": {"x": -6.0, "y": 5.0, "yaw": 1.5708},
                "seed": 42,
                "timeout_s": 120,
            }
        ]
    )
    sut: SutRef = Field(examples=[{"image_ref": "carter-sut:p2"}])
    interface: Interface = Field(default_factory=Interface)
    acceptance_criteria: list[AcceptanceCriterion] = Field(
        min_length=1, examples=[[{"oracle": "reached_goal"}]]
    )
    execution_settings: ExecutionSettings = Field(default_factory=ExecutionSettings)


class RequestEnvelope(_ForbidExtra):
    """N>=1 ``VerificationRequest`` container (REQ-INTAKE-001; single submission
    = size-1 envelope). ``trigger_source`` records human vs CI provenance
    (REQ-INTAKE-003 — required, never silently defaulted); ``is_self_test`` /
    ``origin`` mark self-test envelopes (M7 consumes)."""

    api_version: str = Field(default=API_VERSION, alias="apiVersion", examples=[API_VERSION])
    trigger_source: Literal["human-manual", "ci-cd"] = Field(examples=["ci-cd"])
    is_self_test: bool = False
    origin: str | None = None
    requests: list[VerificationRequest] = Field(min_length=1)


# --------------------------------------------------------------------------- #
# Result side (REQ-EXEC-012/013/014) — wire-equal to the Phase-2 emission
# --------------------------------------------------------------------------- #
class Metrics(_ForbidExtra):
    """Declared-metrics container (REQ-EXEC-012). Values are computed by the M2
    oracle engine; M1 owns only the shape. Defaults are the Phase-2 wire
    defaults (pinned by the golden literals)."""

    time_to_goal_s: float | None = None
    min_clearance_m: float | None = None
    collision_count: int = 0
    path_len_m: float | None = None


class CriterionResult(_ForbidExtra):
    """Per-criterion outcome (one per AcceptanceCriterion)."""

    oracle: str
    passed: bool
    detail: str | None = None


class Artifacts(_ForbidExtra):
    """Result-attached artifact references (REQ-EXEC-014) — attachment
    semantics formalized by D-3 (2026-07-11, schema UNEXTENDED):

    * ``mcap`` — path to the rosbag2 MCAP telemetry recording of the run
      (REQ-EXEC-008/014): the machine-readable replay/debug attachment,
      playable from the referenced path (``ros2 bag info``).
    * ``mp4`` — path to the visual recording of the mission (REQ-EXEC-009/014):
      MVP = exactly ONE camera-view video per job (no per-sensor fan-out),
      playable from the referenced path (ffprobe/frame count).

    Paths are meaningful on the plane that persisted the result (runner
    out-dir / its host mount). ``None`` = that recorder produced nothing —
    honest degradation, loud at the source, never a fabricated path (P2-02).
    Additional fields (e.g. ``media_type``) are DEFERRED until a 2nd media
    type has real demand (D-3 — no speculative extension)."""

    mcap: str | None = None
    mp4: str | None = None


class Result(_ForbidExtra):
    """Exactly one result per job (REQ-EXEC-013). The wire dict (key set,
    nesting, defaults) is IDENTICAL to the Phase-2 runner emission — bound by
    the emission-binding tests in tests/test_contract_schema_p3.py against the
    real producer (G-25: guard + positive control, not prose).

    ``request_identity_key`` is a FIELD only — derivation/normalization is
    M4's (LOCKED §7-13). ``origin`` / ``is_self_test`` are M7 markers.
    """

    job_id: str = Field(min_length=1)
    verdict: Verdict
    metrics: Metrics = Field(default_factory=Metrics)
    criteria_results: list[CriterionResult] = Field(default_factory=list)
    artifacts: Artifacts = Field(default_factory=Artifacts)
    request_identity_key: str | None = None
    origin: str | None = None
    is_self_test: bool = False


# --------------------------------------------------------------------------- #
# Resource budget (REQ-DEPLOY-012 — schema shared: M5 configures, M3 consumes)
# --------------------------------------------------------------------------- #
class ResourceBudget(_ForbidExtra):
    """Operator resource budget (M1 §3.2). Feeds the Phase-4 concurrency cap
    ``k = min(max_concurrent, floor(VRAM / vram_per_instance_gb), ...)`` — the
    VALUES are measured/operator-set (never hardcoded, CLAUDE §2-4); only the
    shape is fixed here. ``scheduling_policy`` vocabulary is M3's to extend
    (Phase-2 scheduler is FIFO wave-based)."""

    vram_per_instance_gb: float = Field(gt=0, examples=[8.0])
    max_concurrent: int = Field(ge=1, examples=[2])
    scheduling_policy: str = Field(default="fifo", min_length=1, examples=["fifo"])
