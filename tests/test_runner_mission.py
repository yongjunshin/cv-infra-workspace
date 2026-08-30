"""CPU unit tests for the cycle-5 mission-drive pure surface (no Isaac/rclpy).

Covers the scene mapping table (sim_runtime), the adapter's pure sequencing/math
(readiness order G-19, goal quaternion, Nav2 status mapping, param-service
derivation), and the deferred-import structural invariant. The rclpy/Isaac
bodies themselves are T3 (workstation) scope — no GPU claim is made here.
"""

import math
import sys
from pathlib import Path

import pytest

from cv_infra.contract.adapter_schema import Ros2AdapterConfig
from cv_infra.runner import sim_runtime
from cv_infra.runner.adapter import ros2


# --------------------------------------------------------------------------- #
# Scene mapping (scenario.scene -> sample asset ref) — REQ-EXEC-002, reuse pin.
# --------------------------------------------------------------------------- #
def test_scene_mapping_resolves_carter_warehouse_sample():
    asset = sim_runtime.resolve_scene("nova_carter_warehouse")
    # do-not-reinvent: the official carter_warehouse_navigation sample, not a
    # self-authored scene.
    assert asset.scene_usd == "/Isaac/Samples/ROS2/Scenario/carter_warehouse_navigation.usd"
    assert asset.robot_prim_candidates  # pre-wired robot expected in the sample


def test_scene_mapping_direct_usd_ref_passes_through():
    asset = sim_runtime.resolve_scene("omniverse://assets/warehouse.usd")
    assert asset.scene_usd == "omniverse://assets/warehouse.usd"
    assert asset.robot_prim_candidates == ()


def test_scene_mapping_unknown_name_is_loud():
    with pytest.raises(ValueError) as excinfo:
        sim_runtime.resolve_scene("mars_base")
    assert "nova_carter_warehouse" in str(excinfo.value)  # lists known scenes


def test_is_direct_usd_ref():
    assert not sim_runtime.is_direct_usd_ref("/Isaac/Samples/ROS2/Scenario/x.usd")
    assert sim_runtime.is_direct_usd_ref("omniverse://assets/warehouse.usd")
    assert sim_runtime.is_direct_usd_ref("https://assets/warehouse.usd")
    assert sim_runtime.is_direct_usd_ref("/mnt/scenes/warehouse.usd")


def test_asset_url_passes_a_direct_ref_through_untouched():
    """The CPU-reachable arm of the assets-root join: a direct ref needs no root.

    (The join itself imports isaacsim and stays GPU-only; what is testable here
    is that a consumer-supplied URL/path is handed on verbatim, whatever the
    caller is resolving.)
    """
    for ref in ("omniverse://assets/warehouse.usd", "https://a/w.usd", "/mnt/scenes/w.usd"):
        assert sim_runtime._asset_url(ref, "sample scene") == ref
        assert sim_runtime._asset_url(ref, "obstacle asset") == ref


def test_the_assets_root_join_has_exactly_one_home():
    """p8c1 dedup (G-17/G-25 anti-drift): scene and prop join through ONE function.

    Before this cycle ``load_scene`` and ``SimRuntime._asset_url`` each spelled
    the join AND the operator-facing unreachable-root message; the copies already
    differed only in the noun. A re-inlined ``get_assets_root_path()`` join is
    how they would drift again, so the call site is counted here.
    """
    source = Path(sim_runtime.__file__).read_text(encoding="utf-8")
    assert source.count("get_assets_root_path()") == 1
    assert source.count("Isaac assets root unreachable") == 1


# --------------------------------------------------------------------------- #
# Goal math + Nav2 status mapping (drive_mission pure surface).
# --------------------------------------------------------------------------- #
def test_quat_z_w_from_yaw_identity_and_half_turn():
    assert ros2.quat_z_w_from_yaw(0.0) == pytest.approx((0.0, 1.0))
    qz, qw = ros2.quat_z_w_from_yaw(math.pi)
    assert (qz, qw) == pytest.approx((1.0, 0.0), abs=1e-9)


def test_quat_z_w_roundtrips_with_oracle_yaw():
    from cv_infra.oracles.reached_goal import yaw_from_quat_wxyz

    for yaw in (0.3, 1.5708, -2.0):
        qz, qw = ros2.quat_z_w_from_yaw(yaw)
        assert yaw_from_quat_wxyz((qw, 0.0, 0.0, qz)) == pytest.approx(yaw)


def test_nav_status_str_terminal_codes():
    assert ros2.nav_status_str(4) == "succeeded"
    assert ros2.nav_status_str(5) == "canceled"
    assert ros2.nav_status_str(6) == "aborted"
    assert ros2.nav_status_str(None) == "unknown"
    assert ros2.nav_status_str(99) == "status_99"


