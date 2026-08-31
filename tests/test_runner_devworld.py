"""CPU tests for the dev-world entry point (M2, D-7) — no Isaac, no ROS, no GPU.

The dev world's whole claim is "the same world, admitted the same way": a
scenario that boots here is one CI will accept, and a scenario CI rejects fails
here first, at exit 2, before a GPU second is spent. That claim lives entirely in
the CPU half tested below (argument parsing, the M1 admission gate, the policy
slot, the loop predicate); the GPU half is the same ``sim_runtime`` /
``go2_sensors`` / ``go2_policy`` code a job runs, verified on the workstation.
"""

from __future__ import annotations

import hashlib
from pathlib import Path

import pytest

from cv_infra.runner import devworld
from cv_infra.runner.devworld import (
    DevWorldUsage,
    admit,
    banner,
    main,
    parse_args,
    policy_pin_for,
    should_stop,
)
from cv_infra.runner.main import EXIT_PLATFORM, EXIT_USAGE
from cv_infra.runner.sim_runtime import EulaNotAcceptedError

POLICY_BYTES = b"not a TorchScript file - the loader only ever hashes these bytes\n"
POLICY_SHA = hashlib.sha256(POLICY_BYTES).hexdigest()

GO2_DOC = """\
apiVersion: cv-infra/v1
scenario:
  scene: go2_warehouse
  robot: go2
  initial_pose: {{x: -6.0, y: -1.0, yaw: 0.0}}
  goal: {{x: -3.5, y: -1.0, yaw: 0.0}}
  seed: 11
  timeout_s: 60
sut:
  image_ref: ghcr.io/example/go2-app@sha256:{digest}
{policy}interface:
  type: ros2
  adapter_config:
    odom_topics: [/odom]
    sensors:
      - {{topic: /camera/image_raw, type: sensor_msgs/msg/Image}}
      - {{topic: /scan, type: sensor_msgs/msg/LaserScan}}
acceptance_criteria:
  - oracle: reached_goal
    params:
      position_tolerance_m: 0.5
  - oracle: no_collision
    params:
      chassis_path: /World/Go2/base
      collision_excluded_paths:
        - /World/GroundPlane/collisionPlane
"""

POLICY_BLOCK = "  locomotion_policy:\n    file: policy.pt\n    sha256: {sha}\n"


def _scenario(tmp_path: Path, *, with_policy: bool = True) -> Path:
    path = tmp_path / "go2_devworld.yaml"
    path.write_text(
        GO2_DOC.format(
            digest="a" * 64,
            policy=POLICY_BLOCK.format(sha=POLICY_SHA) if with_policy else "",
        ),
        encoding="utf-8",
    )
    (tmp_path / "policy.pt").write_bytes(POLICY_BYTES)
    return path


# --------------------------------------------------------------------------- #
# Argument parsing.
# --------------------------------------------------------------------------- #
def test_one_scenario_file_is_the_whole_command_line():
    args = parse_args(["scenarios/go2.yaml"])
    assert args.scenario == "scenarios/go2.yaml"
    assert args.max_steps == 0  # 0 = until Ctrl-C


def test_max_steps_bounds_the_loop_for_an_unattended_smoke_run():
    assert parse_args(["s.yaml", "--max-steps", "400"]).max_steps == 400


@pytest.mark.parametrize(
    ("argv", "message"),
    [
        ([], "expected exactly one scenario file"),
        (["a.yaml", "b.yaml"], "expected exactly one scenario file"),
        (["s.yaml", "--max-steps"], "--max-steps needs a value"),
        (["s.yaml", "--max-steps", "soon"], "--max-steps must be an integer"),
        (["s.yaml", "--max-steps", "-3"], "--max-steps must be >= 0"),
        (["s.yaml", "--record"], "unknown option"),
    ],
)
def test_a_malformed_command_line_is_a_friendly_usage_error(argv, message):
    with pytest.raises(DevWorldUsage, match=message):
        parse_args(argv)


# --------------------------------------------------------------------------- #
# Admission — the SAME gate a submitted job passes.
# --------------------------------------------------------------------------- #
def test_a_valid_go2_scenario_is_admitted_with_its_oracles_bound(tmp_path):
    admitted = admit(str(_scenario(tmp_path)))
    assert admitted.admitted is True
    assert admitted.oracles == ("reached_goal", "no_collision")
    assert admitted.request.scenario.scene == "go2_warehouse"
    assert [s.topic for s in admitted.request.interface.adapter_config.sensors] == [
        "/camera/image_raw",
        "/scan",
    ]


def test_a_rejected_scenario_never_reaches_the_gpu(tmp_path):
    """Same rejection, same exit code as ``cv-infra run``: the dev world is not a
    softer gate, or "it worked in my dev world" would stop meaning anything."""
    bad = tmp_path / "bad.yaml"
    bad.write_text("apiVersion: cv-infra/v1\nscenario: {scene: go2_warehouse}\n", encoding="utf-8")
    with pytest.raises(DevWorldUsage):
        admit(str(bad))


def test_a_missing_scenario_file_is_a_usage_error_not_a_crash(tmp_path):
    with pytest.raises(DevWorldUsage, match="readable YAML request file"):
        admit(str(tmp_path / "absent.yaml"))


