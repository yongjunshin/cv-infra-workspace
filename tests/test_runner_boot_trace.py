"""CPU unit tests for the p4c5 T1 boot instrumentation (phase timings + cache census).

The GPU bodies (SimulationApp/open_stage/world.reset) stay uncovered by design — what
IS pinned here is everything the Wave-2 experiment will read: the verbatim marker
shapes (T4 greps them), the elapsed/phase arithmetic, the hang localizer (a phase that
began and never ended), the read-only vs writable MEASUREMENT (EROFS branch driven
through an injected opener — no read-only filesystem needed), the written/not-written
delta that decides H1, and the non-fatal contract (a broken observation must never kill
a job).

Fixture anchor (G-28): the read-only signature under test is ``OSError(errno 30,
'Read-only file system')`` — the form p4c4 actually captured from the RO cache mount
(``~/cv-infra-p2-out/p4c4/T4/L0/erofs-lines.txt``), not a shape invented to fit the code.
"""

import errno
import io
import logging

from cv_infra.orchestrator import supervisor
from cv_infra.runner import boot_trace, sim_runtime
from cv_infra.runner.adapter.ros2 import Ros2Adapter


class _FakeClock:
    """Monotonic clock the test drives by hand (deterministic phase arithmetic)."""

    def __init__(self) -> None:
        self.t = 0.0

    def __call__(self) -> float:
        return self.t


def _trace() -> tuple[boot_trace.BootTrace, _FakeClock, io.StringIO]:
    clock = _FakeClock()
    stream = io.StringIO()
    return boot_trace.BootTrace(stream=stream, clock=clock), clock, stream


# --------------------------------------------------------------------------- #
# Phase markers — the grep contract (T4 builds its gate from these literals).
# --------------------------------------------------------------------------- #
def test_phase_lines_carry_both_cumulative_and_phase_time():
    trace, clock, stream = _trace()
    clock.t = 1.0
    trace.begin(boot_trace.PHASE_SIMULATION_APP_INIT)
    clock.t = 15.5
    trace.end(boot_trace.PHASE_SIMULATION_APP_INIT)
    lines = stream.getvalue().splitlines()
    assert lines[0] == "[cv-runner] boot_phase=simulation_app_init event=begin elapsed_s=1.00"
    # phase_s (14.5 = 15.5-1.0) AND elapsed_s (15.5 since tracer start) — task req 1.
    assert lines[1] == (
        "[cv-runner] boot_phase=simulation_app_init event=end phase_s=14.50 elapsed_s=15.50"
    )


def test_readiness_end_line_keeps_the_p4c4_barrier_vocabulary():
    # The k=4 grave: ok/phase/clock_count must ride the end line (p4c4's timeout log
    # already printed phase/clock_count — that contract is preserved, not replaced).
    trace, clock, stream = _trace()
    trace.begin(boot_trace.PHASE_SUT_READINESS_WAIT, timeout_s=180.0)
    clock.t = 180.02
    trace.end(boot_trace.PHASE_SUT_READINESS_WAIT, ok=False, readiness_phase="clock", clock_count=1)
    begin, end = stream.getvalue().splitlines()
    assert begin == (
        "[cv-runner] boot_phase=sut_readiness_wait event=begin elapsed_s=0.00 timeout_s=180.00"
    )
    assert end == (
        "[cv-runner] boot_phase=sut_readiness_wait event=end phase_s=180.02 "
        "elapsed_s=180.02 ok=false readiness_phase=clock clock_count=1"
    )


def test_end_without_begin_falls_back_to_tracer_start():
    trace, clock, stream = _trace()
    clock.t = 3.0
    trace.end(boot_trace.PHASE_MISSION)  # never begun (defensive: no crash, no lie)
    assert "phase_s=3.00 elapsed_s=3.00" in stream.getvalue()


