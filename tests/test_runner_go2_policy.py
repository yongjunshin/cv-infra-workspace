"""CPU tests for the Go2 locomotion policy loop — no torch, no Isaac, no GPU.

Everything this module can get wrong is SILENT in production: a swapped
quaternion convention, a joint vector in the wrong order, an observation slice
off by three, a torque model without its saturation curve. None of them raise —
they produce a robot that walks badly, which on the workstation reads as "the
policy is bad" and sends the next person to retrain a network that was fine.
So the trained contract (C0 probe §3, ``go2_constants``) is asserted here term
by term, and the two collaborators that need a GPU image are FAKES:

* ``torch`` — a module object injected into ``sys.modules`` (the same lever
  ``tests/test_orchestrator_edge_paths.py`` uses for pynvml). This is what makes
  the deferred-import branch of ``load()`` a tested branch rather than an
  excluded one, on a venv that has no torch and must not gain one.
* the articulation — a RECORDING duck (``isaacsim.core.prims.SingleArticulation``
  method names, nothing else), so the write side of the loop (drive gains,
  efforts, dof order) is inspectable without a physics engine.

What is NOT provable here, and is C2b's on the workstation: that this contract
makes the real network actually walk (probe §6-11).
"""

import hashlib
import os
import subprocess
import sys
from types import ModuleType, SimpleNamespace

import pytest

from cv_infra.runner import go2_policy
from cv_infra.runner.go2_constants import (
    ACTION_DIM,
    ACTION_SCALE,
    DECIMATION,
    DEFAULT_JOINT_POS,
    EFFORT_LIMIT,
    JOINT_ORDER,
    KD,
    KP,
    OBS_DIM,
    OBS_LAYOUT,
    SATURATION_EFFORT,
    VEL_AT_EFFORT_LIM,
    VELOCITY_LIMIT,
)
from cv_infra.runner.go2_policy import (
    Go2PolicyLoop,
    PolicyContractError,
    assemble_obs,
    dc_motor_torque,
    joint_pos_target,
    normalize_digest,
    quat_apply_inverse,
)

UPRIGHT = (1.0, 0.0, 0.0, 0.0)
#: yaw = +90 deg (w = z = sqrt(2)/2) — the robot faces world +y.
YAW_90 = (0.7071067811865476, 0.0, 0.0, 0.7071067811865476)
ZERO12 = (0.0,) * ACTION_DIM


def _obs_slice(obs, name):
    """The observation slice OBS_LAYOUT assigns to ``name`` (single source)."""
    start, stop = next((s, e) for term, s, e in OBS_LAYOUT if term == name)
    return tuple(obs[start:stop])


def _obs(**overrides):
    kwargs = {
        "base_quat_wxyz": UPRIGHT,
        "base_lin_vel_w": (0.0, 0.0, 0.0),
        "base_ang_vel_w": (0.0, 0.0, 0.0),
        "command": (0.0, 0.0, 0.0),
        "joint_pos": DEFAULT_JOINT_POS,
        "joint_vel": ZERO12,
        "last_actions": ZERO12,
    }
    kwargs.update(overrides)
    return assemble_obs(**kwargs)


# --------------------------------------------------------------------------- #
# Fakes.
# --------------------------------------------------------------------------- #
class _Tensor:
    """What the fake ``torch.tensor`` returns — carries the payload, nothing else."""

    def __init__(self, data, dtype=None):
        self.data = data
        self.dtype = dtype


class _FakePolicy:
    """A TorchScript-module-shaped callable: (1, 48) in, (1, N) out."""

    def __init__(self, actions=((0.0,) * ACTION_DIM,)):
        self._actions = list(actions)
        self.observations = []  # every obs it was called with, in order
        self.dtypes = []
        self.eval_calls = 0

    def eval(self):
        self.eval_calls += 1

    def __call__(self, tensor):
        self.observations.append(tuple(tensor.data[0]))
        self.dtypes.append(tensor.dtype)
        index = min(len(self.observations) - 1, len(self._actions) - 1)
        return [tuple(self._actions[index])]


