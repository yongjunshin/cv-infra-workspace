"""Batch runner entrypoint — ONE container, n samples of ONE request (M2, p6 §0-7).

The single-job entrypoint (``runner.main``) is UNTOUCHED and stays the contract
for "one job -> one result.json". This module is the p6 CARRIER: the sample is
still the logical job (``Job(request_id, repeat_index)``, one ``JobResult`` each —
M3/M4 unchanged), and the container carries n of them so the ~25 s Isaac boot,
the stage open and the DDS wiring are paid ONCE instead of n times.

Wire (M1 ``contract.job_batch``): ``JOB_SPEC_BATCH`` names a wrapper document
``{specs: [...], request_id}``; ``RESULT_OUT`` is the batch out-dir ROOT. The
invariant both sides agree on is ``specs[i] <-> results/<i>/ <-> repeat_index i``,
and this module renders that path (``iteration_result_path``).

Sequence — boot ONCE, then per sample i:

    admit every spec (0 GPU seconds) -> bridge bootstrap -> SimulationApp ->
    ros2 bridge -> scene/robot/telemetry/sensors (sample 0's staging) ->
    adapter wire (+ sensor & /cmd_vel publishers) -> render product
      | i: re-pin seed -> restage (obstacle move + set, soft reset, repose) ->
      |    sim_config line -> settle -> SUT realign (seeded from the post-settle
      |    GT pose) -> converge -> readiness -> cycle telemetry accumulator ->
      |    bag/mp4 -> mission -> evaluate -> results/<i>/result.json
      |    -> batch_summary.json (atomic flush = the carrier's heartbeat)

Exit contract (CARRIER level — the sample verdicts live in their own result.json):
0 = ran the whole batch, whatever the verdicts; 2 = pre-boot rejection (bad
wrapper, bad spec i, non-uniform batch); 3 = platform (EULA, boot failure, death
mid-batch). **1 is deliberately unused**: "some sample failed" is not a property
of the carrier, and collapsing n verdicts into one status is exactly the question
the C-2 spike showed has no honest answer (M3 folds each sample separately, and
a completed sample's verdict is preserved by its own flushed result). The code is
delivered by ``hard_exit`` for the same measured reason as main's (G-62).

The Dockerfile ENTRYPOINT is unchanged (``runner.main``): M3 overrides the
command for a batch, so a runner image that predates batching fails loudly with
"No module named cv_infra.runner.batch" instead of half-running the request.
"""

from __future__ import annotations

import json
import os
import sys
import time
from dataclasses import dataclass
from pathlib import Path

from pydantic import ValidationError

from cv_infra.contract.errors import from_validation_error
from cv_infra.contract.job_batch import (
    BATCH_SUMMARY_FILENAME,
    BATCH_SUMMARY_SCHEMA,
    JOB_SPEC_BATCH_ENV,
    JobSpecBatch,
)
from cv_infra.runner.evaluate import (
    VERDICT_ERROR,
    EvaluationEngine,
    build_result_dict,
    read_field,
)
from cv_infra.runner.main import (
    EXIT_PASS,
    EXIT_PLATFORM,
    EXIT_USAGE,
    READINESS_TIMEOUT_S,
    BadJobSpec,
    _abort_recorders,
    _emit,
    _print_contact_partners,
    _start_quiet,
    _stop_quiet,
    admit_firmware_slot,
    announce_oracle_plugin_dir,
    artifact_paths,
    build_oracles,
    build_sensor_suite,
    criteria_view,
    hard_exit,
    load_firmware_slot,
    obstacle_specs,
    parse_request,
    read_request_identity_key,
    register_sensor_hooks,
    require_job_id,
    sim_config_for,
    validate_oracle_params,
    write_result,
)
from cv_infra.runner.sim_runtime import EulaNotAcceptedError, obstacle_pool_plan

#: Directory under ``RESULT_OUT`` holding the per-sample outputs. The runner owns
#: the out-dir LAYOUT (M1 owns the index agreement — see job_batch's docstring).
BATCH_RESULTS_DIRNAME = "results"

#: This module's import path, as a LITERAL: under ``python -m`` this module's
#: ``__name__`` is ``"__main__"``, so the re-exec argv cannot be derived from it
#: (it would re-exec ``-m __main__``). Pinned to the real name by CPU test.
BATCH_MODULE = "cv_infra.runner.batch"

#: Sim-seconds pumped on EACH SIDE of the realign (AR-19). Runner runtime policy,
#: NOT consumer contract (same stance as ``READINESS_TIMEOUT_S``). One budget, two
#: windows, because the pre-C5b single window was silently doing two jobs:
#:
#: * BEFORE the realign — the physics settling of a teleported robot and, on a
#:   legged one, its policy's spawn lunge, so the pose the seed carries is a pose
#:   that has stopped moving;
#: * AFTER it — the blackbox acting on that seed (AMCL's forced update on its next
#:   scan, the TF chain, the just-cleared costmaps) before the mission is
#:   dispatched. Measured: without this window the seed lands verbatim and the goal
#:   still aborts on the previous sample's belief (see the call site).
#:
#: Env name unchanged (an operator knob W1/W2 already measure with), so a different
#: budget still needs no rebuild.
REALIGN_SETTLE_ENV = "CV_REALIGN_SETTLE_S"
DEFAULT_REALIGN_SETTLE_S = 3.0

#: Wall cap on the settle pump. The loop advances SIM time (D-F), but a sim whose
#: /clock has stopped would spin here forever — the cap turns that into a normal
#: readiness failure (reported, exit 3) instead of a hung carrier.
SETTLE_WALL_BUDGET_S = 60.0


