"""M1 embodiment-profile tests — contract/profile.py.

This file guards the boundary move: **a robot the platform has never seen is
described entirely by the consumer's own document, and the runner reproduces it
exactly.** The platform constants this used to compare against
(``runner/go2_constants``, the ``SCENE_ASSETS`` go2 row, the go2 constants atop
the sensors module) are DELETED — those values now live only in the profile
below, which is where a new consumer writes its own.

Deleting the comparison target would normally weaken the guard to a tautology
("the profile says what the profile says"), so §1 asserts the two things a
tautology cannot: the DERIVED quantities the runner computes from the profile
(observation width, action width, the saturation clip, whether the sim drive is
zeroed), and GOLDEN vectors — observation, joint target and torque for a fixed
input, captured while the constants still existed and pinned here as literals.
A misread profile, a reordered obs term or an actuator branch taken the wrong
way all move those numbers.

§2 pins the shape rules that make a HAND-WRITTEN profile safe (a new consumer
writes this file with no platform help, so a length mismatch or a defaulted
saturation curve must reject loudly rather than run a different robot), and §3
pins that a pre-wired scene still falls out of the defaults.
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
from cv_infra.runner.onboard import Plant, assemble_obs, dc_motor_torque, joint_pos_target
from cv_infra.runner.runner_sensors import SensorRig
from cv_infra.runner.sim_runtime import SCENE_ASSETS, SceneAsset

FIXTURE = Path(__file__).parent / "fixtures" / "go2_embodiment.yaml"


@pytest.fixture(scope="module")
def go2() -> EmbodimentProfile:
    return EmbodimentProfile.model_validate(yaml.safe_load(FIXTURE.read_text(encoding="utf-8")))


# --- §1 the profile drives the runner, and the numbers do not move ---------------------

#: Fixed, deliberately asymmetric inputs — every joint different, a non-identity
#: attitude, non-zero commands — so a swapped pair or an off-by-one slice shows.
_Q = tuple(v + 0.05 for v in (0.1, -0.1, 0.1, -0.1, 0.8, 0.8, 1.0, 1.0, -1.5, -1.5, -1.5, -1.5))
_QDOT = tuple(0.1 * (i % 3 - 1) for i in range(12))
_TILTED = (0.9238795, 0.0, 0.0, 0.3826834)  # 45 deg about z

#: Captured from the constant-driven implementation on 2026-09-07, BEFORE
#: `go2_constants` was deleted, with the equality of the two proven at that
#: moment. These literals are the only surviving witness of that equality, which
#: is why they are literals and not recomputed here.
OBS_GOLDEN = (
    0.212132076,
    -0.494974714,
    0.1,
    0.0,
    0.0,
    0.3,
    0.0,
    0.0,
    -1.0,
    0.2,
    0.0,
    0.1,
    0.05,
    0.05,
    0.05,
    0.05,
    0.05,
    0.05,
    0.05,
    0.05,
    0.05,
    0.05,
    0.05,
    0.05,
    -0.1,
    0.0,
    0.1,
    -0.1,
    0.0,
    0.1,
    -0.1,
    0.0,
    0.1,
    -0.1,
    0.0,
    0.1,
    0.0,
    0.01,
    0.02,
    0.03,
    0.04,
    0.05,
    0.06,
    0.07,
    0.08,
    0.09,
    0.1,
    0.11,
)
TARGET_GOLDEN = (
    -0.05,
    -0.225,
    0.0,
    -0.175,
    0.75,
    0.775,
    1.0,
    1.025,
    -1.45,
    -1.425,
    -1.4,
    -1.375,
)
TORQUE_GOLDEN = (
    -4.95,
    -4.375,
    -3.8,
    -3.075,
    -2.5,
    -1.925,
    -1.2,
    -0.625,
    -0.05,
    0.675,
    1.25,
    1.825,
)


@pytest.fixture(scope="module")
def plant(go2: EmbodimentProfile) -> Plant:
    assert go2.robot.onboard is not None
    return Plant.from_profile(go2.robot.onboard)


def test_the_profile_alone_decides_the_plants_dimensions(plant: Plant) -> None:
    """Widths are DERIVED, never declared — a profile cannot disagree with itself."""
    assert plant.action_dim == 12
    assert plant.obs_dim == 48
    assert plant.obs_layout[0] == ("base_lin_vel", 0, 3)
    assert plant.obs_layout[-1] == ("actions", 36, 48)


def test_the_explicit_actuator_branch_zeroes_the_sim_drive(plant: Plant) -> None:
    """The trap this guards: implicit PD would look right and BE a different plant —
    the speed-dependent torque saturation would simply not exist."""
    assert plant.sim_drive_stiffness == 0.0
    assert plant.sim_drive_damping == 0.0
    assert plant.vel_at_effort_lim == 60.0  # velocity_limit * (1 + sat/effort)


def test_an_implicit_actuator_keeps_its_gains() -> None:
    """The other branch, so the zeroing above is a DECISION and not a constant."""
    implicit = OnboardProfile(
        artifact="p.pt",
        rate_hz=50,
        joint_order=("a",),
        actuator=ActuatorProfile(kind="implicit", kp=25.0, kd=0.5),
    )
    plant = Plant.from_profile(implicit)
    assert (plant.sim_drive_stiffness, plant.sim_drive_damping) == (25.0, 0.5)


def test_observation_assembly_reproduces_the_golden_vector(plant: Plant) -> None:
    obs = assemble_obs(
        plant=plant,
        base_quat_wxyz=_TILTED,
        base_lin_vel_w=(0.5, -0.2, 0.1),
        base_ang_vel_w=(0.0, 0.0, 0.3),
        command=(0.2, 0.0, 0.1),
        joint_pos=_Q,
        joint_vel=_QDOT,
        last_actions=tuple(0.01 * i for i in range(12)),
    )
    assert obs == pytest.approx(OBS_GOLDEN, abs=1e-9)


def test_action_scaling_and_torque_reproduce_the_golden_vectors(plant: Plant) -> None:
    target = joint_pos_target(tuple(0.1 * (i - 6) for i in range(12)), plant=plant)
    assert target == pytest.approx(TARGET_GOLDEN, abs=1e-9)
    torque = dc_motor_torque(target, _Q, _QDOT, plant=plant)
    assert torque == pytest.approx(TORQUE_GOLDEN, abs=1e-9)


def test_the_sensor_rig_comes_from_the_document(go2: EmbodimentProfile) -> None:
    """Mount, optics and lidar MODEL are the consumer's — the platform holds none."""
    rig = SensorRig.from_profile(go2.robot)
    assert rig.camera_frame == "go2_camera"
    assert rig.lidar_frame == "go2_lidar"
    assert rig.camera_mount_xyz == (0.28, 0.0, 0.12)
    assert rig.camera_optical_quat_wxyz == (0.5, -0.5, 0.5, -0.5)
    assert rig.camera_resolution == (640, 480)
    assert rig.camera_focal_length == 1.2
    assert rig.camera_clipping_range == (0.05, 100.0)
    assert rig.lidar_mount_xyz == (0.0, 0.0, 0.15)
    assert rig.lidar_config == "RPLIDAR_S2E"
    assert (rig.camera_rate_hz, rig.scan_rate_hz, rig.odom_rate_hz) == (10.0, 10.0, 30.0)