def _fake_torch(policy):
    """A ``torch`` MODULE object — attribute access only (no submodule import)."""
    module = ModuleType("torch")
    module.float32 = "float32"
    module.threads = []
    module.load_calls = []

    def _load(path, map_location=None):
        module.load_calls.append((path, map_location))
        return policy

    class _InferenceMode:
        def __enter__(self):
            module.threads.append("inference_mode")

        def __exit__(self, *exc):
            return False

    module.tensor = _Tensor
    module.inference_mode = _InferenceMode
    module.set_num_threads = lambda n: module.threads.append(("set_num_threads", n))
    module.jit = SimpleNamespace(load=_load)
    return module


class _FakeArticulation:
    """Recording stand-in for ``SingleArticulation`` (reads answer, writes log)."""

    def __init__(self, dof_names=JOINT_ORDER, joint_pos=None, joint_vel=None):
        self.dof_names = list(dof_names)
        n = len(self.dof_names)
        self.joint_pos = list(joint_pos if joint_pos is not None else [0.0] * n)
        self.joint_vel = list(joint_vel if joint_vel is not None else [0.0] * n)
        self.position = [0.0, 0.0, 0.3]
        self.quat = list(UPRIGHT)
        self.lin_vel = [0.0, 0.0, 0.0]
        self.ang_vel = [0.0, 0.0, 0.0]
        self.efforts = []  # one list per physics step, in DOF order
        self.gains = []

    # reads
    def get_world_pose(self):
        return list(self.position), list(self.quat)

    def get_linear_velocity(self):
        return list(self.lin_vel)

    def get_angular_velocity(self):
        return list(self.ang_vel)

    def get_joint_positions(self):
        return list(self.joint_pos)

    def get_joint_velocities(self):
        return list(self.joint_vel)

    # writes
    def get_articulation_controller(self):
        return self

    def set_gains(self, kps=None, kds=None):
        self.gains.append((list(kps), list(kds)))

    def set_joint_efforts(self, efforts):
        self.efforts.append(list(efforts))


def _policy_file(tmp_path, payload=b"pretend-this-is-torchscript"):
    path = tmp_path / "policy.pt"
    path.write_bytes(payload)
    return path, hashlib.sha256(payload).hexdigest()


def _loaded(monkeypatch, tmp_path, actions=((0.0,) * ACTION_DIM,)):
    path, digest = _policy_file(tmp_path)
    policy = _FakePolicy(actions)
    torch = _fake_torch(policy)
    monkeypatch.setitem(sys.modules, "torch", torch)
    loop = Go2PolicyLoop(path, digest)
    loop.load()
    return loop, policy, torch


def _ready(monkeypatch, tmp_path, articulation, actions=((0.0,) * ACTION_DIM,)):
    loop, policy, torch = _loaded(monkeypatch, tmp_path, actions)
    loop.bind(articulation)
    return loop, policy, torch


# --------------------------------------------------------------------------- #
# The measured contract itself (probe §3) — a constants edit must break a test.
# --------------------------------------------------------------------------- #
def test_obs_layout_is_contiguous_and_sums_to_the_declared_dim():
    """48 is not a total to trust — it is the sum of seven terms that must tile
    [0, 48) with no gap and no overlap, or every slice after the gap is wrong."""
    cursor = 0
    for _name, start, stop in OBS_LAYOUT:
        assert start == cursor, f"OBS_LAYOUT is not contiguous at {_name}"
        cursor = stop
    assert cursor == OBS_DIM


def test_joint_vectors_are_all_twelve_and_ordered_by_the_measured_dof_order():
    assert len(JOINT_ORDER) == ACTION_DIM == 12
    assert len(set(JOINT_ORDER)) == ACTION_DIM  # no duplicate name -> unique mapping
    assert len(DEFAULT_JOINT_POS) == ACTION_DIM
    # Probe §3: hips L=+0.1 / R=-0.1, thighs front 0.8 / rear 1.0, calves -1.5.
    stance = dict(zip(JOINT_ORDER, DEFAULT_JOINT_POS, strict=True))
    assert stance["FL_hip_joint"] == 0.1 and stance["FR_hip_joint"] == -0.1
    assert stance["FL_thigh_joint"] == 0.8 and stance["RL_thigh_joint"] == 1.0
    assert stance["RR_calf_joint"] == -1.5