def test_get_parameters_service_derivation():
    assert (
        ros2.get_parameters_service_for("/lifecycle_manager_navigation/is_active")
        == "/lifecycle_manager_navigation/get_parameters"
    )


# --------------------------------------------------------------------------- #
# Readiness barrier order (REQ-EXEC-007, G-19: clock FLOW before the gate).
# --------------------------------------------------------------------------- #
class _Clockwork:
    """Fake probes with a call log + a fake monotonic clock."""

    def __init__(self, clock_after: int | None, active_after: int | None):
        self.calls: list[str] = []
        self.t = 0.0
        self._clock_after = clock_after
        self._active_after = active_after
        self._clock_polls = 0
        self._active_polls = 0

    def clock_flowing(self) -> bool:
        self.calls.append("clock")
        self._clock_polls += 1
        return self._clock_after is not None and self._clock_polls > self._clock_after

    def sut_active(self) -> bool:
        self.calls.append("active")
        self._active_polls += 1
        return self._active_after is not None and self._active_polls > self._active_after

    def now(self) -> float:
        return self.t

    def wait(self) -> None:
        self.calls.append("wait")  # = step-and-spin (sim must keep stepping)
        self.t += 1.0


def test_readiness_clock_flows_before_gate_is_probed():
    fake = _Clockwork(clock_after=3, active_after=2)
    ok, phase = ros2.readiness_sequence(
        fake.clock_flowing, fake.sut_active, fake.now, deadline=100.0, wait=fake.wait
    )
    assert (ok, phase) == (True, "ready")
    # G-19 order: no gate probe may precede the last pre-flow clock probe.
    first_active = fake.calls.index("active")
    last_clock = len(fake.calls) - 1 - fake.calls[::-1].index("clock")
    assert last_clock < first_active
    assert "wait" in fake.calls  # the barrier keeps stepping (the sim IS /clock)


def test_readiness_timeout_in_clock_phase_never_probes_gate():
    fake = _Clockwork(clock_after=None, active_after=0)
    ok, phase = ros2.readiness_sequence(
        fake.clock_flowing, fake.sut_active, fake.now, deadline=5.0, wait=fake.wait
    )
    assert (ok, phase) == (False, "clock")
    assert "active" not in fake.calls


def test_readiness_timeout_in_active_phase():
    fake = _Clockwork(clock_after=0, active_after=None)
    ok, phase = ros2.readiness_sequence(
        fake.clock_flowing, fake.sut_active, fake.now, deadline=5.0, wait=fake.wait
    )
    assert (ok, phase) == (False, "active")


# --------------------------------------------------------------------------- #
# Adapter CPU-safe surface (no rclpy import before wire()).
# --------------------------------------------------------------------------- #
def test_adapter_accepts_stepper_and_starts_cold():
    steps: list[int] = []
    adapter = ros2.Ros2Adapter(Ros2AdapterConfig(), stepper=lambda: steps.append(1))
    assert adapter.clock_count == 0
    assert adapter.sim_time_s == 0.0


def test_adapter_teardown_is_cpu_safe_when_unwired():
    adapter = ros2.Ros2Adapter()
    adapter.teardown()  # must not import/require rclpy (main calls it in finally)


def test_mission_outcome_shape():
    outcome = ros2.MissionOutcome("timeout", None, 12.5)
    assert (outcome.status, outcome.nav_status_code) == ("timeout", None)
    assert outcome.sim_time_elapsed_s == 12.5


# --------------------------------------------------------------------------- #
# Relay fidelity: the step-and-spin budget + its measurement (G-63).
# --------------------------------------------------------------------------- #
class _FakeRclpy:
    """Records spin_once calls — the rclpy seam the adapter holds after wire()."""

    def __init__(self) -> None:
        self.spins: list[tuple[object, float]] = []

    def spin_once(self, node, timeout_sec=None) -> None:
        self.spins.append((node, timeout_sec))


def _spun(adapter) -> _FakeRclpy:
    """Put the adapter in the post-wire() state without rclpy (private by design:
    ``wire`` imports the bundled jazzy rclpy, which does not exist on CPU)."""
    fake = _FakeRclpy()
    adapter._rclpy = fake
    adapter._node = object()
    return fake


def test_step_and_spin_drains_a_bounded_batch_not_a_single_callback():
    """One spin_once = one callback, so a 1-per-step spin starves subscriptions:
    measured p5c13, the /odom relay passed 24.9 % of the sim's stream (G-63)."""
    steps: list[int] = []
    adapter = ros2.Ros2Adapter(Ros2AdapterConfig(), stepper=lambda: steps.append(1))
    fake = _spun(adapter)

    adapter._step_and_spin()

    assert len(steps) == 1  # still exactly ONE sim step per call (the /clock source)
    assert len(fake.spins) == ros2.SPIN_CALLBACK_BUDGET_PER_STEP
    assert 1 < ros2.SPIN_CALLBACK_BUDGET_PER_STEP <= 64  # drains more than one, stays bounded
    assert {timeout for _, timeout in fake.spins} == {0.0}  # non-blocking: never eats the step


