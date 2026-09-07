"""M1 embodiment-profile tests — contract/profile.py.

This file exists to prove ONE claim, mechanically: **the go2 facts the platform
currently holds are data, not code.** If a profile document loaded from the
consumer's side reproduces every platform constant exactly, then threading it
through M2 is a mechanical substitution and not a rewrite — which is the whole
premise of moving the boundary.

The proof is deliberately TOTAL on the value side (§1): every field of the
``SCENE_ASSETS`` go2 row, every constant in ``runner/go2_constants``, and every
go2 constant at the top of ``runner/go2_sensors`` is asserted equal to what the
document yields. A partial proof would be worthless — the one value nobody
checked is exactly the one that silently changes the plant.

§2 pins the shape rules that make a HAND-WRITTEN profile safe (a new consumer
writes this file with no platform help, so a length mismatch or a defaulted
saturation curve must reject loudly rather than run a different robot), and §3
pins that the pre-go2 Carter meaning still falls out of the defaults.

When M2's threading lands, the platform symbols this file imports are DELETED
and these assertions move to comparing the runner's live wiring against the same
document. Until then, this is the guard that the extraction is faithful.
"""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from cv_infra.contract.profile import (
    ActuatorProfile,
    EmbodimentProfile,
    OnboardProfile,
    RobotProfile,
)
from cv_infra.runner import go2_constants as K
from cv_infra.runner import go2_sensors as S
from cv_infra.runner.sim_runtime import SCENE_ASSETS

FIXTURE = Path(__file__).parent / "fixtures" / "go2_embodiment.yaml"


@pytest.fixture(scope="module")
def go2() -> EmbodimentProfile:
    return EmbodimentProfile.model_validate(yaml.safe_load(FIXTURE.read_text(encoding="utf-8")))


# --- §1 the extraction is faithful, value for value ------------------------------------


def test_world_matches_the_platform_scene_row(go2: EmbodimentProfile) -> None:
    row = SCENE_ASSETS["go2_warehouse"]
    assert go2.world.scene_usd == row.scene_usd
    assert go2.world.extra_usds == row.extra_scene_usds


def test_robot_placement_matches_the_platform_scene_row(go2: EmbodimentProfile) -> None:
    row = SCENE_ASSETS["go2_warehouse"]
    assert go2.robot.usd == row.robot_usd
    assert go2.robot.spawn_prim == row.robot_spawn_prim
    assert go2.robot.prim_candidates == row.robot_prim_candidates
    assert go2.robot.spawn_z == row.robot_spawn_z
    assert go2.robot.render_interval == row.render_interval
    assert go2.robot.reset_joint_pos == row.default_joint_pos


def test_the_firmware_slot_becomes_a_declared_onboard_controller(go2: EmbodimentProfile) -> None:
    """``firmware_slots=('locomotion_policy',)`` was the platform DECLARING what the
    consumer runs. The declaration moves; the idea is unchanged."""
    assert SCENE_ASSETS["go2_warehouse"].firmware_slots == ("locomotion_policy",)
    assert go2.robot.onboard is not None
    assert go2.robot.onboard.artifact == "policy.pt"


def test_onboard_reproduces_every_training_constant(go2: EmbodimentProfile) -> None:
    """The constants that make this a REPRODUCTION of the trained plant rather than
    a plausible imitation. All of them, or the test is theatre."""
    ob = go2.robot.onboard
    assert ob is not None
    assert ob.joint_order == K.JOINT_ORDER
    assert ob.default_joint_pos == K.DEFAULT_JOINT_POS
    assert ob.default_joint_vel == K.DEFAULT_JOINT_VEL
    assert ob.action_scale == K.ACTION_SCALE
    assert ob.decimation == K.DECIMATION
    assert ob.gravity_direction_w == K.GRAVITY_DIRECTION_W
    assert ob.obs_dim == K.OBS_DIM
    assert len(ob.joint_order) == K.ACTION_DIM


def test_obs_layout_reproduces_the_declaration_order_and_spans(go2: EmbodimentProfile) -> None:
    ob = go2.robot.onboard
    assert ob is not None
    assert tuple((t.name, t.start, t.end) for t in ob.obs_layout) == K.OBS_LAYOUT


