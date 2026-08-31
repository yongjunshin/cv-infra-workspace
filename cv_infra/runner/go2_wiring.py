"""Firmware-slot wiring (M2 C2b, plan D-3) — JOB_SPEC pin -> ``Go2PolicyLoop``.

``go2_policy`` owns the control law; this module owns everything AROUND it, i.e.
the three seams a job crosses before a policy can move a robot:

1. **the wire** — the resolved policy path + digest ride the JOB_SPEC as two
   runner-envelope keys (``locomotion_policy_path`` / ``locomotion_policy_sha256``,
   named by M1's C2a report §3), peeled off exactly like ``job_id`` before the
   canonical ``VerificationRequest`` validation sees the document;
2. **the slot** — the scene registry row says which artifacts this ROBOT runs
   onboard (``SceneAsset.firmware_slots``, D-3). A go2 world without a policy and
   a policy without a slot are both bad input, and both are decided PRE-BOOT, at
   0 GPU seconds (exit 2), like every other spec rejection;
3. **the run** — the loop binds to the robot articulation, is driven by the
   physics callback (every step; the loop itself decimates), and takes its
   command from the SUT's ``/cmd_vel`` on the adapter's ONE rclpy node.

The platform holds no policy and substitutes nothing (plan §1-1): the file the
request shipped is hashed again here (``Go2PolicyLoop.load``) and a mismatch is a
rejection, never a fallback.

Everything except the two ROS/Isaac constructor lines is duck-typed and
CPU-tested: ``sim`` needs ``robot_articulation()`` + ``world``, the node needs
``create_subscription``.
"""

from __future__ import annotations

from dataclasses import dataclass

from cv_infra.runner.go2_policy import Go2PolicyLoop, PolicyContractError
from cv_infra.runner.sim_runtime import resolve_scene

#: JOB_SPEC runner-envelope keys carrying the 2nd SUT artifact (D2 2026-08-31).
#: The path is ABSOLUTE and valid INSIDE the runner container: the supervisor
#: ro-mounts the scenario dir at the same absolute path it has on the host
#: (``supervisor._runner_volumes``), and admit resolved the file against exactly
#: that anchor (``contract.loader`` stage 5 -> ``AdmittedRequest`` field), so no
#: consumer of this wire ever re-derives a path — there is ONE resolution site.
POLICY_PATH_KEY = "locomotion_policy_path"
POLICY_SHA_KEY = "locomotion_policy_sha256"

#: The firmware slot name a scene registry row declares for a robot that runs a
#: locomotion policy onboard (``SCENE_ASSETS["go2_warehouse"].firmware_slots``).
LOCOMOTION_SLOT = "locomotion_policy"

#: ``world.add_physics_callback`` name for the policy step. Distinct from the
#: telemetry sampler's (``cv_infra_telemetry``): both are registered on the same
#: World and a shared name would silently replace one with the other.
POLICY_CALLBACK_NAME = "cv_infra_go2_policy"

#: The ``interface.adapter_config.cmd_vel.type`` spellings this runner can drive
#: a policy from. Both are the SAME twist payload — ``TwistStamped`` wraps it in
#: ``.twist`` — and nav2 ships both spellings depending on configuration, so the
#: declared type is honored rather than assumed. Anything else is rejected
#: PRE-BOOT: a subscription created with the wrong type matches no publisher and
#: the robot then stands still with no error at all (G-26).
CMD_VEL_TYPES = ("geometry_msgs/msg/Twist", "geometry_msgs/msg/TwistStamped")


@dataclass(frozen=True)
class PolicyPin:
    """One admitted locomotion-policy artifact: where it is and what it must hash to."""

    path: str
    sha256: str


