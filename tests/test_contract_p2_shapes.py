"""Phase 2 contract-shape unit tests (M1 / p2-scaffold-and-fixture).

Proves the minimal, non-frozen JOB_SPEC-in / RESULT_OUT-out shapes the single-runner
spine consumes (decision 2026-07-07 D-2): the M2 import surface resolves, and every
model round-trips through from_dict/to_dict without loss. These are CPU-only,
stdlib-only shape checks — the formal pydantic contract + friendly validation are
Phase 3. Stdlib + pytest only.
"""

from __future__ import annotations

from dataclasses import fields

# --- exact M2 import surface (verbatim; if a name is missing, collection fails) ---
from cv_infra.adapter.adapter_schema import Ros2AdapterConfig
from cv_infra.contract.models import (
    VERDICTS,
    AcceptanceCriterion,
    Artifacts,
    CriterionResult,
    Goal,
    Interface,
    Metrics,
    Scenario,
    VerificationRequest,
    VerificationResult,
)

# ---------------------------------------------------------------------------
# fixtures — a realistic JOB_SPEC payload and its RESULT_OUT counterpart
# ---------------------------------------------------------------------------

JOB_SPEC = {
    "job_id": "job-0001",
    "scenario": {
        "scene": "warehouse",
        "robot": "nova_carter",
        "goal": {"x": 5.0, "y": -3.0, "yaw": 1.57, "frame": "map"},
        "seed": 42,
        "timeout_s": 120.0,
    },
    "sut_image_ref": "carter-sut",
    "interface": {
        "type": "ros2",
        # SEAM-2 canonical adapter_config keys (full measured fixture + loud-reject
        # policy live in tests/test_adapter_schema.py).
        "adapter_config": {
            "ros_distro": "jazzy",
            "rmw": "rmw_fastrtps_cpp",
            "use_sim_time": True,
            "goal_interface": {
                "kind": "action",
                "name": "/navigate_to_pose",
                "type": "nav2_msgs/action/NavigateToPose",
            },
            "cmd_vel": {"topic": "/cmd_vel", "type": "geometry_msgs/msg/Twist"},
            "clock_topic": "/clock",
            "odom_topics": ["/odom", "/chassis/odom"],
            "sensors": [
                {
                    "topic": "/front_3d_lidar/lidar_points",
                    "type": "sensor_msgs/msg/PointCloud2",
                    "frame": "front_3d_lidar",
                }
            ],
            "frames": {"map": "map", "odom": "odom", "base_link": "base_link"},
            "readiness": {"is_active_service": "/lifecycle_manager_navigation/is_active"},
        },
    },
    "acceptance_criteria": [
        {"oracle": "reached_goal", "params": {"tolerance_m": 0.5}},
        {"oracle": "no_collision", "params": {}},
    ],
}

RESULT_OUT = {
    "job_id": "job-0001",
    "verdict": "pass",
    "metrics": {
        "time_to_goal_s": 42.5,
        "min_clearance_m": None,  # None allowed in Phase 2 (needs PhysX scene-query)
        "collision_count": 0,
        "path_len_m": 7.3,
    },
    "criteria_results": [
        {"oracle": "reached_goal", "passed": True, "detail": "within 0.5m"},
        {"oracle": "no_collision", "passed": True, "detail": None},
    ],
    "artifacts": {"mcap": None, "mp4": None},
    "request_identity_key": None,
    "origin": None,
    "is_self_test": False,
}


# ---------------------------------------------------------------------------
# (a) VerificationRequest — JOB_SPEC round-trip
# ---------------------------------------------------------------------------


def test_request_from_dict_builds_typed_tree():
    req = VerificationRequest.from_dict(JOB_SPEC)
    assert req.job_id == "job-0001"
    assert req.sut_image_ref == "carter-sut"
    assert isinstance(req.scenario, Scenario)
    assert isinstance(req.scenario.goal, Goal)
    assert req.scenario.goal.frame == "map"
    assert req.scenario.timeout_s == 120.0  # sim-time budget
    assert isinstance(req.interface, Interface)
    assert req.interface.type == "ros2"
    assert isinstance(req.acceptance_criteria[0], AcceptanceCriterion)
    assert req.acceptance_criteria[0].oracle == "reached_goal"
    assert req.acceptance_criteria[0].params == {"tolerance_m": 0.5}