def test_actuator_reproduces_the_explicit_dc_motor_model(go2: EmbodimentProfile) -> None:
    """The trap: implicit PD would look right and BE a different plant (no
    speed-dependent torque saturation). The profile must carry the whole curve."""
    ob = go2.robot.onboard
    assert ob is not None
    act = ob.actuator
    assert act.kind == "dc_motor"
    assert (act.kp, act.kd) == (K.KP, K.KD)
    assert act.effort_limit == K.EFFORT_LIMIT
    assert act.saturation_effort == K.SATURATION_EFFORT
    assert act.velocity_limit == K.VELOCITY_LIMIT
    assert act.joint_friction == K.JOINT_FRICTION
    assert act.armature == K.ARMATURE
    assert act.sim_drive_stiffness == K.SIM_DRIVE_STIFFNESS
    assert act.sim_drive_damping == K.SIM_DRIVE_DAMPING
    assert act.sim_effort_limit == K.SIM_EFFORT_LIMIT


def test_the_saturation_clip_is_derivable_from_the_profile_alone(go2: EmbodimentProfile) -> None:
    """``VEL_AT_EFFORT_LIM`` is a DERIVED platform constant. A profile that cannot
    re-derive it would have moved the values but not the model."""
    act = go2.robot.onboard.actuator  # type: ignore[union-attr]
    derived = act.velocity_limit * (1 + act.saturation_effort / act.effort_limit)  # type: ignore[operator]
    assert derived == K.VEL_AT_EFFORT_LIM == 60.0


def test_materials_reproduce_the_training_contact_conditions(go2: EmbodimentProfile) -> None:
    mat = go2.robot.materials
    assert mat is not None
    assert mat.terrain_static_friction == K.TERRAIN_STATIC_FRICTION
    assert mat.terrain_dynamic_friction == K.TERRAIN_DYNAMIC_FRICTION
    assert mat.terrain_restitution == K.TERRAIN_RESTITUTION
    assert mat.body_static_friction == K.ROBOT_BODY_STATIC_FRICTION
    assert mat.body_dynamic_friction == K.ROBOT_BODY_DYNAMIC_FRICTION
    assert mat.combine_mode == K.COMBINE_MODE


def test_sensor_rig_reproduces_every_go2_sensor_constant(go2: EmbodimentProfile) -> None:
    by_stream = {s.stream: s for s in go2.robot.sensors}
    assert set(by_stream) == {"camera_rgb", "camera_depth", "scan"}

    rgb = by_stream["camera_rgb"]
    assert rgb.frame == S.DEFAULT_CAMERA_FRAME
    assert rgb.mount_xyz == S.CAMERA_MOUNT_XYZ
    assert rgb.mount_quat_wxyz == S.CAMERA_OPTICAL_QUAT_WXYZ
    assert rgb.rate_hz == S.CAMERA_RATE_HZ
    assert rgb.resolution == S.CAMERA_RESOLUTION
    assert rgb.focal_length == S.CAMERA_FOCAL_LENGTH_STAGE_UNITS
    assert rgb.clipping_range == S.CAMERA_CLIPPING_RANGE_M
    assert rgb.distortion_model == S.CAMERA_DISTORTION_MODEL
    assert rgb.distortion_coeffs == S.CAMERA_DISTORTION_COEFFS
    assert rgb.encoding == S.RGB_ENCODING

    assert by_stream["camera_depth"].encoding == S.DEPTH_ENCODING

    scan = by_stream["scan"]
    assert scan.frame == S.DEFAULT_LIDAR_FRAME
    assert scan.mount_xyz == S.LIDAR_MOUNT_XYZ
    assert scan.rate_hz == S.SCAN_RATE_HZ
    assert scan.config == S.LIDAR_CONFIG
    assert scan.no_return_value == S.LIDAR_NO_RETURN


