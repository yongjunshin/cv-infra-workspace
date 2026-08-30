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

# Fixable example for a SUT image ref — a PLACEHOLDER SHAPE, never a real image
# name, and the single definition of it (loader.py imports this one).
#
# It is not documentation: ``errors.py:_example_for`` renders ``examples=[...]``
# VERBATIM into the friendly error a user gets when ``image_ref`` is missing
# (NFR-INTAKE-001), so whatever stands here is what they will type next. Two
# reasons, both measured on 2026-08-20, keep it synthetic:
#   * a concrete name is advice to run THAT image, and no image is right for
#     someone else's SUT. The example this replaced (``carter-sut:p2``) was
#     deleted by the production cutover — ``RepoDigests: []``, not even
#     re-fetchable (docs/evidence-anchors.md) — and kept being recommended.
#   * the platform cannot guard a consumer image's lifetime: boundary rule 3
#     means platform CI pulls nothing of the consumer's, so a real ref here has
#     no mechanical anchor and dies silently (G-25). A shape cannot expire.
# The shape is digest-pinned because that is the reproducibility rule (CLAUDE
# §2-7) and because the live consumer ref proves the form runs end to end.
# Pinned by tests/test_contract_errors_p3.py (a concrete tag fails that guard).
EXAMPLE_IMAGE_REF = "ghcr.io/<org>/<image>@sha256:<64-hex-digest>"


class _ForbidExtra(BaseModel):
    """Shared config: unknown keys are a loud contract violation, never dropped."""

    model_config = ConfigDict(extra="forbid", populate_by_name=True)


# --------------------------------------------------------------------------- #
# Randomizable scalars (p6 랜덤 표기 — implementation-plan/p6-implementation-plan.md
# §0-1/§2). A consumer submits ONE document and the platform derives N samples
# from it (contract/derive.py). The notation is INLINE on the field that varies:
#
#     x: -6.0                  # static, exactly as before
#     x: {uniform: [-6.5, -5.5]}
#     x: {choice: [-6.0, 5.0]}
#
# WHY INLINE and not a parallel "randomized request" model tree: a parallel tree
# drifts from this one silently (G-25) and forks every consumer of the request
# wire (identity projection, the JOB_SPEC twins, the runner's re-validation).
#
# BYTES-UNCHANGED PROPERTY (what makes the union safe to add to a live contract):
# a static document validates into the plain ``float`` branch and dumps as a
# plain float, so every pre-p6 request keeps its exact wire bytes AND its
# ``request_identity_key`` (pinned: tests/test_report_regression.py
# ::CANONICAL_FIXTURE_KEY). Derivation happens at the job_spec producers, never
# here — this module only says what may be written.
# --------------------------------------------------------------------------- #

#: Distribution list elements must be FINITE: an ``inf``/``nan`` bound would ride
#: the identity projection into canonical JSON (``Infinity``/``NaN`` — which no
#: other JSON reader accepts) and no draw from it names a pose. The STATIC branch
#: stays a plain ``float`` on purpose: tightening THAT is a separate contract
#: change with its own compatibility story, and this cycle must not move a byte
#: of a static document.
_FiniteFloat = Annotated[float, Field(allow_inf_nan=False)]


class Uniform(_ForbidExtra):
    """Continuous uniform draw over the closed interval ``[lo, hi]``.

    ``lo == hi`` is LEGAL (degenerate = "this exact value, said in the random
    notation"): pinning one axis while another sweeps must not force a consumer
    to rewrite the field's SHAPE. ``lo > hi`` is rejected loudly — that is a
    typo, not a convention for a descending range.
    """

    uniform: list[_FiniteFloat] = Field(min_length=2, max_length=2, examples=[[-6.5, -5.5]])

    @model_validator(mode="after")
    def _bounds_are_ordered(self) -> Uniform:
        low, high = self.uniform
        if low > high:
            raise ValueError(
                f"uniform bounds must be [lo, hi] with lo <= hi (got [{low}, {high}]) — "
                f"write 'uniform: [{high}, {low}]'"
            )
        return self


class Choice(_ForbidExtra):
    """Uniform draw from an explicit value list, used VERBATIM (never rounded).

    A single element is legal for the same reason ``lo == hi`` is. The values
    are the consumer's own literals, so the platform reproduces them exactly —
    only ``uniform`` draws are rounded (derive.UNIFORM_DECIMALS).
    """

    choice: list[_FiniteFloat] = Field(min_length=1, examples=[[-6.0, 5.0]])


