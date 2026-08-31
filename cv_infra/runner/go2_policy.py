"""Go2 locomotion policy loop — the robot's own controller, run in-process (M2, D-3).

The Go2's walking policy runs ON THE ROBOT in the real deployment, so the
mirror of that placement is the SIM robot's body = this runner process
(go2-extension-plan §1-2, decision 2026-08-31 D3). It is NOT the SUT: the SUT
container drives this robot the same way it drives the carter — by publishing
``/cmd_vel`` — and never sees a joint.

What this module reproduces is the TRAINING plant, exactly (constants =
``go2_constants``, measured — C0 probe §3):

* 48-D observation assembled in the trained layout, no normalization, no noise;
* action -> joint POSITION TARGET (``default + 0.25 * raw``), 50 Hz;
* torque from an **explicit DC-motor model** at 200 Hz, applied as joint
  EFFORT with the sim drive gains forced to zero.

That last point is the trap AR-6 exists for (probe §6-2): ``DCMotorCfg`` is an
EXPLICIT actuator, so Isaac Lab zeroes the PhysX drive and computes torque in
python every physics step. Implementing this as ``set_joint_position_targets``
+ drive gains (25, 0.5) would look right and BE a different plant — the
speed-dependent torque saturation would simply not exist, and the only symptom
would be "the robot walks badly".

Isaac and torch are DEFERRED imports (torch is bundled in the runner image —
probe A3 measured 2.7.0+cu128 — and is deliberately NOT a pyproject dependency:
the host control plane must never pull it). Everything that decides anything is
a pure function over plain float sequences, so the whole contract is CPU
unit-testable without either. Sequences, not numpy: the CPU test surface must
stay importable in the bundle-independent venv (same discipline as
``telemetry.py``), and 12 floats at 200 Hz is not a place where array math pays.

Determinism (NFR-EXEC-001): all loop state is explicit (step counter, last raw
action, command, joint target), nothing reads a wall clock, and the policy runs
single-threaded on the CPU.
"""

from __future__ import annotations

import hashlib
from collections.abc import Sequence
from pathlib import Path

from cv_infra.runner.go2_constants import (
    ACTION_DIM,
    ACTION_SCALE,
    DECIMATION,
    DEFAULT_JOINT_POS,
    DEFAULT_JOINT_VEL,
    EFFORT_LIMIT,
    GRAVITY_DIRECTION_W,
    JOINT_ORDER,
    KD,
    KP,
    OBS_DIM,
    OBS_LAYOUT,
    SATURATION_EFFORT,
    SIM_DRIVE_DAMPING,
    SIM_DRIVE_STIFFNESS,
    VEL_AT_EFFORT_LIM,
    VELOCITY_LIMIT,
)

Vec3 = tuple[float, float, float]
QuatWXYZ = tuple[float, float, float, float]

#: sha256 is read in chunks — the policy file is ~170 KiB today, but the digest
#: helper must not become a memory cliff if a consumer ships a bigger network.
_HASH_CHUNK = 1 << 20


class PolicyContractError(RuntimeError):
    """The declared locomotion policy is absent or is not the declared bytes.

    A CONTRACT failure, not an infra one: the request said "verify THIS policy"
    and that policy did not arrive (plan §1-1 — the platform holds no policy and
    never substitutes one). Callers map this to ``EXIT_CONTRACT`` (2).
    """


# --------------------------------------------------------------------------- #
# Pure math (CPU unit-test surface) — no torch, no isaacsim, no numpy.
# --------------------------------------------------------------------------- #
def _checked(name: str, values: Sequence[float], size: int) -> tuple[float, ...]:
    """Coerce to a float tuple of exactly ``size``, else raise LOUDLY.

    Every silent failure mode of this module is a length/order mistake (an
    11-joint vector, an obs built from a 235-D env, a policy that returns 16
    numbers). None of them raise on their own — they just make the robot walk
    strangely — so the shape is asserted at every boundary.
    """
    out = tuple(float(v) for v in values)
    if len(out) != size:
        raise ValueError(f"{name}: expected {size} values, got {len(out)}")
    return out


