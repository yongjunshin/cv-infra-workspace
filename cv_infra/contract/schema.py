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

CONTRACT CONVENTION — ``null`` means "unspecified", NEVER a distinct value:
do not add a field whose ``null`` carries its own meaning (a third state
separate from "absent"). The M4 identity normalization prunes null-valued keys
recursively (CEO decision D-5 @ p5c10, header block of
``cv_infra/report/regression.py``), so such a meaning would be erased from
``request_identity_key`` — two materially different requests would collide on
one key. Encode a third state as a distinct VALUE or a distinct field instead.
LIMIT (honest): this is a REVIEW-TIME rule with no mechanical enforcement. The
guard in tests/test_report_regression.py can only assert that absent == null,
which pruning makes true BY CONSTRUCTION; no test can see that a difference was
intended. Nothing here will catch a violation for you.

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


class InitialPose(_ForbidExtra):
    """Pose the robot is spawned at before the mission starts (REQ-EXEC-002).

    Planar 3-DoF on the SAME axes as ``Goal`` (x/y in metres, ``yaw`` in
    radians, scene world frame): the MVP SUT is a ground robot on a warehouse
    floor, so what a consumer can meaningfully choose is where on the plane it
    starts and which way it faces. Example values = the SUT's AMCL start pose
    as RECORDED IN the consumer scenario's own comment (cv-infra-user
    scenarios/nova_carter_warehouse_goal.yaml) — the contract does not couple
    the two: a spawn pose that disagrees with the SUT's localization init is
    the consumer's to reconcile (SUT config is black-box, REQ-EXEC-005).

    Deliberately ABSENT (no field without a basis AND a consumer):

    * ``z`` — floor contact determines it; a consumer-supplied height that
      disagrees with the scene either drops or embeds the robot. The runner's
      former never-consumed ``SimConfig.initial_pose_xyz`` (replaced by
      ``SimConfig.initial_pose`` in p5c11 T4) was the dead Phase-2 placeholder
      this requirement was open on, not evidence of demand — the runner now
      READS the asset's own z and keeps it.
      Adding ``z: float | None = None`` later stays baseline-safe (D-5).
    * ``frame`` — unlike ``Goal`` (which is PUBLISHED to the SUT's nav stack,
      where the frame is part of the message), this pose is applied by the
      runner to a stage prim. A frame field the runner cannot honour would be
      the ``goal_tolerance_m`` silent-ignore pattern (G-25).

    All three components are REQUIRED inside the block: "spawn here, facing
    this way" then has exactly one meaning, and required->optional stays
    backwards compatible if partial overrides ever earn their demand.
    """

    x: float = Field(examples=[-6.0])
    y: float = Field(examples=[-1.0])
    yaw: float = Field(examples=[3.1416])


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
    initial_pose: InitialPose | None = Field(
        default=None,
        description=(
            "Robot spawn pose (REQ-EXEC-002). Omitted (or null — same thing, see "
            "the module convention) = the scene asset's own placement stands, "
            "which is the behaviour every pre-p5c11 scenario got."
        ),
        examples=[{"x": -6.0, "y": -1.0, "yaw": 3.1416}],
    )


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
class GoalToleranceBudget(_ForbidExtra):
    """SUT-declared error budget the goal tolerance is DERIVED from (CEO D-6,
    decision 2026-08-05 §D-6 — determinism repair (B), "structural verdict").

    Why it exists: the pass/fail threshold was a constant a consumer typed into
    the scenario (``position_tolerance_m: 0.75``), so the verdict rode the
    margin between that constant and the observed GT residual — a margin
    measured SMALLER than the residual's own spread (p5c10/p5c11: spread
    0.341–0.736 m vs margin +0.193). A gate whose bool depends on that margin
    reports "not blown up yet", not determinism (G-55). The repair is to take
    the threshold from what the SUT itself claims plus the localization error
    budget the consumer declares, instead of raising the constant (raising it
    is explicitly NOT (B) — it only pushes the tail out).

    Since SUT configuration is BLACK-BOX (REQ-EXEC-005), the platform cannot
    read the planner's ``xy_goal_tolerance`` out of the SUT image; the scenario
    contract is the only declaration path. Hence this block.

    Derivation (documented intent, VERBATIM from the p5c13 cross-team pin)::

        derived_tolerance_m = sut_xy_goal_tolerance_m + localization_budget_m

    M1 owns the SHAPE only — applying the formula (and its default when the
    block is absent) is the reached_goal oracle's, i.e. M2's (same split as
    ``ReachedGoalParams``: values oracle-owned, shape contract-owned).

    Both keys are REQUIRED once the block is declared: half a budget is not a
    budget — a lone ``sut_xy_goal_tolerance_m`` would silently mean "zero
    localization error", which is the assumption the observed residuals refute.
    """

    sut_xy_goal_tolerance_m: float = Field(gt=0, examples=[0.25])
    localization_budget_m: float = Field(gt=0, examples=[0.30])


