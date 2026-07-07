"""CPU unit tests for the M2 runner core (Isaac-independent surface only).

Covers JOB_SPEC parsing, RESULT_OUT resolution + exactly-one result.json write,
verdict/exit mapping, the metric math (path length, sim-time-to-goal, chassis
collision filtering D-E), the two MVP oracles, and the EULA/env guards. GPU wiring
(sim/bridge/telemetry acquisition/recording/DDS) is out of scope this cycle and is
covered on the workstation in cycles 2-6.
"""

import json
import math

import pytest

from cv_infra.runner import evaluate, main, ros_bridge, sim_runtime, telemetry
from cv_infra.runner.telemetry import ContactEvent, PoseSample

CHASSIS = "/World/carter/chassis"


# --------------------------------------------------------------------------- #
# JOB_SPEC / RESULT_OUT I/O glue (D-2).
# --------------------------------------------------------------------------- #
def test_job_spec_inline_json():
    env = {"JOB_SPEC": '{"scene_ref": "s.usd", "acceptance_criteria": {}}'}
    assert main.resolve_job_spec_dict(env)["scene_ref"] == "s.usd"


def test_job_spec_from_file(tmp_path):
    p = tmp_path / "job.json"
    p.write_text(json.dumps({"scene_ref": "file.usd"}), encoding="utf-8")
    assert main.resolve_job_spec_dict({"JOB_SPEC": str(p)})["scene_ref"] == "file.usd"


def test_job_spec_missing_raises_usage():
    with pytest.raises(main.BadJobSpec):
        main.resolve_job_spec_dict({})


def test_job_spec_invalid_json_raises_usage():
    with pytest.raises(main.BadJobSpec):
        main.resolve_job_spec_dict({"JOB_SPEC": "{not json"})


def test_job_spec_non_object_raises_usage():
    with pytest.raises(main.BadJobSpec):
        main.resolve_job_spec_dict({"JOB_SPEC": "[1, 2, 3]"})


def test_result_path_dir_appends_result_json(tmp_path):
    assert main.resolve_result_path({"RESULT_OUT": str(tmp_path)}) == tmp_path / "result.json"


def test_result_path_explicit_json(tmp_path):
    target = tmp_path / "out" / "custom.json"
    assert main.resolve_result_path({"RESULT_OUT": str(target)}) == target


def test_result_path_missing_raises_usage():
    with pytest.raises(main.BadJobSpec):
        main.resolve_result_path({})


def test_write_result_writes_exactly_one_file(tmp_path):
    payload = {"verdict": "pass", "metrics": {"path_len_m": 1.5}}
    out = main.write_result(payload, tmp_path / "result.json")
    assert out.exists()
    assert sorted(q.name for q in tmp_path.iterdir()) == ["result.json"]  # no .tmp leftover
    assert json.loads(out.read_text(encoding="utf-8")) == payload


# --------------------------------------------------------------------------- #
# Verdict / exit-code contract.
# --------------------------------------------------------------------------- #
def test_exit_code_mapping():
    assert main.exit_code_for_verdict("pass") == main.EXIT_PASS
    assert main.exit_code_for_verdict("fail") == main.EXIT_FAIL
    assert main.exit_code_for_verdict("timeout") == main.EXIT_FAIL
    assert main.exit_code_for_verdict("error") == main.EXIT_PLATFORM
    assert main.exit_code_for_verdict("weird") == main.EXIT_PLATFORM


def _outcome(name, passed, reason=""):
    return evaluate.OracleOutcome(name=name, passed=passed, reason=reason)


def test_fold_verdict_pass():
    assert evaluate.fold_verdict([_outcome("a", True), _outcome("b", True)]) == "pass"


def test_fold_verdict_fail():
    assert evaluate.fold_verdict([_outcome("a", True), _outcome("b", False)]) == "fail"


def test_fold_verdict_timeout_promotes():
    outs = [_outcome("reached_goal", False, "timeout"), _outcome("no_collision", True)]
    assert evaluate.fold_verdict(outs) == "timeout"


# --------------------------------------------------------------------------- #
# Metric math (REQ-EXEC-012).
# --------------------------------------------------------------------------- #
def _line(n, dt=1.0):
    return [
        PoseSample(
            sim_time_s=i * dt, position=(float(i), 0.0, 0.0), orientation_wxyz=(1.0, 0.0, 0.0, 0.0)
        )
        for i in range(n)
    ]


def test_path_length():
    assert telemetry.path_length_m(_line(4)) == pytest.approx(3.0)
    assert telemetry.path_length_m([]) == 0.0
    assert telemetry.path_length_m(_line(1)) == 0.0


def test_first_reach_index_and_time_to_goal():
    samples = _line(4)  # positions (0,0,0)..(3,0,0) at t=0..3
    assert telemetry.first_reach_index(samples, (3.0, 0.0, 0.0), 0.1) == 3
    assert telemetry.first_reach_index(samples, (9.0, 0.0, 0.0), 0.1) is None
    assert telemetry.time_to_goal_s(samples, (3.0, 0.0, 0.0), 0.1) == pytest.approx(3.0)
    assert telemetry.time_to_goal_s(samples, (9.0, 0.0, 0.0), 0.1) is None


def test_min_clearance_is_none_in_p2():
    assert telemetry.min_clearance_m() is None


