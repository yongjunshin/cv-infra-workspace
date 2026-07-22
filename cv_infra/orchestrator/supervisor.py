"""Supervisor-min — one job: SUT+runner co-spawn -> exactly one result.json (M3 §3.5, D-2).

Control-plane execution seam behind ``cv-infra run`` (decision 2026-07-07 D-2): the
supervisor is the ONLY component holding docker.sock (the runner/adapter are DDS-only).
Per job it

1. creates a per-job docker bridge network + allocates a deterministic
   ``ROS_DOMAIN_ID`` (dual isolation, LOCKED §7.5 — 0..101 domain space);
2. starts the RUNNER first — the sim is the ``/clock`` source, and a ``use_sim_time``
   SUT started before clock flows freezes and aborts its nav2 bringup (G-19 supply
   order: clock -> TF/odom -> sensors). The runner is Isaac and always needs the GPU
   on the default ``cv-infra run`` path, so it gets an all-GPU device request by
   default (``runner_gpus=False`` is the CPU-test opt-out); the SUT never gets one
   (carter nav2 is CPU-only);
3. gates on runner readiness (injectable probe; the default only checks the container
   is running — per G-19, endpoint existence is never flow evidence, so the measured
   /clock-flow probe is workstation glue injected by the Wave-2 task);
4. starts the SUT on the same network/domain as an UNMODIFIED blackbox (no command /
   entrypoint override — DoD-P2-03), absorbing early SUT death (nav2 60s bringup
   window -> ``Aborting bringup`` is terminal with no self-retry — G-19) via a
   bounded restart contract (``sut_restart_limit``; the runner is never restarted);
5. waits for the runner to exit (wall-clock ``job_timeout_s`` watchdog) and collects
   EXACTLY ONE ``result.json`` from RESULT_OUT (REQ-EXEC-013 — 0 or 2+ found is
   recorded as ``infra_error`` with ``result_path=None``);
6. always tears down both containers and the network in ``finally`` — no leftover on
   any path, including exceptions (REQ-EXEC-015 결).

EULA/privacy consent is an OPERATOR input passed through ``runner_env`` verbatim
(decision 2026-07-03 — no consent literal lives in this module); the runner's own
boot guard refuses to start Isaac without it.

``import docker`` is deferred into ``run_job`` so ``import cv_infra.orchestrator``
keeps working where the docker SDK is absent — the runner image installs the wheel
with ``--no-deps`` (DoD-P2-12). Tests inject a duck-typed fake client.

Phase 4 layers ``ParallelSupervisor`` (end of module) on top: k-parallel asyncio
supervision of the per-job seam via ``JobQueue`` + ``SlotAccountant`` +
``DomainIdAllocator``, plus ``reconcile_at_restart`` (R14 — label sweep +
RUNNING-orphan re-label + domain-id/envelope reconciliation after a crash), plus
``RunJobRunner`` (p4c4 glue — the production Runner seam wrapping this very
``run_job`` for the REST path: JOB_SPEC off ``Job.job_spec``, outcome ->
``JobResult`` fold, duck-typed ``job_timeout_s`` declaration). The
single-runner ``run_job`` path above stays frozen (P2/P3 ``cv-infra run`` 계약).
The pure isolation helpers (``allocate_ros_domain_id`` / ``network_name_for`` /
``ROS_DOMAIN_ID_SPACE``) moved verbatim to ``allocator.py`` (M3 §3.6 home) and
are re-exported here.
"""

from __future__ import annotations

import asyncio
import json
import os
import shutil
import stat
import subprocess
import sys
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Any

from cv_infra.orchestrator.allocator import (
    LABEL_JOB_ID,
    LABEL_ROS_DOMAIN_ID,
    DomainIdAllocator,
)

# Redundant-alias imports below = explicit re-exports: these helpers moved
# verbatim to allocator.py (M3 §3.6 home); the supervisor import path stays
# frozen for P2/P3 consumers/tests.
from cv_infra.orchestrator.allocator import (
    ROS_DOMAIN_ID_SPACE as ROS_DOMAIN_ID_SPACE,
)
from cv_infra.orchestrator.allocator import (
    allocate_ros_domain_id as allocate_ros_domain_id,
)
from cv_infra.orchestrator.allocator import (
    network_name_for as network_name_for,
)
from cv_infra.orchestrator.fake_runner import Runner
from cv_infra.orchestrator.models import Job, JobResult, JobState, Verdict
from cv_infra.orchestrator.queue import JobQueue
from cv_infra.orchestrator.scheduler import SlotAccountant
from cv_infra.orchestrator.store import Store, job_key

# Container-side seam paths (M3 -> M2 env contract; 정본 = cv_infra/runner/main.py
# resolve_job_spec_dict / resolve_result_path). JOB_SPEC is bind-mounted read-only
# and passed BY PATH (safe for large specs); RESULT_OUT is a mounted rw directory.
JOB_SPEC_MOUNT = "/cv/job_spec.json"
RESULT_OUT_MOUNT = "/cv/out"

# FU-16 asset cache (decision 2026-07-09 D-1): mount the host Omniverse/asset cache into
# the runner so the ~680 MB / 241-file scene closure downloads ONCE, not every job (T0
# probe: 2nd receive 688 MB -> 1.29 MB — reports/deployment-2026-07-09-fu16-probe.md).
# Bind paths are the MEASURED Isaac 5.1.0 on-disk layout (differs from 6.0, R2).
CACHE_ROOT_ENV = "CV_ISAAC_CACHE_ROOT"

# D-B per-job cache seeding (DoD-P4-15, repaired p4c5): when a scratch root is ALSO given
# (arg or this env), the shared warm base is a COPY SOURCE ONLY — the three warm cache
# SETS (ov/GL/compute, D-B "복수 집합") are eagerly copied (``cp -a``) into a per-job
# scratch dir under this root and bound rw from there, alongside the three always-written
# runtime dirs; the base itself is never bound into any container (writes to the shared
# tree: structurally 0). The per-job tree is discarded at job end (stateless).
#
# WHY NOT the ``:ro`` base bind this used to do (p4c4 D-B): a read-only mount does not
# make the CUDA/Kit caches read-only — it DISABLES them (they cannot open their lock/index
# files for write, so they fall back to recompiling everything). MEASURED on the
# workstation (T4, reports/runner-2026-07-14-p4c5-experiments.md §E1/E2, identical warm
# bytes, mount flag the only variable): ``robot_spawn`` 47s (ro) -> 1.05s (rw), job wall
# 318s -> 104s at k=4, 8/8 pass, while the runner's own ``cache_delta`` showed
# ``entries_added=0`` — the warm content was ALWAYS sufficient; only WRITABILITY was
# missing. Seeding cost, measured: 1.07 s / 930 MB per job. R4 explicitly allows
# "read-only 또는 copy-on-write"; this is the eager-copy CoW branch.
CACHE_SCRATCH_ROOT_ENV = "CV_ISAAC_CACHE_SCRATCH_ROOT"

# (host subpath relative to the cache/scratch root, container bind path)
# In seeding mode these three are BOTH the base subpaths (copy sources under the shared
# base root) AND the per-job destinations (same subpath under the job scratch dir).
CACHE_BASE_MOUNTS: tuple[tuple[str, str], ...] = (
    ("cache/kit", "/isaac-sim/kit/cache"),
    ("cache/home", "/isaac-sim/.cache"),
    ("cache/computecache", "/isaac-sim/.nv/ComputeCache"),
)
CACHE_SCRATCH_MOUNTS: tuple[tuple[str, str], ...] = (
    ("logs", "/isaac-sim/.nvidia-omniverse/logs"),
    ("data", "/isaac-sim/.local/share/ov/data"),
    ("documents", "/isaac-sim/Documents"),
)
# Frozen P2 single-tier contract (D-1 세칙 6종, order preserved verbatim).
CACHE_MOUNTS: tuple[tuple[str, str], ...] = CACHE_BASE_MOUNTS + CACHE_SCRATCH_MOUNTS

# D-1 custom-oracle plugin dir (decision 2026-07-11, wiring contract #3): the scenario
# directory holding consumer oracle .py files is bind-mounted read-only at the SAME
# absolute path inside the RUNNER (G-26 idiom — the runner sys.path's that very string,
# so host/container paths must agree verbatim) and announced via this env. Runner-only:
# the SUT never sees the mount or the env (blackbox no-leak invariant).
ORACLE_PLUGIN_DIR_ENV = "CV_ORACLE_PLUGIN_DIR"

_TEARDOWN_STOP_TIMEOUT_S = 10  # graceful stop window before force-remove
_EXIT_CODE_WAIT_S = 30  # API wait on an already-exited container (returns immediately)

# Single source of the wall-clock runaway watchdog default (operational
# placeholder, not an NFR claim — run_job docstring): run_job's signature and
# the production ``RunJobRunner`` share this ONE constant (이중 정의 금지).
DEFAULT_JOB_TIMEOUT_S = 1800.0

