"""CPU tests for the firmware-slot wiring (M2 C2b) — no torch, no Isaac, no ROS.

``go2_policy`` (C2a) owns the control law and is tested next door; this file
covers everything AROUND it: the JOB_SPEC pin, the scene-slot cross-check, the
pre-boot admission both entrypoints share, the physics-callback attach and the
``/cmd_vel`` subscription. The two collaborators that cannot exist on this host
are injected — ``torch`` as a ``sys.modules`` module object (the C2a lever) and
the articulation / rclpy node as duck-typed recorders.
"""

from __future__ import annotations

import hashlib
import sys
from types import ModuleType, SimpleNamespace

import pytest

from cv_infra.contract.schema import VerificationRequest
from cv_infra.runner import go2_wiring
from cv_infra.runner.go2_policy import PolicyContractError

# --------------------------------------------------------------------------- #
# Fakes / builders.
# --------------------------------------------------------------------------- #
POLICY_BYTES = b"pretend-this-is-torchscript"
POLICY_SHA = hashlib.sha256(POLICY_BYTES).hexdigest()


def _request(scene="go2_warehouse", *, policy=None, cmd_vel_type=None) -> VerificationRequest:
    """A canonical request document, go2 by default (the scene WITH the slot)."""
    sut: dict = {"image_ref": "go2-sut:c2b"}
    if policy is not None:
        sut["locomotion_policy"] = policy
    adapter: dict = {}
    if cmd_vel_type is not None:
        adapter["cmd_vel"] = {"topic": "/cmd_vel", "type": cmd_vel_type}
    return VerificationRequest.model_validate(
        {
            "scenario": {
                "scene": scene,
                "robot": "go2",
                "goal": {"x": 2.0, "y": 0.0, "yaw": 0.0},
                "seed": 11,
                "timeout_s": 60.0,
            },
            "sut": sut,
            "interface": {"type": "ros2", "adapter_config": adapter},
            "acceptance_criteria": [{"oracle": "reached_goal"}],
        }
    )


def _wire_document() -> dict:
    """The JOB_SPEC body ``parse_request`` validates (go2, no nested sut block)."""
    return {
        "scenario": {
            "scene": "go2_warehouse",
            "robot": "go2",
            "goal": {"x": 2.0, "y": 0.0, "yaw": 0.0},
            "seed": 11,
            "timeout_s": 60.0,
        },
        "sut_image_ref": "go2-sut:c2b",
        "interface": {"type": "ros2", "adapter_config": {}},
        "acceptance_criteria": [{"oracle": "reached_goal"}],
    }


def _spec(path="/scn/policy.pt", sha256=POLICY_SHA) -> dict:
    spec: dict = {"job_id": "job-1"}
    if path is not None:
        spec[go2_wiring.POLICY_PATH_KEY] = path
    if sha256 is not None:
        spec[go2_wiring.POLICY_SHA_KEY] = sha256
    return spec


def _fake_torch(policy=None):
    """A ``torch`` MODULE object — attribute access only (no submodule import)."""
    module = ModuleType("torch")
    module.set_num_threads = lambda n: None
    module.jit = SimpleNamespace(load=lambda path, map_location=None: policy)
    return module


class _FakeArticulation:
    """Duck-typed ``SingleArticulation``: it only has to answer ``bind``."""

    def __init__(self) -> None:
        from cv_infra.runner.go2_constants import JOINT_ORDER

        self.dof_names = list(JOINT_ORDER)
        self.initialized = 0
        self.gains: list = []

    def initialize(self) -> None:
        self.initialized += 1

    def get_articulation_controller(self):
        return self

    def set_gains(self, kps=None, kds=None) -> None:
        self.gains.append((list(kps), list(kds)))


class _FakeWorld:
    def __init__(self) -> None:
        self.callbacks: dict = {}

    def add_physics_callback(self, name, callback) -> None:
        self.callbacks[name] = callback