def test_dc_motor_speed_clip_is_the_derived_sixty_not_a_typed_number():
    """``VEL_AT_EFFORT_LIM`` is DERIVED (V*(1+S/E)); the literal 60.0 would go
    stale the moment a re-measured saturation/effort limit made them differ."""
    assert VEL_AT_EFFORT_LIM == VELOCITY_LIMIT * (1 + SATURATION_EFFORT / EFFORT_LIMIT) == 60.0


# --------------------------------------------------------------------------- #
# quat_apply_inverse — world -> body.
# --------------------------------------------------------------------------- #
def test_identity_quaternion_leaves_the_vector_alone():
    assert quat_apply_inverse(UPRIGHT, (1.0, 2.0, 3.0)) == pytest.approx((1.0, 2.0, 3.0))


def test_yaw_rotation_is_applied_INVERSELY_not_forward():
    """The robot faces world +y (yaw +90 deg) and moves along world +x. In its
    OWN frame that is motion to its right = body -y. The forward rotation would
    answer +y, and an obs built with it teaches the policy the opposite of what
    the robot is doing."""
    assert quat_apply_inverse(YAW_90, (1.0, 0.0, 0.0)) == pytest.approx((0.0, -1.0, 0.0))


def test_projected_gravity_is_down_when_upright_and_forward_when_nose_down():
    assert _obs_slice(_obs(), "projected_gravity") == pytest.approx((0.0, 0.0, -1.0))
    # pitch +90 deg about y (nose down): the body's +x axis now points at the floor.
    nose_down = (0.7071067811865476, 0.0, 0.7071067811865476, 0.0)
    assert _obs_slice(_obs(base_quat_wxyz=nose_down), "projected_gravity") == pytest.approx(
        (1.0, 0.0, 0.0)
    )


def test_rotation_preserves_length_for_an_arbitrary_orientation():
    quat = (0.5, 0.5, 0.5, 0.5)  # unit quaternion, all axes involved
    out = quat_apply_inverse(quat, (0.3, -1.2, 4.0))
    assert sum(v * v for v in out) == pytest.approx(0.3**2 + 1.2**2 + 4.0**2)


def test_a_wrong_length_quaternion_or_vector_is_rejected_loudly():
    with pytest.raises(ValueError, match="quat_wxyz: expected 4"):
        quat_apply_inverse((1.0, 0.0, 0.0), (1.0, 0.0, 0.0))
    with pytest.raises(ValueError, match="vec: expected 3"):
        quat_apply_inverse(UPRIGHT, (1.0, 0.0))


# --------------------------------------------------------------------------- #
# assemble_obs.
# --------------------------------------------------------------------------- #
def test_every_term_lands_in_its_declared_slice():
    obs = _obs(
        base_quat_wxyz=YAW_90,
        base_lin_vel_w=(1.0, 0.0, 0.0),
        base_ang_vel_w=(0.0, 0.0, 2.0),
        command=(0.5, -0.25, 0.75),
        joint_pos=tuple(d + 0.01 for d in DEFAULT_JOINT_POS),
        joint_vel=tuple(float(i) for i in range(ACTION_DIM)),
        last_actions=tuple(-float(i) for i in range(ACTION_DIM)),
    )
    assert len(obs) == OBS_DIM
    assert _obs_slice(obs, "base_lin_vel") == pytest.approx((0.0, -1.0, 0.0))
    assert _obs_slice(obs, "base_ang_vel") == pytest.approx((0.0, 0.0, 2.0))  # yaw rate is z-only
    assert _obs_slice(obs, "velocity_commands") == pytest.approx((0.5, -0.25, 0.75))
    assert _obs_slice(obs, "joint_pos") == pytest.approx((0.01,) * ACTION_DIM)
    assert _obs_slice(obs, "joint_vel") == pytest.approx(tuple(float(i) for i in range(12)))
    assert _obs_slice(obs, "actions") == pytest.approx(tuple(-float(i) for i in range(12)))


def test_joint_positions_are_relative_to_the_training_stance():
    """The term is ``joint_pos - DEFAULT``: a robot standing in the trained
    stance observes ZERO, not its absolute angles (which is what an absolute
    vector would silently feed the network)."""
    assert _obs_slice(_obs(), "joint_pos") == pytest.approx(ZERO12)


