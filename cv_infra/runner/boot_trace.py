"""Boot-phase timing + cache warm/cold observation (M2, p4c5 T1) — INSTRUMENTATION ONLY.

Why this exists: p4c4 발견 ① — at k=4 every job (8/8) died on the SUT-readiness
barrier with the GPU at 3% util (NOT saturated) and a ~190 s block before the first
frame. The runner could not say WHERE those seconds went, nor whether the mounted
Isaac caches were actually warm. This module makes the boot path say both, one
grep-friendly stderr line at a time, so the Wave-2 GPU experiment can decide
between the cycle-plan's hypotheses with FILE evidence instead of inference.

H1 (the leading hypothesis this module must be able to support or refute): the D-B
two-tier cache binds the three warm cache SETS **read-only** (supervisor
``CACHE_BASE_MOUNTS``), so Kit's shader-cache writes fail (EROFS measured in p4c4
``T4/L0/erofs-lines.txt``) and every job re-pays the cold shader compile — the term
p2c6 measured at 52 s of the 73 s cold penalty (nfr-measurement-notes 2026-07-09).
The discriminating evidence pair emitted here is
(a) ``cache_probe writable=false`` + ``cache_delta written=false entries_added=0``
    on ``/isaac-sim/kit/cache`` (the cache is present but never updated), and
(b) a long ``boot_phase=first_render_frame``/``scene_load`` on EVERY job.
Flip the cache to single-tier rw and, if H1 holds, (a) becomes written=true on the
first job and the phase in (b) collapses.

Scope discipline (cycle-plan Wave 1): this module OBSERVES and PRINTS. It changes
no boot order, no cache mode, no thread count, no setting — mitigation is a Wave-3
decision. Cost is O(1) per job (two bounded filesystem scans, one bool check per
step); there is no polling loop. Instrumentation failures are swallowed with one
loud line (``observe``) — a diagnostic must never kill a job.

Emitted markers (VERBATIM — the grep contract for T4/QA):

    [cv-runner] boot_phase=<name> event=begin elapsed_s=<f> [k=v ...]
    [cv-runner] boot_phase=<name> event=end phase_s=<f> elapsed_s=<f> [k=v ...]
    [cv-runner] boot_summary total_s=<f> boot_to_mission_s=<f|none> reached=<name|none>
                pending=<name[,name]|none> <phase>_s=<f> ...
    [cv-runner] cache_probe path=<p> kind=<base|scratch> exists=<bool> writable=<bool|none>
                errno=<int|none> entries=<int> bytes=<int> newest_age_s=<f|none> truncated=<bool>
    [cv-runner] cache_delta path=<p> kind=<...> written=<bool> entries_added=<int>
                bytes_added=<int> newest_age_s=<f|none> writable=<bool|none> truncated=<bool>
    [cv-runner] cache_summary observed=<n> writable=<n> readonly=<n> written=<n> erofs_py=<n>
                readonly_paths=<p,p,...|none>
    [cv-runner] cache_write_denied errno=30 logger=<name> msg=<truncated>   (first one only)
    [cv-runner] instrumentation error: <label>: <repr>

A phase that BEGAN but never ENDED is the hang localizer: the process can be killed
mid-phase and the streamed ``event=begin`` line still names the blocking phase (the
``boot_summary`` ``pending=`` field says the same when the runner survives).
"""

from __future__ import annotations

import errno as errno_mod
import logging
import os
import sys
import time
from dataclasses import dataclass
from pathlib import Path

LOG_PREFIX = "[cv-runner]"