def test_summary_folds_durations_and_names_the_pending_phase():
    # The k=4 shape: readiness begun, first frame begun, neither ended (process killed
    # or barrier expired) -> pending= is the hang localizer.
    trace, clock, stream = _trace()
    trace.begin(boot_trace.PHASE_SIMULATION_APP_INIT)
    clock.t = 14.0
    trace.end(boot_trace.PHASE_SIMULATION_APP_INIT)
    trace.begin(boot_trace.PHASE_SUT_READINESS_WAIT)
    clock.t = 20.0
    trace.begin(boot_trace.PHASE_FIRST_RENDER_FRAME)
    clock.t = 210.0
    line = trace.emit_summary()
    assert line.startswith("[cv-runner] boot_summary total_s=210.00")
    assert "boot_to_mission_s=none" in line  # never reached the mission
    assert "reached=first_render_frame" in line
    assert "pending=sut_readiness_wait,first_render_frame" in line
    assert "simulation_app_init_s=14.00" in line  # only ENDED phases get a duration
    assert "sut_readiness_wait_s=" not in line
    assert line in stream.getvalue()


def test_summary_reports_boot_to_mission_on_the_happy_path():
    trace, clock, stream = _trace()
    trace.begin(boot_trace.PHASE_MISSION_START)
    clock.t = 61.2
    trace.end(boot_trace.PHASE_MISSION_START)
    clock.t = 103.3
    line = trace.emit_summary()
    assert "boot_to_mission_s=61.20" in line  # elapsed at goal dispatch = the headline
    assert "mission_start_s=61.20" in line
    assert "pending=none" in line
    assert stream  # emitted, not just returned


def test_required_phase_set_is_complete():
    # Task req 1's minimum set (+ two seams that isolate the barrier: adapter_wire,
    # mission). Renaming a phase breaks T4's grep gate -> break this test first.
    required = {
        "simulation_app_init",
        "scene_load",
        "robot_spawn",
        "first_render_frame",
        "ros_bridge_ready",
        "sut_readiness_wait",
        "mission_start",
    }
    assert required <= set(boot_trace.BOOT_PHASES)
    assert set(boot_trace.BOOT_PHASES) - required == {"adapter_wire", "mission"}


def test_trace_failure_is_loud_but_never_fatal():
    # A broken clock (stand-in for any instrumentation fault) must not raise out of
    # begin/end — the job is the product, the marker is not (task req 4).
    def broken() -> float:
        raise RuntimeError("clock exploded")

    stream = io.StringIO()
    trace = boot_trace.BootTrace(stream=stream, clock=lambda: 0.0)
    trace._clock = broken  # after construction, so __init__'s t0 succeeded
    trace.begin("x")
    trace.end("x")
    out = stream.getvalue()
    assert out.count("[cv-runner] instrumentation error:") == 2
    assert "clock exploded" in out


def test_observe_swallows_and_reports_a_failed_observation():
    stream = io.StringIO()

    def boom():
        raise OSError("no /proc for you")

    assert boot_trace.observe("cache probe", boom, stream=stream) is None
    assert "[cv-runner] instrumentation error: cache probe: OSError" in stream.getvalue()


# --------------------------------------------------------------------------- #
# Cache census — H1's evidence (warm? writable? written?).
# --------------------------------------------------------------------------- #
def test_observed_cache_paths_match_the_supervisor_mount_table():
    # G-25 mechanical guard: the runner mirrors the container-side bind paths (it may
    # not import the control plane), so a supervisor mount-table edit that leaves this
    # copy behind FAILS here instead of silently observing dirs nobody mounts.
    assert boot_trace.CACHE_BASE_PATHS == tuple(c for _, c in supervisor.CACHE_BASE_MOUNTS)
    assert boot_trace.CACHE_SCRATCH_PATHS == tuple(c for _, c in supervisor.CACHE_SCRATCH_MOUNTS)
    assert [p for p, _ in boot_trace.CACHE_OBSERVATIONS] == [c for _, c in supervisor.CACHE_MOUNTS]
    kinds = {kind for _, kind in boot_trace.CACHE_OBSERVATIONS}
    assert kinds == {"base", "scratch"}


