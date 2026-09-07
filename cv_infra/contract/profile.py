"""Embodiment profile (M1) — the world/robot facts, moved from platform source to the request.

Until now the platform HELD these facts. ``runner/sim_runtime.SCENE_ASSETS`` knew
which USD to open and how high to drop the robot; ``runner/go2_constants`` held a
consumer's TRAINING configuration (joint order, trained stance, observation
layout, action scale, actuator model, policy MLP shape, training seed);
``runner/runner_sensors`` held where that consumer's camera is bolted and which
lidar it is. Every one of those is a fact about somebody else's robot, and the
cost of holding them was measured: **one new robot = 2,000 lines inside the
platform** (runner_sensors 1,138 + onboard 458 + onboard_wiring 250 + go2_constants
154), which is the reason a new consumer could not self-serve.

This module is where those facts live instead. A consumer ships ONE profile
document with its repository; the platform validates its SHAPE and reproduces it
faithfully, and infers no meaning from any value in it. The precedent is already
in this codebase twice — the custom-oracle plugin (a user module the runner
loads and the platform cannot interpret) and ``sut.locomotion_policy`` (a
ride-along file pinned by digest) — and ``SceneAsset.firmware_slots`` already
carries the exact idea in its docstring ("DECLARATION ONLY: the platform infers
no meaning from a slot here"). It was simply declared on the platform's side of
the boundary. This module moves the declaration, not the idea.

**What the platform still knows, on purpose** (LOCKED §5, unchanged): Isaac Sim
5.1.0, ROS 2 Jazzy, the bridge, fixed-dt determinism, GT telemetry, oracle
evaluation. Providing those IS the product. What it must not know is which
asset, which robot, which policy, which sensor rig — i.e. everything below.

Nothing here is Go2-, quadruped- or Carter-specific: a field exists only because
some robot needs it, and the defaults are "this profile composes nothing", which
is exactly the pre-go2 Carter meaning (its scene asset ships its own robot and
graphs, so its profile declares a scene and stops).

pydantic v2, no I/O — the foundational layer imports no sibling package
(``.importlinter``). Every nesting level rejects unknown keys loudly so a typo'd
axis is a rejected request, not a silently ignored one.
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator


class _ForbidExtra(BaseModel):
    """Shared config: every nesting level loud-rejects unknown keys."""

    model_config = ConfigDict(extra="forbid")


class WorldProfile(_ForbidExtra):
    """The stage: which USD(s) to open, composed at identity under one root.

    ``extra_usds`` exists because a scene is often more than one layer and the
    layers must arrive TOGETHER: the Carter sample is itself
    ``warehouse_with_forklifts.usd`` + ``Stage/warehouse_extras.usd``, and
    opening only the first silently drops the props — which skews any occupancy
    map built against the full scene by exactly those props. The platform does
    not know that; the consumer declares both refs and gets both.
    """

    scene_usd: str = Field(
        min_length=1, examples=["/Isaac/Environments/Simple_Warehouse/warehouse_with_forklifts.usd"]
    )
    extra_usds: tuple[str, ...] = ()
    compose_root: str = "/World"


class ActuatorProfile(_ForbidExtra):
    """The joint drive the consumer's controller was trained against.

    ``kind`` selects a reproduction path, not a behaviour the platform invents:

    * ``dc_motor`` — an EXPLICIT actuator. The trainer computes torque itself
      every physics step and the sim drive gains are forced to zero. Getting
      this wrong is the trap that looks right and IS a different plant: with
      implicit PD the speed-dependent torque saturation simply does not exist
      and the only symptom is "the robot walks badly".
    * ``implicit`` — the sim's own PD drive does the work from ``kp``/``kd``.

    Values are the consumer's; the platform reproduces them and reads no meaning
    into any of them.
    """

    kind: Literal["dc_motor", "implicit"] = "implicit"
    kp: float
    kd: float
    effort_limit: float | None = None
    saturation_effort: float | None = None
    velocity_limit: float | None = None
    joint_friction: float = 0.0
    #: ``None`` = keep whatever the robot USD itself declares.
    armature: float | None = None
    sim_drive_stiffness: float | None = None
    sim_drive_damping: float | None = None
    sim_effort_limit: float | None = None

    @model_validator(mode="after")
    def _explicit_needs_its_curve(self) -> ActuatorProfile:
        if self.kind == "dc_motor":
            missing = [
                name
                for name in ("effort_limit", "saturation_effort", "velocity_limit")
                if getattr(self, name) is None
            ]
            if missing:
                raise ValueError(
                    f"actuator kind 'dc_motor' needs {', '.join(missing)} — an explicit "
                    "actuator computes torque from its own saturation curve, so the curve "
                    "cannot be defaulted"
                )
        return self


class ObsTerm(_ForbidExtra):
    """One slice of the controller's observation vector: name and half-open span.

    The platform assembles terms it has a source for (body velocities, projected
    gravity, the velocity command, joint state, the previous raw action) into
    the declared order. It does not know what the vector means, only where each
    term goes — the same relationship it has with an oracle's metric names.
    """

    name: str = Field(min_length=1)
    start: int = Field(ge=0)
    end: int = Field(gt=0)

    @model_validator(mode="after")
    def _span_is_forward(self) -> ObsTerm:
        if self.end <= self.start:
            raise ValueError(
                f"obs term '{self.name}': end ({self.end}) must exceed start ({self.start})"
            )
        return self


class OnboardProfile(_ForbidExtra):
    """A controller that runs in the RUNNER process, in physics lockstep.

    Why in-process and not in the SUT container: on the real robot this
    controller runs ON the robot, so the mirror of that placement is the sim
    robot's body. Round-tripping joint state through DDS at this rate would put
    transport jitter inside the physics loop and end determinism.

    Placement is not ownership. A custom oracle also runs inside the runner
    process and the platform cannot interpret it; this is the same arrangement
    one layer down. ``artifact`` names a file from the request's ``sut``
    artifacts (ride-along, digest-pinned at admit), and every number below is
    the consumer's.
    """

    kind: str = Field(default="joint_policy", min_length=1)
    artifact: str = Field(min_length=1, examples=["policy.pt"])
    rate_hz: float = Field(gt=0)
    decimation: int = Field(default=1, ge=1)
    joint_order: tuple[str, ...] = Field(min_length=1)
    default_joint_pos: tuple[float, ...] = ()
    default_joint_vel: tuple[float, ...] = ()
    obs_layout: tuple[ObsTerm, ...] = ()
    action_scale: float = 1.0
    gravity_direction_w: tuple[float, float, float] = (0.0, 0.0, -1.0)
    actuator: ActuatorProfile

    @model_validator(mode="after")
    def _vectors_match_the_joint_count(self) -> OnboardProfile:
        n = len(self.joint_order)
        for name in ("default_joint_pos", "default_joint_vel"):
            values = getattr(self, name)
            if values and len(values) != n:
                raise ValueError(
                    f"{name} has {len(values)} entries but joint_order declares {n} joints — "
                    "a silent length mismatch would scatter the wrong value onto the wrong joint"
                )
        return self

    @property
    def obs_dim(self) -> int:
        """Vector width the layout implies (0 when no layout is declared)."""
        return max((t.end for t in self.obs_layout), default=0)


class SensorProfile(_ForbidExtra):
    """One sim-published stream the SUT consumes, and where it is mounted.

    Declared only for a world whose asset ships no sensor graph of its own. A
    scene that comes pre-wired (the Carter sample) declares none of these and
    the runner supplements rather than publishes — the profile's silence IS that
    distinction.

    ``topic`` is the SUT-facing name and stays in ``interface.adapter_config``
    where it already lives; this block is the physical rig only.
    """

    stream: Literal["camera_rgb", "camera_depth", "camera_info", "scan"]
    frame: str = Field(min_length=1)
    mount_xyz: tuple[float, float, float]
    mount_quat_wxyz: tuple[float, float, float, float] = (1.0, 0.0, 0.0, 0.0)
    rate_hz: float = Field(gt=0)
    # camera-shaped fields
    resolution: tuple[int, int] | None = None
    focal_length: float | None = None
    clipping_range: tuple[float, float] | None = None
    distortion_model: str | None = None
    distortion_coeffs: tuple[float, ...] = ()
    encoding: str | None = None
    # lidar-shaped fields
    config: str | None = None
    no_return_value: float | None = None


class MaterialProfile(_ForbidExtra):
    """Contact material the consumer's controller was trained under."""

    terrain_static_friction: float
    terrain_dynamic_friction: float
    terrain_restitution: float = 0.0
    body_static_friction: float | None = None
    body_dynamic_friction: float | None = None
    combine_mode: str = "multiply"


