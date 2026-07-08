"""Runner entrypoint — one job -> exactly one result.json (M2, REQ-EXEC-013/015).

Data-plane workload run by M3 inside an isaac-sim:5.1.0 container as
``./python.sh -m cv_infra.runner.main``. It reads the job from ``JOB_SPEC`` (path to,
or inline JSON of, a VerificationRequest dict), runs the fixed M2 §3.2 sequence
(boot -> wire -> readiness -> drive -> telemetry -> evaluate), and writes EXACTLY
one ``result.json`` to ``RESULT_OUT`` (REQ-EXEC-013, D-2). The image carries no
state/result (stateless — volume mounts, DoD-P2-10 direction).

The runner holds NO docker.sock and creates NO network/domain: ``ros_domain_id`` /
``network`` are injected by M3 and only *honored* (see ros_bridge). The GPU pipeline
(``run``) is deferred (Isaac imports inside the stubs); the JOB_SPEC/RESULT_OUT I/O,
exit-code mapping, and result assembly below are Isaac-independent and CPU-tested.

Exit-code contract (0/1/2/3 — LOCKED §9): 0=pass, 1=fail/timeout (mission not met;
the fine-grained verdict is retained in result.json), 2=bad JOB_SPEC/usage,
3=platform (EULA missing, runner crash, verdict=error).
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

from cv_infra.adapter.adapter_schema import Ros2AdapterConfig
from cv_infra.contract.models import VerificationRequest
from cv_infra.runner.evaluate import (
    VERDICT_ERROR,
    EvaluationEngine,
    build_result_dict,
    read_field,
)
from cv_infra.runner.sim_runtime import EulaNotAcceptedError

EXIT_PASS = 0
EXIT_FAIL = 1
EXIT_USAGE = 2
EXIT_PLATFORM = 3

# Readiness barrier budget (M2 runtime policy, NOT consumer contract): the measured
# nav2 lifecycle bringup window is 60s and ``Aborting bringup`` is terminal (G-19);
# SUT start-order/restart policy is M3's. Deliberately not an adapter_config field
# (shape frozen cycle-3) — revisit at the Phase-3 schema formalization if consumers
# need to tune it.
READINESS_TIMEOUT_S = 60.0

# verdict (result.json) -> process exit code. timeout collapses to FAIL at the exit
# level (only 4 slots); the precise verdict stays in result.json for M3/M8.
_VERDICT_EXIT = {
    "pass": EXIT_PASS,
    "fail": EXIT_FAIL,
    "timeout": EXIT_FAIL,
    "error": EXIT_PLATFORM,
}


class BadJobSpec(Exception):
    """Malformed/absent JOB_SPEC or RESULT_OUT — maps to exit 2 (usage)."""


# --------------------------------------------------------------------------- #
# JOB_SPEC / RESULT_OUT I/O glue (D-2) — CPU-testable.
# --------------------------------------------------------------------------- #
def resolve_job_spec_dict(env: dict | None = None) -> dict:
    """Read JOB_SPEC (a file path OR inline JSON) into a VerificationRequest dict."""
    environ = os.environ if env is None else env
    raw = environ.get("JOB_SPEC")
    if not raw:
        raise BadJobSpec("JOB_SPEC is required (path to VerificationRequest JSON, or inline JSON)")
    candidate = Path(raw)
    text = candidate.read_text(encoding="utf-8") if candidate.is_file() else raw
    try:
        data = json.loads(text)
    except json.JSONDecodeError as exc:
        raise BadJobSpec(f"JOB_SPEC is neither a readable file nor valid JSON: {exc}") from exc
    if not isinstance(data, dict):
        raise BadJobSpec("JOB_SPEC must decode to a JSON object (VerificationRequest dict)")
    return data


def require_job_id(spec: dict) -> str:
    """Extract the mandatory ``job_id`` from a JOB_SPEC dict (REQ-EXEC-013).

    The canonical result.json echoes ``job_id`` (M1 ``VerificationResult``), so a
    spec without one is bad input, pre-sim -> exit 2 (usage), like the other
    JOB_SPEC failures.
    """
    job_id = spec.get("job_id")
    if not job_id or not isinstance(job_id, str):
        raise BadJobSpec("JOB_SPEC must include a non-empty job_id (echoed into result.json)")
    return job_id


def resolve_result_path(env: dict | None = None) -> Path:
    """Resolve RESULT_OUT to the result.json path (dir -> dir/result.json)."""
    environ = os.environ if env is None else env
    raw = environ.get("RESULT_OUT")
    if not raw:
        raise BadJobSpec("RESULT_OUT is required (output dir or explicit result.json path)")
    p = Path(raw)
    return p if p.suffix == ".json" else p / "result.json"


def write_result(result: dict, result_path: Path) -> Path:
    """Write EXACTLY one result.json (atomic replace) — CPU-testable (REQ-EXEC-013)."""
    result_path.parent.mkdir(parents=True, exist_ok=True)
    tmp = result_path.with_name(result_path.name + ".tmp")
    tmp.write_text(json.dumps(result, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    os.replace(tmp, result_path)
    return result_path


def exit_code_for_verdict(verdict: str) -> int:
    """Map a result verdict to the 0/1/2/3 exit contract."""
    return _VERDICT_EXIT.get(verdict, EXIT_PLATFORM)


def parse_request(spec: dict) -> tuple[VerificationRequest, Ros2AdapterConfig]:
    """Build the typed request + adapter config from a JOB_SPEC dict (FU-13 (2)).

    REAL ``from_dict`` calls — the cycle-3 canonical contract is the only shape
    definition (G-17: no field-name drift by construction). Any contract violation
    (missing/renamed key, unknown adapter_config key, non-ros2 interface) is bad
    input, pre-sim -> BadJobSpec -> exit 2 (usage), like the other JOB_SPEC
    failures. CPU-testable (no Isaac).
    """
    try:
        request = VerificationRequest.from_dict(spec)
    except (KeyError, TypeError, ValueError) as exc:
        raise BadJobSpec(f"JOB_SPEC is not a canonical VerificationRequest dict: {exc!r}") from exc
    if request.interface.type != "ros2":
        raise BadJobSpec(
            f"unsupported interface.type {request.interface.type!r} (Phase 2: ros2 only)"
        )
    try:
        adapter_config = Ros2AdapterConfig.from_dict(request.interface.adapter_config)
    except (KeyError, TypeError, ValueError) as exc:
        raise BadJobSpec(f"interface.adapter_config rejected: {exc}") from exc
    return request, adapter_config


def criteria_view(request: VerificationRequest) -> dict:
    """Flatten the typed request into the criteria mapping oracles/metrics read.

    Scenario-derived fields first — ``goal_position`` from the 2D nav goal
    (planar: z=0.0; an explicit ``goal_position`` param wins if a consumer needs
    a non-ground goal) and the sim-time budget ``timeout_s`` (D-F) — then each
    ``AcceptanceCriterion.params`` merged on top. Oracle *selection* stays
    engine-side (the MVP pair is fixed in Phase 2, REQ-EXEC-011); params travel
    per-criterion per the canonical shape.
    """
    view: dict = {
        "goal_position": [request.scenario.goal.x, request.scenario.goal.y, 0.0],
        "timeout_s": request.scenario.timeout_s,
    }
    for criterion in request.acceptance_criteria:
        view.update(criterion.params)
    return view


# --------------------------------------------------------------------------- #
# GPU orchestration (M2 §3.2 order) — Isaac-deferred; wired in cycles 2-4.
# --------------------------------------------------------------------------- #
def build_oracles() -> list:
    """Bind the MVP oracles (deferred import keeps evaluate<-oracles edge one-way)."""
    from cv_infra.oracles.no_collision import NoCollisionOracle  # noqa: PLC0415
    from cv_infra.oracles.reached_goal import ReachedGoalOracle  # noqa: PLC0415

    return [ReachedGoalOracle(), NoCollisionOracle()]


def run(env: dict | None = None) -> int:  # pragma: no cover - GPU path (cycles 2-4)
    """Execute one job end-to-end and write exactly one result.json.

    Runs the fixed M2 §3.2 sequence on GPU. On CPU this is import-only; the sequence
    below documents the assembly and is filled with the Isaac/ROS bodies in cycles
    2-4. The I/O + evaluation seams it calls are the CPU-tested functions above.
    """
    from cv_infra.runner.adapter.ros2 import Ros2Adapter
    from cv_infra.runner.recording import RosbagRecorder, VideoRecorder, plan_artifacts
    from cv_infra.runner.ros_bridge import enable_bridge, honored_env
    from cv_infra.runner.sim_runtime import SimConfig, SimRuntime
    from cv_infra.runner.telemetry import (
        PhysicsTelemetrySampler,
        count_real_collisions,
        min_clearance_m,
        path_length_m,
        time_to_goal_s,
    )

    result_path = resolve_result_path(env)
    spec = resolve_job_spec_dict(env)
    job_id = require_job_id(spec)  # echoed into the canonical result (REQ-EXEC-013)
    # FU-13 (2): REAL typed parse (canonical from_dict chain) — contract violations
    # are BadJobSpec -> exit 2, raised pre-sim (before any Isaac import/boot).
    request, adapter_config = parse_request(spec)
    criteria = criteria_view(request)

    sim = SimRuntime(
        SimConfig(
            scene_ref=request.scenario.scene,
            robot_usd_ref=request.scenario.robot,
            seed=request.scenario.seed,
        )
    )
    adapter = Ros2Adapter(adapter_config)
    try:
        sim.boot()  # step 1: SimulationApp first
        _ = honored_env()  # step 2: honor M3-injected env
        enable_bridge(sim.simulation_app)  # step 2: enable bridge
        sim.load_scene()  # step 3: scene/spawn/dt/seed
        adapter.wire(sim.simulation_app, adapter_config)  # step 4: DDS wiring (no SUT spawn)
        if not adapter.await_ready(timeout_s=READINESS_TIMEOUT_S):  # step 5
            raise RuntimeError("SUT readiness barrier timed out")
        artifacts = plan_artifacts(result_path.parent)
        chassis_path = read_field(criteria, "chassis_path", "")
        excluded_paths = read_field(criteria, "collision_excluded_paths", []) or []
        sampler = PhysicsTelemetrySampler(chassis_path, excluded_paths)
        sampler.attach(sim.world)  # step 6: telemetry
        RosbagRecorder(artifacts).start()  # step 6: MCAP
        VideoRecorder(artifacts).start()  # step 7: mp4
        adapter.drive_mission(request.scenario.goal)  # step 8: mission (typed Goal)

        record = sampler.record  # step 9: evaluate
        goal = read_field(criteria, "goal_position")
        pos_tol = float(read_field(criteria, "position_tolerance_m", 0.25))
        goal_xyz = (float(goal[0]), float(goal[1]), float(goal[2]))
        metrics = {
            "time_to_goal_s": time_to_goal_s(record.gt_pose_samples, goal_xyz, pos_tol),
            "min_clearance_m": min_clearance_m(),
            "collision_count": count_real_collisions(
                record.contact_events, chassis_path, excluded_paths
            ),
            "path_len_m": path_length_m(record.gt_pose_samples),
        }
        verdict, outcomes = EvaluationEngine(build_oracles()).evaluate(record, criteria)
        # artifacts=None -> canonical None fields until the recorders produce files
        # (cycle 4 passes Artifacts(mcap=..., mp4=...) from the recorder stops).
        result = build_result_dict(job_id, verdict, outcomes, metrics, artifacts=None)
        write_result(result, result_path)  # step 10: exactly one result
        return exit_code_for_verdict(verdict)
    except EulaNotAcceptedError:
        raise
    except Exception as exc:  # step 10 (degraded): still emit a (canonical) result for M3
        print(f"[cv-runner] runner error: {exc!r}", file=sys.stderr, flush=True)
        write_result(build_result_dict(job_id, VERDICT_ERROR, [], {}), result_path)
        return EXIT_PLATFORM
    finally:
        adapter.teardown()  # step 11: clean shutdown
        sim.close()


def main(env: dict | None = None) -> int:
    """CLI-less entrypoint. Maps setup/platform failures to the exit contract."""
    try:
        return run(env)
    except BadJobSpec as exc:
        print(f"[cv-runner] bad job spec: {exc}", file=sys.stderr, flush=True)
        return EXIT_USAGE
    except EulaNotAcceptedError as exc:
        print(f"[cv-runner] {exc}", file=sys.stderr, flush=True)
        return EXIT_PLATFORM


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