# (a)+(b) transport-gap repair (p5c5 T2, history 2026-07-21 놀란점 3): the SUT/runner
# image pull is UPSTREAM of every existing watchdog. The wall-clock ``job_timeout_s``
# only starts inside ``_supervise_until_runner_exit`` — AFTER the SUT container is
# created — and ``containers.run`` pulls a missing image implicitly and BLOCKING, so a
# wedged GHCR layer pull hung a live E2E 37 min+ with no watchdog firing, while the runner
# meanwhile ran its mission against an absent SUT. ``run_job`` now makes both images PRESENT
# before starting any container (so the runner never begins its mission with the SUT image
# still pulling — the order gate), bounding the pull by a progress-LIVENESS watchdog.
#
# PLACEHOLDER default (operational, not an NFR claim — SAME discipline as
# ``DEFAULT_JOB_TIMEOUT_S``): this is a no-PROGRESS window, NOT a total-pull cap. An active
# pull emits a docker progress event per layer chunk far more often than this, so a
# slow-but-moving 784 MB layer never trips it; a truly wedged registry connection terminates
# the job in minutes, not the 37 min+ measured. The exact value pends T3 workstation
# measurement of real GHCR pull progress cadence (실측-후-기입) — parameterized so it stays
# tunable, never a magic constant asserted as measured.
DEFAULT_PULL_STALL_TIMEOUT_S = 300.0

# Floor for the pull-monitor poll when ``poll_interval_s`` is 0 (the CPU-test knob for the
# tight readiness/supervise loops would hot-spin the pull monitor otherwise).
_PULL_MONITOR_MIN_INTERVAL_S = 0.05


class ImagePullStalled(RuntimeError):
    """A registry image pull made no progress within the liveness window (T2 a).

    Raised inside ``run_job``'s infra boundary, so it surfaces as an ``infra_error``
    (classified FAILED — a finite terminal state) rather than an unbounded hang.
    """


# The watchdog kill's infra_error marker — producer = ``_supervise_until_runner_exit``,
# consumer = ``_job_result_of`` (p4c4 glue: marker-prefixed infra_error classifies the
# attempt TIMEOUT per the termination contract; shared constant, never a re-typed string).
JOB_TIMEOUT_MARKER = "job timeout:"

# Failure-reason hygiene (p4c5 관측성 + NEG, DoD-P4-13 정신). The reason a job failed is
# an OPERATIONAL breadcrumb that rides the JobResult -> Job -> store -> status API: a
# BOUNDED, single-line string authored by THIS module (docker/OS failure, watchdog kill,
# collection violation, crash-boundary exception message). It is deliberately NOT a
# channel for runner stderr dumps, consent/secret values or SUT domain detail — those
# live in the runner logs and result.json (a different data source by design; the
# operational view is never a filtered domain view). The cap makes that structural: an
# accidentally large payload is truncated, not carried.
_REASON_MAX_CHARS = 300
_TRUNCATION_SUFFIX = "...(truncated)"


def _reason(text: str | None) -> str | None:
    """Normalize an infra reason to one bounded single-line diagnostic (None passthrough)."""
    if text is None:
        return None
    collapsed = " ".join(text.split())  # newlines/indent collapse — one line, always
    if len(collapsed) <= _REASON_MAX_CHARS:
        return collapsed
    return collapsed[: _REASON_MAX_CHARS - len(_TRUNCATION_SUFFIX)] + _TRUNCATION_SUFFIX


_GATE_READY = "ready"
_GATE_EXITED = "exited"
_GATE_TIMEOUT = "timeout"

ReadinessProbe = Callable[[Any], bool]


@dataclass
class JobOutcome:
    """Terminal control-plane outcome of one job (seam pin — cycle-plan 2026-07-08 §1).

    Invariant: ``result_path is not None`` implies exactly one result.json existed at
    collection time (REQ-EXEC-013); 0 or 2+ found means ``result_path=None`` plus
    ``infra_error``. ``runner_exit_code`` is None when the runner never exited
    (readiness/job timeout, spawn failure).
    """

    job_id: str
    result_path: Path | None = None
    runner_exit_code: int | None = None
    infra_error: str | None = None


def default_readiness_probe(runner_container: Any) -> bool:
    """Default runner readiness = the container reports ``running`` (post-reload).

    Deliberately weak: it proves the process is up, not that ``/clock`` flows — per
    G-19 flow claims need received-count measurement, so the measured /clock probe is
    injected by workstation glue (Wave-2 task), never defaulted here.
    """
    return getattr(runner_container, "status", None) == "running"


def _env_path(name: str) -> str | None:
    """Read an optional path env: None when unset, LOUD when set-but-empty.

    An empty string is indistinguishable from unset under truthiness and would
    silently mean "0 cache mounts" — the G-26 변종 that turns every measurement
    cold while everyone believes it warm. Refuse to guess.
    """
    value = os.environ.get(name)
    if value is None:
        return None
    if not value.strip():
        raise ValueError(
            f"{name} is set but empty — unset it (no cache mounts) or set an absolute"
            " host path; an empty value must never silently mean 'unset' (G-26)"
        )
    return value


def _cache_volumes(
    cache_root: str | os.PathLike[str] | None,
    cache_scratch_root: str | os.PathLike[str] | None,
    job_id: str,
) -> tuple[dict[str, dict[str, str]], Path | None]:
    """Resolve cache roots to docker ``volumes`` binds — single-tier (FU-16 / D-1)
    or per-job seeded (D-B / DoD-P4-15). Returns ``(volumes, per-job scratch | None)``.

    Effective roots = the arguments (win) or ``$CV_ISAAC_CACHE_ROOT`` /
    ``$CV_ISAAC_CACHE_SCRATCH_ROOT`` (set-but-empty env raises — ``_env_path``);
    when neither is set there are ZERO cache mounts (frozen P2 behavior).

    * base root alone -> the frozen P2 single layer: all six binds ``rw`` from
      the base. UNCHANGED (동결 계약).
    * base + scratch roots -> per-job seeding (D-B, repaired p4c5): the three warm
      cache SETS (``CACHE_BASE_MOUNTS`` — ov/GL/compute) are COPIED from the shared
      base into ``<scratch_root>/<slug(job_id)>/<same subpath>`` and bound **rw**
      from there; the three always-written runtime dirs (``CACHE_SCRATCH_MOUNTS``)
      are created empty in the same per-job tree and bound **rw**. All six binds are
      rw and every bind SOURCE lives under the per-job scratch — **the shared base is
      never bound into any container**, so k parallel jobs cannot write to or corrupt
      it (DoD-P4-15 불변식, now structural rather than mount-flag-dependent). The
      whole per-job tree is DISCARDED by run_job's finally (stateless, NFR-EXEC-002).
      The slug is ``network_name_for``'s (flat, docker-safe, collision-free); the
      supervisor pre-creates every bind source (G-15 — dockerd would create missing
      dirs root-owned).
    * scratch root without a base root is a loud config error — a half-configured
      cache would silently run all-cold (G-26).

    A given-but-missing / non-directory root is a loud ``ValueError`` — the
    caller runs this in the same seat as the ``job_id`` check, so it raises
    BEFORE any docker resource. Roots are resolved to host ABSOLUTE paths
    (``Path.resolve()``): the runner is spawned via docker.sock, so ``-v`` binds
    resolve against the HOST daemon, not this process's cwd (sibling-container
    hazard D-O/F5).

    Base subdir existence: NOT required in single-tier mode (frozen — creating +
    ``chown 1234:1234`` is M5 ``warm_cache.sh``'s job, G-15). In seeding mode the
    base subdirs are READ (copy sources), so a missing tier is a loud
    ``ValueError``: it means the warm cache was never provisioned, and silently
    seeding an empty tier would reproduce exactly the all-cold-believed-warm run
    this repair exists to kill (G-26).
    """
    root = cache_root or _env_path(CACHE_ROOT_ENV)
    scratch_root = cache_scratch_root or _env_path(CACHE_SCRATCH_ROOT_ENV)
    if scratch_root and not root:
        raise ValueError(
            "cache_scratch_root given without cache_root — a half-configured per-job"
            " cache (D-B) would silently run all-cold; give both roots or neither (G-26)"
        )
    if not root:
        return {}, None
    resolved = Path(root).resolve()
    if not resolved.is_dir():
        raise ValueError(
            f"cache_root {resolved} does not exist or is not a directory "
            f"(create + chown 1234:1234 is M5 warm_cache.sh's job, D-1)"
        )
    if scratch_root is None:
        # Frozen P2 single layer (D-1 세칙): all six binds rw from the base.
        return {
            str(resolved / subpath): {"bind": container_path, "mode": "rw"}
            for subpath, container_path in CACHE_MOUNTS
        }, None
    scratch_resolved = Path(scratch_root).resolve()
    if not scratch_resolved.is_dir():
        raise ValueError(
            f"cache_scratch_root {scratch_resolved} does not exist or is not a directory "
            f"(the scratch ROOT is host provisioning's job; per-job dirs are created here)"
        )
    job_scratch = scratch_resolved / network_name_for(job_id)
    try:
        _seed_cache_tiers(job_id, resolved, job_scratch)
    except Exception:
        # A failed seed leaves no ~1 GB orphan behind the loud error (the finally-
        # teardown never runs — this raises pre-resource, before run_job's try).
        _discard_scratch(job_scratch)
        raise
    volumes = {
        str(job_scratch / subpath): {"bind": container_path, "mode": "rw"}
        for subpath, container_path in CACHE_BASE_MOUNTS
    }
    for subpath, container_path in CACHE_SCRATCH_MOUNTS:
        host_dir = job_scratch / subpath
        host_dir.mkdir(parents=True, exist_ok=True)
        host_dir.chmod(0o777)  # runner is non-root (uid 1234, R2 실측) — result-dir idiom
        volumes[str(host_dir)] = {"bind": container_path, "mode": "rw"}
    return volumes, job_scratch