#: 한 그룹이 전개할 수 있는 인스턴스 수의 상한. 물리값이 아니라 **구조적 상한**이다(측정
#: 아님 — CLAUDE §2-4의 "측정 없는 상수" 금지는 물리/성능 값에 대한 것): count 오타 하나가
#: JOB_SPEC 바이트와 스테이지 prim 수를 곱셈으로 부풀리고, 그 실패는 dispatch·store 기록
#: **이후** GPU에서만 드러난다. 상한을 계약에 두면 admit 단계(0 GPU초, NFR-INTAKE-003)에서
#: 죽는다. 실수요가 상한을 넘으면 올린다 — 올리는 것은 하위호환(기존 문서는 전부 이하).
MAX_OBSTACLE_COUNT = 32

#: 개수 축의 정수: 음수 개수는 없고, 상한은 위 구조적 상한. ``_FiniteFloat``와 같은 자리.
_CountInt = Annotated[int, Field(ge=0, le=MAX_OBSTACLE_COUNT)]


class Randint(_ForbidExtra):
    """정수 개수의 균등 draw — 닫힌 구간 ``[lo, hi]`` (양끝 포함).

    CEO 표기 "n={0~5}"의 계약형. ``lo == hi``는 합법(``Uniform``과 같은 이유: 한 축을
    고정한 채 다른 축을 쓸어야 한다), ``lo > hi``는 오타로 loud 거부. 값은 개수이므로
    반올림 개념이 없고(정수), ``choice``처럼 verbatim이다.
    """

    randint: list[_CountInt] = Field(min_length=2, max_length=2, examples=[[0, 5]])

    @model_validator(mode="after")
    def _bounds_are_ordered(self) -> Randint:
        low, high = self.randint
        if low > high:
            raise ValueError(
                f"randint bounds must be [lo, hi] with lo <= hi (got [{low}, {high}]) — "
                f"write 'randint: [{high}, {low}]'"
            )
        return self


def _randomizable_tag(value: Any) -> str:
    """Discriminate a randomizable scalar: plain number vs distribution mapping.

    A mapping's SOLE key IS the tag, so an unknown vocabulary word fails with
    pydantic's ``union_tag_invalid``, which names what the consumer wrote and
    lists the accepted words (measured: ``Input tag 'gaussian' found using
    _randomizable_tag() does not match any of the expected tags: 'static',
    'uniform', 'choice'``). A mapping with 0 or 2+ keys gets the same loud
    treatment — silently picking one key is the ``goal_tolerance_m``
    silent-ignore pattern (G-25).
    """
    if isinstance(value, Mapping):
        return "+".join(sorted(str(key) for key in value)) or "(no keys)"
    if isinstance(value, Uniform):
        return "uniform"
    if isinstance(value, Choice):
        return "choice"
    if isinstance(value, Randint):
        return "randint"
    return "static"


#: A scalar a consumer may randomize. Same ``Discriminator(callable)`` idiom as
#: ``AcceptanceCriterion`` below (one union style in this contract, not two).
RandomizableFloat = Annotated[
    (
        Annotated[float, Tag("static")]
        | Annotated[Uniform, Tag("uniform")]
        | Annotated[Choice, Tag("choice")]
    ),
    Discriminator(_randomizable_tag),
]

#: 개수 축의 union. ``RandomizableFloat``과 **같은 관용구**(callable Discriminator + Tag),
#: 같은 태그 함수 — 이 계약에는 union 스타일이 하나뿐이다.
RandomizableCount = Annotated[
    (Annotated[_CountInt, Tag("static")] | Annotated[Randint, Tag("randint")]),
    Discriminator(_randomizable_tag),
]