def test_writable_dir_is_measured_writable_and_censused(tmp_path):
    (tmp_path / "sub").mkdir()
    (tmp_path / "sub" / "shader.bin").write_bytes(b"x" * 128)
    snap = boot_trace.snapshot_cache(str(tmp_path), "base")
    assert snap.exists and snap.writable is True and snap.write_errno is None
    assert snap.entries == 1 and snap.bytes == 128 and not snap.truncated
    assert snap.newest_mtime is not None
    line = boot_trace.cache_probe_line(snap, now=snap.newest_mtime + 10.0)
    assert line.startswith(f"[cv-runner] cache_probe path={tmp_path} kind=base ")
    assert "exists=true writable=true errno=none entries=1 bytes=128" in line
    assert "newest_age_s=10.00 truncated=false" in line


def test_readonly_mount_is_measured_by_the_write_attempt(tmp_path):
    # Injected opener reproduces the MEASURED RO-bind failure (p4c4 erofs-lines.txt);
    # os.access-style permission bits would say "writable" on some of these mounts.
    def erofs(_path):
        raise OSError(errno.EROFS, "Read-only file system")

    writable, err = boot_trace.probe_writable(tmp_path, opener=erofs)
    assert writable is False and err == 30
    snap = boot_trace.snapshot_cache(str(tmp_path), "base", opener=erofs)
    assert snap.writable is False and snap.write_errno == 30
    assert "writable=false errno=30" in boot_trace.cache_probe_line(snap)
    assert not any(p.name.startswith(".cv_boot_probe") for p in tmp_path.iterdir())


def test_missing_cache_path_is_exists_false_not_a_crash(tmp_path):
    snap = boot_trace.snapshot_cache(str(tmp_path / "absent"), "scratch")
    assert snap.exists is False and snap.writable is None and snap.entries == 0
    assert "exists=false writable=none" in boot_trace.cache_probe_line(snap)


def test_scan_is_bounded_and_says_so(tmp_path):
    for i in range(5):
        (tmp_path / f"f{i}").write_bytes(b"ab")
    snap = boot_trace.snapshot_cache(str(tmp_path), "base", budget=2)
    assert snap.truncated is True and snap.entries == 2  # O(1) per job, never a full walk
    assert "truncated=true" in boot_trace.cache_probe_line(snap)


def test_delta_says_written_when_the_cache_grew(tmp_path):
    before = boot_trace.snapshot_cache(str(tmp_path), "base")
    (tmp_path / "new_shader.bin").write_bytes(b"y" * 64)
    after = boot_trace.snapshot_cache(str(tmp_path), "base")
    assert boot_trace.cache_written(before, after) is True
    line = boot_trace.cache_delta_line(before, after)
    assert "written=true entries_added=1 bytes_added=64" in line
    assert f"path={tmp_path} kind=base" in line


def test_delta_says_not_written_when_boot_only_read_it(tmp_path):
    # H1's positive signature: a POPULATED cache that the job never updated.
    (tmp_path / "warm_shader.bin").write_bytes(b"z" * 32)
    before = boot_trace.snapshot_cache(str(tmp_path), "base")
    after = boot_trace.snapshot_cache(str(tmp_path), "base")  # nothing happened between
    assert boot_trace.cache_written(before, after) is False
    assert "written=false entries_added=0 bytes_added=0" in boot_trace.cache_delta_line(
        before, after
    )


def _snap(path, kind, writable, write_errno, entries, size) -> boot_trace.CacheSnapshot:
    return boot_trace.CacheSnapshot(
        path, kind, True, writable, write_errno, entries, size, None, False
    )