def _seed_cache_tiers(job_id: str, base_root: Path, job_scratch: Path) -> None:
    """Copy the warm base cache tiers into this job's writable scratch (``cp -a``).

    The repair's whole content (T4 실측, §E1/E4): the runner needs the warm cache
    bytes AND the ability to write its lock/index files — a shared ``:ro`` mount
    gives the first and silently kills the second, so every job recompiles its CUDA
    kernels (~47 s/job, 32 cores saturated). An eager per-job copy gives both at a
    measured 1.07 s / 930 MB.

    ``cp -a`` (not ``shutil.copytree``) because ownership must survive the copy: the
    runner is uid 1234 (R2 실측) and M5's ``warm_cache.sh provision`` chowns the base
    tree to 1234:1234, so a preserving copy is writable by the runner while a
    copytree (owned by whoever runs the control plane) would not be — the same silent
    cache-off failure in a new costume. Preservation is then VERIFIED per tier (uid +
    owner-write bit), so a non-preserving ``cp`` is loud, never silent (G-26).

    Every failure raises (missing tier = unprovisioned warm cache; copy failure =
    e.g. a full scratch filesystem — a partial copy must never be swallowed into a
    silently-cold run). Emits ONE structured ``cache-seed`` stderr line with the
    measured cost (seconds + bytes), the operator/QA-visible proof that the seeding
    actually ran (G-26 feature-on gate, sibling of ``runner-mounts``).
    """
    started = time.monotonic()
    tiers: list[dict[str, Any]] = []
    for subpath, container_path in CACHE_BASE_MOUNTS:
        source = base_root / subpath
        if not source.is_dir():
            raise ValueError(
                f"cache base tier {source} does not exist or is not a directory — the warm"
                " cache was never provisioned (M5 warm_cache.sh provision|warm); refusing to"
                " seed an empty tier, which would run all-cold while measured as warm (G-26)"
            )
        destination = job_scratch / subpath
        destination.parent.mkdir(parents=True, exist_ok=True)
        # `cp -a src dst` with a NON-existent dst copies the tree AS dst (preserving the
        # tier dir's own mode/ownership); an existing dst would nest it one level deeper.
        completed = subprocess.run(  # noqa: S603 — fixed argv, no shell
            ["cp", "-a", str(source), str(destination)],  # noqa: S607
            capture_output=True,
            text=True,
            check=False,
        )
        if completed.returncode != 0 or not destination.is_dir():
            raise RuntimeError(
                f"cache seed failed for {source} -> {destination}"
                f" (cp -a exit {completed.returncode}): {completed.stderr.strip()[:200]}"
                " — a partial/absent per-job cache would run all-cold (disk full? base"
                " unreadable?); the job is refused rather than measured wrong (G-26)"
            )
        _assert_runner_writable(source, destination)
        tiers.append(
            {
                "source": str(source),
                "target": container_path,
                "bytes": _tree_bytes(destination),
            }
        )
    seconds = time.monotonic() - started
    line = json.dumps(
        {
            "job_id": job_id,
            "seconds": round(seconds, 3),
            "bytes": sum(int(tier["bytes"]) for tier in tiers),
            "tiers": tiers,
        },
        sort_keys=True,
    )
    print(f"[cv-supervisor] cache-seed {line}", file=sys.stderr, flush=True)


def _assert_runner_writable(source: Path, destination: Path) -> None:
    """Loud guard: the seeded tier must be writable by the same uid as the base (G-15).

    ``cp -a`` preserves ownership only for a privileged copier; GNU cp already exits
    non-zero otherwise, but a non-GNU ``cp`` might not — and a copy the runner cannot
    write is a cache that turns itself OFF (silently, at 47 s/job). Cheap structural
    check on the tier dir: same owner as the base tier + owner-write bit set.
    """
    src_stat = source.stat()
    dst_stat = destination.stat()
    if dst_stat.st_uid != src_stat.st_uid:
        raise RuntimeError(
            f"cache seed did not preserve ownership: {destination} is uid {dst_stat.st_uid},"
            f" base {source} is uid {src_stat.st_uid} — the runner (uid 1234, R2 실측) could"
            " not write its cache lock/index files and the cache would be silently DISABLED"
            " (T4 §E1); run the control plane with a `cp -a`-capable (root) identity"
        )
    if not dst_stat.st_mode & stat.S_IWUSR:
        raise RuntimeError(
            f"seeded cache tier {destination} is not owner-writable (mode"
            f" {stat.filemode(dst_stat.st_mode)}) — the cache would be silently DISABLED;"
            " the base tier must be writable by its owner (M5 warm_cache.sh chown 1234:1234)"
        )


def _tree_bytes(root: Path) -> int:
    """Sum the file bytes actually on disk under ``root`` (seed-cost evidence)."""
    total = 0
    for dirpath, _dirnames, filenames in os.walk(root):
        for name in filenames:
            total += os.lstat(os.path.join(dirpath, name)).st_size
    return total


def _resolve_oracle_plugin_dir(oracle_plugin_dir: str | None) -> str | None:
    """Validate + absolutize the custom-oracle plugin dir (D-1 wiring contract #3).

    None -> None: no mount, no env — behavior fully unchanged. A given-but-missing /
    non-directory path is a loud ``ValueError``, never a silent no-op (G-26); the
    caller runs this pre-resource, in the same seat as the job_id / cache_root checks.
    ``Path.resolve()`` matters twice here: the ``-v`` bind resolves against the HOST
    daemon (G-26 sibling-container hazard), and the returned string is used verbatim
    as host source, container target AND env value (same-absolute-path idiom).
    """
    if oracle_plugin_dir is None:
        return None
    resolved = Path(oracle_plugin_dir).resolve()
    if not resolved.is_dir():
        raise ValueError(
            f"oracle_plugin_dir {resolved} does not exist or is not a directory "
            f"(expected the scenario directory holding the custom oracle .py, D-1)"
        )
    return str(resolved)


def _log_image_ensure(image: str, kind: str, status: str, **extra: Any) -> None:
    """Feature-on gate (G-26): one structured stderr line per image-present step.

    So T3/QA/operators can assert the pull-present gate actually engaged (present /
    pulled / stalled / skipped) from a file, never from narration — the same idiom as
    ``runner-mounts`` / ``cache-seed``.
    """
    line = json.dumps({"image": image, "kind": kind, "status": status, **extra}, sort_keys=True)
    print(f"[cv-supervisor] image-ensure {line}", file=sys.stderr, flush=True)


def _image_present(images: Any, image: str) -> bool:
    """Is ``image`` present in the LOCAL image store? (no registry round-trip).

    ``images.get`` raises ``ImageNotFound`` when the tag is absent locally — matched by
    class NAME so the module (and its CPU tests) stay 100% docker-free, exactly like the
    duck-typed fake docker CLIENT the seam already relies on. Any OTHER error (a genuine
    daemon fault) propagates to run_job's infra boundary rather than being read as absent.
    """
    try:
        images.get(image)
    except Exception as exc:
        if type(exc).__name__ in ("ImageNotFound", "NotFound"):
            return False
        raise
    return True


def _pull_with_liveness(
    client: Any, image: str, *, kind: str, stall_timeout_s: float, poll_interval_s: float
) -> None:
    """Pull ``image`` (streaming), failing if no pull PROGRESS arrives within
    ``stall_timeout_s`` (T2 a). A blocking pull cannot be cancelled, so a stall abandons
    the daemon drain thread behind a loud ``ImagePullStalled`` rather than hanging the job
    forever. Progress-based, NOT a total cap: a large but progressing layer keeps resetting
    the window, so only a genuinely wedged connection trips it (history 2026-07-21 놀란점 3).
    A registry/daemon error mid-pull is re-raised (surfaced as infra_error upstream).
    """
    progress = {"at": time.monotonic()}  # dict item assign = atomic under the GIL
    finished = threading.Event()
    box: dict[str, Exception] = {}

    def _drain() -> None:
        try:
            for _event in client.api.pull(image, stream=True, decode=True):
                progress["at"] = time.monotonic()  # any progress event resets the window
        except Exception as exc:  # registry/daemon fault mid-pull — carry it back
            box["error"] = exc
        finally:
            finished.set()

    thread = threading.Thread(target=_drain, name=f"cv-pull-{kind}", daemon=True)
    thread.start()
    wait_s = poll_interval_s if poll_interval_s > 0 else _PULL_MONITOR_MIN_INTERVAL_S
    while not finished.wait(wait_s):
        if time.monotonic() - progress["at"] >= stall_timeout_s:
            raise ImagePullStalled(
                f"{kind} image {image} pull made no progress for {stall_timeout_s}s"
                " — the registry pull is stalled; the job is failed in finite time instead"
                " of hanging forever (history 2026-07-21 놀란점 3, T2 a)"
            )
    if "error" in box:
        raise box["error"]