# --------------------------------------------------------------------------- #
# Wire I/O (JOB_SPEC_BATCH / RESULT_OUT) — CPU-testable, no Isaac.
# --------------------------------------------------------------------------- #
def load_batch(env: dict | None = None) -> JobSpecBatch:
    """Read ``JOB_SPEC_BATCH`` (a FILE path) into the validated wrapper document.

    A path only — never inline JSON (n specs do not fit an env value, and a
    read-only mount is what the supervisor already has). Every failure is bad
    input, pre-sim -> ``BadJobSpec`` -> exit 2, rendered with the M1 friendly
    error prose so a malformed wrapper says which key is wrong rather than
    printing a pydantic traceback.
    """
    environ = os.environ if env is None else env
    raw = environ.get(JOB_SPEC_BATCH_ENV)
    if not raw:
        raise BadJobSpec(
            f"{JOB_SPEC_BATCH_ENV} is required (path to a JSON batch document "
            '{"specs": [<JOB_SPEC>, ...], "request_id": ...})'
        )
    path = Path(raw)
    if not path.is_file():
        raise BadJobSpec(f"{JOB_SPEC_BATCH_ENV}={raw!r} is not a readable file")
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise BadJobSpec(f"{JOB_SPEC_BATCH_ENV} is not valid JSON: {exc}") from exc
    if not isinstance(data, dict):
        raise BadJobSpec(
            f"{JOB_SPEC_BATCH_ENV} must decode to a JSON object (the batch wrapper), "
            f"got {type(data).__name__} — a bare array is NOT the wire (job_batch §)"
        )
    try:
        return JobSpecBatch.model_validate(data)
    except ValidationError as exc:
        friendly = "; ".join(str(e) for e in from_validation_error(exc, model=JobSpecBatch))
        raise BadJobSpec(f"{JOB_SPEC_BATCH_ENV} is not a valid batch document: {friendly}") from exc


def resolve_out_root(env: dict | None = None) -> Path:
    """Resolve ``RESULT_OUT`` to the batch out-dir ROOT (``results/<i>/`` live under it).

    Unlike the single-job seam, an explicit ``.json`` path is a producer bug: a
    carrier writes n results plus a summary, so it needs a directory. Loud, pre-boot.
    """
    environ = os.environ if env is None else env
    raw = environ.get("RESULT_OUT")
    if not raw:
        raise BadJobSpec("RESULT_OUT is required (output ROOT dir for the batch)")
    root = Path(raw)
    if root.suffix == ".json":
        raise BadJobSpec(
            f"RESULT_OUT={raw!r} names a file, but a batch writes {BATCH_RESULTS_DIRNAME}/<i>/"
            f"result.json + {BATCH_SUMMARY_FILENAME} — pass the output DIRECTORY"
        )
    return root


def iteration_dir(out_root: str | Path, index: int) -> Path:
    """``<out_root>/results/<i>`` — sample i's own output dir (the wire invariant)."""
    return Path(out_root) / BATCH_RESULTS_DIRNAME / str(index)


def iteration_result_path(out_root: str | Path, index: int) -> Path:
    """``<out_root>/results/<i>/result.json`` — where sample i's ONE result lands."""
    return iteration_dir(out_root, index) / "result.json"


def realign_settle_s(env: dict | None = None) -> float:
    """Settle budget in SIM seconds (env override, else the module default)."""
    environ = os.environ if env is None else env
    raw = environ.get(REALIGN_SETTLE_ENV)
    if not raw:
        return DEFAULT_REALIGN_SETTLE_S
    try:
        return float(raw)
    except ValueError as exc:
        raise BadJobSpec(f"{REALIGN_SETTLE_ENV}={raw!r} is not a number of sim-seconds") from exc


# --------------------------------------------------------------------------- #
# Pre-boot admission (0 GPU seconds) — CPU-testable.
# --------------------------------------------------------------------------- #
@dataclass(frozen=True)
class ParsedSpec:
    """One admitted sample: its index and everything the loop needs, pre-boot."""

    index: int
    job_id: str
    request: object  # VerificationRequest (typed by parse_request)
    adapter_config: object  # Ros2AdapterConfig
    criteria: dict
    oracles: list
    policy: object = None  # go2_wiring.PolicyPin | None (the firmware slot, D-3)


#: What the CARRIER does exactly once, so every spec must agree on it — label ->
#: reader. A spec that disagrees would be run against sample 0's world/wiring and
#: judged as if it were its own, i.e. a silently WRONG verdict; rejecting pre-boot
#: costs 0 GPU seconds (the loop below may then rely on all of it).
_UNIFORM_FIELDS: tuple[tuple[str, object], ...] = (
    ("interface.adapter_config", lambda p: p.adapter_config),
    ("sut.image_ref", lambda p: p.request.sut.image_ref),
    # The 2nd SUT artifact (D2 2026-08-31), same treatment as the image it rides
    # with: one carrier holds ONE SUT, and a sample judged against a different
    # policy than sample 0 is a different SUT wearing sample 0's verdict.
    # C2b resolved M1's open question (C2a report §6): the policy reaches the
    # runner as the FLAT pin (``locomotion_policy_path``/``_sha256``), never as a
    # nested ``sut`` block, so the row reads the admitted pin — reading
    # ``request.sut.locomotion_policy`` here would compare None to None forever.
    ("sut.locomotion_policy", lambda p: p.policy),
    ("scenario.scene", lambda p: p.request.scenario.scene),
    ("scenario.robot", lambda p: p.request.scenario.robot),
    ("execution_settings.fixed_dt", lambda p: p.request.execution_settings.fixed_dt),
    # Declared-NESS only: the position is exactly what varies per sample, but the
    # obstacle prim is spawned once and MOVED afterwards, so "sample 3 suddenly
    # wants an obstacle" has nothing to move (and "sample 3 wants none" would be
    # judged with sample 0's box still on the stage).
    ("scenario.debug_obstacle declared", lambda p: p.request.scenario.debug_obstacle is not None),
    # The telemetry sampler binds ONE chassis prim for the whole carrier.
    ("criteria.chassis_path", lambda p: read_field(p.criteria, "chassis_path", "")),
    (
        "criteria.collision_excluded_paths",
        lambda p: read_field(p.criteria, "collision_excluded_paths", []) or [],
    ),
)