class _FakeSim:
    """Duck-typed ``SimRuntime``: the view accessor + the World."""

    def __init__(self, articulation) -> None:
        self._articulation = articulation
        self.world = _FakeWorld()
        self.views = 0

    def robot_articulation(self):
        self.views += 1
        return self._articulation


class _FakeNode:
    def __init__(self) -> None:
        self.subscriptions: list = []

    def create_subscription(self, msg_type, topic, callback, qos):
        self.subscriptions.append((msg_type, topic, callback, qos))
        return SimpleNamespace(topic=topic)


def _twist(vx=0.0, vy=0.0, wz=0.0):
    return SimpleNamespace(
        linear=SimpleNamespace(x=vx, y=vy, z=0.0),
        angular=SimpleNamespace(x=0.0, y=0.0, z=wz),
    )


# --------------------------------------------------------------------------- #
# policy_pin — the two JOB_SPEC envelope keys.
# --------------------------------------------------------------------------- #
def test_a_spec_without_the_keys_pins_nothing():
    """The carter plane: no keys, no pin, nothing happens (byte-identical path)."""
    assert go2_wiring.policy_pin({"job_id": "job-1"}) is None


def test_the_two_keys_become_one_pin():
    pin = go2_wiring.policy_pin(_spec())
    assert (pin.path, pin.sha256) == ("/scn/policy.pt", POLICY_SHA)


@pytest.mark.parametrize(
    ("path", "sha256"),
    [("/scn/policy.pt", None), (None, POLICY_SHA), ("", POLICY_SHA), ("/scn/policy.pt", "")],
)
def test_half_a_pin_is_refused(path, sha256):
    """A path without a digest is an UNPINNED SUT artifact (D2 records both), and
    a digest without a path names nothing. Empty counts as absent (G-26)."""
    with pytest.raises(PolicyContractError) as excinfo:
        go2_wiring.policy_pin(_spec(path=path, sha256=sha256))
    assert "half the locomotion policy pin" in str(excinfo.value)


# --------------------------------------------------------------------------- #
# scene_firmware_slots / check_firmware_slot — the D-3 cross-check.
# --------------------------------------------------------------------------- #
def test_the_registry_row_is_what_declares_a_slot():
    assert go2_wiring.scene_firmware_slots("go2_warehouse") == ("locomotion_policy",)
    assert go2_wiring.scene_firmware_slots("nova_carter_warehouse") == ()


def test_a_scene_this_runner_cannot_resolve_declares_no_slots():
    """A direct .usd ref (consumer scene) and an unknown NAME both answer "no
    slots" — ``load_scene`` owns the "unknown scene" message, listing the known
    ones, and pre-empting it here would replace a good error with a worse one."""
    assert go2_wiring.scene_firmware_slots("omniverse://assets/whatever.usd") == ()
    assert go2_wiring.scene_firmware_slots("no_such_scene") == ()


def test_a_slotted_scene_without_a_policy_is_refused():
    """C1 §6-3 measured the alternative: the go2 USD ships drive gains of 0, so a
    world booted with no controller lies on the floor and every criterion then
    judges a heap — a SUT-looking failure caused by a missing artifact."""
    with pytest.raises(PolicyContractError) as excinfo:
        go2_wiring.check_firmware_slot(_request(), None)
    message = str(excinfo.value)
    assert "declares the 'locomotion_policy' firmware slot" in message
    assert "sut.locomotion_policy" in message


def test_a_policy_for_a_scene_with_no_such_slot_is_refused():
    pin = go2_wiring.PolicyPin("/scn/policy.pt", POLICY_SHA)
    with pytest.raises(PolicyContractError) as excinfo:
        go2_wiring.check_firmware_slot(_request("nova_carter_warehouse"), pin)
    assert "declares no 'locomotion_policy' slot" in str(excinfo.value)


def test_a_declaration_the_wire_dropped_is_named_as_plane_skew():
    """The request declares a policy but the JOB_SPEC carries no resolved path:
    the honest report names the PLANE (the producer), not "you declared none" —
    and refusing is what stops the job from silently running no policy at all."""
    declared = {"file": "policy.pt", "sha256": POLICY_SHA}
    with pytest.raises(PolicyContractError) as excinfo:
        go2_wiring.check_firmware_slot(_request(policy=declared), None)
    message = str(excinfo.value)
    assert go2_wiring.POLICY_PATH_KEY in message
    assert "build_job_spec" in message