def policy_pin(spec: dict) -> PolicyPin | None:
    """Read the two policy keys off a JOB_SPEC dict — both or neither.

    Undeclared -> ``None`` (the carter plane's exact behaviour: no slot, no pin,
    nothing happens). Half a pin is rejected loudly: a path without a digest is
    an unpinned SUT artifact (D2 requires both to be recorded), and a digest
    without a path names nothing.
    """
    path = spec.get(POLICY_PATH_KEY)
    sha256 = spec.get(POLICY_SHA_KEY)
    if path is None and sha256 is None:
        return None
    if not path or not sha256:
        raise PolicyContractError(
            f"JOB_SPEC carries only half the locomotion policy pin ({POLICY_PATH_KEY}="
            f"{path!r}, {POLICY_SHA_KEY}={sha256!r}) — the 2nd SUT artifact is pinned by "
            "path AND digest or not declared at all"
        )
    return PolicyPin(str(path), str(sha256))


def scene_firmware_slots(scene_ref: str) -> tuple[str, ...]:
    """Firmware slots the scene's ROBOT declares (``()`` for anything unresolvable).

    A direct ``.usd`` ref carries no registry row (P3 consumer scenes) and an
    unknown scene NAME is a scene-load failure this function must not pre-empt —
    ``load_scene`` owns that message and lists the known scenes. Either way the
    honest answer here is "this scene declares no slots".
    """
    try:
        return resolve_scene(scene_ref).firmware_slots
    except ValueError:
        return ()


def check_firmware_slot(request: object, pin: PolicyPin | None) -> None:
    """Cross-check the scene's declared slots against what the request shipped.

    Both directions are bad input (exit 2), and both are decided pre-boot because
    the alternative is worse than a late error: a go2 world booted without a
    policy has no controller at all — the drive gains of that asset are 0, so the
    robot lies down and every criterion then measures a heap on the floor (C1
    §6-3 measured it), which reads as a SUT failure instead of a missing artifact.

    C2c §6-1 (their measurement, our repair): this used to open with a third,
    "PLANE SKEW" branch keyed on ``request.sut.locomotion_policy``. That field is
    ALWAYS None here — ``runner.main.parse_request`` re-nests only
    ``sut_image_ref``, and ``contract.loader`` never returns a declaration
    without its resolved path (devworld's input) — so the branch could not fire
    in either caller, while the failure it described (the wire lost a declared
    path) surfaces as the slot branch below. The skew is therefore named in THAT
    message instead of in a branch nothing can reach: one reachable rejection
    that lists both causes beats two, one of which is decoration (G-74: name the
    plane, not the symptom).
    """
    slots = scene_firmware_slots(request.scenario.scene)
    if LOCOMOTION_SLOT in slots and pin is None:
        raise PolicyContractError(
            f"scenario.scene {request.scenario.scene!r} declares the {LOCOMOTION_SLOT!r} "
            "firmware slot (this robot runs its locomotion policy onboard) but this "
            f"JOB_SPEC carries no {POLICY_PATH_KEY}. Either the request declares no "
            "sut.locomotion_policy — declare it as {file, sha256} next to the scenario "
            "file — or it does and the submission plane dropped the RESOLVED path from "
            "the wire (contract.job_spec.build_job_spec). The platform holds no policy "
            "and substitutes none"
        )
    if pin is not None and LOCOMOTION_SLOT not in slots:
        raise PolicyContractError(
            f"the request ships sut.locomotion_policy but scenario.scene "
            f"{request.scenario.scene!r} declares no {LOCOMOTION_SLOT!r} slot "
            f"(slots: {list(slots)}) — the platform infers no meaning from a policy "
            "file, it only matches declared slots (D-3)"
        )


def cmd_vel_type_name(declared: str) -> str:
    """Validate ``adapter_config.cmd_vel.type`` and return its message class name.

    Pure, and called PRE-BOOT so an unsupported spelling costs 0 GPU seconds
    instead of surfacing as a robot that never moves.
    """
    if declared not in CMD_VEL_TYPES:
        raise PolicyContractError(
            f"interface.adapter_config.cmd_vel.type {declared!r} cannot drive a locomotion "
            f"policy — this runner subscribes as one of {list(CMD_VEL_TYPES)}. A "
            "subscription of the wrong type matches no publisher and the robot then stands "
            "still with no error at all"
        )
    return declared.rsplit("/", 1)[-1]