def admit_specs(batch: JobSpecBatch) -> list[ParsedSpec]:
    """Validate EVERY spec before a single GPU second is spent (p6 §0-7).

    ``main.run`` does this for its one job; in a carrier a spec that only fails
    after 7 completed missions would be the worst possible moment to find out —
    and the samples of one request are produced by one materialization, so a
    difference between them is a PLATFORM bug worth failing loudly on.

    Two classes, both exit 2 and both naming the offending INDEX (the index is
    the only handle M3 has on a sample):

    * per-spec — job_id, canonical shape, oracle load, oracle params (the same
      four gates ``main.run`` applies, in the same order);
    * batch-wide — job_id uniqueness (two samples sharing an id would overwrite
      each other in the store) and the ``_UNIFORM_FIELDS`` the carrier applies once.
    """
    parsed: list[ParsedSpec] = []
    for index, spec in enumerate(batch.specs):
        try:
            job_id = require_job_id(spec)
            request, adapter_config = parse_request(spec)
            criteria = criteria_view(request)
            oracles = build_oracles(request)
            validate_oracle_params(oracles, criteria)
            # D-3 firmware slot: the PIN only (path/digest/slot agreement). The
            # policy BYTES are loaded once by the carrier's boot, not n times —
            # the uniformity row below is what makes "once" correct.
            policy = admit_firmware_slot(spec, request)
        except BadJobSpec as exc:
            raise BadJobSpec(f"batch spec {index}: {exc}") from exc
        parsed.append(ParsedSpec(index, job_id, request, adapter_config, criteria, oracles, policy))

    seen: dict[str, int] = {}
    for item in parsed:
        first = seen.setdefault(item.job_id, item.index)
        if first != item.index:
            raise BadJobSpec(
                f"batch spec {item.index}: job_id {item.job_id!r} is already used by spec "
                f"{first} — each sample needs its own id (it names its own JobResult)"
            )

    head = parsed[0]
    for label, read in _UNIFORM_FIELDS:
        expected = read(head)
        for item in parsed[1:]:
            if read(item) != expected:
                raise BadJobSpec(
                    f"batch spec {item.index}: {label} differs from spec 0 "
                    f"({read(item)!r} != {expected!r}) — one carrier boots ONE world and "
                    "wires the ROS side ONCE, so every sample of the batch must agree on it"
                )

    # Obstacles are the one axis whose SHAPE varies by design (CEO: "desk
    # n={0..5}"), so they get NO uniformity row — the pool absorbs the variance.
    # Two things still have to hold pre-boot, at 0 GPU seconds: every asset
    # designator resolves (``parse_request`` proved that per spec above, where the
    # index is known) and the POOL fits, which only the whole batch can decide —
    # the pool is the per-sample MAXIMUM multiplicity over EVERY spec.
    try:
        obstacle_pool_plan([obstacle_specs(item.request) for item in parsed])
    except ValueError as exc:
        raise BadJobSpec(f"batch obstacles: {exc}") from exc
    return parsed


def sim_time_advanced(previous_end_s: float | None, start_s: float) -> bool:
    """Did the sim clock keep moving FORWARD across an iteration boundary?

    Soft restaging never stops the timeline, so sim-time must be monotonic across
    samples. A hard reset rewinds it (measured p6c2: 23/23 boundaries rewound,
    3.60 -> 0.05 s), and a rewind is not cosmetic — a mission deadline armed on
    the pre-reset value can never be reached and an ``/initialpose`` stamped with
    it lands in the SUT's future. Recorded per sample so a regression to
    rewinding shows up in the summary instead of as unexplained timeouts.
    """
    return previous_end_s is None or start_s >= previous_end_s


class _Stopwatch:
    """Wall-clock stopwatch recording named spans (the W2 per-iteration anchors)."""

    def __init__(self) -> None:
        self.spans: dict[str, float] = {}
        self._t0: dict[str, float] = {}

    def begin(self, name: str) -> None:
        self._t0[name] = time.monotonic()

    def end(self, name: str) -> float:
        elapsed = time.monotonic() - self._t0.pop(name)
        self.spans[name] = round(elapsed, 4)
        return elapsed