def test_a_carter_request_passes_the_cross_check_untouched():
    """Positive control: the plane that declares nothing must stay silent."""
    go2_wiring.check_firmware_slot(_request("nova_carter_warehouse"), None)


# --------------------------------------------------------------------------- #
# cmd_vel type — validated PRE-BOOT (a wrong type = a robot that never moves).
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize(
    ("declared", "expected"),
    [
        ("geometry_msgs/msg/Twist", "Twist"),
        ("geometry_msgs/msg/TwistStamped", "TwistStamped"),
    ],
)
def test_the_declared_cmd_vel_type_resolves_to_its_message_class_name(declared, expected):
    assert go2_wiring.cmd_vel_type_name(declared) == expected


def test_an_undrivable_cmd_vel_type_is_refused_before_the_boot():
    with pytest.raises(PolicyContractError) as excinfo:
        go2_wiring.cmd_vel_type_name("std_msgs/msg/String")
    assert "cannot drive a locomotion policy" in str(excinfo.value)


# --------------------------------------------------------------------------- #
# admit_policy_pin / load_policy — the pre-boot admission both entrypoints share.
# --------------------------------------------------------------------------- #
def test_admission_returns_the_pin_and_validates_the_command_type():
    pin = go2_wiring.admit_policy_pin(_spec(), _request())
    assert pin.sha256 == POLICY_SHA
    with pytest.raises(PolicyContractError):
        go2_wiring.admit_policy_pin(
            _spec(), _request(cmd_vel_type="ackermann_msgs/msg/AckermannDrive")
        )


def test_a_request_with_no_slot_admits_to_none_and_loads_nothing():
    assert go2_wiring.admit_policy_pin({"job_id": "j"}, _request("nova_carter_warehouse")) is None
    assert go2_wiring.load_policy(None) is None


def test_loading_the_pin_verifies_the_bytes_and_says_so(monkeypatch, tmp_path, capsys):
    path = tmp_path / "policy.pt"
    path.write_bytes(POLICY_BYTES)
    monkeypatch.setitem(sys.modules, "torch", _fake_torch(SimpleNamespace(eval=lambda: None)))
    loop = go2_wiring.load_policy(go2_wiring.PolicyPin(str(path), POLICY_SHA))
    assert loop.expected_sha256 == POLICY_SHA
    out = capsys.readouterr().out
    assert "locomotion policy loaded" in out and POLICY_SHA in out


def test_a_pin_whose_file_does_not_hash_to_the_digest_never_yields_a_loop(monkeypatch, tmp_path):
    """The platform substitutes nothing (plan §1-1) — a mismatch is a refusal."""
    path = tmp_path / "policy.pt"
    path.write_bytes(b"different bytes")
    monkeypatch.setitem(sys.modules, "torch", _fake_torch(SimpleNamespace(eval=lambda: None)))
    with pytest.raises(PolicyContractError):
        go2_wiring.load_policy(go2_wiring.PolicyPin(str(path), POLICY_SHA))


# --------------------------------------------------------------------------- #
# attach_policy_loop — bind + the physics callback.
# --------------------------------------------------------------------------- #
def test_attach_initializes_the_view_binds_and_drives_every_physics_step(
    monkeypatch, tmp_path, capsys
):
    path = tmp_path / "policy.pt"
    path.write_bytes(POLICY_BYTES)
    monkeypatch.setitem(sys.modules, "torch", _fake_torch(SimpleNamespace(eval=lambda: None)))
    loop = go2_wiring.load_policy(go2_wiring.PolicyPin(str(path), POLICY_SHA))
    articulation = _FakeArticulation()
    sim = _FakeSim(articulation)

    go2_wiring.attach_policy_loop(loop, sim)

    assert articulation.initialized == 1  # the physics handshake, once
    assert articulation.gains == [([0.0] * 12, [0.0] * 12)]  # AR-6: sim drive off
    assert sim.views == 1
    # Registered under its OWN name: the telemetry sampler shares this World.
    callback = sim.world.callbacks[go2_wiring.POLICY_CALLBACK_NAME]
    assert go2_wiring.POLICY_CALLBACK_NAME != "cv_infra_telemetry"
    steps = []
    loop.on_physics_step = lambda: steps.append(1)  # the loop itself is C2a's
    callback(0.005)  # the callback ignores step_size (the loop counts STEPS)
    assert steps == [1]
    assert "locomotion policy attached" in capsys.readouterr().out