def test_collision_filter_excludes_ground_and_self_counts_obstacle():
    excluded = ["/World/ground", "/World/carter/wheels"]
    events = [
        ContactEvent(0.1, CHASSIS, "/World/ground"),  # ground -> excluded
        ContactEvent(0.2, "/World/carter/wheels/left", CHASSIS),  # self subtree -> excluded
        ContactEvent(0.3, CHASSIS, "/World/obstacle_box"),  # obstacle -> counted
    ]
    assert telemetry.count_real_collisions(events, CHASSIS, excluded) == 1


def test_collision_filter_empty_is_zero():
    assert telemetry.count_real_collisions([], CHASSIS, ["/World/ground"]) == 0


# --------------------------------------------------------------------------- #
# MVP oracles (REQ-EXEC-011).
# --------------------------------------------------------------------------- #
def _record(samples=None, events=None):
    return telemetry.TelemetryRecord(gt_pose_samples=samples or [], contact_events=events or [])


def test_reached_goal_pass():
    from cv_infra.oracles.reached_goal import ReachedGoalOracle

    rec = _record(samples=_line(4))
    criteria = {"goal_position": [3.0, 0.0, 0.0], "position_tolerance_m": 0.1, "timeout_s": 10}
    assert ReachedGoalOracle().evaluate(rec, criteria).passed is True


def test_reached_goal_not_reached_is_fail():
    from cv_infra.oracles.reached_goal import ReachedGoalOracle

    rec = _record(samples=_line(4))
    criteria = {"goal_position": [9.0, 0.0, 0.0], "position_tolerance_m": 0.1, "timeout_s": 10}
    out = ReachedGoalOracle().evaluate(rec, criteria)
    assert out.passed is False and out.reason == "not_reached"


def test_reached_goal_timeout_when_budget_exceeded():
    from cv_infra.oracles.reached_goal import ReachedGoalOracle

    rec = _record(samples=_line(4))  # reaches at sim_time 3
    criteria = {"goal_position": [3.0, 0.0, 0.0], "position_tolerance_m": 0.1, "timeout_s": 1}
    out = ReachedGoalOracle().evaluate(rec, criteria)
    assert out.passed is False and out.reason == "timeout"


def test_reached_goal_validate_requires_goal():
    from cv_infra.oracles.reached_goal import ReachedGoalOracle

    with pytest.raises(ValueError):
        ReachedGoalOracle().validate_params({})


def test_no_collision_negative_normal_drive_passes():
    from cv_infra.oracles.no_collision import NoCollisionOracle

    rec = _record(events=[ContactEvent(0.1, CHASSIS, "/World/ground")])
    criteria = {"chassis_path": CHASSIS, "collision_excluded_paths": ["/World/ground"]}
    assert NoCollisionOracle().evaluate(rec, criteria).passed is True


def test_no_collision_positive_obstacle_fails():
    from cv_infra.oracles.no_collision import NoCollisionOracle

    rec = _record(events=[ContactEvent(0.3, CHASSIS, "/World/obstacle")])
    criteria = {"chassis_path": CHASSIS, "collision_excluded_paths": ["/World/ground"]}
    out = NoCollisionOracle().evaluate(rec, criteria)
    assert out.passed is False and out.reason == "collision"


def test_reached_goal_yaw_helpers():
    from cv_infra.oracles.reached_goal import angle_diff, yaw_from_quat_wxyz

    assert yaw_from_quat_wxyz((1.0, 0.0, 0.0, 0.0)) == pytest.approx(0.0)
    # 90deg about +Z -> quat (cos45, 0, 0, sin45)
    assert yaw_from_quat_wxyz(
        (math.cos(math.pi / 4), 0.0, 0.0, math.sin(math.pi / 4))
    ) == pytest.approx(math.pi / 2)
    assert angle_diff(math.pi + 0.1, -math.pi + 0.1) == pytest.approx(0.0, abs=1e-9)


# --------------------------------------------------------------------------- #
# Result assembly + guards.
# --------------------------------------------------------------------------- #
def test_build_result_dict_shape():
    outs = [_outcome("reached_goal", True), _outcome("no_collision", True)]
    metrics = {"time_to_goal_s": 3.0, "collision_count": 0, "path_len_m": 3.0}
    result = evaluate.build_result_dict("pass", outs, metrics)
    assert result["verdict"] == "pass"
    assert result["metrics"]["min_clearance_m"] is None  # optional in P2
    assert {c["name"] for c in result["criteria"]} == {"reached_goal", "no_collision"}


def test_eula_boot_guard_blocks_without_consent():
    with pytest.raises(sim_runtime.EulaNotAcceptedError):
        sim_runtime.eula_boot_guard({})


def test_eula_boot_guard_allows_with_consent():
    sim_runtime.eula_boot_guard({"ACCEPT_EULA": "Y"})  # no raise


def test_honored_env_reads_injected_isolation_env():
    env = {
        "ROS_DOMAIN_ID": "42",
        "RMW_IMPLEMENTATION": "rmw_fastrtps_cpp",
        "LD_LIBRARY_PATH": "/opt/isaacsim.ros2.bridge/jazzy/lib:/x",
    }
    got = ros_bridge.honored_env(env)
    assert got.ros_domain_id == "42"
    assert got.rmw_implementation == "rmw_fastrtps_cpp"
    assert got.jazzy_on_ld_path is True