def admit_policy_pin(spec: dict, request: object) -> PolicyPin | None:
    """Pre-boot admission of the firmware slot (0 GPU seconds) — the pin, or None.

    Everything except reading the bytes: pin shape -> slot cross-check ->
    ``cmd_vel`` type. Split from ``load_policy`` because a CARRIER admits n specs
    but loads ONE policy (its uniformity row is what makes that correct), while a
    single job does both in one breath. Failures are ``PolicyContractError``,
    which each entrypoint folds into its own bad-input path (exit 2).
    """
    pin = policy_pin(spec)
    check_firmware_slot(request, pin)
    if pin is not None:
        cmd_vel_type_name(request.interface.adapter_config.cmd_vel.type)
    return pin


def load_policy(pin: PolicyPin | None) -> Go2PolicyLoop | None:
    """Build the loop and read the policy bytes (digest re-verified) — pre-boot.

    ``None`` in, ``None`` out: a request that declares no slot never touches
    torch, which is what keeps the carter plane byte-identical.
    """
    if pin is None:
        return None
    loop = Go2PolicyLoop(pin.path, pin.sha256)
    loop.load()
    print(
        f"[cv-runner] locomotion policy loaded: {pin.path} sha256={pin.sha256} "
        f"(slot {LOCOMOTION_SLOT}, in-process on the sim robot — D-3)",
        flush=True,
    )
    return loop


def attach_policy_loop(loop: Go2PolicyLoop, sim: object) -> None:
    """Bind the loop to the robot and drive it from EVERY physics step.

    Call order is the MEASURED one (C2b workstation ``bind`` arm — AR-14 asked
    for both sides and this is the answer): everything here runs AFTER
    ``world.reset()``, because pre-reset the articulation view answers
    ``dof_names = None``, ``initialize()`` raises (no physics sim view yet) and
    — worst of the three — ``set_gains`` returns having done NOTHING, which
    would leave the sim's own PD silently fighting the policy (the exact failure
    AR-6 is ordered to avoid). Post-reset all of it answers.

    Attached BEFORE the readiness barrier on purpose: the barrier pumps the sim
    for up to 180 s of sim time, and an unattached go2 spends all of it lying on
    the floor (its USD drive gains are 0 — C1 §6-3).
    """
    articulation = sim.robot_articulation()
    articulation.initialize()
    loop.bind(articulation)
    sim.world.add_physics_callback(POLICY_CALLBACK_NAME, lambda _step_size: loop.on_physics_step())
    print(
        f"[cv-runner] locomotion policy attached: callback={POLICY_CALLBACK_NAME} "
        f"dof={len(articulation.dof_names)}",
        flush=True,
    )


def subscribe_cmd_vel(node: object, cmd_vel: object, on_command, *, msg_type=None, qos=None):
    """Latch the SUT's ``/cmd_vel`` into ``on_command(vx, vy, wz)``.

    Created on the ADAPTER's node (its public ``node`` seam, p6c3): one rclpy
    node per runner process is the rule, and a second one would be a second DDS
    participant in the job's domain.

    The subscription is BEST_EFFORT, which matches a reliable publisher too, and
    depth 1 — a velocity command is a latest-value signal, so queueing old ones
    would drive the robot with the SUT's past.
    """
    if msg_type is None:  # pragma: no cover - ROS path (bundled jazzy site)
        from geometry_msgs import msg as geometry_msgs  # noqa: PLC0415

        msg_type = getattr(geometry_msgs, cmd_vel_type_name(cmd_vel.type))
    if qos is None:  # pragma: no cover - ROS path
        from rclpy.qos import QoSProfile, ReliabilityPolicy  # noqa: PLC0415

        qos = QoSProfile(depth=1, reliability=ReliabilityPolicy.BEST_EFFORT)

    def on_cmd_vel(message: object) -> None:
        # TwistStamped carries the payload under ``.twist``; Twist IS the payload.
        twist = getattr(message, "twist", message)
        on_command(twist.linear.x, twist.linear.y, twist.angular.z)

    subscription = node.create_subscription(msg_type, cmd_vel.topic, on_cmd_vel, qos)
    print(
        f"[cv-runner] locomotion policy command source: {cmd_vel.topic} ({cmd_vel.type})",
        flush=True,
    )
    return subscription