# --------------------------------------------------------------------------- #
# Boot phases (the p4c5 T1 required set + two seams that isolate the barrier).
# --------------------------------------------------------------------------- #
# Emission ORDER follows the frozen M2 §3.2 boot sequence (instrumentation never
# reorders it), so ``ros_bridge_ready`` precedes ``scene_load`` here even though
# the task lists it later. ``first_render_frame`` NESTS inside
# ``sut_readiness_wait``: the first stepped frame is pumped by the barrier's
# step-and-spin (the sim IS the /clock source — G-19). Nesting is intentional and
# is exactly what tells "the barrier waited" apart from "the first frame blocked".
PHASE_SIMULATION_APP_INIT = "simulation_app_init"  # SimulationApp({...}) ctor + R4 cap
PHASE_ROS_BRIDGE_READY = "ros_bridge_ready"  # enable_extension(isaacsim.ros2.bridge)
PHASE_SCENE_LOAD = "scene_load"  # open_stage + first app pump
PHASE_ROBOT_SPAWN = "robot_spawn"  # World ctor + robot prim resolve + pre_reset + reset
PHASE_ADAPTER_WIRE = "adapter_wire"  # rclpy init/node/clock sub/goal client (no SUT spawn)
PHASE_SUT_READINESS_WAIT = "sut_readiness_wait"  # the barrier (p4c4 발견 ①'s grave)
PHASE_FIRST_RENDER_FRAME = "first_render_frame"  # first world.step(render=True) COMPLETED
PHASE_MISSION_START = "mission_start"  # telemetry attach + recorders -> goal dispatch
PHASE_MISSION = "mission"  # drive_mission (wall; sim-time stays in MissionOutcome)

BOOT_PHASES: tuple[str, ...] = (
    PHASE_SIMULATION_APP_INIT,
    PHASE_ROS_BRIDGE_READY,
    PHASE_SCENE_LOAD,
    PHASE_ROBOT_SPAWN,
    PHASE_ADAPTER_WIRE,
    PHASE_SUT_READINESS_WAIT,
    PHASE_FIRST_RENDER_FRAME,
    PHASE_MISSION_START,
    PHASE_MISSION,
)

# --------------------------------------------------------------------------- #
# Cache observation targets (container-side paths).
# --------------------------------------------------------------------------- #
# SOURCE OF TRUTH: cv_infra/orchestrator/supervisor.py ``CACHE_BASE_MOUNTS`` /
# ``CACHE_SCRATCH_MOUNTS`` (container bind paths). The runner is the data plane and
# must not import the control plane (the runner image installs the wheel --no-deps
# and has no docker SDK), so the paths are MIRRORED here and pinned by a mechanical
# cross-module guard (tests/test_runner_boot_trace.py — G-25: a copy without a guard
# drifts silently). In two-tier mode (D-B) the three ``base`` paths bind READ-ONLY
# and the three ``scratch`` paths bind rw; with no cache mounts at all these are
# plain image dirs in the container's writable layer (writable=true, discarded at
# exit = every job cold). Which of the three configurations a run is in is exactly
# what ``cache_probe``/``cache_summary`` report.
CACHE_BASE_PATHS: tuple[str, ...] = (
    "/isaac-sim/kit/cache",  # Kit cache — the shader cache lives here (H1's target)
    "/isaac-sim/.cache",  # ov / GL shader disk cache / warp
    "/isaac-sim/.nv/ComputeCache",  # CUDA compute cache
)
CACHE_SCRATCH_PATHS: tuple[str, ...] = (
    "/isaac-sim/.nvidia-omniverse/logs",
    "/isaac-sim/.local/share/ov/data",
    "/isaac-sim/Documents",
)
CACHE_OBSERVATIONS: tuple[tuple[str, str], ...] = tuple(
    (path, "base") for path in CACHE_BASE_PATHS
) + tuple((path, "scratch") for path in CACHE_SCRATCH_PATHS)

# Bounded scan budget: keeps the observation O(1) per job (a warm base cache is
# ~1.8 GB / thousands of files; an unbounded walk would itself distort the
# measurement it exists to make). A truncated scan is reported as truncated=true —
# never silently rounded (the delta is then a lower bound).
MAX_SCAN_ENTRIES = 20_000


def _fmt(value: object) -> str:
    """Render one field value: none / true|false / 2-decimal floats (log idiom)."""
    if value is None:
        return "none"
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, float):
        return f"{value:.2f}"
    return str(value)


def _fields(fields: dict | None) -> str:
    return "".join(f" {key}={_fmt(value)}" for key, value in (fields or {}).items())