# --------------------------------------------------------------------------- #
# Request side (REQ-INTAKE-001/002/006)
# --------------------------------------------------------------------------- #
class Goal(_ForbidExtra):
    """Navigation goal pose (REQ-EXEC-004). Coordinates are expressed in ``frame``.

    All three components are ``RandomizableFloat`` (p6 §0-1) — ``yaw`` included:
    it is an axis of the same goal, and a consumer sweeping approach headings is
    the same request shape as one sweeping positions. ``frame`` is not: it names
    a coordinate system, and "a random frame" has no meaning.
    """

    x: RandomizableFloat = Field(examples=[-6.0])
    y: RandomizableFloat = Field(examples=[5.0])
    yaw: RandomizableFloat = Field(examples=[1.5708])
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
    backwards compatible if partial overrides ever earn their demand. Each may
    be randomized (``RandomizableFloat``, p6 §0-1) — a spawn window around the
    SUT's localization init is the motivating case.
    """

    x: RandomizableFloat = Field(examples=[-6.0])
    y: RandomizableFloat = Field(examples=[-1.0])
    yaw: RandomizableFloat = Field(examples=[3.1416])


class DebugObstacle(_ForbidExtra):
    """FAIL-injection cuboid dropped into the stage pre-reset (D-2' 2026-07-10).

    LEGACY SHAPE since p7: a new document declares world obstacles in
    ``Scenario.obstacles`` (kind + how many + yaw). This single box stays for the
    documents already shipped, and the two are MUTUALLY EXCLUSIVE — declaring both
    is rejected (``Scenario._one_home_for_world_obstacles``).

    An obstacle is WORLD STATE, not a judging criterion — hence a ``Scenario``
    field (supersedes the P2 free-form criteria-params ride-along). Keys are
    1:1 with the runner's ``SimRuntime.spawn_debug_obstacle`` read set
    (cv_infra/runner/sim_runtime.py — bound mechanically in the schema tests).
    ``None`` on a dimension means "runner default applies" — the default
    VALUES stay runner-owned (M2), the shape is M1's (ReachedGoalParams
    pattern).

    ``x``/``y`` are randomizable (p6 §0-1) — "put the box somewhere on the
    path" is the FAIL-injection sweep this block exists for. The DIMENSIONS are
    not: they are optional (``None`` = runner default), and a distribution
    inside an optional-with-a-default field would need a third state to mean
    "unspecified" (module-header convention). That third-state problem is
    unchanged, so dimensions stay non-randomizable in ``Obstacle`` too; the
    demand for kinds/counts/yaw is answered by ``Scenario.obstacles``, not here.
    """

    x: RandomizableFloat = Field(examples=[-6.0])
    y: RandomizableFloat = Field(examples=[2.0])
    height: float | None = Field(default=None, gt=0, examples=[0.15])
    width: float | None = Field(default=None, gt=0, examples=[1.2])
    depth: float | None = Field(default=None, gt=0, examples=[0.4])


#: The asset name that means "the built-in cuboid, not an asset at all". Single
#: definition on this plane (the ``Obstacle`` validator uses it); the runner
#: defines its own ``BOX_ASSET_REF`` — the shim layer imports no contract — and a
#: pin test holds the two equal (BATCH_RUNNER_COMMAND <-> Dockerfile precedent).
BUILTIN_BOX_ASSET = "box"


class Obstacle(_ForbidExtra):
    """스테이지에 놓을 장애물 선언 — ``count``개를 같은 규칙으로 배치한다.

    한 블록이 **두 형태**로 읽힌다(같은 스키마, 두 시점):
    * 제출형 — ``count``가 개수/분포, ``x/y/yaw``가 값/분포인 "이런 걸 n개".
    * 구체형 — 파생이 전개한 뒤(derive.materialize_request): ``count == 1``,
      ``x/y/yaw``는 평범한 float. 러너는 이 형태만 본다(§0-5 누출 거부의 확장).
    평행 모델 트리를 만들지 않는 이유가 이것이다: 전개 결과가 **제출 스키마로 재검증**된다.

    ``asset``은 ``scenario.scene``과 **동일한 해석 패턴**(runner/sim_runtime.py
    ``resolve_scene``:203-217): 레지스트리 이름 | ``.usd/.usda/.usdz`` 직접 참조 |
    내장 큐보이드 ``"box"``. 레지스트리 표는 M1에 없다 — 러너가 소유하고, 알 수 없는
    이름은 러너가 아는 이름을 나열하며 loud 거부한다(M1이 ``Literal[...]``로 복제하면
    자산 하나 추가할 때마다 계약 변경이 되고 두 평면이 갈린다).

    ``height/width/depth``는 **내장 box 전용**이다. USD 자산은 자기 extent를 갖고
    오므로 이 값들은 적용할 곳이 없다 — 조용히 무시하면 ``goal_tolerance_m`` 결함
    (G-25)이라 loud 거부한다. 자산 크기 조절(``scale``)은 v1 미포함: CEO 요구는
    종류·개수·yaw였고, 수요 없는 필드는 만들지 않는다(나중에 optional 추가는 안전).

    치수는 랜덤화하지 않는다(``DebugObstacle``과 같은 이유: optional-with-None 필드
    안의 분포는 "미지정"을 뜻할 제3상태를 요구한다). ``z``도 없다: 바닥 접촉이 결정한다
    (``InitialPose``의 z 부재 사유와 동일).
    """

    asset: str = Field(
        min_length=1,
        examples=["box"],
        description=(
            "Obstacle asset: a runner-registry name, a direct .usd/.usda/.usdz "
            "reference, or 'box' for the built-in cuboid (same resolution as scenario.scene)."
        ),
    )
    count: RandomizableCount = Field(default=1, examples=[2])
    x: RandomizableFloat = Field(examples=[-6.0])
    y: RandomizableFloat = Field(examples=[2.0])
    yaw: RandomizableFloat = Field(default=0.0, examples=[1.5708])
    height: float | None = Field(default=None, gt=0, examples=[0.15])
    width: float | None = Field(default=None, gt=0, examples=[1.2])
    depth: float | None = Field(default=None, gt=0, examples=[0.4])

    @model_validator(mode="after")
    def _dimensions_are_the_boxs_only(self) -> Obstacle:
        if self.asset == BUILTIN_BOX_ASSET:
            return self
        declared = [n for n in ("height", "width", "depth") if getattr(self, n) is not None]
        if declared:
            raise ValueError(
                f"{declared} are dimensions of the built-in '{BUILTIN_BOX_ASSET}' obstacle, but "
                f"asset is {self.asset!r} — a USD asset carries its own extent. Delete "
                f"{declared}, or write asset: {BUILTIN_BOX_ASSET}"
            )
        return self


class DerivationMeta(_ForbidExtra):
    """PLATFORM stamp on a materialized sample (derive.materialize_request).

    ``version`` freezes the derivation rule (seeding, draw order, rounding) so a
    stored sample can say which rule produced it; ``index`` is the sample's
    ``repeat_index``. A CONSUMER may not submit this block — the loader rejects
    a submitted one: a stamp the consumer typed claims provenance the platform
    never gave (G-79 — the string that describes a state must be produced by
    whoever owns that state). Hence no ``examples=`` here either: an example is
    rendered into the friendly error as "type this next" (errors._example_for),
    and nobody should type this.

    It lives inside ``Scenario`` rather than at the top level so it rides the
    JOB_SPEC twins unchanged, passes the runner's ``extra="forbid"``
    re-validation, and — optional and ``None`` on every submitted document —
    prunes out of the identity projection (report/regression.py), leaving every
    pre-p6 ``request_identity_key`` exactly where it is.
    """

    version: str = Field(min_length=1)
    index: int = Field(ge=0)


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
    derivation: DerivationMeta | None = Field(
        default=None,
        description=(
            "Platform-stamped sample provenance (derive.materialize_request). "
            "MUST be absent in a submitted document — the loader rejects one that "
            "carries it; the platform writes it when it materializes a sample."
        ),
    )
    obstacles: list[Obstacle] | None = Field(
        default=None,
        min_length=1,
        description=(
            "Obstacles to place in the scene (world state, like debug_obstacle). Each entry "
            "declares an asset + how many + where; the platform expands it into one entry per "
            "instance when it materializes a sample (contract.derive). Omit the key entirely "
            "for a scene with no obstacles — never write an empty list."
        ),
        examples=[[{"asset": "box", "count": 2, "x": {"uniform": [-6.0, 6.0]}, "y": 2.0}]],
    )

    @model_validator(mode="after")
    def _one_home_for_world_obstacles(self) -> Scenario:
        """레거시 단일 상자와 목록은 **택일**이다 — 조용한 합집합은 없다.

        둘 다 선언하면 스테이지에는 상자 1개 + 목록 n개가 함께 서고, 소비자는 목록이
        레거시 필드를 대체했다고 읽는다(측정 불가한 초과 장애물 = 조용한 오판정).
        ``position_tolerance_m`` XOR ``goal_tolerance_budget``(D-6)과 같은 관용구.
        """
        if self.debug_obstacle is not None and self.obstacles is not None:
            box = self.debug_obstacle.model_dump(exclude_none=True)
            migrated = {"asset": BUILTIN_BOX_ASSET, **box}
            raise ValueError(
                "'debug_obstacle' and 'obstacles' both declare world obstacles — declare "
                f"exactly one. Move the box into the list ({migrated!r}) and delete the "
                "'debug_obstacle:' block"
            )
        return self


class SutRef(_ForbidExtra):
    """SUT image reference (REQ-INTAKE-006 required element #1).

    ``image_id`` optionally pins the EXACT image (image-as-artifact, FU-10):
    local tags carry no RepoDigest, so the docker Image Id is the pin. Optional
    — when given it must be a full ``sha256:`` id (loud, friendly reject
    otherwise). Its example is the Image Id measured for the first workstation
    carter build, which the 2026-08-20 cutover deleted — kept as a shape sample
    (a 64-hex id), NOT as a fetchable image (docs/evidence-anchors.md).

    ``image_ref``'s example is a placeholder shape on purpose — see
    ``EXAMPLE_IMAGE_REF`` above for why a real image name must not stand there.
    """

    image_ref: str = Field(min_length=1, examples=[EXAMPLE_IMAGE_REF])
    image_id: str | None = Field(
        default=None,
        pattern=r"^sha256:[0-9a-f]{64}$",
        examples=["sha256:47aff5c993dac05b1664482e44af9401073336f142cb6d4919d81b47f8f9d48a"],
    )


class ExecutionSettings(_ForbidExtra):
    """Execution knobs (M1 §3.2). All optional — the canonical scenario omits it.

    ``repeats`` is the 2-axis fan-out input (M3 ``fanout.py`` — single
    definition, blueprint §8); with the p6 random notation it doubles as the
    SAMPLE COUNT (repeats=5 = 5 samples of the declared distributions — B-U
    semantics, CEO 2026-08-26). ``fixed_dt`` expresses the determinism dt lock
    (LOCKED §7-6; the Phase-2 runner steps at 1/60 — enforcement is M2's).
    ``seed`` / mission ``timeout_s`` live in ``Scenario`` (canonical fixture),
    NOT here — one home per field.
    """

    repeats: int = Field(default=1, ge=1, examples=[3])
    fixed_dt: float | None = Field(default=None, gt=0, examples=[0.016667])
    min_pass_ratio: float | None = Field(
        default=None,
        gt=0,
        le=1,
        examples=[0.8],
        description=(
            "Fraction of the request's samples that must pass for the rolled-up "
            "verdict to be pass (0 < r <= 1). None = today's any-fail rule, "
            "byte-identical. SHAPE ONLY this cycle — the rollup consumes it in "
            "p6c4 (M3), and like repeats it stays OFF the JOB_SPEC wire: it is a "
            "request-level judgement policy, and a runner told about it would be "
            "told something false about the single sample it holds."
        ),
    )


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
    sut: SutRef = Field(examples=[{"image_ref": EXAMPLE_IMAGE_REF}])
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

    ``request_identity_key`` is a FIELD only — derivation/normalization is M4's
    (LOCKED §7-13; M1 §3.1 puts the field here). No producer fills it: the
    runner has no access to the hash, so the wire carries ``None`` = NOT KNOWN
    ON THIS PLANE (never fabricated, same convention as ``Artifacts``). The
    derived key rides the report row instead (``report/regression.identity_key``
    via ``report/aggregate``).

    NOT HERE (p5c16): the self-test provenance markers ``is_self_test`` /
    ``origin``. Their one home is ``RequestEnvelope`` above — domain-model
    *Request Envelope* attribute, ``orchestrator/selftest.py`` MARKER PLACEMENT,
    store v8 ``envelopes`` — and the runner is never told which envelope it came
    from (the M3->M2 JOB_SPEC seam is frozen, ``contract/job_spec.py``).
    A copy here could only be empty or WRONG, and it was wrong: every self-test
    job emitted ``is_self_test: false`` (QA p5c15 D6). One home per field
    (2026-08-04 D-8 idiom); do not re-add without a real producer.
    """

    job_id: str = Field(min_length=1)
    verdict: Verdict
    metrics: Metrics = Field(default_factory=Metrics)
    criteria_results: list[CriterionResult] = Field(default_factory=list)
    artifacts: Artifacts = Field(default_factory=Artifacts)
    request_identity_key: str | None = None


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