class RobotProfile(_ForbidExtra):
    """The body: which robot USD, where it lands, what runs on it, what it senses.

    Every field defaults to "this profile composes nothing", so a scene whose
    asset already places a robot declares only ``prim_candidates`` and stops.

    ``spawn_z`` has no default worth guessing: a referenced robot arrives at its
    own origin, which for a legged asset is the standing base height rather than
    the floor, and the drop height decides how far it SLIDES before it is
    standing still — error on every initial pose the request declares. It is a
    per-robot measurement and therefore the consumer's to make.
    """

    usd: str | None = None
    spawn_prim: str | None = None
    prim_candidates: tuple[str, ...] = ()
    spawn_z: float = 0.0
    chassis_prim: str | None = None
    render_interval: int = Field(default=1, ge=1)
    #: Stance restored between batch samples. ``()`` = leave joints alone, which
    #: is the wheeled meaning (wheel angles do not decide where a mission starts).
    #: A legged robot that keeps sample i's leg configuration starts sample i+1
    #: mid-gait.
    reset_joint_pos: tuple[float, ...] = ()
    onboard: OnboardProfile | None = None
    sensors: tuple[SensorProfile, ...] = ()
    #: Rate for the streams a runner-published world ALWAYS supplies (``/odom``
    #: and the ``odom->base_link`` transform). Not a ``sensors`` entry because it
    #: is not optional: a SUT cannot drive without it. A publication rate, not a
    #: measurement — the default is the conventional one and the consumer
    #: overrides it when its stack wants another.
    odom_rate_hz: float = Field(default=30.0, gt=0)
    materials: MaterialProfile | None = None

    @model_validator(mode="after")
    def _composed_robot_needs_a_prim(self) -> RobotProfile:
        if self.usd is not None and not self.spawn_prim:
            raise ValueError(
                "robot.usd is declared without robot.spawn_prim — a referenced robot needs "
                "the prim path it lands on (the platform does not invent one)"
            )
        return self


class EmbodimentProfile(_ForbidExtra):
    """``world`` + ``robot``: everything the platform must reproduce and must not interpret."""

    world: WorldProfile
    robot: RobotProfile = Field(default_factory=RobotProfile)

    @property
    def composes_robot(self) -> bool:
        """True when the profile references a robot into a robot-free scene."""
        return self.robot.usd is not None