def quat_apply_inverse(quat_wxyz: Sequence[float], vec: Sequence[float]) -> Vec3:
    """Rotate ``vec`` from world frame into the body frame of ``quat_wxyz``.

    Isaac Lab's ``math_utils.quat_apply_inverse`` = ``quat_apply(conj(q), v)``,
    expanded here for a single 3-vector::

        t = -2 * (xyz x v);   result = v + w*t - (xyz x t)

    W-FIRST convention (w, x, y, z) — the same order Isaac's
    ``get_world_pose()`` returns and the same order ``telemetry.PoseSample``
    stores. A caller that hands over an xyzw quaternion gets a silently rotated
    observation, which is why the convention is in the signature's name.
    """
    w, x, y, z = _checked("quat_wxyz", quat_wxyz, 4)
    vx, vy, vz = _checked("vec", vec, 3)
    tx = -2.0 * (y * vz - z * vy)
    ty = -2.0 * (z * vx - x * vz)
    tz = -2.0 * (x * vy - y * vx)
    return (
        vx + w * tx - (y * tz - z * ty),
        vy + w * ty - (z * tx - x * tz),
        vz + w * tz - (x * ty - y * tx),
    )


def assemble_obs(
    *,
    base_quat_wxyz: Sequence[float],
    base_lin_vel_w: Sequence[float],
    base_ang_vel_w: Sequence[float],
    command: Sequence[float],
    joint_pos: Sequence[float],
    joint_vel: Sequence[float],
    last_actions: Sequence[float],
) -> tuple[float, ...]:
    """Build the 48-D observation in the trained layout (``OBS_LAYOUT``).

    Keyword-only on purpose: seven same-shaped vectors in a row is precisely the
    call site where linear/angular or position/velocity get swapped, and the
    swap is invisible afterwards.

    ``joint_pos``/``joint_vel`` arrive ALREADY in ``JOINT_ORDER`` (the loop
    remaps by dof name) and the default offsets are subtracted here. ``command``
    is the raw ``/cmd_vel`` triple — no clipping: the training terms carry
    ``scale=None, clip=None``, and what velocities the SUT is allowed to ask for
    is the SUT's own configuration, not ours (G-107 ③). No observation noise
    either: corruption is a training-time term, off in play/deploy (probe §3).

    The emission is DRIVEN by ``OBS_LAYOUT`` rather than merely documented by
    it, so an edit that makes the table and the assembly disagree raises here
    instead of shifting every slice by three.
    """
    sections = {
        "base_lin_vel": quat_apply_inverse(base_quat_wxyz, base_lin_vel_w),
        "base_ang_vel": quat_apply_inverse(base_quat_wxyz, base_ang_vel_w),
        "projected_gravity": quat_apply_inverse(base_quat_wxyz, GRAVITY_DIRECTION_W),
        "velocity_commands": _checked("command", command, 3),
        "joint_pos": tuple(
            q - d
            for q, d in zip(
                _checked("joint_pos", joint_pos, ACTION_DIM), DEFAULT_JOINT_POS, strict=True
            )
        ),
        "joint_vel": tuple(
            v - d
            for v, d in zip(
                _checked("joint_vel", joint_vel, ACTION_DIM), DEFAULT_JOINT_VEL, strict=True
            )
        ),
        "actions": _checked("last_actions", last_actions, ACTION_DIM),
    }
    obs: list[float] = []
    for name, start, stop in OBS_LAYOUT:
        values = sections[name]
        if len(obs) != start or len(values) != stop - start:
            raise ValueError(
                f"OBS_LAYOUT disagrees with the assembled observation at {name!r}: "
                f"layout says [{start}, {stop}), assembly is at {len(obs)} with "
                f"{len(values)} values"
            )
        obs.extend(values)
    return _checked("obs", obs, OBS_DIM)


def joint_pos_target(raw_action: Sequence[float]) -> tuple[float, ...]:
    """``JointPositionActionCfg``: target = DEFAULT + 0.25 * raw (probe §3).

    ``use_default_offset=True`` is what makes the offset the training stance,
    and there is no output clipping (``clip=None``, ``clip_actions=null``) — an
    exploding network is meant to be visible as an exploding robot, not silently
    squeezed into a plausible pose.
    """
    return tuple(
        default + ACTION_SCALE * raw
        for raw, default in zip(
            _checked("raw_action", raw_action, ACTION_DIM), DEFAULT_JOINT_POS, strict=True
        )
    )