def test_cache_summary_is_the_h1_headline():
    # The two-tier signature: a populated-but-read-only base + a written scratch.
    ro = _snap("/isaac-sim/kit/cache", "base", False, 30, 900, 1)
    rw_before = _snap("/isaac-sim/Documents", "scratch", True, None, 0, 0)
    rw_after = _snap("/isaac-sim/Documents", "scratch", True, None, 2, 8)
    line = boot_trace.cache_summary_line([ro, rw_before], [ro, rw_after], erofs_py=3)
    assert (
        line == "[cv-runner] cache_summary observed=2 writable=1 readonly=1 written=1 "
        "erofs_py=3 readonly_paths=/isaac-sim/kit/cache"
    )


def test_emit_cache_probe_then_delta_round_trip(tmp_path):
    stream = io.StringIO()
    observations = ((str(tmp_path), "base"),)
    before = boot_trace.emit_cache_probe(observations, stream=stream)
    (tmp_path / "compiled.bin").write_bytes(b"q")
    boot_trace.emit_cache_delta(before, counter=None, stream=stream)
    out = stream.getvalue()
    assert out.count("[cv-runner] cache_probe ") == 1
    assert out.count("[cv-runner] cache_delta ") == 1
    assert "written=true entries_added=1" in out
    assert (
        "[cv-runner] cache_summary observed=1 writable=1 readonly=0 written=1 erofs_py=none"
    ) in out


def test_emit_cache_delta_without_a_probe_is_a_no_op():
    stream = io.StringIO()
    assert boot_trace.emit_cache_delta(None, stream=stream) == []
    assert stream.getvalue() == ""


# --------------------------------------------------------------------------- #
# EROFS counter (python-logging scope only — the honest-limit surface).
# --------------------------------------------------------------------------- #
def test_is_readonly_error_matches_the_measured_oserror_and_its_text():
    assert boot_trace.is_readonly_error(OSError(errno.EROFS, "Read-only file system"))
    assert boot_trace.is_readonly_error(None, "[Errno 30] Read-only file system: '/isaac-sim'")
    # Chained (asyncio re-raises inside a task) still resolves.
    try:
        try:
            raise OSError(errno.EROFS, "Read-only file system")
        except OSError as exc:
            raise RuntimeError("cache write failed") from exc
    except RuntimeError as chained:
        assert boot_trace.is_readonly_error(chained)
    # Permission-denied is NOT a read-only mount (different cause, different fix).
    assert not boot_trace.is_readonly_error(OSError(errno.EACCES, "Permission denied"))
    assert not boot_trace.is_readonly_error(None, "all good")


def _record(exc: BaseException | None, msg: str = "Task exception was never retrieved"):
    return logging.LogRecord(
        name="asyncio",
        level=logging.ERROR,
        pathname=__file__,
        lineno=1,
        msg=msg,
        args=(),
        exc_info=(type(exc), exc, None) if exc is not None else None,
    )


def test_counter_counts_erofs_records_and_announces_once():
    stream = io.StringIO()
    counter = boot_trace.ReadOnlyErrorCounter(stream=stream)
    counter.emit(_record(OSError(errno.EROFS, "Read-only file system")))
    counter.emit(_record(OSError(errno.EROFS, "Read-only file system")))
    counter.emit(_record(OSError(errno.EACCES, "Permission denied")))  # not a RO mount
    counter.emit(_record(None, "nothing to see"))
    assert counter.count == 2
    out = stream.getvalue()
    assert out.count("[cv-runner] cache_write_denied errno=30 logger=asyncio") == 1  # loud once