def test_commands_are_passed_through_unscaled_and_unclipped():
    """Training declares scale=None/clip=None for the command term, so a 9 m/s
    request must reach the network as 9.0 — clipping it here would hide a
    misconfigured SUT behind a policy that quietly walks slower than asked."""
    assert _obs_slice(_obs(command=(9.0, -9.0, 42.0)), "velocity_commands") == pytest.approx(
        (9.0, -9.0, 42.0)
    )


def test_wrong_length_joint_vectors_are_rejected_by_name():
    with pytest.raises(ValueError, match="joint_pos: expected 12"):
        _obs(joint_pos=(0.0,) * 11)
    with pytest.raises(ValueError, match="last_actions: expected 12"):
        _obs(last_actions=(0.0,) * 13)


def test_a_layout_that_disagrees_with_the_assembly_raises_instead_of_shifting():
    """OBS_LAYOUT drives the emission, so an edited table cannot silently move
    every downstream slice — this is the guard that makes the table load-bearing."""
    broken = tuple(
        (name, start, stop if name != "velocity_commands" else stop + 1)
        for name, start, stop in OBS_LAYOUT
    )
    with pytest.raises(ValueError, match="OBS_LAYOUT disagrees"):
        with pytest.MonkeyPatch.context() as patch:
            patch.setattr(go2_policy, "OBS_LAYOUT", broken)
            _obs()


# --------------------------------------------------------------------------- #
# action -> joint target.
# --------------------------------------------------------------------------- #
def test_zero_action_holds_the_training_stance():
    assert joint_pos_target(ZERO12) == pytest.approx(DEFAULT_JOINT_POS)


def test_action_is_scaled_by_0_25_and_ADDED_to_the_default_not_multiplied():
    raw = tuple(float(i - 6) for i in range(ACTION_DIM))
    expected = tuple(d + ACTION_SCALE * a for d, a in zip(DEFAULT_JOINT_POS, raw, strict=True))
    assert joint_pos_target(raw) == pytest.approx(expected)
    assert ACTION_SCALE == 0.25


def test_target_rejects_a_wrong_width_action():
    with pytest.raises(ValueError, match="raw_action: expected 12"):
        joint_pos_target((0.0,) * 6)


# --------------------------------------------------------------------------- #
# dc_motor_torque — the AR-6 surface.
# --------------------------------------------------------------------------- #
def _tau(error, speed):
    """Torque of joint 0 for one (position error, joint speed) pair."""
    q = [0.0] * ACTION_DIM
    target = [error] + [0.0] * (ACTION_DIM - 1)
    qdot = [speed] + [0.0] * (ACTION_DIM - 1)
    return dc_motor_torque(target, q, qdot)[0]


def test_inside_the_linear_region_it_is_plain_pd():
    assert _tau(0.1, 0.0) == pytest.approx(KP * 0.1)
    assert _tau(0.1, 1.0) == pytest.approx(KP * 0.1 - KD * 1.0)


def test_torque_is_capped_by_the_continuous_effort_limit_at_zero_speed():
    assert _tau(100.0, 0.0) == pytest.approx(EFFORT_LIMIT)
    assert _tau(-100.0, 0.0) == pytest.approx(-EFFORT_LIMIT)


def test_available_torque_falls_linearly_with_speed_the_dc_motor_way():
    """Half of the no-load speed leaves half the stall torque. THIS is the term
    an implicit-PD implementation does not have (probe §6-2): with drive gains
    (25, 0.5) the joint would still be able to push 23.5 N·m at 15 rad/s."""
    assert _tau(100.0, VELOCITY_LIMIT / 2) == pytest.approx(SATURATION_EFFORT * 0.5)
    assert _tau(100.0, VELOCITY_LIMIT) == pytest.approx(0.0)  # no torque at no-load speed