def test_request_dict_round_trips_exactly():
    assert VerificationRequest.from_dict(JOB_SPEC).to_dict() == JOB_SPEC


def test_request_object_round_trips():
    req = VerificationRequest.from_dict(JOB_SPEC)
    assert VerificationRequest.from_dict(req.to_dict()) == req


def test_goal_frame_defaults_to_map():
    goal = Goal.from_dict({"x": 1.0, "y": 2.0, "yaw": 0.0})
    assert goal.frame == "map"


def test_adapter_config_stays_raw_in_contract_interface():
    # Boundary: the contract keeps adapter_config as a plain mapping (contract must
    # not import the adapter package). The consumer builds the typed view.
    req = VerificationRequest.from_dict(JOB_SPEC)
    assert isinstance(req.interface.adapter_config, dict)


# ---------------------------------------------------------------------------
# (b) VerificationResult — RESULT_OUT round-trip (REQ-EXEC-012/013)
# ---------------------------------------------------------------------------


def test_result_from_dict_builds_typed_tree():
    res = VerificationResult.from_dict(RESULT_OUT)
    assert res.job_id == "job-0001"
    assert res.verdict in VERDICTS
    assert isinstance(res.metrics, Metrics)
    assert res.metrics.min_clearance_m is None
    assert res.metrics.collision_count == 0
    assert isinstance(res.criteria_results[0], CriterionResult)
    assert res.criteria_results[0].passed is True
    assert isinstance(res.artifacts, Artifacts)
    assert res.request_identity_key is None  # field only; derivation = M4
    assert res.is_self_test is False


def test_result_dict_round_trips_exactly():
    assert VerificationResult.from_dict(RESULT_OUT).to_dict() == RESULT_OUT


def test_result_object_round_trips():
    res = VerificationResult.from_dict(RESULT_OUT)
    assert VerificationResult.from_dict(res.to_dict()) == res


def test_result_defaults_are_minimal():
    res = VerificationResult(job_id="j", verdict="error")
    assert res.metrics == Metrics()
    assert res.criteria_results == []
    assert res.artifacts == Artifacts()
    assert res.origin is None


def test_verdict_set_is_the_four_locked_values():
    assert set(VERDICTS) == {"pass", "fail", "timeout", "error"}


# ---------------------------------------------------------------------------
# (c) Ros2AdapterConfig — pins, R7 empty SUT-specific defaults, blackbox negative
#     (SEAM-2 canonical round-trip / loud-reject tests = tests/test_adapter_schema.py)
# ---------------------------------------------------------------------------


def test_adapter_config_defaults_track_locked_pins():
    cfg = Ros2AdapterConfig()
    assert cfg.ros_distro == "jazzy"
    assert cfg.rmw == "rmw_fastrtps_cpp"
    assert cfg.use_sim_time is True
    assert cfg.goal_interface.name == "/navigate_to_pose"


def test_adapter_config_sut_specific_wiring_defaults_empty():
    # R7: no hardcoded SUT/scene-specific wiring — odom topics + sensor streams
    # come from the consumer scenario (carter values = measured T2/T3 fixture).
    cfg = Ros2AdapterConfig()
    assert cfg.odom_topics == []
    assert cfg.sensors == []


def test_adapter_config_mutable_defaults_are_not_shared():
    a, b = Ros2AdapterConfig(), Ros2AdapterConfig()
    a.odom_topics.append("/chassis/odom")
    a.goal_interface.name = "/other"
    assert b.odom_topics == []
    assert b.goal_interface.name == "/navigate_to_pose"


def test_adapter_config_round_trips():
    d = Ros2AdapterConfig().to_dict()
    assert Ros2AdapterConfig.from_dict(d).to_dict() == d


def test_adapter_config_has_no_sut_internal_mutation_field():
    # REQ-EXEC-005 blackbox contract: the schema must carry NO field that reaches
    # inside the SUT container. Assert the exact minimal field set + a blacklist.
    names = {f.name for f in fields(Ros2AdapterConfig)}
    assert names == {
        "ros_distro",
        "rmw",
        "use_sim_time",
        "goal_interface",
        "cmd_vel",
        "clock_topic",
        "odom_topics",
        "sensors",
        "frames",
        "readiness",
    }
    forbidden = {
        "sut_command",
        "sut_entrypoint",
        "sut_env",
        "sut_args",
        "command_override",
        "entrypoint_override",
        "patch",
        "inject",
    }
    assert names.isdisjoint(forbidden)