def test_the_platform_default_rig_names_nobodys_robot() -> None:
    """The fallback must be ROS convention, not a leftover consumer's vocabulary —
    otherwise a new consumer silently inherits somebody else's camera."""
    neutral = SensorRig()
    assert neutral.camera_frame == "camera_link"
    assert neutral.lidar_frame == "lidar_link"


def test_the_profile_builds_the_scene_row_the_registry_used_to_hold(
    go2: EmbodimentProfile,
) -> None:
    row = SceneAsset.from_profile(go2)
    assert row.scene_usd.endswith("warehouse_with_forklifts.usd")
    assert row.extra_scene_usds == (
        "/Isaac/Environments/Simple_Warehouse/Stage/warehouse_extras.usd",
    )
    assert row.robot_usd == "/Isaac/IsaacLab/Robots/Unitree/Go2/go2.usd"
    assert row.robot_spawn_prim == "/World/Go2"
    assert row.robot_spawn_z == 0.32
    assert row.render_interval == 4
    assert row.default_joint_pos == (
        0.1,
        -0.1,
        0.1,
        -0.1,
        0.8,
        0.8,
        1.0,
        1.0,
        -1.5,
        -1.5,
        -1.5,
        -1.5,
    )
    assert row.firmware_slots == ("onboard",)


def test_no_robot_but_the_platforms_own_remains_in_the_registry() -> None:
    """The census, inverted: the v1 registry may keep the scene a consumer still
    NAMES, and nothing else. A row added back here is a robot fact re-entering
    the platform, which is the whole thing this cycle removed."""
    assert set(SCENE_ASSETS) == {"nova_carter_warehouse"}
    row = SCENE_ASSETS["nova_carter_warehouse"]
    assert row.robot_usd is None  # the asset ships its own robot
    assert row.firmware_slots == ()
    assert row.default_joint_pos == ()


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
    """The pre-wired meaning: the asset ships its own robot, graphs and sensors, so
    the profile names the scene and stops. Every composition field stays off."""
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
    assert SceneAsset.from_profile(carter) == row