# --------------------------------------------------------------------------- #
# subscribe_cmd_vel — the SUT's command reaching the loop.
# --------------------------------------------------------------------------- #
def test_the_subscription_latches_twist_into_the_command(capsys):
    node = _FakeNode()
    received: list = []
    cmd_vel = SimpleNamespace(topic="/cmd_vel", type="geometry_msgs/msg/Twist")

    go2_wiring.subscribe_cmd_vel(
        node, cmd_vel, lambda *cmd: received.append(cmd), msg_type=object, qos=1
    )

    msg_type, topic, callback, qos = node.subscriptions[0]
    assert (topic, qos) == ("/cmd_vel", 1)
    callback(_twist(0.5, 0.0, 0.8))
    assert received == [(0.5, 0.0, 0.8)]
    assert "command source: /cmd_vel" in capsys.readouterr().out


def test_the_two_keys_are_peeled_off_before_the_canonical_validation():
    """They are addressed to the RUNNER, like ``job_id`` — the request document
    itself never carries them, and ``extra="forbid"`` would reject the spec if
    they were not popped (control: an unknown key still does)."""
    from cv_infra.runner import main as runner_main

    spec = {**_spec(), **_wire_document()}
    request, adapter_config = runner_main.parse_request(spec)
    assert request.sut.locomotion_policy is None  # the PIN is not a declaration
    assert adapter_config.cmd_vel.topic == "/cmd_vel"
    assert go2_wiring.POLICY_PATH_KEY in spec  # the caller's dict is untouched
    with pytest.raises(runner_main.BadJobSpec):
        runner_main.parse_request({**spec, "locomotion_policy_url": "http://nope"})


def test_a_refused_slot_reaches_the_entrypoints_as_bad_input_not_a_platform_error():
    """Exit-2 family (usage), decided pre-boot: ``BadJobSpec`` is what both
    entrypoints already fold into exit 2, so the fold happens once, in main."""
    from cv_infra.runner import main as runner_main

    request = _request()  # go2: the scene declares the slot
    with pytest.raises(runner_main.BadJobSpec) as excinfo:
        runner_main.admit_firmware_slot({"job_id": "j"}, request)
    assert "firmware slot" in str(excinfo.value)

    missing = go2_wiring.PolicyPin("/nowhere/policy.pt", POLICY_SHA)
    with pytest.raises(runner_main.BadJobSpec) as excinfo:
        runner_main.load_firmware_slot(missing)
    assert "not found" in str(excinfo.value)


def test_a_stamped_twist_is_unwrapped_to_the_same_command():
    """nav2 ships both spellings; TwistStamped carries the payload under .twist,
    so the runner must not read ``linear`` off the envelope (measured contract:
    adapter_config declares which one — CMD_VEL_TYPES)."""
    node = _FakeNode()
    received: list = []
    cmd_vel = SimpleNamespace(topic="/go2/cmd_vel", type="geometry_msgs/msg/TwistStamped")

    go2_wiring.subscribe_cmd_vel(
        node, cmd_vel, lambda *cmd: received.append(cmd), msg_type=object, qos=1
    )

    _, topic, callback, _ = node.subscriptions[0]
    assert topic == "/go2/cmd_vel"
    callback(SimpleNamespace(header=object(), twist=_twist(0.3, -0.1, 0.0)))
    assert received == [(0.3, -0.1, 0.0)]