def _ensure_image_present(
    client: Any, image: str, *, kind: str, stall_timeout_s: float, poll_interval_s: float
) -> str:
    """Make ``image`` present locally BEFORE any container starts (T2 a+b).

    Returns the outcome (``"present"`` | ``"pulled"`` | ``"unknown"``). ``ImagePullStalled``
    (or a re-raised registry/daemon error) on failure — caught by run_job's infra boundary,
    so a stalled/failed pull is a FINITE ``infra_error`` (FAILED) instead of the unbounded
    hang the implicit ``containers.run`` pull produced. For the SUT this doubles as the
    order gate: the runner container is not started until its image is present, so the
    runner never runs its mission against an absent SUT (no mission-timeout masquerade).

    A duck-typed client with no ``images`` API is the legacy CPU fake (it never touches a
    registry): nothing to pull or gate — logged (not silent), then skipped. Real docker
    clients always expose ``images`` + ``api``, so that branch is CPU-test-only.
    """
    images = getattr(client, "images", None)
    if images is None:
        _log_image_ensure(image, kind, "no-images-api")
        return "unknown"
    if _image_present(images, image):
        _log_image_ensure(image, kind, "present")
        return "present"
    _pull_with_liveness(
        client, image, kind=kind, stall_timeout_s=stall_timeout_s, poll_interval_s=poll_interval_s
    )
    _log_image_ensure(image, kind, "pulled")
    return "pulled"


def run_job(
    job_spec: dict[str, Any],
    out_dir: Path,
    runner_image: str,
    sut_image: str,
    docker_client: Any = None,
    *,
    runner_env: dict[str, str] | None = None,
    cache_root: str | os.PathLike[str] | None = None,
    cache_scratch_root: str | os.PathLike[str] | None = None,
    oracle_plugin_dir: str | None = None,
    runner_gpus: bool = True,
    readiness_probe: ReadinessProbe | None = None,
    readiness_timeout_s: float = 120.0,
    job_timeout_s: float = DEFAULT_JOB_TIMEOUT_S,
    pull_stall_timeout_s: float = DEFAULT_PULL_STALL_TIMEOUT_S,
    sut_restart_limit: int = 1,
    poll_interval_s: float = 1.0,
    ros_domain_id: int | None = None,
) -> JobOutcome:
    """Run ONE verification job end-to-end and return its ``JobOutcome`` (D-2 seam).

    The 5 positional parameters are the frozen cross-team pin (cycle-plan 2026-07-08
    §seam-1); everything else is keyword-only with defaults. Defaults are operational
    placeholders (parameterized, not NFR claims): ``readiness_timeout_s`` covers the
    measured Isaac cold boot (~67.5s) with margin, ``job_timeout_s`` is a wall-clock
    runaway watchdog (the sim-time budget lives in the scenario, M1 §3.2).

    Before any container is created BOTH images are made present (T2 a+b): a stalled
    registry pull is bounded by a progress-liveness watchdog (``pull_stall_timeout_s`` — a
    no-PROGRESS window, not a total cap) so a wedged pull fails the job in finite time
    instead of hanging inside ``containers.run``'s implicit pull (history 2026-07-21
    놀란점 3), and the SUT image being present GATES the runner start so the runner never
    runs its mission against a still-pulling SUT. A stall/failure surfaces as
    ``infra_error`` (FAILED), never a raise. A duck-typed client without an ``images`` API
    (legacy CPU fake) skips the gate — logged, not silent.

    ``cache_root`` (or ``$CV_ISAAC_CACHE_ROOT``) mounts the host Isaac asset cache into
    the runner (FU-16 / D-1) so the scene closure downloads once instead of every job;
    unset = 0 cache mounts (backward compatible), a given-but-invalid root raises in the
    same seat as ``job_id``. Adding ``cache_scratch_root`` (or
    ``$CV_ISAAC_CACHE_SCRATCH_ROOT``) switches to the D-B per-job layout (DoD-P4-15,
    repaired p4c5): the warm base tiers are COPIED into a per-job scratch tree (``cp -a``,
    measured 1.07 s / 930 MB) and all six binds are rw from that tree — the shared base is
    a copy source only and is never bound into a container (writes to it: structurally 0),
    and the tree is discarded in the finally-teardown (stateless). A ``:ro`` base bind
    does not make the CUDA/Kit cache read-only, it DISABLES it (T4 실측: 47 s of CUDA JIT
    per job) — see ``CACHE_SCRATCH_ROOT_ENV`` / ``_seed_cache_tiers``. Each spawn emits
    TWO structured stderr lines (the G-26 feature-on gates; tests and operators assert on
    them): ``cache-seed`` (seed cost: seconds + bytes per tier) and ``runner-mounts``
    (the full assembled mount spec — count, ro/rw mode, source/target).

    ``oracle_plugin_dir`` (D-1 2026-07-11, wiring contract #3) is the consumer's
    scenario directory holding custom oracle ``.py`` files: when not None it is
    bind-mounted read-only at the SAME absolute path inside the RUNNER and announced
    via ``CV_ORACLE_PLUGIN_DIR=<that path>`` — runner-only, never the SUT (blackbox
    no-leak invariant); the runner sys.path's it before evaluation (M2). None = no
    mount, no env; a missing directory raises loud (G-26, ``_resolve_oracle_plugin_dir``).

    ``ros_domain_id`` (p4c6 §7-1 allocator 정합): the ``ROS_DOMAIN_ID`` to stamp on both
    containers (env + reconcile labels). None (the default) keeps the FROZEN single-run
    fallback — a pure-hash id from ``allocate_ros_domain_id(job_id)`` (P2 ``cv-infra run``
    계약 불변). Under the orchestrator, admission's store-backed collision-avoiding
    ``DomainIdAllocator`` is the SINGLE source for the concurrent-job domain set (M3 §3.6);
    it passes the allocated id here so run_job does NOT re-derive a colliding pure-hash id
    (the p4c5 defect: k>=~6 동시 admission에서 두 잡이 같은 도메인 — cross-talk은 잡별 전용
    브리지+host_net=0으로 구조 차단되나 "잡별 고유 도메인" 하위 불변식이 확률적 파손).

    Infra failures (docker/spawn/collection) are returned as ``infra_error``, never
    raised; a missing ``job_id`` is a seam-contract violation and raises ValueError
    before any resource is created.
    """
    job_id = job_spec.get("job_id")
    if not isinstance(job_id, str) or not job_id:
        raise ValueError("job_spec must carry a non-empty job_id (seam contract, D-2)")
    if sut_restart_limit < 0:
        raise ValueError(f"sut_restart_limit must be >= 0, got {sut_restart_limit}")
    # Resolve cache mounts + oracle plugin dir up front: a bad path fails loud BEFORE
    # any network or container is created (same seat as the job_id check).
    cache_volumes, scratch_dir = _cache_volumes(cache_root, cache_scratch_root, job_id)
    plugin_dir = _resolve_oracle_plugin_dir(oracle_plugin_dir)

    client = docker_client
    if client is None:
        # Lazy: keep `import cv_infra.orchestrator` docker-free (DoD-P2-12 — the
        # runner image installs the wheel --no-deps, so the SDK is absent there).
        import docker  # noqa: PLC0415

        client = docker.from_env()

    # G-15: pre-create every host path that gets bind-mounted (dockerd would create
    # missing dirs as root). The runner runs non-root (uid 1234, R2 실측), so the
    # result dir is made world-writable; precise chown is workstation glue (Wave 2).
    #
    # Bind-safe host dir (p4c4 colon-bind fix, PM 룰링 옵션 A): fan-out job ids
    # carry ':' (store.job_key "<request_id>:<repeat_index>") and the docker
    # bind spec is colon-delimited "src:dst:mode", so a raw out_dir/job_id
    # source is rejected by the daemon with `invalid volume specification` —
    # MEASURED, T4 L0 재현: ~/cv-infra-p2-out/p4c4/T4/L0/colon-bind-repro.txt.
    # The host dir therefore uses the SAME slug idiom the per-job cache scratch
    # already uses (network_name_for — docker-safe charset + collision-free
    # hash; 비대칭 해소). ONLY the host directory name changes: the JOB_SPEC
    # content (its job_id key), labels, store keys and domain-id derivation all
    # keep the verbatim job_id.
    net_name = network_name_for(job_id)
    job_dir = Path(out_dir) / net_name
    result_dir = job_dir / "result"
    result_dir.mkdir(parents=True, exist_ok=True)
    result_dir.chmod(0o777)
    spec_path = job_dir / "job_spec.json"
    spec_path.write_text(json.dumps(job_spec, indent=2, sort_keys=True) + "\n", encoding="utf-8")

    # p4c6 §7-1: honor the admission-allocated id when given (the store-backed
    # collision-avoiding source), else fall back to the pure-hash derivation
    # (frozen single-run path). The label/env below then carry the ACTUAL id.
    domain_id = ros_domain_id if ros_domain_id is not None else allocate_ros_domain_id(job_id)
    # Crash-reconciliation labels (M3 §3.9, R14): both containers carry the job id
    # and its live domain id so a restarted orchestrator can re-attach in-flight
    # jobs and restore allocations from `docker ps` instead of re-assigning.
    labels = {LABEL_JOB_ID: job_id, LABEL_ROS_DOMAIN_ID: str(domain_id)}
    network: Any = None
    runner_ct: Any = None
    sut_ct: Any = None
    runner_exit_code: int | None = None
    result_path: Path | None = None
    infra_error: str | None = None
    try:
        # (a)+(b) T2: make BOTH images present BEFORE creating any docker resource. This
        # bounds the pull by a progress-liveness watchdog (a wedged pull fails the job in
        # finite time instead of hanging inside the implicit ``containers.run`` pull —
        # history 놀란점 3), and gates start ORDER: the SUT image must be present before
        # the runner container starts, so the runner never begins its mission against a
        # still-pulling SUT. A stall/failure raises here and is absorbed as infra_error by
        # the boundary below (FAILED — finite), with no container/network left behind.
        _ensure_image_present(
            client,
            runner_image,
            kind="runner",
            stall_timeout_s=pull_stall_timeout_s,
            poll_interval_s=poll_interval_s,
        )
        _ensure_image_present(
            client,
            sut_image,
            kind="sut",
            stall_timeout_s=pull_stall_timeout_s,
            poll_interval_s=poll_interval_s,
        )
        # Networks carry the same reconciliation labels as the containers so the
        # restart sweep (reconcile_at_restart, M3 §3.9) can find and remove them.
        # net_name was computed above (it also names the bind-safe job_dir).
        network = client.networks.create(net_name, driver="bridge", labels=labels)

        # Runner FIRST — the sim supplies /clock (G-19 supply order). Supervisor-owned
        # seam keys override operator runner_env on collision; everything else (e.g.
        # the operator's consent env) passes through verbatim (decision 2026-07-03).
        environment = dict(runner_env or {})
        environment.update(
            {
                "JOB_SPEC": JOB_SPEC_MOUNT,
                "RESULT_OUT": RESULT_OUT_MOUNT,
                "ROS_DOMAIN_ID": str(domain_id),
            }
        )
        if plugin_dir is not None:
            # D-1: announce the mounted plugin dir to the runner ONLY (supervisor-
            # owned, so it overrides operator runner_env like the seam keys above).
            environment[ORACLE_PLUGIN_DIR_ENV] = plugin_dir
        # FU-14: scenario-derived ROS env — injected only when the key exists in
        # interface.adapter_config (scenario is the SoT, so these supervisor-owned
        # keys override operator runner_env like the seam keys above). Image-internal
        # paths (LD_LIBRARY_PATH etc.) stay M2 boot-glue knowledge, never set here.
        interface = job_spec.get("interface")
        adapter_config = interface.get("adapter_config") if isinstance(interface, dict) else None
        if isinstance(adapter_config, dict):
            for cfg_key, env_key in (("ros_distro", "ROS_DISTRO"), ("rmw", "RMW_IMPLEMENTATION")):
                if cfg_key in adapter_config:
                    environment[env_key] = str(adapter_config[cfg_key])
        runner_extra: dict[str, Any] = {}
        if runner_gpus:
            # Runner = Isaac = always GPU on the default path (--gpus all equivalent);
            # NVIDIA_DRIVER_CAPABILITIES=all is baked into the runner image (M5), so
            # no env propagation is needed. Lazy import, same discipline as `import
            # docker` above (DoD-P2-12 — module import stays docker-free; this line
            # only ever executes on the control-plane host where the SDK is pinned).
            from docker.types import DeviceRequest  # noqa: PLC0415

            runner_extra["device_requests"] = [DeviceRequest(count=-1, capabilities=[["gpu"]])]
        volumes = {
            # Cache/plugin binds first so the seam mounts below win on any
            # host-path collision (same principle as the seam env keys above).
            # Plugin bind = SAME absolute path host->container, read-only (D-1).
            **cache_volumes,
            **({plugin_dir: {"bind": plugin_dir, "mode": "ro"}} if plugin_dir else {}),
            str(spec_path): {"bind": JOB_SPEC_MOUNT, "mode": "ro"},
            str(result_dir): {"bind": RESULT_OUT_MOUNT, "mode": "rw"},
        }
        _log_runner_mounts(job_id, volumes)
        runner_ct = client.containers.run(
            runner_image,
            detach=True,
            name=f"{net_name}-runner",
            network=net_name,
            labels=labels,
            environment=environment,
            volumes=volumes,
            **runner_extra,
        )

        probe = readiness_probe if readiness_probe is not None else default_readiness_probe
        gate = _gate_runner_ready(runner_ct, probe, readiness_timeout_s, poll_interval_s)
        if gate == _GATE_EXITED:
            # Runner died before ready (e.g. usage error) — no SUT start; keep its
            # exit code and fall through to collection (a degraded runner may still
            # have written an error result — the REQ-EXEC-013 invariant decides).
            runner_exit_code = _exit_code(runner_ct)
        elif gate == _GATE_TIMEOUT:
            infra_error = f"runner readiness gate timed out after {readiness_timeout_s}s"
        else:
            # SUT joins the same network + domain as an unmodified blackbox: no
            # command/entrypoint override, no operator env leak (DoD-P2-03), and
            # no GPU device request (carter nav2 is CPU-only — GPU slots stay
            # with the runner).
            sut_ct = client.containers.run(
                sut_image,
                detach=True,
                name=f"{net_name}-sut",
                network=net_name,
                labels=labels,
                environment={"ROS_DOMAIN_ID": str(domain_id)},
            )
            runner_exit_code, infra_error = _supervise_until_runner_exit(
                runner_ct,
                sut_ct,
                job_timeout_s=job_timeout_s,
                sut_restart_limit=sut_restart_limit,
                poll_interval_s=poll_interval_s,
            )
        if infra_error is None:
            result_path, infra_error = _collect_result(result_dir)
        return JobOutcome(job_id, result_path, runner_exit_code, infra_error)
    except Exception as exc:  # infra boundary: surface docker/OS failures, never raise
        return JobOutcome(job_id, None, runner_exit_code, f"{type(exc).__name__}: {exc}")
    finally:
        _teardown((sut_ct, runner_ct), network)
        _discard_scratch(scratch_dir)  # D-B: per-job scratch dies with the job (stateless)