def dc_motor_torque(
    q_target: Sequence[float], q: Sequence[float], qdot: Sequence[float]
) -> tuple[float, ...]:
    """Explicit DC-motor torque, per joint — Isaac Lab ``DCMotor`` (AR-6)::

        tau     = KP*(q_target - q) - KD*qdot
        v       = clip(qdot, -VEL_AT_EFFORT_LIM, +VEL_AT_EFFORT_LIM)
        tau_max = min(SATURATION_EFFORT*( 1 - v/VELOCITY_LIMIT),  EFFORT_LIMIT)
        tau_min = max(SATURATION_EFFORT*(-1 - v/VELOCITY_LIMIT), -EFFORT_LIMIT)
        applied = clip(tau, tau_min, tau_max)

    The speed clip is not cosmetic: it is what keeps the bounds ORDERED. With
    the measured values (S = E = 23.5, V = 30, so the clip is ±60 rad/s) the
    window degenerates to a single value at the extremes — at qdot = +60 the
    motor can only pull -23.5 N·m, at -60 only +23.5 — but tau_min never
    crosses tau_max. Unclipped speeds would invert the window, and then the
    result would depend on which bound is applied first. Clamping order here is
    ``min(max(...))`` — torch/numpy ``clip`` semantics — so even an inverted
    window (only reachable if the constants are re-measured to S != E) resolves
    to tau_max, like the framework this mirrors.
    """
    target = _checked("q_target", q_target, ACTION_DIM)
    pos = _checked("q", q, ACTION_DIM)
    vel = _checked("qdot", qdot, ACTION_DIM)
    applied = []
    for i in range(ACTION_DIM):
        tau = KP * (target[i] - pos[i]) - KD * vel[i]
        v = min(max(vel[i], -VEL_AT_EFFORT_LIM), VEL_AT_EFFORT_LIM)
        tau_max = min(SATURATION_EFFORT * (1.0 - v / VELOCITY_LIMIT), EFFORT_LIMIT)
        tau_min = max(SATURATION_EFFORT * (-1.0 - v / VELOCITY_LIMIT), -EFFORT_LIMIT)
        applied.append(min(max(tau, tau_min), tau_max))
    return tuple(applied)


def normalize_digest(value: str) -> str:
    """Accept ``<64-hex>`` or ``sha256:<64-hex>`` and return the bare lowercase hex.

    The declared digest crosses a team boundary (M1 owns
    ``sut.locomotion_policy``, this module consumes it) and the repository
    already spells digests BOTH ways — ``image_ref`` uses the ``sha256:``
    prefix, the probe report quotes bare hex. Accepting both here is cheaper
    than a cross-team format drift discovered as "policy sha mismatch" on the
    workstation (G-17); anything else is rejected loudly, because a truncated or
    typo'd digest that silently never matches is indistinguishable from a
    tampered policy.
    """
    digest = value.strip().lower().removeprefix("sha256:")
    if len(digest) != 64 or any(c not in "0123456789abcdef" for c in digest):
        raise PolicyContractError(
            "locomotion policy sha256 must be 64 hex digits (optionally 'sha256:'-prefixed), "
            f"got {value!r}"
        )
    return digest


def _file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while chunk := handle.read(_HASH_CHUNK):
            digest.update(chunk)
    return digest.hexdigest()