class ReachedGoalParams(_ForbidExtra):
    """Known-key params for the ``reached_goal`` oracle.

    Keys are the oracle's OWN read set (``read_field`` call sites in
    cv_infra/oracles/reached_goal.py; fixture-real: ``position_tolerance_m`` /
    ``yaw_tolerance_rad``). ``None`` means "oracle default applies" — the
    default VALUES stay oracle-owned (M2), the shape is M1's.

    Two mutually exclusive ways to fix the position threshold (D-6): the
    constant ``position_tolerance_m`` (kept for backwards compatibility — every
    pre-p5c13 scenario declares it) or the derived ``goal_tolerance_budget``.
    Declaring BOTH is a loud contract violation: a silent precedence rule would
    let a consumer edit the key that is being ignored and never learn (the
    ``goal_tolerance_m`` lesson, G-25). One home per threshold.
    """

    position_tolerance_m: float | None = Field(default=None, gt=0, examples=[0.75])
    yaw_tolerance_rad: float | None = Field(default=None, gt=0, examples=[0.26])
    goal_orientation_wxyz: list[float] | None = Field(
        default=None, min_length=4, max_length=4, examples=[[1.0, 0.0, 0.0, 0.0]]
    )
    goal_tolerance_budget: GoalToleranceBudget | None = Field(
        default=None,
        description=(
            "Derive the position threshold from the SUT's declared goal tolerance "
            "plus the consumer's localization budget (D-6) instead of a typed "
            "constant. Mutually exclusive with position_tolerance_m."
        ),
        examples=[{"sut_xy_goal_tolerance_m": 0.25, "localization_budget_m": 0.30}],
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

    @model_validator(mode="after")
    def _one_home_for_the_position_threshold(self) -> ReachedGoalParams:
        """Constant XOR derived budget (D-6) — never a silent precedence.

        Checked on VALUES, not on key presence, so an explicit ``null`` keeps
        meaning "unspecified" (module-header contract convention; the M4
        identity normalization prunes nulls, so any other reading would be
        erased from ``request_identity_key`` anyway).
        """
        budget = self.goal_tolerance_budget
        if self.position_tolerance_m is not None and budget is not None:
            derived = budget.sut_xy_goal_tolerance_m + budget.localization_budget_m
            raise ValueError(
                "'position_tolerance_m' and 'goal_tolerance_budget' both declare the "
                "position threshold — declare exactly one. Either delete "
                f"'position_tolerance_m: {self.position_tolerance_m}' (the threshold is "
                f"then derived: {budget.sut_xy_goal_tolerance_m} + "
                f"{budget.localization_budget_m} = {derived} m), or delete the whole "
                "'goal_tolerance_budget:' block to keep the constant"
            )
        return self


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
    min_clearance_m: float | None = None  # MVP-descoped: permanently None (CEO 2026-08-04 D-3)
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