def _log_runner_mounts(job_id: str, volumes: dict[str, dict[str, str]]) -> None:
    """Feature-on gate (G-26): one structured stderr line per runner spawn.

    Emits the FULL assembled mount spec (count, ro/rw mode, source/target paths)
    so tests and operators can assert the cache/plugin mounts actually engaged —
    a silent no-mount (all-cold measured as warm) is worse than a loud error.
    Wave-2 GPU evidence additionally uses ``docker inspect`` (G-26 합의).
    """
    spec = [
        {"source": source, "target": bind["bind"], "mode": bind["mode"]}
        for source, bind in volumes.items()
    ]
    line = json.dumps({"job_id": job_id, "mounts": spec}, sort_keys=True)
    print(f"[cv-supervisor] runner-mounts {line}", file=sys.stderr, flush=True)


def _discard_scratch(scratch_dir: Path | None) -> None:
    """Best-effort removal of the per-job scratch tree (D-B: 잡 종료 시 폐기).

    Removes BOTH halves of the per-job tree — the seeded cache copies and the
    runtime scratch dirs (they share one root, ``<scratch_root>/<slug>/``) — so a
    job leaves ~1 GB of disk behind for exactly as long as it runs (stateless,
    NFR-EXEC-002). Same discipline as ``_teardown``: failures surface on stderr but
    never mask the job outcome. None (single-tier / no cache) is a no-op.
    """
    if scratch_dir is None:
        return
    try:
        shutil.rmtree(scratch_dir)
    except Exception as exc:
        print(f"[cv-supervisor] scratch discard failed: {exc!r}", file=sys.stderr)


def _gate_runner_ready(
    runner: Any, probe: ReadinessProbe, timeout_s: float, poll_interval_s: float
) -> str:
    """Poll the readiness probe until ready, runner exit, or timeout (G-19 gate)."""
    deadline = time.monotonic() + timeout_s
    while True:
        runner.reload()
        if probe(runner):
            return _GATE_READY
        if runner.status == "exited":
            return _GATE_EXITED
        if time.monotonic() >= deadline:
            return _GATE_TIMEOUT
        time.sleep(poll_interval_s)