class BatchSummary:
    """``batch_summary.json`` — the carrier's own report, flushed every iteration.

    It answers ONE question M3 cannot otherwise answer when a carrier dies
    mid-batch: which samples ran? An item present = that sample ran (and its
    result.json is on disk); item AND result absent = it never ran, and M3
    charges that slot an infra error (P5-13). That is why the flush is atomic and
    happens at the END of every iteration rather than once at the end: a summary
    written only on the happy path would be missing exactly when it is needed.
    """

    def __init__(
        self,
        out_root: str | Path,
        request_id: str | None,
        n: int,
        started_at: float | None = None,
    ) -> None:
        self.path = Path(out_root) / BATCH_SUMMARY_FILENAME
        self.doc: dict = {
            "schema": BATCH_SUMMARY_SCHEMA,
            "request_id": request_id,
            "n": n,
            "started_at": time.time() if started_at is None else started_at,
            "finished_at": None,
            "boot": {},
            "error": None,
            "iterations": [],
        }

    def flush(self) -> Path:
        """Atomic write (reuses the single-job writer — one definition of 'atomic')."""
        return write_result(self.doc, self.path)

    def add_iteration(self, item: dict) -> Path:
        self.doc["iterations"].append(item)
        return self.flush()

    def verdicts(self) -> list:
        """Every recorded sample's verdict, in iteration order.

        A LIST, never a fold: "some sample failed" is not a property of the
        carrier (see the exit contract in this module's docstring), so the
        closing log line reports them side by side and M3 folds each sample
        from its own result.json.
        """
        return [item["verdict"] for item in self.doc["iterations"]]

    def finish(self, error: str | None = None, finished_at: float | None = None) -> Path:
        self.doc["finished_at"] = time.time() if finished_at is None else finished_at
        self.doc["error"] = error
        return self.flush()


def _dumped(model: object, *, exclude_none: bool = False) -> dict | None:
    """``model.model_dump(...)`` or None — the "declared, or absent" hand-off shape.

    No contract model crosses into ``sim_runtime``, so every declared block is
    dumped to a plain dict at this boundary; an UNDECLARED one must arrive as
    None (not as an empty dict) or the runner's own defaults never apply.
    """
    return None if model is None else model.model_dump(exclude_none=exclude_none)


@dataclass(frozen=True)
class _Admission:
    """What the carrier knows before a single GPU second is spent."""

    out_root: Path
    settle_s: float
    identity_key: str | None
    specs: list[ParsedSpec]
    summary: BatchSummary


def _admit(env: dict | None = None) -> _Admission:
    """Read the wire, admit EVERY spec, open the summary heartbeat. 0 GPU seconds.

    Order matters and is the order a bad batch fails in: out-dir shape, wrapper
    document, settle budget, plugin dir (on sys.path BEFORE any oracle loads),
    identity key, then the per-spec + batch-wide admission. The heartbeat is
    flushed HERE, before Isaac is touched, so a carrier that dies at the boot
    guard still tells M3 "the container started and admitted n" (P5-13).
    """
    out_root = resolve_out_root(env)
    batch = load_batch(env)
    settle_s = realign_settle_s(env)
    announce_oracle_plugin_dir(env)
    identity_key = read_request_identity_key(env)
    specs = admit_specs(batch)
    print(
        f"[cv-runner] batch admitted: {len(specs)} sample(s) request_id={batch.request_id} "
        f"out_root={out_root} settle_s={settle_s}",
        flush=True,
    )
    summary = BatchSummary(out_root, batch.request_id, len(specs))
    summary.flush()  # heartbeat: the carrier exists, before Isaac is even touched
    return _Admission(out_root, settle_s, identity_key, specs, summary)


@dataclass(frozen=True)
class _Staging:
    """What the ONE boot stages, decided from EVERY admitted spec (pre-boot, pure)."""

    debug_obstacle: dict | None
    pool_plan: dict
    head_obstacles: list[dict]
    pool_total: int
    sensor_topics: list[str]


def _plan_staging(specs: list[ParsedSpec], base_config: object) -> _Staging:
    """Plan sample 0's staging + the pool the whole batch shares.

    The pool is the UNION over EVERY spec, not spec 0's: sample i's obstacle
    COUNT is exactly what varies (CEO: "desk n={0..5}"), so a spec-0-sized pool
    would leave sample 3 with nothing to place. Placement is NOT decided here —
    the boot's second pre-reset hook owns it, so "where sample 0's obstacles go"
    has one definition shared with every later sample.
    """
    head = specs[0]
    pool_plan = obstacle_pool_plan([obstacle_specs(p.request) for p in specs])
    return _Staging(
        debug_obstacle=_dumped(head.request.scenario.debug_obstacle, exclude_none=True),
        pool_plan=pool_plan,
        head_obstacles=obstacle_specs(head.request),
        pool_total=sum(pool_plan.values()),
        sensor_topics=[s.topic for s in base_config.sensors],
    )


def boot_total_s(boot: dict) -> float:
    """Sum of the boot phase spans recorded so far (every ``*_s`` key)."""
    return round(sum(value for key, value in boot.items() if key.endswith("_s")), 4)


def reexec_argv() -> list[str]:
    """The argv the bridge re-exec must use — THIS module, not the single-job one.

    Measured (p6c1, first C-2 attempt: the runner exited 2 in 2.4 s): the default
    argv inside ``reexec_for_bridge_lib`` is ``cv_infra.runner.main``, so a second
    entry point is silently REPLACED by main mid-boot and then dies on the
    JOB_SPEC main expects. The parameter existed; passing it is the whole fix.
    """
    return [sys.executable, "-m", BATCH_MODULE]