# --------------------------------------------------------------------------- #
# The loop (Isaac + torch collaborators are INJECTED / deferred).
# --------------------------------------------------------------------------- #
class Go2PolicyLoop:
    """Owns one robot's control loop: 50 Hz policy, 200 Hz torque.

    Wiring (C2b) mirrors ``PhysicsTelemetrySampler``'s two-phase shape, with the
    directions reversed — the sampler READS the articulation, this WRITES it:

    * ``load()`` — any time before the first step (no Isaac needed);
    * ``bind(articulation)`` — after the articulation VIEW is initialized and
      before the first step: it READS ``dof_names`` and WRITES the drive gains,
      so it needs a live view, which places it at ``attach``'s position in the
      sampler's shape, not ``bind``'s. The wrapper's CONSTRUCTION still belongs
      pre-reset (probe-02/03: a view created after the scene's reset-time prim
      churn is already invalidated) — that construction is the caller's, and
      this class only asks for the object. If C2b measures that ``dof_names`` /
      ``set_gains`` answer pre-reset too, the call can move earlier without any
      change here (the loop keeps no reset-sensitive state);
    * ``on_physics_step()`` — from a ``world.add_physics_callback`` adapter.
      Deliberately argument-free: the callback's ``step_size`` is not used at
      all (the loop counts STEPS, not seconds — ``fixed_dt`` is the scenario's
      knob), and taking it would suggest otherwise.

    The articulation is duck-typed (``dof_names``, ``get_world_pose``,
    ``get_linear_velocity``, ``get_angular_velocity``, ``get_joint_positions``,
    ``get_joint_velocities``, ``get_articulation_controller``,
    ``set_joint_efforts``) — every one of those is an existing
    ``isaacsim.core.prims.SingleArticulation`` method (do-not-reinvent), and
    duck-typing is what lets the entire loop be exercised on a CPU with a
    recording fake.

    SURFACED ASSUMPTIONS (unmeasured here — C2b measures them on the GPU, G-107):
    (a) ``get_linear_velocity()`` is taken as the COM linear velocity the
    training term (``root_com_lin_vel_w``) wants; angular velocity is
    frame-independent for a rigid body, so only this one is at risk.
    (b) plain python lists are accepted by ``set_joint_efforts`` /
    ``set_gains`` (numpy backend ``expand_dims`` takes a sequence). If either
    turns out to need arrays, that is a one-line ``np.asarray`` at the call
    site — kept out of here so the loop stays numpy-free on the test plane.
    (c) the drive-gain write is spelled
    ``get_articulation_controller().set_gains(kps=, kds=)``. A wrong spelling
    fails LOUDLY at bind (AttributeError) rather than leaving a second,
    unmodelled PD controller fighting the policy — which is the failure this
    ordering is chosen to avoid.
    """

    def __init__(self, policy_path: str | Path, expected_sha256: str) -> None:
        self.policy_path = Path(policy_path)
        self.expected_sha256 = normalize_digest(expected_sha256)
        self._torch = None
        self._policy = None
        self._articulation = None
        #: JOINT_ORDER slot -> articulation dof index (built in ``bind``).
        self._dof_index: tuple[int, ...] = ()
        self.reset()

    # --- lifecycle ---------------------------------------------------------
    def load(self) -> None:
        """Verify the policy bytes, then TorchScript-load them (deferred import).

        The digest is re-verified HERE even though the contract layer already
        checked it at admission: between the two there is a file copy into the
        job's staging dir, and "the runner ran the bytes it verified" is the
        claim the result's sha pin is supposed to back.
        """
        if not self.policy_path.is_file():
            raise PolicyContractError(
                f"locomotion policy file not found: {self.policy_path} — the platform "
                "holds no policy and never substitutes one (plan §1-1); the request must "
                "ship sut.locomotion_policy.file"
            )
        digest = _file_sha256(self.policy_path)
        if digest != self.expected_sha256:
            raise PolicyContractError(
                f"locomotion policy sha256 mismatch for {self.policy_path}: declared "
                f"{self.expected_sha256}, file is {digest}"
            )
        import torch  # noqa: PLC0415 (runner-image bundle only — never a host dependency)

        # Single-threaded CPU inference: a 3x128 MLP at 50 Hz costs nothing, and
        # both alternatives cost determinism — multi-threaded reductions can
        # reassociate, and a GPU forward adds a host<->device sync inside the
        # physics callback (NFR-EXEC-001).
        torch.set_num_threads(1)
        self._torch = torch
        self._policy = torch.jit.load(str(self.policy_path), map_location="cpu")
        self._policy.eval()

    def bind(self, articulation: object) -> None:
        """Map dof names and force the sim drive gains to zero (live view needed)."""
        names = tuple(str(n) for n in articulation.dof_names)
        if sorted(names) != sorted(JOINT_ORDER):
            raise RuntimeError(
                "articulation dof names do not match the trained Go2 joint set — "
                f"missing {sorted(set(JOINT_ORDER) - set(names))}, "
                f"unexpected {sorted(set(names) - set(JOINT_ORDER))}. The observation "
                "and action vectors are positional, so a mismatched asset would scramble "
                "legs silently (probe §5)"
            )
        self._dof_index = tuple(names.index(joint) for joint in JOINT_ORDER)
        if names != JOINT_ORDER:
            # Measured 12/12 identical on both Go2 USDs (probe §5). Remapping by
            # NAME still works, but a reordered asset is news: it means the
            # robot asset changed under a policy that was trained on the old one.
            print(
                f"[cv-infra][go2-policy] articulation dof order differs from the trained "
                f"order — remapping by name. asset={list(names)} trained={list(JOINT_ORDER)}",
                flush=True,
            )
        # Explicit actuator (AR-6): the PhysX drive must contribute NOTHING, all
        # torque comes from ``dc_motor_torque``. Uniform zeros, so this write is
        # dof-order independent by construction. (The USD's own drive gains
        # measured 0.0 already — probe §5 — this makes it true by assertion
        # rather than by luck.)
        articulation.get_articulation_controller().set_gains(
            kps=[SIM_DRIVE_STIFFNESS] * len(names), kds=[SIM_DRIVE_DAMPING] * len(names)
        )
        self._articulation = articulation

    def reset(self) -> None:
        """Drop the episode's carried state (repose / new sample — D-5).

        ``last_actions`` is an OBSERVATION term, so carrying it across a repose
        would feed the next mission the previous mission's last twitch; the
        counter is reset so the first step of a mission is always a policy step.
        """
        self._command: Vec3 = (0.0, 0.0, 0.0)
        self._last_actions: tuple[float, ...] = (0.0,) * ACTION_DIM
        self._joint_target: tuple[float, ...] = DEFAULT_JOINT_POS
        self._step = 0

    def set_command(self, vx: float, vy: float, wz: float) -> None:
        """Latch the base velocity command (``/cmd_vel`` -> obs slice 9:12)."""
        self._command = (float(vx), float(vy), float(wz))

    # --- the loop ----------------------------------------------------------
    def on_physics_step(self) -> None:
        """One physics step: policy every ``DECIMATION``-th, torque EVERY time.

        This split IS the Isaac Lab semantics being mirrored: ``env.step``
        computes an action once, then ``_apply_action`` runs the actuator model
        on the CURRENT joint state for each of the ``decimation`` physics
        sub-steps. Recomputing the torque every step (rather than latching one)
        is what makes the DC-motor's velocity term a real damping term at 200 Hz
        instead of a 50 Hz staircase.
        """
        if self._articulation is None:
            raise RuntimeError("bind(articulation) must run before stepping")
        if self._policy is None:
            raise RuntimeError("load() must run before stepping — no policy is loaded")

        joint_pos = self._ordered(self._articulation.get_joint_positions())
        joint_vel = self._ordered(self._articulation.get_joint_velocities())
        if self._step % DECIMATION == 0:
            # obs carries the PREVIOUS raw action; overwrite only after reading.
            obs = self._observe(joint_pos, joint_vel)
            self._last_actions = self._forward(obs)
            self._joint_target = joint_pos_target(self._last_actions)
        self._step += 1
        torque = dc_motor_torque(self._joint_target, joint_pos, joint_vel)
        self._articulation.set_joint_efforts(self._scattered(torque))

    # --- internals ---------------------------------------------------------
    def _ordered(self, values: Sequence[float]) -> tuple[float, ...]:
        """Articulation dof order -> JOINT_ORDER."""
        return tuple(float(values[index]) for index in self._dof_index)

    def _scattered(self, values: Sequence[float]) -> list[float]:
        """JOINT_ORDER -> articulation dof order (the order the sim writes back)."""
        efforts = [0.0] * len(self._dof_index)
        for slot, index in enumerate(self._dof_index):
            efforts[index] = values[slot]
        return efforts

    def _observe(self, joint_pos: Sequence[float], joint_vel: Sequence[float]) -> tuple[float, ...]:
        _, quat = self._articulation.get_world_pose()  # GT pose, w-first quaternion
        return assemble_obs(
            base_quat_wxyz=quat,
            base_lin_vel_w=self._articulation.get_linear_velocity(),
            base_ang_vel_w=self._articulation.get_angular_velocity(),
            command=self._command,
            joint_pos=joint_pos,
            joint_vel=joint_vel,
            last_actions=self._last_actions,
        )

    def _forward(self, obs: Sequence[float]) -> tuple[float, ...]:
        """Run the TorchScript actor: (1, 48) -> (1, 12), no grad."""
        torch = self._torch
        with torch.inference_mode():
            out = self._policy(torch.tensor([list(obs)], dtype=torch.float32))
        raw = tuple(float(v) for v in out[0])
        if len(raw) != ACTION_DIM:
            raise RuntimeError(
                f"policy returned {len(raw)} values, expected {ACTION_DIM} — the "
                "declared locomotion_policy slot is obs48/act12 (plan §1-2)"
            )
        return raw