def _supervise_until_runner_exit(
    runner: Any,
    sut: Any,
    *,
    job_timeout_s: float,
    sut_restart_limit: int,
    poll_interval_s: float,
) -> tuple[int | None, str | None]:
    """Wait for runner exit, absorbing early SUT death with bounded restarts.

    Returns ``(runner_exit_code, infra_error)`` — exactly one side is set. Early SUT
    exit (nav2 ``Aborting bringup`` is terminal, no self-retry — G-19) is restarted at
    most ``sut_restart_limit`` times; past the limit it is an infra failure. On job
    timeout the kill happens in the finally-teardown (stop + force-remove).
    """
    deadline = time.monotonic() + job_timeout_s
    restarts_used = 0
    while True:
        runner.reload()
        if runner.status == "exited":
            return _exit_code(runner), None
        if time.monotonic() >= deadline:
            return None, (
                f"{JOB_TIMEOUT_MARKER} runner still running after {job_timeout_s}s"
                " (teardown kills it)"
            )
        sut.reload()
        if sut.status == "exited":
            if restarts_used >= sut_restart_limit:
                return None, (
                    f"SUT exited {restarts_used + 1} time(s); "
                    f"restart limit ({sut_restart_limit}) exhausted"
                )
            sut.restart()
            restarts_used += 1
        time.sleep(poll_interval_s)


def _exit_code(container: Any) -> int:
    """Fetch an exited container's exit code (``wait`` returns immediately post-exit)."""
    return int(container.wait(timeout=_EXIT_CODE_WAIT_S)["StatusCode"])


def _collect_result(result_dir: Path) -> tuple[Path | None, str | None]:
    """Enforce the exactly-one-result invariant (REQ-EXEC-013)."""
    found = sorted(result_dir.rglob("result.json"))
    if len(found) == 1:
        return found[0], None
    return None, (
        f"expected exactly 1 result.json under {result_dir}, found {len(found)} (REQ-EXEC-013)"
    )


def _teardown(containers: tuple[Any, ...], network: Any) -> None:
    """Best-effort stop/remove of every spawned resource (REQ-EXEC-015 결).

    Every step is attempted regardless of earlier failures; failures are surfaced on
    stderr but never raised (teardown must not mask the job outcome). Containers go
    first (SUT, then runner), the network last — members must leave it first.
    """
    for container in containers:
        if container is None:
            continue
        try:
            container.stop(timeout=_TEARDOWN_STOP_TIMEOUT_S)
        except Exception as exc:
            print(f"[cv-supervisor] teardown stop failed: {exc!r}", file=sys.stderr)
        try:
            container.remove(force=True)
        except Exception as exc:
            print(f"[cv-supervisor] teardown remove failed: {exc!r}", file=sys.stderr)
    if network is not None:
        try:
            network.remove()
        except Exception as exc:
            print(f"[cv-supervisor] teardown network remove failed: {exc!r}", file=sys.stderr)


# --------------------------------------------------------------------------- #
# Phase 4: k-parallel asyncio supervision (M3 §3.5) — layered ON TOP of the
# frozen single-job seam above.
# --------------------------------------------------------------------------- #


class ParallelSupervisor:
    """k-parallel asyncio supervision — REQ-ORCH-006/007/009/010, NFR-ORCH-003.

    Generalizes the single-runner seam: one asyncio task per admitted job
    (잡↔러너 1:1, REQ-ORCH-007), driving the injected synchronous ``Runner``
    seam. The real per-job callable is ``run_job`` (blocking Docker SDK), so
    every attempt is offloaded to the default thread pool via
    ``loop.run_in_executor`` (M3 §3.5 R-DS — the event loop is never blocked
    and the k supervision tasks stay concurrent); CPU tests inject fakes.

    * **admission**: fill free slots from the ``JobQueue`` through the
      ``SlotAccountant`` gate — a closed gate means the launch never happens
      (over-launch 0, NFR-ORCH-003). When an allocator is attached, the job's
      ``ROS_DOMAIN_ID`` is allocated at admission (M3 §3.6).
    * **wall-clock watchdog** (REQ-ORCH-009): ``asyncio.wait_for(job_timeout_s)``
      classifies the attempt TIMEOUT. The container kill itself is the real
      runner seam's own watchdog + finally-teardown (``run_job(job_timeout_s=)``)
      — this layer only classifies; sim-time mission timeouts stay M2-owned
      (D-F, never judged on wall-clock).
    * **dual-watchdog coherence** (p4c1 후속 ②): this outer watchdog must be
      **>= the runner seam's own container watchdog**, else ``wait_for`` fires
      first and strands the executor thread + a live container until the inner
      watchdog catches up. A runner seam that owns an inner watchdog declares
      it via a ``job_timeout_s`` attribute (duck-typed — production run_job
      wrappers MUST expose it); a violating combination raises at construction,
      never silently.
    * **completion path**: the slot token and domain id are reclaimed FIRST,
      then the retry policy (``JobQueue.record_outcome``) decides re-queue vs
      terminal — a freed slot is immediately re-assignable to a waiting job
      (REQ-ORCH-006, REQ-EXEC-015 수신).
    * **crash boundary**: a raising runner marks THAT attempt FAILED — with the
      exception message kept as the job's ``infra_error`` (p4c5: a swallowed
      message left an untraceable bare ``failed``); other in-flight jobs are
      unaffected (NFR-EXEC-004 받침).
    * **failure diagnostics**: each terminal attempt's ``runner_exit_code`` +
      ``infra_error`` are written back onto the ``Job``, so the queue's existing
      persist (REQ-ORCH-011) carries them into SQLite and onto the status API.

    ``events`` is the observation log — ``("start"|"end", job_key)`` in
    wall-clock order — that makes slot re-assignment / cap invariants
    unit-assertable (DoD-P4-03/04 CPU 선행).
    """

    def __init__(
        self,
        queue: JobQueue,
        slots: SlotAccountant,
        runner: Runner,
        *,
        allocator: DomainIdAllocator | None = None,
        job_timeout_s: float | None = None,
    ) -> None:
        inner_watchdog_s = getattr(runner, "job_timeout_s", None)
        if (
            job_timeout_s is not None
            and inner_watchdog_s is not None
            and job_timeout_s < inner_watchdog_s
        ):
            raise ValueError(
                f"supervisor watchdog ({job_timeout_s}s) is shorter than the runner seam's"
                f" own container watchdog ({inner_watchdog_s}s) — the outer wait_for would"
                " fire first and strand the executor thread + live container (p4c1 후속 ②:"
                " ParallelSupervisor watchdog must be >= run_job's)"
            )
        self._queue = queue
        self._slots = slots
        self._runner = runner
        self._allocator = allocator
        self._job_timeout_s = job_timeout_s
        self.events: list[tuple[str, str]] = []

    async def run(self) -> list[JobResult]:
        """Drive every queued job to a terminal state; one JobResult per job.

        Retried attempts do not emit intermediate results — only the terminal
        outcome of each job is returned (same semantics as the P1 Scheduler).
        """
        loop = asyncio.get_running_loop()
        results: list[JobResult] = []
        in_flight: dict[asyncio.Task[JobResult], Job] = {}
        while self._queue.pending() or in_flight:
            self._admit(loop, in_flight)
            if not in_flight:
                # Unreachable when slot/allocator accounting is correct (slots
                # free whenever nothing is in flight) — loud beats a silent hang.
                raise RuntimeError(
                    "admission produced no task while jobs are pending"
                    " (slot/allocator accounting bug)"
                )
            done, _ = await asyncio.wait(in_flight, return_when=asyncio.FIRST_COMPLETED)
            for task in done:
                job = in_flight.pop(task)
                key = job_key(job)
                # Reclaim BEFORE the retry decision: the freed slot/domain id is
                # available to the next admission wave immediately (REQ-ORCH-006).
                self._slots.release()
                if self._allocator is not None:
                    self._allocator.release(key)
                self.events.append(("end", key))
                result = task.result()  # _run_one never raises (crash boundary inside)
                # p4c5 실패 관측성: the attempt's diagnostics ride onto the job so
                # ``record_outcome``'s persist (REQ-ORCH-011) writes them with the
                # state — one write path, no new store call site. Last-attempt
                # semantics: a clean retry resets them to None.
                job.runner_exit_code = result.runner_exit_code
                job.infra_error = result.infra_error
                if not self._queue.record_outcome(job, result.state):
                    results.append(result)
        return results

    def _admit(self, loop: asyncio.AbstractEventLoop, in_flight: dict) -> None:
        """Admission gate: fill free slots up to k from the queue (REQ-ORCH-006)."""
        while self._queue.pending() and self._slots.try_acquire():
            job = self._queue.pop_next()
            assert job is not None  # pending() > 0 above
            key = job_key(job)
            self._queue.mark_running(job)
            if self._allocator is not None:
                # p4c6 §7-1: the allocated id RIDES the job to run_job (the single
                # source for the concurrent-job domain set) — run_job no longer
                # re-derives a colliding pure-hash id. Re-set on every admission
                # (a retry re-allocates); None when no allocator (fallback path).
                job.ros_domain_id = self._allocator.allocate(key)
            self.events.append(("start", key))
            in_flight[loop.create_task(self._run_one(loop, job))] = job

    async def _run_one(self, loop: asyncio.AbstractEventLoop, job: Job) -> JobResult:
        """One attempt: offload the blocking Runner seam; classify the outcome.

        Both boundaries below now carry their REASON (p4c5, T1.5 §8-2): a
        swallowed crash-boundary message left a bare ``failed`` in the store with
        nothing to trace. The classification itself is unchanged.
        """
        try:
            attempt = loop.run_in_executor(None, self._runner.run, job)
            if self._job_timeout_s is None:
                return await attempt
            return await asyncio.wait_for(attempt, timeout=self._job_timeout_s)
        except TimeoutError:
            # py3.11: asyncio.TimeoutError IS this builtin. The executor thread
            # may still be draining — the real seam's own watchdog kills the
            # container (run_job); classification is all the state machine needs.
            return JobResult(
                job=job,
                state=JobState.TIMEOUT,
                verdict=None,
                infra_error=_reason(
                    f"{JOB_TIMEOUT_MARKER} supervisor watchdog fired after"
                    f" {self._job_timeout_s}s (the runner seam's own watchdog kills"
                    " the container)"
                ),
            )
        except Exception as exc:
            # Runner crash boundary: this attempt failed; other jobs unaffected.
            # The exception MESSAGE is the only trace of why (the seam raised
            # instead of returning an outcome) — preserve it, bounded.
            return JobResult(
                job=job,
                state=JobState.FAILED,
                verdict=None,
                infra_error=_reason(f"runner seam crashed: {type(exc).__name__}: {exc}"),
            )