def _attach_optional_streams(
    adapter: object, sim: object, config: object, sensors: object, policy: object
) -> None:
    """The two "declared, or nothing happens" attachments, after ``adapter.wire``.

    A carter carrier attaches NEITHER (its scene's OmniGraphs publish, and it runs
    no onboard policy); a go2 carrier attaches both. They belong together because
    they share one deadline: both must be live BEFORE the first sample's
    readiness barrier — in a composed world WE are the ``/clock`` source and the
    barrier waits for clock FLOW (G-19), and an unattached legged robot spends
    the whole wait lying on the floor (C1 §6-3).

    Extracted from ``run`` rather than inlined next to the wire: the carrier loop
    sits at the C901 ceiling, and these two branches are the ones a unit test can
    still reach (everything around them needs a GPU).
    """
    from cv_infra.runner.go2_wiring import subscribe_cmd_vel  # noqa: PLC0415

    if sensors is not None:
        _emit(sensors.attach(adapter.node, sim.on_step))
    if policy is not None:
        subscribe_cmd_vel(adapter.node, config.cmd_vel, policy.set_command)


def _settle_world(adapter: object, settle_s: float) -> float:
    """Pump the sim ``settle_s`` SIM-seconds; return the sim time reached.

    Called on BOTH sides of the realign (see ``REALIGN_SETTLE_ENV``): before it so
    the seed carries a pose that has stopped moving (AR-19), after it so the SUT
    has acted on that seed before the mission is dispatched.

    SIM seconds (D-F) because that is what the teleported robot's physics and
    AMCL's re-convergence actually run on. The wall cap is the escape
    hatch: a sim whose ``/clock`` has stopped would spin here forever, and a
    hung carrier is strictly worse than a normal readiness failure — the loop
    ends, the barrier below reports it, and the batch exits 3.

    Pumping (not sleeping) is mandatory: the sim IS the /clock source the SUT is
    waiting on (G-19), so a wait that does not step it waits on itself.
    """
    settle_until = adapter.sim_time_s + settle_s
    settle_deadline = time.monotonic() + SETTLE_WALL_BUDGET_S
    while adapter.sim_time_s < settle_until and time.monotonic() < settle_deadline:
        adapter.step_and_spin()
    return adapter.sim_time_s


