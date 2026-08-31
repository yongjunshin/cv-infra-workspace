"""Go2 flat-locomotion training contract — MEASURED constants (M2, go2 D-3).

VERBATIM port of the C0 probe report §3 block
(``agent-comms/reports/runner-2026-08-31-go2-c0-probe.md`` §3, measured
2026-09-01 on the workstation). Every value below came from one of two
independent sources — never from memory of "how Go2 policies usually look":

* the ACTUAL training artefacts
  ``checkpoints/logs/rsl_rl/unitree_go2_flat/2026-08-18_23-01-09/params/{env,agent}.yaml``
  (= stock ``Isaac-Velocity-Flat-Unitree-Go2-v0``, seed 42, 4096 envs, 1000
  iters; the finally-adopted ``unitree_go2_flat_spin`` run's ``env.yaml`` was
  cross-checked and is identical in observations/actions/dt), and
* Isaac Lab **v2.3.2** (tag ``37ddf62``) source + a live stage dump of the Go2
  USD (``SingleArticulation.dof_names``).

This module is DATA ONLY — no torch, no isaacsim, no numpy, no I/O. The
formulas that consume it (``assemble_obs`` / ``joint_pos_target`` /
``dc_motor_torque``) live in ``cv_infra.runner.go2_policy`` so that the
contract (here) and its implementation (there) can be diffed against the probe
report separately.

Why a MODULE of measured constants and not scenario YAML: these are properties
of the trained policy, not of a scenario. A scenario that changed them would
not be "a different test", it would be a different plant than the one the
network was trained on, and the robot would simply stop walking (D-3/AR-6).
The one value that IS per-request is the policy file itself (+ its sha256) —
that arrives in ``sut.locomotion_policy`` and the platform never supplies a
default for it (plan §1-1: missing/mismatched = exit 2, no substitution).
"""

from __future__ import annotations

# ── 시뮬레이션 조건 (env.yaml: sim.dt / decimation / sim.render_interval / seed) ──

#: Physics step [s] — 200 Hz. Scenario knob ``fixed_dt`` must be set to this to
#: reproduce the training plant (D-4).
FIXED_DT = 0.005
#: Policy runs every N-th physics step -> 50 Hz control.
DECIMATION = 4
RENDER_INTERVAL = 4
#: Training gravity vector [m/s^2].
GRAVITY = (0.0, 0.0, -9.81)
#: Isaac Lab's ``projected_gravity`` term uses the NORMALIZED gravity direction
#: (unit vector), not ``GRAVITY`` — the observation is dimensionless.
GRAVITY_DIRECTION_W = (0.0, 0.0, -1.0)
#: Training episode length. Informational: platform mission timeouts are the
#: scenario's (D-F sim-time), unrelated to this.
EPISODE_LENGTH_S = 20.0
TRAIN_SEED = 42

# ── 조인트 순서 = 시뮬 dof 순서 ──
# ``preserve_order: false`` + ``joint_names: [".*"]`` in the training cfg means
# action/observation indices ARE the articulation's internal dof order. Two
# independent sources agree 12/12 (probe §5): a live ``dof_names`` dump of both
# Go2 USDs, and Isaac Lab's own
# ``scripts/sim2sim_transfer/config/newton_to_physx_go2.yaml`` target_joint_names.
# NOTE: URDF / Unitree-SDK vectors use a leg-major order — those need remapping.
JOINT_ORDER = (
    "FL_hip_joint",
    "FR_hip_joint",
    "RL_hip_joint",
    "RR_hip_joint",
    "FL_thigh_joint",
    "FR_thigh_joint",
    "RL_thigh_joint",
    "RR_thigh_joint",
    "FL_calf_joint",
    "FR_calf_joint",
    "RL_calf_joint",
    "RR_calf_joint",
)

# 기본 스탠스 = ArticulationCfg.init_state.joint_pos, JOINT_ORDER 순으로 전개
DEFAULT_JOINT_POS = (
    0.1,
    -0.1,
    0.1,
    -0.1,  # hips   (L=+0.1, R=-0.1)
    0.8,
    0.8,
    1.0,
    1.0,  # thighs (front=0.8, rear=1.0)
    -1.5,
    -1.5,
    -1.5,
    -1.5,  # calves
)
DEFAULT_JOINT_VEL = (0.0,) * 12
#: ``init_state.pos = (0, 0, 0.4)``. Measured settle height was z ≈ 0.279~0.288
#: (probe A7) and the settle itself slid the base ~0.10 m in x (probe §6-12).
DEFAULT_BASE_POS_Z = 0.4