def test_past_the_no_load_speed_the_motor_can_only_brake():
    """Beyond ``VELOCITY_LIMIT`` the whole window is negative, so even a huge
    positive position error yields a NEGATIVE torque — the model refuses to
    accelerate a joint that is already overspeeding."""
    assert _tau(100.0, 45.0) == pytest.approx(SATURATION_EFFORT * (1 - 45.0 / VELOCITY_LIMIT))
    assert _tau(100.0, VEL_AT_EFFORT_LIM) == pytest.approx(-EFFORT_LIMIT)
    assert _tau(-100.0, -VEL_AT_EFFORT_LIM) == pytest.approx(EFFORT_LIMIT)


def test_the_speed_clip_keeps_the_window_ordered_past_the_clip_point():
    """At ±VEL_AT_EFFORT_LIM the window degenerates to ONE value; without the
    clip it would invert past that point and the answer would depend on which
    bound is applied first. Speeds far beyond the clip must therefore give
    exactly the clipped answer."""
    assert _tau(100.0, 1000.0) == pytest.approx(_tau(100.0, VEL_AT_EFFORT_LIM))
    assert _tau(-100.0, -1000.0) == pytest.approx(_tau(-100.0, -VEL_AT_EFFORT_LIM))


def test_torque_rejects_mismatched_vectors():
    with pytest.raises(ValueError, match="qdot: expected 12"):
        dc_motor_torque(ZERO12, ZERO12, (0.0,) * 4)


# --------------------------------------------------------------------------- #
# load() — the sha gate + the deferred torch import.
# --------------------------------------------------------------------------- #
def test_digest_accepts_both_spellings_and_rejects_anything_else():
    hex64 = "a" * 64
    assert normalize_digest(hex64) == hex64
    assert normalize_digest(f"  SHA256:{'A' * 64}  ") == hex64
    with pytest.raises(PolicyContractError, match="64 hex digits"):
        normalize_digest("a" * 63)
    with pytest.raises(PolicyContractError, match="64 hex digits"):
        normalize_digest("z" * 64)


def test_load_verifies_the_bytes_then_jit_loads_them_on_the_cpu(monkeypatch, tmp_path):
    loop, policy, torch = _loaded(monkeypatch, tmp_path)
    path, digest = _policy_file(tmp_path)
    assert torch.load_calls == [(str(path), "cpu")]  # deterministic device, no GPU sync
    assert ("set_num_threads", 1) in torch.threads  # deterministic reductions
    assert policy.eval_calls == 1
    assert loop.expected_sha256 == digest


def test_a_policy_whose_bytes_are_not_the_declared_ones_is_a_contract_error(monkeypatch, tmp_path):
    """The platform holds no policy and substitutes nothing (plan §1-1): the
    only honest answer to 'these are not the declared bytes' is to refuse, and
    the message names BOTH digests so the operator can tell stale from tampered."""
    path, _digest = _policy_file(tmp_path)
    monkeypatch.setitem(sys.modules, "torch", _fake_torch(_FakePolicy()))
    loop = Go2PolicyLoop(path, "b" * 64)
    with pytest.raises(PolicyContractError, match="sha256 mismatch"):
        loop.load()


def test_a_missing_policy_file_is_a_contract_error_not_a_crash(tmp_path):
    loop = Go2PolicyLoop(tmp_path / "absent.pt", "c" * 64)
    with pytest.raises(PolicyContractError, match="not found"):
        loop.load()


# --------------------------------------------------------------------------- #
# bind() — dof mapping + the explicit-actuator drive-gain zeroing.
# --------------------------------------------------------------------------- #
def test_bind_forces_the_sim_drive_gains_to_zero(monkeypatch, tmp_path):
    """AR-6: the trained actuator is EXPLICIT, so PhysX must contribute nothing.
    Leaving the USD/default drive in place adds a second controller nobody
    modelled, and the only symptom is a robot that walks like it is in syrup."""
    art = _FakeArticulation()
    _ready(monkeypatch, tmp_path, art)
    assert art.gains == [([0.0] * 12, [0.0] * 12)]