# --------------------------------------------------------------------------- #
# Phase 4 (p4c4 glue): production Runner seam — the frozen run_job driven per
# fanned-out Job (T1 report §7-1 (b)).
# --------------------------------------------------------------------------- #

# Control-plane fold of the RECOVERED result.json ``verdict`` (the M1 canonical
# key — nothing else is read; 재계산·재해석 금지, M4 경계와 동일 원칙).
# "timeout" collapses to FAIL exactly like the runner/CLI exit fold
# (contract/schema.py Verdict comment: the SUT missed the sim-time budget = a
# SUT verdict, not infra; the fine-grained literal stays in result.json for M4).
# "error" / unknown / unreadable are deliberately ABSENT: they stay verdict-less
# (infra outcome -> rollup 'errored' territory, never a fabricated judgement).
_RESULT_VERDICT_FOLD: dict[str, Verdict] = {
    "pass": Verdict.PASS,
    "fail": Verdict.FAIL,
    "timeout": Verdict.FAIL,
}


class RunJobRunner:
    """Production ``Runner`` seam: one fanned-out ``Job`` -> ``run_job`` -> ``JobResult``.

    The p4c4 REST->runner glue: ``ParallelSupervisor`` drives this synchronous
    seam per job on the executor pool; every call reuses ``run_job`` with the
    construction-time operational knobs. The FROZEN positional contract (5 args)
    is unchanged; p4c6 added ONE keyword-only ``ros_domain_id`` (default None, so
    every existing caller is unaffected). CPU tests inject ``run_job_fn``
    (duck-typed fake — G-20 주입식, never a module stub); production leaves it None
    (= the real ``run_job``).

    * ``job.job_spec`` (materialized by the api submit path, persisted with the
      job) is the canonical JOB_SPEC passed by value; ``sut_image_ref`` comes
      off that spec (same fold as ``cv_infra/cli/main.py`` line ``run_job(...,
      job_spec["sut_image_ref"], ...)``). A spec-less job raises loud — a REST
      job that lost its spec must never silently no-op (G-26); the
      ``ParallelSupervisor`` crash boundary records that attempt FAILED.
    * ``job.oracle_plugin_dir`` rides into ``run_job(oracle_plugin_dir=...)``
      (D-1 wiring #3 — ro mount + ``CV_ORACLE_PLUGIN_DIR``, runner-only).
    * ``job.ros_domain_id`` (p4c6 §7-1) — the id ``ParallelSupervisor._admit``
      allocated from the store-backed collision-avoiding ``DomainIdAllocator`` —
      rides into ``run_job(ros_domain_id=...)`` so the container env/label carries
      the ALLOCATED id, not a re-derived colliding pure-hash one. None (a job with
      no allocator) => run_job's frozen pure-hash fallback.
    * ``job_timeout_s`` is a PUBLIC attribute — the duck-typed inner-watchdog
      declaration the ``ParallelSupervisor`` coherence gate reads (p4c1 후속 ②)
      — and the SAME attribute is what every ``run_job`` call receives (single
      source, 이중 정의 금지; default = ``DEFAULT_JOB_TIMEOUT_S``).
    """

    def __init__(
        self,
        *,
        out_dir: str | os.PathLike[str],
        runner_image: str,
        docker_client: Any = None,
        runner_env: dict[str, str] | None = None,
        cache_root: str | os.PathLike[str] | None = None,
        cache_scratch_root: str | os.PathLike[str] | None = None,
        runner_gpus: bool = True,
        readiness_probe: ReadinessProbe | None = None,
        job_timeout_s: float = DEFAULT_JOB_TIMEOUT_S,
        run_job_fn: Callable[..., JobOutcome] | None = None,
    ) -> None:
        self.job_timeout_s = job_timeout_s  # public: coherence-gate contract (class docstring)
        self._out_dir = Path(out_dir)
        self._runner_image = runner_image
        self._docker_client = docker_client
        self._runner_env = runner_env
        self._cache_root = cache_root
        self._cache_scratch_root = cache_scratch_root
        self._runner_gpus = runner_gpus
        self._readiness_probe = readiness_probe
        self._run_job = run_job_fn if run_job_fn is not None else run_job

    def run(self, job: Job) -> JobResult:
        spec = job.job_spec
        if not spec:
            raise ValueError(
                f"job {job_key(job)} carries no job_spec — the REST submit path must"
                " materialize the admitted request onto every fanned-out job"
                " (api._job_spec_for); refusing a silent no-op run (G-26)"
            )
        outcome = self._run_job(
            spec,
            self._out_dir,
            self._runner_image,
            spec["sut_image_ref"],
            self._docker_client,
            runner_env=self._runner_env,
            cache_root=self._cache_root,
            cache_scratch_root=self._cache_scratch_root,
            oracle_plugin_dir=job.oracle_plugin_dir,
            runner_gpus=self._runner_gpus,
            readiness_probe=self._readiness_probe,
            job_timeout_s=self.job_timeout_s,
            # p4c6 §7-1: hand run_job the admission-allocated id (None when no
            # allocator is attached -> run_job's pure-hash fallback).
            ros_domain_id=job.ros_domain_id,
        )
        return _job_result_of(job, outcome)


def _read_result_doc(result_path: Path | None) -> dict[str, Any] | None:
    """Read the runner-emitted result.json for ADDITIVE capture (p5c3 — honest absence).

    The report row consumes each repeat's declared ``metrics`` map + ``artifacts``
    paths off the runner's result.json (M4 ``aggregate``); this reads that doc so
    ``_job_result_of`` can carry it on the ``JobResult`` ALONGSIDE the classification.
    Purely informational — ``_classify`` below is frozen and still reads only the
    ``verdict`` key (verdict 날조 0). A missing / unreadable / non-dict result (fake-runner
    path, REQ-EXEC-013 collection violation, corrupt file) returns None, so the report keeps
    its existing empty ``{}``/None (현행 동작 회귀 0) — never a fabricated value, never loud
    (the classification already recorded the outcome; a second read for capture must not
    raise). The same ``(OSError, ValueError)`` guard ``_classify`` uses (JSONDecodeError ⊂
    ValueError)."""
    if result_path is None:
        return None
    try:
        payload = json.loads(Path(result_path).read_text(encoding="utf-8"))
    except (OSError, ValueError):  # unreadable / not JSON — includes JSONDecodeError
        return None
    if not isinstance(payload, dict):
        return None
    # (c) T2: the runner writes mcap/mp4 as CONTAINER-frame paths (under RESULT_OUT =
    # RESULT_OUT_MOUNT); rewrite them to host-resolvable absolute paths so T1's uploader
    # can stage them, consistent with ``result_json`` (already a host path). The RESULT_OUT
    # dir is the parent of this result.json on the host (M2 ``resolve_result_path``:
    # result.json lives directly under RESULT_OUT), which is the host side of the bind.
    return _hostify_artifact_paths(payload, Path(result_path).parent)


def _hostify_artifact_paths(doc: dict[str, Any], result_out_host_root: Path) -> dict[str, Any]:
    """Rewrite the runner's CONTAINER-frame artifact paths to HOST-resolvable ones (T2 c).

    The runner writes ``artifacts.mcap`` / ``artifacts.mp4`` as absolute paths under
    ``RESULT_OUT`` (= ``RESULT_OUT_MOUNT`` inside the container), which is bind-mounted to
    ``result_out_host_root`` on the host — so ``/cv/out/<rel>`` maps to
    ``<host_root>/<rel>``. T1's uploader (``aggregate._select_artifacts`` ->
    ``render_artifact_manifest``) stages off the HOST, so it needs the host path; before
    this it got the raw container path and could not find the file. Field NAMES stay
    unchanged (mcap/mp4) — only the value FRAME is corrected. A path NOT under the mount is
    left verbatim (honest — never guess a mapping we cannot prove; G-26), and None stays
    None (정직한 부재). Returns a NEW doc (input not mutated); a no-op returns it unchanged.
    """
    artifacts = doc.get("artifacts")
    if not isinstance(artifacts, dict):
        return doc
    rewritten = dict(artifacts)
    changed = False
    for key in ("mcap", "mp4"):
        container_path = artifacts.get(key)
        if isinstance(container_path, str) and container_path:
            host_path = _container_to_host_path(container_path, result_out_host_root)
            if host_path != container_path:
                rewritten[key] = host_path
                changed = True
    if not changed:
        return doc
    new_doc = dict(doc)
    new_doc["artifacts"] = rewritten
    return new_doc


