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
the fine-grained verdict is retained in result.json), 2=bad JOB_SPEC/usage (incl.
oracle-load failure — defence-in-depth, admit rejects first), 3=platform (EULA
missing, runner crash, verdict=error).
"""

from __future__ import annotations

import json
import os
import sys
from importlib import metadata
from pathlib import Path

from pydantic import ValidationError

from cv_infra.contract.adapter_schema import Ros2AdapterConfig
from cv_infra.contract.errors import ContractError, from_validation_error
from cv_infra.contract.schema import VerificationRequest
from cv_infra.oracles.base import ENTRY_POINT_GROUP, load_oracle
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
# need to tune it. 180 = bring-up margin over the 60s nav2 window: the barrier
# budget also absorbs the SUT's restart round (M3 contract) and the first-flow
# settling after a cold scene stream; tighten with cycle-6 measurements.
READINESS_TIMEOUT_S = 180.0

# D-1 (4) supervisor->runner env naming the ro-mounted custom-oracle plugin dir.
# The name is the cross-team wire (M3 injects it character-exact — G-17 drift
# class): change it only with a decisions/ update on both sides.
ORACLE_PLUGIN_DIR_ENV = "CV_ORACLE_PLUGIN_DIR"

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
    """Build the typed request + adapter config from a JOB_SPEC dict (D-4').

    REAL ``contract.schema`` ``model_validate`` — the M1 pydantic canon is the
    only shape definition (G-17: no field-name drift by construction; the runner
    executes it on the BUNDLED pydantic, D-4'). The JOB_SPEC *wire* itself is
    unchanged (T1 seam, frozen): its runner-envelope keys are adapted into the
    schema's nesting before validation — ``job_id`` is peeled off (owned by
    ``require_job_id``) and the flattened ``sut_image_ref`` maps to
    ``sut.image_ref``. Any contract violation (missing/renamed/unknown key,
    non-ros2 interface, debug_obstacle riding in criteria params — D-2'
    supersedes that home) is bad input, pre-sim -> BadJobSpec -> exit 2 (usage),
    rendered with the M1 friendly-error prose. CPU-testable (no Isaac).
    """
    wire = dict(spec)
    wire.pop("job_id", None)
    if "sut_image_ref" in wire:
        if "sut" in wire:
            raise BadJobSpec("JOB_SPEC carries both 'sut_image_ref' and 'sut' — ambiguous SUT pin")
        wire["sut"] = {"image_ref": wire.pop("sut_image_ref")}
    try:
        request = VerificationRequest.model_validate(wire)
    except ValidationError as exc:
        friendly = "; ".join(str(e) for e in from_validation_error(exc, model=VerificationRequest))
        raise BadJobSpec(f"JOB_SPEC is not a canonical VerificationRequest: {friendly}") from exc
    if request.interface.type != "ros2":
        raise BadJobSpec(f"unsupported interface.type {request.interface.type!r} (MVP: ros2 only)")
    # The schema already parsed adapter_config into the typed M1 model — no
    # second from_dict pass (single validation, single definition).
    return request, request.interface.adapter_config


def criteria_view(request: VerificationRequest) -> dict:
    """Flatten the typed request into the criteria mapping oracles/metrics read.

    Scenario-derived fields first — ``goal_position`` from the 2D nav goal
    (planar: z=0.0; an explicit ``goal_position`` param from a custom criterion
    wins if a consumer needs a non-ground goal) and the sim-time budget
    ``timeout_s`` (D-F) — then each ``AcceptanceCriterion.params`` merged on
    top. Known-key params models merge with ``exclude_none`` (None = "oracle
    default applies" — the default VALUES stay oracle-owned); a custom
    criterion's free mapping merges as-is. Duck-typed on purpose: the same view
    works over the old dataclass request (QA fixture guard) during the P3
    migration window. Ownership: M2 (D-3' — execution-plane merge, M1 validates
    intrinsics only).
    """
    view: dict = {
        "goal_position": [request.scenario.goal.x, request.scenario.goal.y, 0.0],
        "timeout_s": request.scenario.timeout_s,
    }
    for criterion in request.acceptance_criteria:
        params = criterion.params
        if not isinstance(params, dict):
            params = params.model_dump(exclude_none=True)
        view.update(params)
    return view


# --------------------------------------------------------------------------- #
# Evaluation-engine composition (D-1 (4), 2026-07-11) — CPU-testable.
# --------------------------------------------------------------------------- #
def insert_oracle_plugin_dir(env: dict | None = None) -> str | None:
    """Put the M3-mounted custom-oracle dir on ``sys.path`` (D-1 (4)), pre-engine.

    The supervisor bind-mounts the consumer's scenario dir into the runner
    container at the SAME absolute path (ro) and injects
    ``CV_ORACLE_PLUGIN_DIR`` — inserting it makes ``module:Class`` criteria
    importable exactly as they were on the admit plane (M1 loader stage 5).
    Unset -> no-op (sys.path untouched); an empty string counts as unset
    (inserting ``""`` would put the CWD on sys.path — G-26's empty-env
    variant). Returns the inserted dir (for the boot log) or None.
    """
    environ = os.environ if env is None else env
    plugin_dir = environ.get(ORACLE_PLUGIN_DIR_ENV)
    if not plugin_dir:
        return None
    sys.path.insert(0, plugin_dir)
    return plugin_dir


def build_oracles(request: VerificationRequest | None = None) -> list:
    """Compose one oracle instance per acceptance criterion via the M1 loader.

    The UNIFORM path (D-1 (4)): every ``criterion.oracle`` goes through
    ``load_oracle`` — an MVP name resolves in the ``cv_infra.oracles``
    entry-point group, a custom ``module:Class`` through the explicit path —
    so no hardcoded oracle imports remain here. Two criteria naming the same
    oracle load twice (two instances) on purpose: no caching/dedup (D-1).
    Without a request the registered built-in set is composed (entry-point
    enumeration; the QA fixture-canonical guard derives oracle read-sets from
    this form).

    A load failure here is DEFENCE-IN-DEPTH only — the FIRST rejection is
    admit's (M1 loader stage 5, pre-execution-plane); reaching it means the
    runner got a spec admit never saw, or the plugin-dir mount is broken. It
    maps onto the friendly BadJobSpec -> exit 2 path, never a traceback.
    """
    if request is None:
        names = sorted(ep.name for ep in metadata.entry_points(group=ENTRY_POINT_GROUP))
    else:
        names = [criterion.oracle for criterion in request.acceptance_criteria]
    oracles = []
    for name in names:
        try:
            oracles.append(load_oracle(name))
        except ContractError as exc:
            raise BadJobSpec(f"oracle {name!r} failed to load: {exc}") from exc
    return oracles


# --------------------------------------------------------------------------- #
# GPU orchestration (M2 §3.2 order) — Isaac-deferred; wired in cycles 2-4.
# --------------------------------------------------------------------------- #


def run(env: dict | None = None) -> int:  # pragma: no cover - GPU path (T3 proves)
    """Execute one job end-to-end and write exactly one result.json.

    Runs the fixed M2 §3.2 sequence on GPU. On CPU this is import-only (all Isaac/
    ROS bodies are deferred imports). The I/O + evaluation seams it calls are the
    CPU-tested functions above.

    Recording stance (P2-02): recorder failures are LOUD but non-fatal — the
    artifacts are attachments, the verdict is the product. A missing backend
    (RecorderUnavailable — pending M5 MCAP routing) or a capture failure leaves
    the canonical artifact field None + a stderr warning instead of poisoning the
    verdict with error.
    """
    from cv_infra.contract.schema import Artifacts
    from cv_infra.runner.adapter.ros2 import Ros2Adapter
    from cv_infra.runner.recording import RosbagRecorder, VideoRecorder, plan_artifacts
    from cv_infra.runner.ros_bridge import (
        bootstrap_bridge_env,
        enable_bridge,
        honored_env,
        reexec_for_bridge_lib,
    )
    from cv_infra.runner.sim_runtime import SimConfig, SimRuntime
    from cv_infra.runner.telemetry import (
        PhysicsTelemetrySampler,
        contact_partners,
        count_real_collisions,
        min_clearance_m,
        path_length_m,
        time_to_goal_s,
    )

    result_path = resolve_result_path(env)
    spec = resolve_job_spec_dict(env)
    job_id = require_job_id(spec)  # echoed into the canonical result (REQ-EXEC-013)
    # D-4': REAL typed parse (contract.schema model_validate on the bundled
    # pydantic) — contract violations are BadJobSpec -> exit 2, raised pre-sim
    # (before any Isaac import/boot).
    request, adapter_config = parse_request(spec)
    criteria = criteria_view(request)
    # D-1 (4): plugin dir on sys.path BEFORE the engine composes, then the
    # engine composes uniformly via the M1 loader — still PRE-sim, so a load
    # failure (defence-in-depth; admit already rejected it once) is
    # BadJobSpec -> exit 2 before any Isaac import/boot, like the parse above.
    plugin_dir = insert_oracle_plugin_dir(env)
    if plugin_dir is not None:
        print(f"[cv-runner] oracle plugin dir on sys.path: {plugin_dir}", flush=True)
    engine = EvaluationEngine(build_oracles(request))

    sim = SimRuntime(
        SimConfig(
            scene_ref=request.scenario.scene,
            robot_usd_ref=request.scenario.robot,
            seed=request.scenario.seed,
        )
    )
    # The adapter's readiness/mission loops must keep the sim stepping (the sim IS
    # the /clock source — G-19), so it gets the step function as a dependency.
    adapter = Ros2Adapter(adapter_config, stepper=sim.step)
    rosbag = None
    video = None
    sampler = None
    try:
        # step 0.5: FU-14 boot glue — BEFORE SimulationApp so the bridge extension
        # sees ROS_DISTRO/RMW/LD_LIBRARY_PATH; supervisor-injected keys win, absent
        # keys default from adapter_config (runner works without the T1 supervisor).
        bootstrap = bootstrap_bridge_env(adapter_config.ros_distro, adapter_config.rmw)
        print(f"[cv-runner] bridge bootstrap: {bootstrap}", flush=True)
        # Measured (p2c5 probe-01): the loader snapshots LD_LIBRARY_PATH at process
        # start — when bootstrap had to prepend it, re-exec once pre-boot so the
        # bridge's shared libs resolve (idempotent: marker present after re-exec).
        reexec_for_bridge_lib(bootstrap)
        sim.boot()  # step 1: SimulationApp first
        _ = honored_env()  # step 2: honor M3-injected env
        enable_bridge(sim.simulation_app)  # step 2: enable bridge
        chassis_path = read_field(criteria, "chassis_path", "")
        excluded_paths = read_field(criteria, "collision_excluded_paths", []) or []
        sampler = PhysicsTelemetrySampler(chassis_path, excluded_paths)
        # bind() must run PRE-reset (probe-03 recipe A: the tensor-view wrapper
        # created post-reset is invalidated) — load_scene calls it via the hook.
        sim.pre_reset.append(sampler.bind)
        if request.scenario.debug_obstacle is not None:  # D-2': obstacle = WORLD state
            obstacle = request.scenario.debug_obstacle.model_dump(exclude_none=True)
            sim.pre_reset.append(lambda _world: sim.spawn_debug_obstacle(obstacle))
        # FU-17: declared-sensor render products must be enabled PRE-play
        # (BEFORE world.reset() — mid-play toggling is a measured no-op), so
        # this rides the same pre_reset seam as the telemetry bind.
        sensor_topics = [s.topic for s in adapter_config.sensors]
        if sensor_topics:
            sim.pre_reset.append(lambda _world: sim.enable_declared_sensors(sensor_topics))
        sim.load_scene()  # step 3: scene/spawn/dt/seed (+ telemetry pre-bind)
        adapter.wire(sim.simulation_app, adapter_config)  # step 4: DDS wiring (no SUT spawn)
        if not adapter.await_ready(timeout_s=READINESS_TIMEOUT_S):  # step 5
            raise RuntimeError("SUT readiness barrier timed out")
        artifact_plan = plan_artifacts(result_path.parent)
        sampler.attach(sim.world)  # step 6: telemetry (callbacks only; bound above)
        rosbag = _start_quiet(RosbagRecorder(artifact_plan, adapter_config))  # step 6: MCAP
        video = _start_quiet(VideoRecorder(artifact_plan))  # step 7: mp4
        if video is not None:
            sim.on_step.append(video.capture_frame)
        # step 8: mission on the sim-time budget (D-F; wall runaway watchdog = M3).
        outcome = adapter.drive_mission(request.scenario.goal, timeout_s=request.scenario.timeout_s)
        print(f"[cv-runner] mission outcome: {outcome}", flush=True)

        sampler.detach()
        mcap_path = _stop_quiet(rosbag)
        mp4_path = _stop_quiet(video)

        record = sampler.record  # step 9: evaluate
        goal_dbg = read_field(criteria, "goal_position")
        if record.gt_pose_samples and goal_dbg is not None:
            # Bring-up debug surface (T4 tolerance tuning): the exact GT
            # closest-approach — run5 measured nav2 "Reached the goal!" with GT
            # still outside the oracle tol (AMCL error + nav xy tol stack).
            import math  # noqa: PLC0415

            gxyz = (float(goal_dbg[0]), float(goal_dbg[1]), float(goal_dbg[2]))
            closest = min(math.dist(s.position, gxyz) for s in record.gt_pose_samples)
            print(f"[cv-runner] GT closest-approach to goal: {closest:.3f} m", flush=True)
        if record.contact_events:  # bring-up debug surface: name the contact partners
            partners = contact_partners(record.contact_events, chassis_path)
            print(
                f"[cv-runner] contact events: {len(record.contact_events)} with "
                f"{len(partners)} distinct partner prim(s): {partners[:10]}",
                flush=True,
            )
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
        verdict, outcomes = engine.evaluate(record, criteria)  # engine composed pre-sim
        result = build_result_dict(
            job_id,
            verdict,
            outcomes,
            metrics,
            artifacts=Artifacts(
                mcap=str(mcap_path) if mcap_path is not None else None,
                mp4=str(mp4_path) if mp4_path is not None else None,
            ),
        )
        write_result(result, result_path)  # step 10: exactly one result
        return exit_code_for_verdict(verdict)
    except EulaNotAcceptedError:
        raise
    except Exception as exc:  # step 10 (degraded): still emit a (canonical) result for M3
        print(f"[cv-runner] runner error: {exc!r}", file=sys.stderr, flush=True)
        write_result(build_result_dict(job_id, VERDICT_ERROR, [], {}), result_path)
        return EXIT_PLATFORM
    finally:
        if sampler is not None:
            sampler.detach()
        for recorder in (rosbag, video):  # failure paths: no child proc/writer leak
            if recorder is not None:
                recorder.abort()
        adapter.teardown()  # step 11: clean shutdown
        sim.close()


def _start_quiet(recorder):  # pragma: no cover - GPU path helper
    """Start a recorder; on failure warn LOUDLY and record without it (P2-02
    honest degradation — the artifact field stays None, visible to PM/QA)."""
    try:
        recorder.start()
        return recorder
    except Exception as exc:
        print(f"[cv-runner] recorder unavailable: {exc}", file=sys.stderr, flush=True)
        return None


def _stop_quiet(recorder):  # pragma: no cover - GPU path helper
    """Stop a recorder and return its artifact path; None (loud) on failure."""
    if recorder is None:
        return None
    try:
        return recorder.stop()
    except Exception as exc:
        print(f"[cv-runner] recorder produced no artifact: {exc}", file=sys.stderr, flush=True)
        return None


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