def test_bind_maps_by_NAME_so_a_reordered_asset_still_gets_the_right_joint(
    monkeypatch, tmp_path, capsys
):
    """Probe §5 measured 12/12 identical order — this is the defence for the day
    the asset changes, since the observation/action vectors are positional and a
    scrambled mapping produces a robot that fights itself in silence."""
    shuffled = tuple(reversed(JOINT_ORDER))
    art = _FakeArticulation(dof_names=shuffled, joint_pos=[float(i + 1) for i in range(12)])
    loop, policy, _ = _ready(monkeypatch, tmp_path, art)
    assert "differs from the trained order" in capsys.readouterr().out  # loud, not silent

    loop.on_physics_step()
    # dof index of JOINT_ORDER[i] is 11-i, so slot i reads raw value 12-i.
    expected = tuple((12 - i) - DEFAULT_JOINT_POS[i] for i in range(12))
    assert _obs_slice(policy.observations[0], "joint_pos") == pytest.approx(expected)
    # ...and the effort written back is scattered into the ASSET's order.
    torque = dc_motor_torque(DEFAULT_JOINT_POS, [12 - i for i in range(12)], ZERO12)
    assert art.efforts[0] == pytest.approx([torque[11 - i] for i in range(12)])


def test_bind_stays_quiet_when_the_asset_order_is_the_measured_one(monkeypatch, tmp_path, capsys):
    _ready(monkeypatch, tmp_path, _FakeArticulation())
    assert capsys.readouterr().out == ""


def test_bind_refuses_an_articulation_that_is_not_the_trained_joint_set(monkeypatch, tmp_path):
    wrong = (*JOINT_ORDER[:-1], "tail_joint")
    with pytest.raises(RuntimeError, match="do not match the trained Go2 joint set"):
        _ready(monkeypatch, tmp_path, _FakeArticulation(dof_names=wrong))


# --------------------------------------------------------------------------- #
# on_physics_step — decimation, torque cadence, carried state.
# --------------------------------------------------------------------------- #
def test_stepping_before_bind_or_before_load_says_which_one_is_missing(monkeypatch, tmp_path):
    loop, _policy, _torch = _loaded(monkeypatch, tmp_path)
    with pytest.raises(RuntimeError, match="bind"):
        loop.on_physics_step()

    path, digest = _policy_file(tmp_path)
    unloaded = Go2PolicyLoop(path, digest)
    unloaded.bind(_FakeArticulation())
    with pytest.raises(RuntimeError, match="load"):
        unloaded.on_physics_step()


def test_policy_runs_at_the_decimated_rate_while_torque_is_written_every_step(
    monkeypatch, tmp_path
):
    """50 Hz action / 200 Hz torque is the Isaac Lab semantics being mirrored
    (``env.step`` computes one action, ``_apply_action`` runs the actuator for
    each of the ``decimation`` sub-steps). Forwarding every step would be a
    different controller; latching the torque would drop the DC motor's damping
    to a 50 Hz staircase."""
    art = _FakeArticulation()
    loop, policy, _ = _ready(monkeypatch, tmp_path, art)
    for _ in range(2 * DECIMATION):
        loop.on_physics_step()
    assert len(policy.observations) == 2
    assert len(art.efforts) == 2 * DECIMATION


def test_torque_is_recomputed_from_the_CURRENT_joint_state_between_policy_steps(
    monkeypatch, tmp_path
):
    art = _FakeArticulation()
    loop, _policy, _ = _ready(monkeypatch, tmp_path, art)
    loop.on_physics_step()  # policy step: target = stance (action 0)
    first = art.efforts[0]
    assert first == pytest.approx(dc_motor_torque(DEFAULT_JOINT_POS, [0.0] * 12, ZERO12))
    assert first[0] == pytest.approx(KP * DEFAULT_JOINT_POS[0])  # hip: inside the linear region
    assert first[6] == pytest.approx(EFFORT_LIMIT)  # rear thigh: 25 N.m demand, capped

    art.joint_pos[0] = DEFAULT_JOINT_POS[0]  # the joint reached its target
    art.joint_vel[1] = 2.0
    loop.on_physics_step()  # NOT a policy step (decimation)
    assert art.efforts[1][0] == pytest.approx(0.0)
    assert art.efforts[1][1] == pytest.approx(KP * DEFAULT_JOINT_POS[1] - KD * 2.0)