def _container_to_host_path(container_path: str, result_out_host_root: Path) -> str:
    """Map a ``RESULT_OUT_MOUNT``-relative container path to its host bind-mount path."""
    try:
        rel = PurePosixPath(container_path).relative_to(RESULT_OUT_MOUNT)
    except ValueError:
        return container_path  # not under the runner's RESULT_OUT mount — leave verbatim
    return str(result_out_host_root.joinpath(*rel.parts))


def _job_result_of(job: Job, outcome: JobOutcome) -> JobResult:
    """Fold one ``JobOutcome`` into the control-plane ``JobResult`` (p4c4 glue).

    The state/verdict classification is ``_classify`` below — UNCHANGED. What
    changes in p4c5 is that this fold no longer DROPS the outcome's diagnostics:
    the runner's container exit code and the infra reason ride along on the
    ``JobResult`` (informational — the classification never reads them back), so
    ``ParallelSupervisor`` can persist them onto the job (store v4) and the
    status API can show them. Before this, a runner hard-crash reached the store
    as a bare ``failed`` and nobody could tell 137 (OOM-kill) from 139 (segfault)
    from a plain exit 1 (history 2026-07-14 놀란 점 7 — 두 번 실증).

    p5c3 adds a second ADDITIVE ride-along: the runner's result.json doc + its host
    path (``_read_result_doc``) so ``api._result_wire`` emits real ``metrics``/``artifacts``
    into the report row instead of empty placeholders (P5-02/P5-10). Same discipline as
    the diagnostics above — informational only, the classification never reads them, and an
    absent/unreadable result is honest ``None`` (회귀 0). The doc is read a SECOND time here
    (``_classify`` reads it for the verdict) to keep ``_classify`` frozen — a terminal-fold,
    once-per-job read of a tiny file.
    """
    state, verdict = _classify(outcome)
    return JobResult(
        job=job,
        state=state,
        verdict=verdict,
        runner_exit_code=outcome.runner_exit_code,
        infra_error=_reason(outcome.infra_error),
        result_doc=_read_result_doc(outcome.result_path),
        result_json_path=str(outcome.result_path) if outcome.result_path is not None else None,
    )


def _classify(outcome: JobOutcome) -> tuple[JobState, Verdict | None]:
    """The frozen outcome->(state, verdict) table (p4c4 계약 — 의미론 불변).

    Precedence: ``infra_error`` first (TIMEOUT when it carries the
    ``JOB_TIMEOUT_MARKER`` — the watchdog kill, termination contract 'timeout ⇒
    kill+timeout'; anything else FAILED) -> missing result (FAILED,
    belt-and-braces: run_job's invariant sets infra_error alongside) -> the
    recovered result.json ``verdict`` key via ``_RESULT_VERDICT_FOLD``. The
    recovered verdict OUTRANKS the informational ``runner_exit_code`` (the
    runner exits 1 on a domain FAIL — same fold principle as
    ``cv_infra/cli/main.py::_exit_from_outcome``). An unreadable / non-dict /
    unknown-verdict result is an infra outcome: FAILED verdict-less, never a
    fabricated domain judgement. Classification reads the RAW ``infra_error``
    (the marker prefix), never the display-normalized one.
    """
    if outcome.infra_error is not None:
        if outcome.infra_error.startswith(JOB_TIMEOUT_MARKER):
            return JobState.TIMEOUT, None
        return JobState.FAILED, None
    if outcome.result_path is None:
        return JobState.FAILED, None
    try:
        payload = json.loads(Path(outcome.result_path).read_text(encoding="utf-8"))
    except (OSError, ValueError):  # unreadable / not JSON — includes JSONDecodeError
        return JobState.FAILED, None
    raw_verdict = payload.get("verdict") if isinstance(payload, dict) else None
    verdict = _RESULT_VERDICT_FOLD.get(raw_verdict) if isinstance(raw_verdict, str) else None
    if verdict is None:
        return JobState.FAILED, None
    return JobState.COMPLETED, verdict


# --------------------------------------------------------------------------- #
# Phase 4: crash reconciliation at orchestrator restart (M3 §3.9) — R14.
# --------------------------------------------------------------------------- #


@dataclass
class RestartReconciliation:
    """Observation record of one restart reconciliation — assertable, never silent (G-26).

    Counts are best-effort attempts for the sweep halves (teardown failures
    surface on stderr, ``_teardown`` discipline) and exact for the store halves.
    """

    containers_removed: int = 0
    networks_removed: int = 0
    orphans_requeued: int = 0
    orphans_failed: int = 0
    domain_ids_cleared: int = 0
    envelopes_failed: int = 0


_RESTART_ENVELOPE_ERROR = (
    "orchestrator restarted mid-envelope: supervision was not resumed;"
    " RUNNING jobs were reconciled per the retry policy (M3 §3.9, R14)"
)

# The RUNNING-orphan's failure reason (p4c5): the interrupted attempt is recorded
# FAILED — it now says why, like every other failure path (a bare 'failed' with no
# reason is exactly the traceability gap this cycle closes). Same seat as the
# envelope marker above; the runner exit code is genuinely unknown (the crash took
# the supervising loop with it), so it stays None rather than being invented.
_RESTART_ORPHAN_ERROR = (
    "orchestrator crashed/restarted while this attempt was RUNNING;"
    " its containers were swept and the attempt was recorded FAILED (M3 §3.9, R14)"
)


def reconcile_at_restart(
    store: Store,
    docker_client: Any = None,
    *,
    max_attempts: int = 1,
    retry_on_timeout: bool = True,
) -> tuple[JobQueue, RestartReconciliation]:
    """Reconcile a restarted orchestrator with what the crash left behind (R14).

    Single-deployment assumption (LOCKED §7.3): at restart NO other orchestrator
    supervises runners on this host, so every container/network carrying the
    ``cv-infra.job_id`` label is stale (its supervising loop died with the
    process) and every SQLite domain-id liveness row is stale once the sweep
    ran. Steps, in this order:

    1. **label sweep** (when a docker client is given): stop/remove every
       container labeled ``LABEL_JOB_ID`` and remove every so-labeled per-job
       network — teardown precedes any re-queue so a reconciled job can never
       run twice concurrently (1잡=1러너=1결과 불변식).
    2. **domain-id clear**: release every liveness row (stale by step 1) so
       fresh allocations never collide with ghosts (M3 §3.6 D-O).
    3. **RUNNING-orphan re-label** (task 2026-07-13 ① 시맨틱): a job persisted
       RUNNING is the attempt the crash interrupted — it is recorded as a
       FAILED attempt through the normal retry policy
       (``JobQueue.record_outcome``): re-queued onto the returned queue while
       attempts remain, else terminal ``failed``. Counting the interrupted
       attempt keeps a poison job (one that kills the orchestrator) from
       crash-looping forever; no job is lost on either path.
    4. **envelope marker**: still-RUNNING envelopes are completed with a loud
       ``error`` (envelope supervision is NOT resumed this cycle) — a 500 on
       status reads beats an envelope stuck 'running' forever.

    ``docker_client=None`` skips step 1 only (docker-free hosts / CPU tests);
    production passes the real client. Returns the restored, driveable queue
    plus the observation record.
    """
    report = RestartReconciliation()
    if docker_client is not None:
        report.containers_removed, report.networks_removed = _sweep_stale(docker_client)
    report.domain_ids_cleared = store.release_all_domain_ids()
    queue = JobQueue.restore(store, max_attempts=max_attempts, retry_on_timeout=retry_on_timeout)
    for job in store.load_jobs():
        if job.state is JobState.RUNNING:
            job.infra_error = _RESTART_ORPHAN_ERROR  # persisted by record_outcome below
            job.runner_exit_code = None  # unknowable — never invented (p4c5)
            if queue.record_outcome(job, JobState.FAILED):
                report.orphans_requeued += 1
            else:
                report.orphans_failed += 1
    report.envelopes_failed = store.fail_running_envelopes(_RESTART_ENVELOPE_ERROR)
    return queue, report


def _sweep_stale(client: Any) -> tuple[int, int]:
    """Tear down every cv-infra-labeled container, then network (M3 §3.9 '정리' half)."""
    containers = list(client.containers.list(all=True, filters={"label": LABEL_JOB_ID}))
    _teardown(tuple(containers), None)  # containers first; networks below (members leave first)
    networks = list(client.networks.list(filters={"label": LABEL_JOB_ID}))
    for network in networks:
        try:
            network.remove()
        except Exception as exc:
            print(f"[cv-supervisor] sweep network remove failed: {exc!r}", file=sys.stderr)
    return len(containers), len(networks)