def test_relay_odom_counts_every_message_it_fans_out():
    """The relay body itself: count first, then publish to EVERY target."""

    class _Pub:
        def __init__(self) -> None:
            self.got: list[object] = []

        def publish(self, msg) -> None:
            self.got.append(msg)

    adapter = ros2.Ros2Adapter()
    pubs = [_Pub(), _Pub()]
    first, second = object(), object()
    adapter._relay_odom(first, pubs)
    adapter._relay_odom(second, pubs)

    assert adapter._odom_relayed == 2  # what the fidelity line reports
    assert all(pub.got == [first, second] for pub in pubs)  # dualization: BOTH targets


def test_odom_relay_fidelity_line_reports_the_ratio_against_clock():
    # The p5c13 measurement itself (1800 relayed against 7210 clock messages).
    line = ros2.odom_relay_fidelity_line(1800, 1, 7210)
    assert "relayed=1800" in line and "targets=1" in line and "clock_msgs=7210" in line
    assert "ratio_vs_clock=0.250" in line
    assert "ratio_vs_clock=n/a" in ros2.odom_relay_fidelity_line(0, 0, 0)  # never a ZeroDivision


def test_teardown_reports_relay_fidelity_only_when_a_relay_was_wired(capsys):
    adapter = ros2.Ros2Adapter()
    adapter.teardown()
    assert "odom relay fidelity" not in capsys.readouterr().out  # no relay -> no claim

    relayed = ros2.Ros2Adapter()
    relayed._odom_relay = ("subscription", ["publisher"])  # post-_ensure_odom_fanout state
    relayed._odom_relayed, relayed._odom_targets, relayed._clock_count = 150, 1, 600
    relayed.teardown()
    assert "ratio_vs_clock=0.250" in capsys.readouterr().out


# --------------------------------------------------------------------------- #
# Structural invariant: importing the runner NEVER pulls Isaac/ROS on CPU.
# --------------------------------------------------------------------------- #
def test_runner_imports_stay_isaac_free():
    import cv_infra.runner.adapter.ros2  # noqa: F401
    import cv_infra.runner.main  # noqa: F401
    import cv_infra.runner.recording  # noqa: F401
    import cv_infra.runner.ros_bridge  # noqa: F401
    import cv_infra.runner.sim_runtime  # noqa: F401
    import cv_infra.runner.telemetry  # noqa: F401

    for forbidden in ("isaacsim", "omni", "pxr", "rclpy", "cv2", "numpy"):
        assert forbidden not in sys.modules, f"{forbidden} leaked into a CPU import"


# --------------------------------------------------------------------------- #
# p8c2 — the pump seam the realigner shares, and the node it shares it through.
# --------------------------------------------------------------------------- #
def test_node_is_a_public_seam_that_reads_none_before_wire():
    """``runner.realign`` creates its publisher/clients on THIS node (one node per
    runner process = one DDS participant per job). Before ``wire`` there is none,
    and the realigner's ``node is None`` arm reports that instead of crashing —
    so the property must answer None rather than raise or auto-create."""
    assert ros2.Ros2Adapter().node is None


def test_public_step_and_spin_is_the_same_pump_the_internal_callers_use():
    """p6c3 exposed the pump so collaborators (batch's realign/settle waits) stop
    reaching into ``_step_and_spin``. Public and private must stay ONE behavior:
    a public alias that drifted would give the carrier a wait that steps the sim a
    different number of times than the barrier does."""
    steps: list[int] = []
    adapter = ros2.Ros2Adapter(Ros2AdapterConfig(), stepper=lambda: steps.append(1))
    fake = _spun(adapter)

    adapter.step_and_spin()

    assert len(steps) == 1  # exactly ONE sim step per pump (the /clock source)
    assert len(fake.spins) == ros2.SPIN_CALLBACK_BUDGET_PER_STEP


def test_pumping_without_a_stepper_still_drains_callbacks():
    """The stepper is an OPTIONAL dependency (both entrypoints inject one; the
    adapter is constructible without it — see the teardown test above). Every
    public method has to stay safe in that state, so the pump degrades to a pure
    rclpy drain rather than raising ``TypeError: 'NoneType' is not callable``
    inside a readiness loop."""
    adapter = ros2.Ros2Adapter()  # no stepper
    fake = _spun(adapter)

    adapter.step_and_spin()

    assert len(fake.spins) == ros2.SPIN_CALLBACK_BUDGET_PER_STEP