# --------------------------------------------------------------------------- #
# Phase timing — pure line assembly + the tiny stateful tracer (CPU-tested).
# --------------------------------------------------------------------------- #
def phase_line(
    phase: str,
    event: str,
    elapsed_s: float,
    phase_s: float | None = None,
    fields: dict | None = None,
) -> str:
    """One boot-phase record: cumulative elapsed AND (on end) the phase's own cost."""
    head = f"{LOG_PREFIX} boot_phase={phase} event={event}"
    if phase_s is not None:
        head += f" phase_s={_fmt(phase_s)}"
    return f"{head} elapsed_s={_fmt(elapsed_s)}{_fields(fields)}"


@dataclass
class _PhaseSpan:
    began_s: float
    ended_s: float | None = None


class BootTrace:
    """Streams one line per phase boundary; ``summary_line`` folds the run.

    ``elapsed_s`` is relative to tracer construction (runner entry, POST the
    ``reexec_for_bridge_lib`` re-exec — G-23 — so the container's first ~1 s of
    interpreter startup sits outside it; the container log timestamps carry that).
    All emission is guarded: a broken clock/stream degrades to one loud line, never
    to a dead job.
    """

    def __init__(self, stream=None, clock=time.monotonic) -> None:
        self._stream = sys.stderr if stream is None else stream
        self._clock = clock
        self._t0 = clock()
        self._spans: dict[str, _PhaseSpan] = {}
        self._order: list[str] = []

    def begin(self, phase: str, **fields) -> None:
        try:
            now = self._clock()
            self._spans[phase] = _PhaseSpan(began_s=now)
            if phase not in self._order:
                self._order.append(phase)
            self._emit(phase_line(phase, "begin", elapsed_s=now - self._t0, fields=fields))
        except Exception as exc:  # instrumentation must never kill the job
            self._loud(f"boot_phase begin {phase}", exc)

    def end(self, phase: str, **fields) -> None:
        try:
            now = self._clock()
            span = self._spans.get(phase)
            began = self._t0 if span is None else span.began_s
            if span is not None:
                span.ended_s = now
            self._emit(
                phase_line(
                    phase,
                    "end",
                    elapsed_s=now - self._t0,
                    phase_s=now - began,
                    fields=fields,
                )
            )
        except Exception as exc:
            self._loud(f"boot_phase end {phase}", exc)

    def durations(self) -> dict[str, float]:
        """Completed phases -> wall seconds (emission order preserved)."""
        return {
            phase: self._spans[phase].ended_s - self._spans[phase].began_s
            for phase in self._order
            if self._spans[phase].ended_s is not None
        }

    def pending(self) -> list[str]:
        """Phases that began and never ended — the hang localizer."""
        return [phase for phase in self._order if self._spans[phase].ended_s is None]

    def summary_line(self) -> str:
        """One-line fold of the whole boot (T4 builds its per-job table from this)."""
        total_s = self._clock() - self._t0
        mission_span = self._spans.get(PHASE_MISSION_START)
        boot_to_mission_s = (
            None
            if mission_span is None or mission_span.ended_s is None
            else mission_span.ended_s - self._t0
        )
        pending = self.pending()
        parts = [
            f"{LOG_PREFIX} boot_summary",
            f"total_s={_fmt(total_s)}",
            f"boot_to_mission_s={_fmt(boot_to_mission_s)}",
            f"reached={_fmt(self._order[-1] if self._order else None)}",
            f"pending={','.join(pending) if pending else 'none'}",
        ]
        parts += [f"{phase}_s={_fmt(value)}" for phase, value in self.durations().items()]
        return " ".join(parts)

    def emit_summary(self) -> str:
        line = self.summary_line()
        self._emit(line)
        return line

    def _emit(self, line: str) -> None:
        print(line, file=self._stream, flush=True)

    def _loud(self, label: str, exc: Exception) -> None:
        try:
            print(
                f"{LOG_PREFIX} instrumentation error: {label}: {exc!r}",
                file=self._stream,
                flush=True,
            )
        except Exception:  # the stream itself is gone — stay silent, stay alive
            pass