# ── 관측 48차원 ──
# Order = ``ObservationsCfg.PolicyCfg`` declaration order, ``concatenate_terms=True``.
# EVERY term has scale=None, clip=None; ``actor_obs_normalization=False`` and
# ``empirical_normalization=null`` => there is NO normalization layer (the
# exporter's normalizer is ``torch.nn.Identity`` and carries no parameters).
# The flat env has ``height_scan=None``, which is why this is 48 and not 235.
OBS_LAYOUT = (
    ("base_lin_vel", 0, 3),  # quat_apply_inverse(root_link_quat_w, root_com_lin_vel_w) [m/s]
    ("base_ang_vel", 3, 6),  # quat_apply_inverse(root_link_quat_w, root_com_ang_vel_w) [rad/s]
    ("projected_gravity", 6, 9),  # quat_apply_inverse(root_link_quat_w, (0,0,-1)) 단위벡터
    ("velocity_commands", 9, 12),  # (vx, vy, wz)  <- /cmd_vel
    ("joint_pos", 12, 24),  # joint_pos[JOINT_ORDER] - DEFAULT_JOINT_POS   [rad]
    ("joint_vel", 24, 36),  # joint_vel[JOINT_ORDER] - DEFAULT_JOINT_VEL   [rad/s]
    ("actions", 36, 48),  # 직전 스텝의 RAW 정책 출력(스케일 적용 전). reset 시 0.
)
OBS_DIM, ACTION_DIM = 48, 12

# 훈련 시 obs 노이즈(enable_corruption=True)는 play/배포에서 꺼진다 -> 러너는 노이즈 주입 금지:
#   base_lin_vel ±0.1 / base_ang_vel ±0.2 / projected_gravity ±0.05
#   joint_pos ±0.01 / joint_vel ±1.5 / (commands·actions 노이즈 없음)

# ── 행동 -> 조인트 목표 (JointPositionActionCfg) ──
# scale=0.25, offset=0.0, use_default_offset=True -> offset := DEFAULT_JOINT_POS
# clip=None, clip_actions=null  (네트워크 출력 클리핑 없음)
ACTION_SCALE = 0.25

# ── 액추에이터: DCMotorCfg (**EXPLICIT** 모델 — 프로브 §6-2가 이 항목의 함정) ──
KP, KD = 25.0, 0.5
#: continuous torque [N·m]
EFFORT_LIMIT = 23.5
#: stall torque [N·m]
SATURATION_EFFORT = 23.5
#: no-load speed [rad/s]
VELOCITY_LIMIT = 30.0
JOINT_FRICTION = 0.0
#: ``None`` = use the USD's own value (measured: all 12 are 0.0).
ARMATURE = None
#: ``DCMotor`` clips the velocity used for the saturation curve to
#: ``VELOCITY_LIMIT * (1 + SATURATION_EFFORT / EFFORT_LIMIT)`` = 60.0 rad/s
#: (``actuator_pd.py``). That clip is also what keeps tau_min <= tau_max at any
#: joint speed — see ``go2_policy.dc_motor_torque``.
VEL_AT_EFFORT_LIM = 30.0 * (1 + 23.5 / 23.5)  # = 60.0
#: Explicit actuator => Isaac Lab FORCES the sim drive gains to zero
#: (``articulation.py:1768-1769`` write_joint_stiffness_to_sim(0.0) /
#: write_joint_damping_to_sim(0.0)) and applies its own torque as joint EFFORT.
#: Reproducing this is AR-6; implicit PD is the fallback only.
SIM_DRIVE_STIFFNESS = 0.0
SIM_DRIVE_DAMPING = 0.0
#: ``ActuatorBase._DEFAULT_MAX_EFFORT_SIM`` for explicit models — the sim-side
#: effort clamp is effectively disabled so the python-side clip is THE limit.
SIM_EFFORT_LIMIT = 1.0e9

# ── 정책 네트워크 (agent.yaml) ──
#: actor MLP hidden sizes, ELU. TorchScript ``forward(x) == actor(normalizer(x))``
#: with normalizer = Identity, non-recurrent. Measured parameter shapes:
#: actor.0 [128,48] / actor.2 [128,128] / actor.4 [128,128] / actor.6 [12,128].
POLICY_MLP = (128, 128, 128)

# ── 물리 재질(훈련 조건) ──
TERRAIN_STATIC_FRICTION, TERRAIN_DYNAMIC_FRICTION, TERRAIN_RESTITUTION = 1.0, 1.0, 0.0
ROBOT_BODY_STATIC_FRICTION, ROBOT_BODY_DYNAMIC_FRICTION = 0.8, 0.6  # startup event, robot 전 body
COMBINE_MODE = "multiply"