def test_no_go2_fact_is_left_unaccounted_for() -> None:
    """A census, so a constant ADDED to the platform later cannot quietly stay there.

    Every public name in ``go2_constants`` is either reproduced by the profile
    (asserted above) or listed here as deliberately platform-side. A new symbol
    lands in neither set and fails — which is the point.
    """
    reproduced = {
        "JOINT_ORDER",
        "DEFAULT_JOINT_POS",
        "DEFAULT_JOINT_VEL",
        "ACTION_SCALE",
        "DECIMATION",
        "GRAVITY_DIRECTION_W",
        "OBS_LAYOUT",
        "OBS_DIM",
        "ACTION_DIM",
        "KP",
        "KD",
        "EFFORT_LIMIT",
        "SATURATION_EFFORT",
        "VELOCITY_LIMIT",
        "JOINT_FRICTION",
        "ARMATURE",
        "SIM_DRIVE_STIFFNESS",
        "SIM_DRIVE_DAMPING",
        "SIM_EFFORT_LIMIT",
        "VEL_AT_EFFORT_LIM",
        "RENDER_INTERVAL",
        "TERRAIN_STATIC_FRICTION",
        "TERRAIN_DYNAMIC_FRICTION",
        "TERRAIN_RESTITUTION",
        "ROBOT_BODY_STATIC_FRICTION",
        "ROBOT_BODY_DYNAMIC_FRICTION",
        "COMBINE_MODE",
        "DEFAULT_BASE_POS_Z",
        "POLICY_MLP",
    }
    # Platform-side on purpose: FIXED_DT and GRAVITY are the SIMULATION's settings
    # (execution_settings / LOCKED determinism), not facts about someone's robot;
    # EPISODE_LENGTH_S and TRAIN_SEED are provenance notes consumed by nothing.
    platform_side = {"FIXED_DT", "GRAVITY", "EPISODE_LENGTH_S", "TRAIN_SEED"}
    public = {n for n in vars(K) if n.isupper() and not n.startswith("_")}
    assert public - reproduced - platform_side == set(), "a go2 fact nobody accounted for"
    # ...and the census must be typo-free in the other direction too, or a
    # misspelled name here would silently shrink what it actually guards.
    assert (
        reproduced | platform_side
    ) - public == set(), "census names a constant that does not exist"


# --- §2 a hand-written profile fails loudly ---------------------------------------------


def test_unknown_key_is_rejected(go2: EmbodimentProfile) -> None:
    raw = yaml.safe_load(FIXTURE.read_text(encoding="utf-8"))
    raw["robot"]["spawn_height"] = 0.32  # a plausible typo for spawn_z
    with pytest.raises(ValueError, match="spawn_height"):
        EmbodimentProfile.model_validate(raw)


def test_joint_vector_length_mismatch_is_rejected() -> None:
    with pytest.raises(ValueError, match="joint_order declares"):
        OnboardProfile(
            artifact="p.pt",
            rate_hz=50,
            joint_order=("a", "b"),
            default_joint_pos=(0.0, 0.0, 0.0),
            actuator=ActuatorProfile(kind="implicit", kp=1.0, kd=0.1),
        )


def test_explicit_actuator_cannot_default_its_saturation_curve() -> None:
    with pytest.raises(ValueError, match="effort_limit"):
        ActuatorProfile(kind="dc_motor", kp=25.0, kd=0.5)


def test_composed_robot_without_a_prim_is_rejected() -> None:
    with pytest.raises(ValueError, match="spawn_prim"):
        RobotProfile(usd="/Isaac/Robots/x.usd")


def test_backwards_obs_span_is_rejected() -> None:
    with pytest.raises(ValueError, match="must exceed start"):
        OnboardProfile(
            artifact="p.pt",
            rate_hz=50,
            joint_order=("a",),
            obs_layout=[{"name": "x", "start": 5, "end": 5}],
            actuator=ActuatorProfile(kind="implicit", kp=1.0, kd=0.1),
        )


# --- §3 the pre-go2 meaning still falls out of the defaults -----------------------------


def test_a_prewired_scene_profile_composes_nothing() -> None:
    """The carter meaning: the asset ships its own robot, graphs and sensors, so the
    profile names the scene and stops. Every composition field stays off."""
    row = SCENE_ASSETS["nova_carter_warehouse"]
    carter = EmbodimentProfile.model_validate(
        {
            "world": {"scene_usd": row.scene_usd},
            "robot": {"prim_candidates": list(row.robot_prim_candidates)},
        }
    )
    assert carter.composes_robot is False
    assert carter.robot.onboard is None
    assert carter.robot.sensors == ()
    assert carter.robot.reset_joint_pos == ()
    assert carter.robot.render_interval == row.render_interval == 1
    assert carter.robot.spawn_z == row.robot_spawn_z == 0.0
    assert carter.world.extra_usds == row.extra_scene_usds == ()