def test_the_observation_carries_the_PREVIOUS_raw_action_and_starts_at_zero(monkeypatch, tmp_path):
    """``actions`` is an observation term: it must be the raw network output of
    the last policy step (pre-scale), and zero on the first one."""
    raw = tuple(0.1 * (i + 1) for i in range(ACTION_DIM))
    loop, policy, _ = _ready(monkeypatch, tmp_path, _FakeArticulation(), actions=(raw,))
    for _ in range(DECIMATION + 1):
        loop.on_physics_step()
    assert _obs_slice(policy.observations[0], "actions") == pytest.approx(ZERO12)
    assert _obs_slice(policy.observations[1], "actions") == pytest.approx(raw)


def test_the_raw_action_becomes_the_joint_target_through_the_trained_offset(monkeypatch, tmp_path):
    raw = tuple(1.0 for _ in range(ACTION_DIM))
    art = _FakeArticulation()
    loop, _policy, _ = _ready(monkeypatch, tmp_path, art, actions=(raw,))
    loop.on_physics_step()
    assert art.efforts[0] == pytest.approx(
        dc_motor_torque(joint_pos_target(raw), [0.0] * 12, ZERO12)
    )


def test_the_latched_command_is_what_the_network_sees(monkeypatch, tmp_path):
    loop, policy, _ = _ready(monkeypatch, tmp_path, _FakeArticulation())
    loop.set_command(0.4, -0.1, 0.7)
    loop.on_physics_step()
    assert _obs_slice(policy.observations[0], "velocity_commands") == pytest.approx(
        (0.4, -0.1, 0.7)
    )


def test_the_observation_is_handed_over_as_a_single_batched_float32_row(monkeypatch, tmp_path):
    loop, policy, _ = _ready(monkeypatch, tmp_path, _FakeArticulation())
    loop.on_physics_step()
    assert len(policy.observations[0]) == OBS_DIM
    assert policy.dtypes == ["float32"]


def test_reset_drops_the_carried_state_so_the_next_sample_starts_clean(monkeypatch, tmp_path):
    """Between batch samples (D-5 repose) the loop must forget the previous
    mission's last action, its command and its step phase — otherwise sample
    i+1's first observation measures sample i."""
    raw = tuple(0.5 for _ in range(ACTION_DIM))
    loop, policy, _ = _ready(monkeypatch, tmp_path, _FakeArticulation(), actions=(raw,))
    loop.set_command(1.0, 1.0, 1.0)
    loop.on_physics_step()
    loop.on_physics_step()  # step counter now mid-decimation

    loop.reset()
    loop.on_physics_step()  # a policy step again, immediately
    assert len(policy.observations) == 2
    assert _obs_slice(policy.observations[1], "actions") == pytest.approx(ZERO12)
    assert _obs_slice(policy.observations[1], "velocity_commands") == pytest.approx((0.0, 0.0, 0.0))


def test_a_policy_with_the_wrong_output_width_is_named_not_broadcast(monkeypatch, tmp_path):
    """A 16-wide (or 45-in) network is the loudest symptom of the wrong
    checkpoint; scattering it into 12 joints would 'work' and walk wrong."""
    art = _FakeArticulation()
    loop, _policy, _ = _ready(monkeypatch, tmp_path, art, actions=((0.0,) * 16,))
    with pytest.raises(RuntimeError, match="policy returned 16 values, expected 12"):
        loop.on_physics_step()


# --------------------------------------------------------------------------- #
# Import surface: this module must cost nothing on the control plane.
# --------------------------------------------------------------------------- #
def test_importing_the_policy_modules_pulls_no_torch_isaac_or_numpy():
    """torch is a RUNNER-IMAGE bundle (probe A3: 2.7.0+cu128), deliberately not a
    pyproject dependency — importing this module on the host must not need it.
    Child process on purpose: this module is already imported in the session."""
    code = (
        "import sys; import cv_infra.runner.go2_policy, cv_infra.runner.go2_constants\n"
        "roots = {'torch', 'isaacsim', 'omni', 'carb', 'pxr', 'rclpy', 'cv2', 'numpy'}\n"
        "print(sorted(m for m in sys.modules if m.split('.')[0] in roots))\n"
    )
    proc = subprocess.run(
        [sys.executable, "-c", code],
        env={k: v for k, v in os.environ.items() if k != "PYTHONPATH"},
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert proc.returncode == 0, proc.stderr
    assert proc.stdout.strip() == "[]"