# --------------------------------------------------------------------------- #
# Cache warm/cold observation (H1) — filesystem truth, no Isaac/Kit API.
# --------------------------------------------------------------------------- #
@dataclass(frozen=True)
class CacheSnapshot:
    """What one mounted cache dir looked like at one instant."""

    path: str
    kind: str  # base (ro in two-tier) | scratch (always rw)
    exists: bool
    writable: bool | None  # None = path absent (nothing to write into)
    write_errno: int | None  # 30 = EROFS (the RO-mount signature), 13 = EACCES, ...
    entries: int  # files (dirs are walked, not counted)
    bytes: int
    newest_mtime: float | None
    truncated: bool  # scan hit MAX_SCAN_ENTRIES -> counts are a lower bound


def _create_probe_file(path: Path) -> None:
    fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    os.close(fd)


def probe_writable(path, opener=None) -> tuple[bool | None, int | None]:
    """MEASURE rw/ro by actually creating (then removing) a file.

    ``os.access`` answers the permission bits and is advisory; a read-only bind
    mount answers only on the write itself (EROFS=30). The probe file is unlinked
    immediately and is never counted (snapshots count files, so a bumped directory
    mtime cannot fake a cache write). ``opener`` is injectable so the EROFS branch
    is CPU-testable without a read-only filesystem (same idiom as
    ``ros_bridge.reexec_for_bridge_lib``'s ``execv``).
    """
    directory = Path(path)
    if not directory.is_dir():
        return None, None
    probe = directory / f".cv_boot_probe.{os.getpid()}"
    create = _create_probe_file if opener is None else opener
    try:
        create(probe)
    except OSError as exc:
        return False, exc.errno
    finally:
        try:
            probe.unlink()
        except OSError:
            pass
    return True, None


def scan_dir(path, budget: int = MAX_SCAN_ENTRIES) -> tuple[int, int, float | None, bool]:
    """Bounded recursive walk -> (files, bytes, newest_mtime, truncated)."""
    files = 0
    total_bytes = 0
    newest: float | None = None
    stack = [str(path)]
    while stack:
        current = stack.pop()
        try:
            scanner = os.scandir(current)
        except OSError:
            continue  # unreadable subtree: skip, never raise (diagnostic path)
        with scanner:
            for entry in scanner:
                if files >= budget:
                    return files, total_bytes, newest, True
                try:
                    if entry.is_dir(follow_symlinks=False):
                        stack.append(entry.path)
                        continue
                    stat = entry.stat(follow_symlinks=False)
                except OSError:
                    continue
                files += 1
                total_bytes += stat.st_size
                if newest is None or stat.st_mtime > newest:
                    newest = stat.st_mtime
    return files, total_bytes, newest, False


def snapshot_cache(
    path: str, kind: str, budget: int = MAX_SCAN_ENTRIES, opener=None
) -> CacheSnapshot:
    """One cache dir: existence + measured writability + bounded content census."""
    directory = Path(path)
    if not directory.is_dir():
        return CacheSnapshot(str(path), kind, False, None, None, 0, 0, None, False)
    writable, write_errno = probe_writable(directory, opener=opener)
    files, total_bytes, newest, truncated = scan_dir(directory, budget)
    return CacheSnapshot(
        str(path), kind, True, writable, write_errno, files, total_bytes, newest, truncated
    )


def snapshot_caches(
    observations: tuple[tuple[str, str], ...] = CACHE_OBSERVATIONS,
    budget: int = MAX_SCAN_ENTRIES,
) -> list[CacheSnapshot]:
    return [snapshot_cache(path, kind, budget=budget) for path, kind in observations]