def test_a_policy_whose_digest_does_not_match_is_rejected_at_admission(tmp_path):
    """The dev world runs the policy the request DECLARES, so the same sha gate
    applies (D2: the platform holds no policy and substitutes nothing)."""
    path = _scenario(tmp_path)
    (tmp_path / "policy.pt").write_bytes(b"different bytes entirely")
    with pytest.raises(DevWorldUsage, match="sha256"):
        admit(str(path))


# --------------------------------------------------------------------------- #
# Policy slot.
# --------------------------------------------------------------------------- #
def test_the_policy_pin_is_the_loaders_resolved_path_and_declared_digest(tmp_path):
    pin = policy_pin_for(admit(str(_scenario(tmp_path))))
    assert (pin.path, pin.sha256) == (str(tmp_path / "policy.pt"), POLICY_SHA)


def test_a_go2_scenario_without_a_policy_is_refused_by_the_slot_check(tmp_path):
    """C2b's cross-check, reused: the go2 registry row declares a
    locomotion_policy slot, and a world booted without one stands up a robot
    whose drive gains are 0 — it lies down and every criterion then measures a
    heap on the floor (C1 §6-3)."""
    with pytest.raises(DevWorldUsage, match="locomotion_policy"):
        policy_pin_for(admit(str(_scenario(tmp_path, with_policy=False))))


def test_a_scene_that_declares_no_slot_runs_the_world_without_a_policy(tmp_path):
    """carter-shaped scenarios still boot the dev world — they just do not walk."""
    path = tmp_path / "carter.yaml"
    path.write_text(
        GO2_DOC.format(digest="a" * 64, policy="").replace(
            "scene: go2_warehouse", "scene: nova_carter_warehouse"
        ),
        encoding="utf-8",
    )
    assert policy_pin_for(admit(str(path))) is None


# --------------------------------------------------------------------------- #
# Loop predicate + banner.
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize(
    ("steps", "max_steps", "stop_requested", "expected"),
    [
        (0, 0, False, False),  # unbounded, still running
        (10**9, 0, False, False),  # unbounded means unbounded
        (0, 0, True, True),  # Ctrl-C always wins
        (399, 400, False, False),
        (400, 400, False, True),
        (10, 400, True, True),
    ],
)
def test_the_loop_stops_on_ctrl_c_or_the_step_bound(steps, max_steps, stop_requested, expected):
    assert should_stop(steps, max_steps, stop_requested) is expected


def test_the_banner_names_the_world_the_policy_and_how_to_drive_it(tmp_path):
    admitted = admit(str(_scenario(tmp_path)))
    lines = banner(admitted, policy_pin_for(admitted))
    text = "\n".join(lines)
    assert "no mission, no oracle, no recording" in text
    assert "scene=go2_warehouse" in text
    assert "policy.pt" in text
    assert "/cmd_vel" in text and "Ctrl-C" in text


def test_the_banner_says_none_when_no_policy_is_declared(tmp_path):
    admitted = admit(str(_scenario(tmp_path, with_policy=False)))
    assert "locomotion_policy=none" in "\n".join(banner(admitted, None))


# --------------------------------------------------------------------------- #
# main() — exit-code mapping.
# --------------------------------------------------------------------------- #
def test_main_maps_a_rejected_scenario_to_exit_2(tmp_path, capsys):
    assert main([str(tmp_path / "nope.yaml")]) == EXIT_USAGE
    assert "[cv-devworld]" in capsys.readouterr().err


def test_main_maps_a_bad_command_line_to_exit_2(capsys):
    assert main([]) == EXIT_USAGE
    assert "expected exactly one scenario file" in capsys.readouterr().err


def test_main_hands_the_admitted_request_and_the_step_bound_to_the_run(tmp_path, monkeypatch):
    seen = {}

    def _fake_run(admitted, pin, max_steps):
        seen["scene"] = admitted.request.scenario.scene
        seen["policy"] = pin.path
        seen["max_steps"] = max_steps
        return 0

    monkeypatch.setattr(devworld, "run", _fake_run)
    assert main([str(_scenario(tmp_path)), "--max-steps", "7"]) == 0
    assert seen == {
        "scene": "go2_warehouse",
        "policy": str(tmp_path / "policy.pt"),
        "max_steps": 7,
    }


def test_main_reads_sys_argv_when_called_with_no_arguments(tmp_path, monkeypatch):
    monkeypatch.setattr(devworld, "run", lambda _admitted, _pin, _max_steps: 0)
    monkeypatch.setattr("sys.argv", ["devworld", str(_scenario(tmp_path))])
    assert main() == 0


def test_a_world_booted_without_operator_consent_exits_3(tmp_path, monkeypatch, capsys):
    """NEG-2: the EULA gate is the one platform failure the dev world translates
    instead of letting the traceback through — an operator meets it constantly."""

    def _no_consent(_admitted, _pin, _max_steps):
        raise EulaNotAcceptedError("EULA not accepted for this run")

    monkeypatch.setattr(devworld, "run", _no_consent)
    assert main([str(_scenario(tmp_path))]) == EXIT_PLATFORM
    assert "EULA not accepted" in capsys.readouterr().err