def test_counter_never_swallows_the_lastresort_stderr_sink(capsys):
    # THE non-invasiveness trap: python routes records to ``logging.lastResort``
    # (stderr) only while the hierarchy has ZERO handlers. A naive counting handler on
    # a handler-less root would therefore SILENCE every unconfigured library warning —
    # including the asyncio OSError(30) line p4c4 harvested from the container log.
    # Measuring must not consume the evidence it measures.
    root = logging.getLogger()
    saved = root.handlers[:]
    root.handlers = []
    try:
        counter = boot_trace.install_readonly_error_counter(stream=io.StringIO())
        logging.getLogger("cv.test.unconfigured").warning("kit wants stderr")
        assert "kit wants stderr" in capsys.readouterr().err  # still printed
        assert counter.count == 0
    finally:
        root.handlers = saved


def test_counter_adds_no_output_when_logging_is_already_configured(capsys):
    root = logging.getLogger()
    saved = root.handlers[:]
    root.handlers = [logging.StreamHandler(io.StringIO())]
    try:
        boot_trace.install_readonly_error_counter(stream=io.StringIO())
        logging.getLogger("cv.test.configured").warning("goes to the app's sink")
        assert capsys.readouterr().err == ""  # no forward -> no double print
    finally:
        root.handlers = saved


def test_installed_counter_sees_records_through_the_root_logger():
    stream = io.StringIO()
    counter = boot_trace.install_readonly_error_counter(stream=stream)
    try:
        try:
            raise OSError(errno.EROFS, "Read-only file system")
        except OSError:
            logging.getLogger("cv.test.kit").error("cache write failed", exc_info=True)
        assert counter.count == 1
    finally:
        logging.getLogger().removeHandler(counter)


# --------------------------------------------------------------------------- #
# SimRuntime seam — first_render_frame is emitted exactly once, untraced still runs.
# --------------------------------------------------------------------------- #
class _FakeWorld:
    """A World whose FIRST step blocks (the p4c4 shape) — the clock only moves while
    the step call is in flight, so the trace must bracket the call itself, not the
    step-loop iteration around it."""

    def __init__(self, clock: _FakeClock | None = None, first_step_block_s: float = 0.0) -> None:
        self.steps = 0
        self._clock = clock
        self._block_s = first_step_block_s

    def step(self, render: bool = True) -> None:
        if self.steps == 0 and self._clock is not None:
            self._clock.t += self._block_s
        self.steps += 1


def _sim(trace=None, world: _FakeWorld | None = None) -> sim_runtime.SimRuntime:
    sim = sim_runtime.SimRuntime(
        sim_runtime.SimConfig(scene_ref="nova_carter_warehouse", robot_usd_ref="x"), trace=trace
    )
    sim.world = _FakeWorld() if world is None else world
    return sim


def test_first_step_is_traced_exactly_once():
    trace, clock, stream = _trace()
    clock.t = 30.0
    sim = _sim(trace, world=_FakeWorld(clock, first_step_block_s=190.0))
    sim.step()  # the ~190 s first-frame block p4c4 measured at k=4
    sim.step()
    sim.step()
    lines = [ln for ln in stream.getvalue().splitlines() if "first_render_frame" in ln]
    assert lines == [
        "[cv-runner] boot_phase=first_render_frame event=begin elapsed_s=30.00 render=true",
        "[cv-runner] boot_phase=first_render_frame event=end phase_s=190.00 elapsed_s=220.00",
    ]
    assert sim.world.steps == 3  # instrumentation costs one bool per step, changes nothing


def test_untraced_sim_runtime_still_steps():
    sim = _sim(trace=None)  # runner works without instrumentation (behavior identical)
    sim.step()
    assert sim.world.steps == 1


def test_step_callbacks_still_fire_on_the_traced_first_step():
    trace, _clock, _stream = _trace()
    sim = _sim(trace)
    fired: list[int] = []
    sim.on_step.append(lambda: fired.append(1))
    sim.step()
    sim.step()
    assert fired == [1, 1]  # recorder/video hook contract unchanged


def test_adapter_readiness_phase_is_none_before_the_barrier_runs():
    assert Ros2Adapter().readiness_phase is None  # the trace field's pre-barrier value