def cache_probe_line(snapshot: CacheSnapshot, now: float | None = None) -> str:
    """Pre-boot census of one cache dir (was it warm? is it writable?)."""
    stamp = time.time() if now is None else now
    age = None if snapshot.newest_mtime is None else stamp - snapshot.newest_mtime
    return (
        f"{LOG_PREFIX} cache_probe path={snapshot.path} kind={snapshot.kind} "
        f"exists={_fmt(snapshot.exists)} writable={_fmt(snapshot.writable)} "
        f"errno={_fmt(snapshot.write_errno)} entries={snapshot.entries} "
        f"bytes={snapshot.bytes} newest_age_s={_fmt(age)} truncated={_fmt(snapshot.truncated)}"
    )


def cache_written(before: CacheSnapshot, after: CacheSnapshot) -> bool:
    """Did this job put anything INTO the cache? (H1's write-side evidence.)"""
    if after.entries != before.entries or after.bytes != before.bytes:
        return True
    return (after.newest_mtime or 0.0) > (before.newest_mtime or 0.0)


def cache_delta_line(before: CacheSnapshot, after: CacheSnapshot, now: float | None = None) -> str:
    """Post-job delta for one cache dir — written=false + a long first frame on EVERY
    job is H1's signature (cache read-only => shaders recompiled and thrown away)."""
    stamp = time.time() if now is None else now
    age = None if after.newest_mtime is None else stamp - after.newest_mtime
    return (
        f"{LOG_PREFIX} cache_delta path={after.path} kind={after.kind} "
        f"written={_fmt(cache_written(before, after))} "
        f"entries_added={after.entries - before.entries} "
        f"bytes_added={after.bytes - before.bytes} newest_age_s={_fmt(age)} "
        f"writable={_fmt(after.writable)} "
        f"truncated={_fmt(before.truncated or after.truncated)}"
    )


def cache_summary_line(
    before: list[CacheSnapshot], after: list[CacheSnapshot], erofs_py: int | None
) -> str:
    """The H1 headline: how many observed cache dirs were read-only, and how many
    were actually written during this job."""
    after_by_path = {snapshot.path: snapshot for snapshot in after}
    written = sum(
        1
        for snapshot in before
        if snapshot.path in after_by_path and cache_written(snapshot, after_by_path[snapshot.path])
    )
    readonly = [snapshot.path for snapshot in before if snapshot.writable is False]
    return (
        f"{LOG_PREFIX} cache_summary observed={len(before)} "
        f"writable={sum(1 for s in before if s.writable is True)} "
        f"readonly={len(readonly)} written={written} erofs_py={_fmt(erofs_py)} "
        f"readonly_paths={','.join(readonly) if readonly else 'none'}"
    )


# --------------------------------------------------------------------------- #
# EROFS (read-only cache write) counter — python-logging scope ONLY.
# --------------------------------------------------------------------------- #
def is_readonly_error(exc: BaseException | None, message: str = "") -> bool:
    """True when this exception/message is a read-only-filesystem write refusal.

    Anchored on the p4c4 measurement (``T4/L0/erofs-lines.txt``): the RO cache mount
    surfaced as ``OSError(30, 'Read-only file system')`` raised inside an asyncio
    task, which asyncio reports through ``logging`` with ``exc_info``. Both the
    exception chain and the rendered text are checked (a re-raised/pre-formatted
    record loses the OSError but keeps the phrase).
    """
    seen: set[int] = set()
    current = exc
    while current is not None and id(current) not in seen:
        seen.add(id(current))
        if isinstance(current, OSError) and current.errno == errno_mod.EROFS:
            return True
        current = current.__cause__ or current.__context__
    text = message or ""
    return "Read-only file system" in text or f"Errno {errno_mod.EROFS}]" in text