# --------------------------------------------------------------------------- #
# The carrier loop (M2 §3.2 order, boot-once) — Isaac-deferred; W1/W2/W3 measured it.
# --------------------------------------------------------------------------- #
def run(env: dict | None = None) -> int:  # pragma: no cover - GPU path (W2/W3 measured)
    """Boot once, run every admitted sample in series, write one result each.

    Every seam this touches is the SAME one ``main.run`` uses (SimRuntime,
    Ros2Adapter, PhysicsTelemetrySampler, EvaluationEngine, the recorders,
    ``build_result_dict``, ``write_result``) — a per-sample result is assembled by
    the very code that assembles a per-job one, which is what keeps the two
    entrypoints from drifting into two different definitions of a result.

    Failure stance (P5-13): a completed sample's result is on disk before the next
    one starts, so a later death cannot retract it. A readiness failure or an
    exception stops the batch (the world is no longer trustworthy) with a degraded
    error result for the sample in flight, a summary carrying the reason, and
    exit 3 — never a silently short batch.
    """
    from cv_infra.contract.schema import Artifacts  # noqa: PLC0415
    from cv_infra.oracles.reached_goal import resolve_position_tolerance  # noqa: PLC0415
    from cv_infra.runner.adapter.ros2 import Ros2Adapter  # noqa: PLC0415
    from cv_infra.runner.boot_trace import (  # noqa: PLC0415
        PHASE_ADAPTER_WIRE,
        PHASE_MISSION,
        PHASE_ROS_BRIDGE_READY,
        PHASE_SIMULATION_APP_INIT,
        BootTrace,
        emit_cache_delta,
        emit_cache_probe,
        install_readonly_error_counter,
        observe,
    )
    from cv_infra.runner.go2_wiring import attach_policy_loop  # noqa: PLC0415
    from cv_infra.runner.realign import (  # noqa: PLC0415
        SutRealigner,
        realign_seed,
        realign_seed_log,
    )
    from cv_infra.runner.recording import (  # noqa: PLC0415
        LoopVideoRecorder,
        RosbagRecorder,
        plan_artifacts,
        step_rate_hz,
    )
    from cv_infra.runner.ros_bridge import (  # noqa: PLC0415
        bootstrap_bridge_env,
        enable_bridge,
        honored_env,
        reexec_for_bridge_lib,
    )
    from cv_infra.runner.sim_runtime import SimRuntime  # noqa: PLC0415
    from cv_infra.runner.telemetry import (  # noqa: PLC0415
        PhysicsTelemetrySampler,
        TelemetryRecord,
        count_real_collisions,
        min_clearance_m,
        path_length_m,
        time_to_goal_s,
    )

    # ---- pre-boot: nothing below costs a GPU second ----------------------- #
    admitted = _admit(env)
    out_root, specs, summary = admitted.out_root, admitted.specs, admitted.summary
    identity_key, settle_s = admitted.identity_key, admitted.settle_s

    head = specs[0]
    base_config = head.adapter_config
    chassis_path = read_field(head.criteria, "chassis_path", "")
    excluded_paths = read_field(head.criteria, "collision_excluded_paths", []) or []
    staging = _plan_staging(specs, base_config)
    # D-3: ONE policy for the carrier — ``_UNIFORM_FIELDS`` already refused a
    # batch whose samples pin different ones, so loading sample 0's bytes here is
    # loading every sample's. Still pre-boot: a digest mismatch is exit 2.
    policy = load_firmware_slot(head.policy)
    # D-2: the same runner-published sensor suite ``main.run`` builds — a COMPOSED
    # scene (go2) ships no vendor ROS graph, not even /clock, so without this a
    # carrier would publish nothing and every sample would fail the readiness
    # barrier on a clock nobody sources. Built from the HEAD spec because the
    # three inputs it reads (scene, adapter_config, criteria.chassis_path) are all
    # ``_UNIFORM_FIELDS`` rows, i.e. already proven identical across the batch.
    sensors = build_sensor_suite(head.request, head.criteria)

    trace = BootTrace()
    sim = SimRuntime(sim_config_for(head.request), trace=trace)
    adapter = Ros2Adapter(base_config, stepper=sim.step)
    sampler = PhysicsTelemetrySampler(chassis_path, excluded_paths)
    video = None
    rosbag = None
    cache_before = None
    erofs_counter = None
    current: ParsedSpec | None = None
    watch = _Stopwatch()
    try:
        # ---- boot ONCE (main.run's step 0.5-4, unchanged in order) --------- #
        watch.begin("bootstrap")
        bootstrap = bootstrap_bridge_env(base_config.ros_distro, base_config.rmw)
        print(f"[cv-runner] bridge bootstrap: {bootstrap}", flush=True)
        # OUR module, not main's (see reexec_argv): the default argv would replace
        # this process image with the single-job entrypoint mid-boot.
        reexec_for_bridge_lib(bootstrap, argv=reexec_argv())
        erofs_counter = observe("erofs counter", install_readonly_error_counter)
        cache_before = observe("cache probe", emit_cache_probe)
        summary.doc["boot"]["bootstrap_s"] = round(watch.end("bootstrap"), 4)

        watch.begin("simulation_app_init")
        trace.begin(PHASE_SIMULATION_APP_INIT)
        sim.boot()
        trace.end(PHASE_SIMULATION_APP_INIT)
        summary.doc["boot"]["simulation_app_init_s"] = round(watch.end("simulation_app_init"), 4)

        _ = honored_env()
        watch.begin("ros_bridge_ready")
        trace.begin(PHASE_ROS_BRIDGE_READY)
        enable_bridge(sim.simulation_app)
        trace.end(PHASE_ROS_BRIDGE_READY)
        summary.doc["boot"]["ros_bridge_ready_s"] = round(watch.end("ros_bridge_ready"), 4)

        # Scene + robot + sample 0's staging. The pre_reset hooks are main's, in
        # main's order: telemetry bind must precede reset (the tensor view is
        # invalidated by a post-reset create), the obstacle must exist before the
        # physics parse, and declared sensors' render products only engage pre-play.
        watch.begin("scene_load")
        sim.pre_reset.append(sampler.bind)
        if staging.debug_obstacle is not None:
            sim.pre_reset.append(lambda _world: sim.spawn_debug_obstacle(staging.debug_obstacle))
        if staging.pool_plan:
            sim.pre_reset.append(lambda _world: sim.spawn_obstacle_pool(staging.pool_plan))
            sim.pre_reset.append(lambda _world: sim.apply_obstacle_set(staging.head_obstacles))
        register_sensor_hooks(sim, sensors, staging.sensor_topics)
        sim.load_scene(identity_key)  # emits sample 0's sim_config line
        sampler.attach(sim.world)
        if policy is not None:  # C2b: post-reset bind + physics callback (measured)
            attach_policy_loop(policy, sim)
        summary.doc["boot"]["scene_load_s"] = round(watch.end("scene_load"), 4)

        watch.begin("adapter_wire")
        trace.begin(PHASE_ADAPTER_WIRE)
        adapter.wire(sim.simulation_app, base_config)
        trace.end(PHASE_ADAPTER_WIRE)
        # Sensor publishers + the policy's /cmd_vel, ONCE per carrier and before
        # the first sample's barrier (see the helper). The publishers outlive
        # every sample: a per-sample attach would create n publishers on one node
        # and be discovered by nobody (G-26).
        _attach_optional_streams(adapter, sim, base_config, sensors, policy)
        summary.doc["boot"]["adapter_wire_s"] = round(watch.end("adapter_wire"), 4)

        # ONE render product + ONE realigner for the whole carrier: a per-sample
        # render product would re-add the VRAM growth term p6c2 removed, and a
        # per-sample publisher would never be discovered in time (G-26).
        video = LoopVideoRecorder(sim_fps=step_rate_hz(sim.config.rendering_dt))
        video.open_render_product()
        sim.on_step.append(video.capture_frame)
        realigner = SutRealigner(adapter.node, adapter.step_and_spin, lambda: adapter.sim_time_s)
        summary.doc["boot"]["total_s"] = boot_total_s(summary.doc["boot"])
        summary.flush()
        print(f"[cv-runner] boot done: {summary.doc['boot']}", flush=True)

        # ---- the samples ---------------------------------------------------- #
        previous_sim_time_s: float | None = None
        for position, parsed in enumerate(specs):
            current = parsed
            request = parsed.request
            criteria = parsed.criteria
            index = parsed.index
            out_dir = iteration_dir(out_root, index)
            iter_watch = _Stopwatch()
            iter_watch.begin("iteration")
            print(
                f"[cv-runner] === sample {position + 1}/{len(specs)} "
                f"index={index} job_id={parsed.job_id} ===",
                flush=True,
            )
            pose = _dumped(request.scenario.initial_pose)
            obstacle = _dumped(request.scenario.debug_obstacle, exclude_none=True)
            # ``None`` = this carrier has no pool (nothing to do). A LIST — empty
            # included — means "these are THIS sample's obstacles, park the rest":
            # folding ``[]`` into None would leave a 0-obstacle sample standing on
            # sample i-1's placement. A carrier without a pool passes None, so a
            # legacy (debug_obstacle-only) batch logs exactly what it logged before.
            obstacle_set = obstacle_specs(request) if staging.pool_plan else None

            iter_watch.begin("restage")
            sim.config.seed = request.scenario.seed
            sim.pin_determinism_seeds()
            if position:  # sample 0's world is the one load_scene just staged
                if policy is not None:
                    # D-5: the loop's carried state IS episode state (the last raw
                    # action feeds the next observation, the command is the previous
                    # mission's last twitch, the joint target is mid-gait). Dropped
                    # BEFORE the restage, not after: ``World.reset(soft=True)`` "will
                    # do one step internally regardless" (vendor docstring, the same
                    # property the telemetry accumulator swap is placed around), and
                    # that step runs the physics callback — with a stale target it
                    # would apply sample i's last gait torque to sample i+1's robot.
                    # After the reset the loop's target IS the stance repose writes
                    # (both are ``go2_constants.DEFAULT_JOINT_POS``), so the world
                    # the settle starts from is coherent by construction.
                    policy.reset()
                sim.restage(pose, obstacle, obstacle_set=obstacle_set)
                sim.emit_sim_config(identity_key)  # per-sample applied-settings line
            iter_watch.end("restage")

            sim_time_start_s = adapter.sim_time_s
            # SETTLE FIRST, THEN REALIGN (AR-19). The reverse order — the one this
            # carrier shipped through W3 — seeds AMCL and only then lets the world
            # move, so a robot that settles somewhere else (a legged one walks:
            # measured 0.97 m, U1 §6-1) starts every mission believing it is a
            # metre behind itself. Nothing else changes: both steps already pumped
            # the sim, and both still run before the record swap below, so the
            # boundary's contact reports stay charged to the retired sample.
            iter_watch.begin("settle")
            _settle_world(adapter, settle_s)
            iter_watch.end("settle")

            iter_watch.begin("realign")
            # The seed is the pose the sampler JUST wrote (the settle's last
            # physics step), not the declared coordinate — ``sampler.record`` is
            # still the retired accumulator here, which is exactly the one those
            # steps appended to.
            seed, seed_source = realign_seed(pose, sampler.latest_pose())
            realign = realigner.realign(seed)
            print(
                f"[cv-runner] sut realign: {realign} {realign_seed_log(seed, seed_source)}",
                flush=True,
            )
            iter_watch.end("realign")

            # ...and the SAME budget again, now for the SUT to ACT on the seed. The
            # pre-C5b order gave the blackbox this window for free (it settled
            # AFTER realigning); moving the settle up to make the seed truthful
            # took it away, and the first measured run said so loudly: the seed
            # landed verbatim (`amcl: Setting pose -5.975 -0.961 1.606`) yet the
            # mission was dispatched 0.1 sim-seconds later, while nav2's TF chain
            # still held the PREVIOUS sample's belief — `Begin navigating from
            # current location (-6.84, 5.89)`, plan of 0 poses, goal aborted after
            # 0.28 sim-seconds (WS run 1, sample 1). A seed nobody has acted on yet
            # is not a realigned SUT.
            iter_watch.begin("converge")
            _settle_world(adapter, settle_s)
            iter_watch.end("converge")

            iter_watch.begin("readiness")
            ready = adapter.await_ready(timeout_s=READINESS_TIMEOUT_S)
            iter_watch.end("readiness")

            item: dict = {
                "index": index,
                "job_id": parsed.job_id,
                "ready": ready,
                "readiness_phase": adapter.readiness_phase,
                "clock_count": adapter.clock_count,
                "realign": realign,
                "sim_time_start_s": sim_time_start_s,
                "sim_time_monotonic": sim_time_advanced(previous_sim_time_s, sim_time_start_s),
                "verdict": None,
                "metrics": {},
                "artifacts": {"mcap": None, "mp4": None},
                "video_frames": 0,
                "gt_pose_samples": 0,
                "contact_events": 0,
                # The obstacle-set counters (NEG-6 gate 5). ``placed`` is what this
                # sample DECLARED, ``pool`` is what the boot authored once, and
                # ``parked`` is the difference — so a pool that grows between
                # samples (i.e. something respawned) is visible in the summary
                # without reading a log. Derived, not GPU-observed: they prove the
                # placement CALL, never that a prim is at those coordinates (W1
                # owns that — same layering as the realign counters).
                "obstacles_placed": len(obstacle_set or []),
                "obstacles_parked": staging.pool_total - len(obstacle_set or []),
                "obstacles_pool": staging.pool_total,
            }
            if not ready:
                # The barrier is the carrier's trust boundary: without a live SUT
                # the remaining samples would all measure the same dead thing.
                item["sim_time_end_s"] = adapter.sim_time_s
                item["verdict"] = VERDICT_ERROR
                iter_watch.end("iteration")
                item["timings_s"] = dict(iter_watch.spans)
                write_result(
                    build_result_dict(
                        parsed.job_id, VERDICT_ERROR, [], {}, request_identity_key=identity_key
                    ),
                    iteration_result_path(out_root, index),
                )
                summary.add_iteration(item)
                summary.finish(
                    error=(
                        f"SUT readiness barrier timed out at sample {index} "
                        f"(phase={adapter.readiness_phase}, clock_count={adapter.clock_count})"
                    )
                )
                print(
                    f"[cv-runner] batch stopped at sample {index}: SUT not ready",
                    file=sys.stderr,
                    flush=True,
                )
                return EXIT_PLATFORM

            plan = plan_artifacts(out_dir)
            # The accumulator is REPLACED, not the binding: a soft restage never
            # destroys the physics simulation view, so the sampler stays bound for
            # the carrier's life (measured p6c2: 60 iterations, bind() 0 extra
            # times). The physics callback reads the attribute every step, so
            # swapping it IS the sample boundary — and it belongs HERE, where
            # ``main.run`` attaches (step 6), not at the top of the iteration:
            # ``World.reset(soft=True)`` "will do one step internally regardless"
            # (vendor docstring), so a record swapped before the restage takes its
            # FIRST GT sample at the previous sample's pose. Measured (p6c3 T3
            # §4): 11/11 teleported samples reported time_to_goal_s = 0.0 and a
            # path_len_m inflated by the teleport distance (i=3: +6.354 m).
            sampler.record = TelemetryRecord()
            iter_watch.begin("record_start")
            rosbag = _start_quiet(RosbagRecorder(plan, base_config))
            video.begin_iteration(plan.video_mp4)
            iter_watch.end("record_start")

            iter_watch.begin("mission")
            trace.begin(PHASE_MISSION)
            outcome = adapter.drive_mission(
                request.scenario.goal, timeout_s=request.scenario.timeout_s
            )
            trace.end(PHASE_MISSION, outcome=outcome.status)
            iter_watch.end("mission")
            print(f"[cv-runner] mission outcome: {outcome}", flush=True)

            iter_watch.begin("record_stop")
            mcap_path = _stop_quiet(rosbag)
            rosbag = None
            mp4_path = video.end_iteration()
            iter_watch.end("record_stop")

            iter_watch.begin("evaluate")
            telemetry = sampler.record
            goal = read_field(criteria, "goal_position")
            tolerance = resolve_position_tolerance(criteria)
            print(f"[cv-runner] reached_goal {tolerance.audit}", flush=True)
            goal_xyz = (float(goal[0]), float(goal[1]), float(goal[2]))
            metrics = {
                "time_to_goal_s": time_to_goal_s(
                    telemetry.gt_pose_samples, goal_xyz, tolerance.value_m
                ),
                "min_clearance_m": min_clearance_m(),
                "collision_count": count_real_collisions(
                    telemetry.contact_events, chassis_path, excluded_paths
                ),
                "path_len_m": path_length_m(telemetry.gt_pose_samples),
            }
            _print_contact_partners(telemetry, chassis_path)  # bring-up debug surface
            verdict, outcomes = EvaluationEngine(parsed.oracles).evaluate(telemetry, criteria)
            artifacts = artifact_paths(mcap_path, mp4_path)
            write_result(
                build_result_dict(
                    parsed.job_id,
                    verdict,
                    outcomes,
                    metrics,
                    artifacts=Artifacts(**artifacts),
                    request_identity_key=identity_key,
                ),
                iteration_result_path(out_root, index),
            )
            iter_watch.end("evaluate")

            previous_sim_time_s = adapter.sim_time_s
            item.update(
                verdict=verdict,
                metrics=metrics,
                artifacts=artifacts,
                video_frames=video.last_frame_count,
                gt_pose_samples=len(telemetry.gt_pose_samples),
                contact_events=len(telemetry.contact_events),
                sim_time_end_s=previous_sim_time_s,
            )
            iter_watch.end("iteration")
            item["timings_s"] = dict(iter_watch.spans)
            summary.add_iteration(item)
            print(f"[cv-runner] sample {index} verdict={verdict} metrics={metrics}", flush=True)
            current = None

        summary.finish()
        print(
            f"[cv-runner] batch complete: {len(specs)} sample(s), verdicts={summary.verdicts()}",
            flush=True,
        )
        return EXIT_PASS
    except EulaNotAcceptedError:
        raise  # main() maps it (exit 3) — same as the single-job entrypoint
    except Exception as exc:
        print(f"[cv-runner] batch runner error: {exc!r}", file=sys.stderr, flush=True)
        if current is not None:
            # The sample in flight still gets a canonical (degraded) result, so M3
            # folds a named error instead of an unexplained missing slot.
            write_result(
                build_result_dict(
                    current.job_id, VERDICT_ERROR, [], {}, request_identity_key=identity_key
                ),
                iteration_result_path(out_root, current.index),
            )
        summary.finish(error=repr(exc))
        return EXIT_PLATFORM
    finally:
        observe("boot summary", trace.emit_summary)
        observe("cache delta", emit_cache_delta, cache_before, erofs_counter)
        if sensors is not None:
            # On EVERY path (main.run's stance): "the camera never produced a
            # frame" is exactly the silence a failed batch needs stated aloud.
            _emit(sensors.detach())
        sampler.detach()
        _abort_recorders(rosbag, video)  # no child process / writer leak
        adapter.teardown()
        # The sim is deliberately NOT closed (G-62): SimulationApp.close() does not
        # return, it ends the process with status 0 and would erase the carrier's
        # exit code. main() -> hard_exit delivers it instead; see runner/main.py's
        # finally block for the measured evidence and the REQ-EXEC-015 trade-off.


def main(env: dict | None = None) -> int:
    """CLI-less entrypoint. Maps setup/platform failures to the exit contract."""
    try:
        return run(env)
    except BadJobSpec as exc:
        print(f"[cv-runner] bad batch spec: {exc}", file=sys.stderr, flush=True)
        return EXIT_USAGE
    except EulaNotAcceptedError as exc:
        print(f"[cv-runner] {exc}", file=sys.stderr, flush=True)
        return EXIT_PLATFORM


if __name__ == "__main__":  # pragma: no cover
    hard_exit(main())  # NOT sys.exit: interpreter shutdown can still eat the code (G-62)