class ReadOnlyErrorCounter(logging.Handler):
    """Counts read-only write refusals that reach PYTHON logging during the job.

    HONEST LIMIT (report this, never round it up): Kit/carb/RTX C++ cache writers
    log through their own sink, not python ``logging`` — so ``erofs_py=0`` does NOT
    prove the caches were writable. The filesystem observations
    (``cache_probe writable=`` / ``cache_delta written=``) are the primary,
    layer-independent evidence; this counter is corroboration for the python-side
    failures p4c4 actually captured, and the container's raw stderr still carries
    the C++-side lines for T4's grep.
    """

    def __init__(self, stream=None, forward: logging.Handler | None = None) -> None:
        super().__init__(level=logging.WARNING)
        self.count = 0
        self._stream = sys.stderr if stream is None else stream
        self._announced = False
        # NON-SUPPRESSION (see install_readonly_error_counter): when we are the only
        # handler on root, every record we accept would otherwise have gone to
        # ``logging.lastResort`` — we pass it on so no log line is LOST by measuring.
        self._forward = forward

    def emit(self, record: logging.LogRecord) -> None:
        try:
            if self._forward is not None and record.levelno >= self._forward.level:
                self._forward.handle(record)
        except Exception:
            pass
        try:
            exc = record.exc_info[1] if record.exc_info else None
            try:
                message = record.getMessage()
            except Exception:
                message = str(record.msg)
            if not is_readonly_error(exc, message):
                return
            self.count += 1
            if not self._announced:
                self._announced = True
                print(
                    f"{LOG_PREFIX} cache_write_denied errno={errno_mod.EROFS} "
                    f"logger={record.name} msg={message[:200]!r}",
                    file=self._stream,
                    flush=True,
                )
        except Exception:  # a counting handler that raises would poison logging
            pass


def install_readonly_error_counter(logger=None, stream=None) -> ReadOnlyErrorCounter:
    """Attach the counter to the root logger (child records propagate up to it).

    Non-suppression is the whole point of the ``forward`` wiring: python's
    ``callHandlers`` uses ``logging.lastResort`` (the implicit stderr sink) ONLY while
    the hierarchy has zero handlers — so naively adding a counting handler to a
    handler-less root would SILENCE every un-configured library warning, including the
    asyncio ``OSError(30)`` report p4c4 harvested from the container log. Measuring
    must not consume the evidence: when root has no handlers we hand each accepted
    record to lastResort ourselves (identical sink, identical default format).
    """
    root = logging.getLogger() if logger is None else logger
    forward = logging.lastResort if not root.handlers else None
    handler = ReadOnlyErrorCounter(stream=stream, forward=forward)
    root.addHandler(handler)
    return handler


# --------------------------------------------------------------------------- #
# Non-fatal wrapper + the two emission entry points main.py calls.
# --------------------------------------------------------------------------- #
def observe(label: str, func, *args, stream=None, **kwargs):
    """Run one instrumentation step; a failure is LOUD but never fatal (req 4)."""
    try:
        return func(*args, **kwargs)
    except Exception as exc:
        print(
            f"{LOG_PREFIX} instrumentation error: {label}: {exc!r}",
            file=sys.stderr if stream is None else stream,
            flush=True,
        )
        return None


def emit_cache_probe(
    observations: tuple[tuple[str, str], ...] = CACHE_OBSERVATIONS, stream=None
) -> list[CacheSnapshot]:
    """Pre-boot: one ``cache_probe`` line per observed cache dir; returns the census
    the post-job delta is measured against."""
    out = sys.stderr if stream is None else stream
    before = snapshot_caches(observations)
    for snapshot in before:
        print(cache_probe_line(snapshot), file=out, flush=True)
    return before


def emit_cache_delta(
    before: list[CacheSnapshot] | None,
    counter: ReadOnlyErrorCounter | None = None,
    stream=None,
) -> list[CacheSnapshot]:
    """Post-job: one ``cache_delta`` line per dir + the ``cache_summary`` headline.

    Called from ``main.run``'s ``finally`` so it lands on EVERY path — above all the
    barrier-timeout path that k=4 dies on (a delta only emitted on success would be
    blind to exactly the failure we are diagnosing).
    """
    if not before:
        return []
    out = sys.stderr if stream is None else stream
    after = snapshot_caches(tuple((s.path, s.kind) for s in before))
    for old, new in zip(before, after, strict=False):
        print(cache_delta_line(old, new), file=out, flush=True)
    print(
        cache_summary_line(before, after, None if counter is None else counter.count),
        file=out,
        flush=True,
    )
    return after
