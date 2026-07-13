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
import sys
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
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

# D-B two-tier cache (DoD-P4-15, p4c4): when a scratch root is ALSO given (arg or this
# env), the six binds split — the three warm cache SETS (ov/GL/compute, D-B "복수 집합")
# bind read-only from the shared base, and the three always-written runtime dirs bind rw
# from a per-job scratch dir under this root (created per job, discarded at job end).
# Base root alone keeps the frozen P2 single layer (all six rw).
CACHE_SCRATCH_ROOT_ENV = "CV_ISAAC_CACHE_SCRATCH_ROOT"

# (host subpath relative to the cache/scratch root, container bind path)
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

# The watchdog kill's infra_error marker — producer = ``_supervise_until_runner_exit``,
# consumer = ``_job_result_of`` (p4c4 glue: marker-prefixed infra_error classifies the
# attempt TIMEOUT per the termination contract; shared constant, never a re-typed string).
JOB_TIMEOUT_MARKER = "job timeout:"

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
    or two-tier (D-B / DoD-P4-15). Returns ``(volumes, per-job scratch dir | None)``.

    Effective roots = the arguments (win) or ``$CV_ISAAC_CACHE_ROOT`` /
    ``$CV_ISAAC_CACHE_SCRATCH_ROOT`` (set-but-empty env raises — ``_env_path``);
    when neither is set there are ZERO cache mounts (frozen P2 behavior).

    * base root alone -> the frozen P2 single layer: all six binds ``rw``.
    * base + scratch roots -> two-tier (D-B): the three warm cache SETS
      (``CACHE_BASE_MOUNTS`` — ov/GL/compute) bind **read-only** from the shared
      base, and the three always-written runtime dirs (``CACHE_SCRATCH_MOUNTS``)
      bind ``rw`` from a per-job scratch dir ``<scratch_root>/<slug(job_id)>/``
      created here (G-15 — dockerd would create missing dirs root-owned; the
      slug is ``network_name_for``'s, flat + collision-free) and DISCARDED by
      run_job's finally (D-B: 잡 종료 시 폐기, stateless).
    * scratch root without a base root is a loud config error — a
      half-configured two-tier cache would silently run all-cold (G-26).

    A given-but-missing / non-directory root is a loud ``ValueError`` — the
    caller runs this in the same seat as the ``job_id`` check, so it raises
    BEFORE any docker resource. Roots are resolved to host ABSOLUTE paths
    (``Path.resolve()``): the runner is spawned via docker.sock, so ``-v`` binds
    resolve against the HOST daemon, not this process's cwd (sibling-container
    hazard D-O/F5). BASE subdir existence is NOT required — creating them +
    ``chown 1234:1234`` is M5 ``warm_cache.sh``'s job (G-15), never this
    module's; per-job scratch subdirs are the one exception (job ids are
    dynamic, so M5 cannot pre-create them).
    """
    root = cache_root or _env_path(CACHE_ROOT_ENV)
    scratch_root = cache_scratch_root or _env_path(CACHE_SCRATCH_ROOT_ENV)
    if scratch_root and not root:
        raise ValueError(
            "cache_scratch_root given without cache_root — a half-configured two-tier"
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
    volumes = {
        str(resolved / subpath): {"bind": container_path, "mode": "ro"}
        for subpath, container_path in CACHE_BASE_MOUNTS
    }
    for subpath, container_path in CACHE_SCRATCH_MOUNTS:
        host_dir = job_scratch / subpath
        host_dir.mkdir(parents=True, exist_ok=True)
        host_dir.chmod(0o777)  # runner is non-root (uid 1234, R2 실측) — result-dir idiom
        volumes[str(host_dir)] = {"bind": container_path, "mode": "rw"}
    return volumes, job_scratch


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
    sut_restart_limit: int = 1,
    poll_interval_s: float = 1.0,
) -> JobOutcome:
    """Run ONE verification job end-to-end and return its ``JobOutcome`` (D-2 seam).

    The 5 positional parameters are the frozen cross-team pin (cycle-plan 2026-07-08
    §seam-1); everything else is keyword-only with defaults. Defaults are operational
    placeholders (parameterized, not NFR claims): ``readiness_timeout_s`` covers the
    measured Isaac cold boot (~67.5s) with margin, ``job_timeout_s`` is a wall-clock
    runaway watchdog (the sim-time budget lives in the scenario, M1 §3.2).

    ``cache_root`` (or ``$CV_ISAAC_CACHE_ROOT``) mounts the host Isaac asset cache into
    the runner (FU-16 / D-1) so the scene closure downloads once instead of every job;
    unset = 0 cache mounts (backward compatible), a given-but-invalid root raises in the
    same seat as ``job_id``. Adding ``cache_scratch_root`` (or
    ``$CV_ISAAC_CACHE_SCRATCH_ROOT``) switches to the D-B two-tier layout (DoD-P4-15):
    warm base sets read-only + per-job writable scratch discarded in the finally-teardown
    (mount split, empty-env loudness and half-config errors: see ``_cache_volumes``).
    The assembled runner mount spec is emitted as one structured stderr line per spawn
    (``runner-mounts`` — the G-26 feature-on gate; tests and operators assert on it).

    ``oracle_plugin_dir`` (D-1 2026-07-11, wiring contract #3) is the consumer's
    scenario directory holding custom oracle ``.py`` files: when not None it is
    bind-mounted read-only at the SAME absolute path inside the RUNNER and announced
    via ``CV_ORACLE_PLUGIN_DIR=<that path>`` — runner-only, never the SUT (blackbox
    no-leak invariant); the runner sys.path's it before evaluation (M2). None = no
    mount, no env; a missing directory raises loud (G-26, ``_resolve_oracle_plugin_dir``).

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

    domain_id = allocate_ros_domain_id(job_id)
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
    """Best-effort removal of the per-job scratch cache dir (D-B: 잡 종료 시 폐기).

    Same discipline as ``_teardown``: failures surface on stderr but never mask
    the job outcome. None (single-tier / no cache) is a no-op.
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
    * **crash boundary**: a raising runner marks THAT attempt FAILED; other
      in-flight jobs are unaffected (NFR-EXEC-004 받침).

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
                self._allocator.allocate(key)
            self.events.append(("start", key))
            in_flight[loop.create_task(self._run_one(loop, job))] = job

    async def _run_one(self, loop: asyncio.AbstractEventLoop, job: Job) -> JobResult:
        """One attempt: offload the blocking Runner seam; classify the outcome."""
        try:
            attempt = loop.run_in_executor(None, self._runner.run, job)
            if self._job_timeout_s is None:
                return await attempt
            return await asyncio.wait_for(attempt, timeout=self._job_timeout_s)
        except TimeoutError:
            # py3.11: asyncio.TimeoutError IS this builtin. The executor thread
            # may still be draining — the real seam's own watchdog kills the
            # container (run_job); classification is all the state machine needs.
            return JobResult(job=job, state=JobState.TIMEOUT, verdict=None)
        except Exception:
            # Runner crash boundary: this attempt failed; other jobs unaffected.
            return JobResult(job=job, state=JobState.FAILED, verdict=None)


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
    seam per job on the executor pool; every call reuses the FROZEN ``run_job``
    contract verbatim (signature unchanged) with the construction-time
    operational knobs. CPU tests inject ``run_job_fn`` (duck-typed fake — G-20
    주입식, never a module stub); production leaves it None (= the real
    ``run_job``).

    * ``job.job_spec`` (materialized by the api submit path, persisted with the
      job) is the canonical JOB_SPEC passed by value; ``sut_image_ref`` comes
      off that spec (same fold as ``cv_infra/cli/main.py`` line ``run_job(...,
      job_spec["sut_image_ref"], ...)``). A spec-less job raises loud — a REST
      job that lost its spec must never silently no-op (G-26); the
      ``ParallelSupervisor`` crash boundary records that attempt FAILED.
    * ``job.oracle_plugin_dir`` rides into ``run_job(oracle_plugin_dir=...)``
      (D-1 wiring #3 — ro mount + ``CV_ORACLE_PLUGIN_DIR``, runner-only).
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
        )
        return _job_result_of(job, outcome)


def _job_result_of(job: Job, outcome: JobOutcome) -> JobResult:
    """Fold one ``JobOutcome`` into the control-plane ``JobResult`` (p4c4 glue).

    Precedence: ``infra_error`` first (TIMEOUT when it carries the
    ``JOB_TIMEOUT_MARKER`` — the watchdog kill, termination contract 'timeout ⇒
    kill+timeout'; anything else FAILED) -> missing result (FAILED,
    belt-and-braces: run_job's invariant sets infra_error alongside) -> the
    recovered result.json ``verdict`` key via ``_RESULT_VERDICT_FOLD``. The
    recovered verdict OUTRANKS the informational ``runner_exit_code`` (the
    runner exits 1 on a domain FAIL — same fold principle as
    ``cv_infra/cli/main.py::_exit_from_outcome``). An unreadable / non-dict /
    unknown-verdict result is an infra outcome: FAILED verdict-less, never a
    fabricated domain judgement.
    """
    if outcome.infra_error is not None:
        if outcome.infra_error.startswith(JOB_TIMEOUT_MARKER):
            return JobResult(job=job, state=JobState.TIMEOUT, verdict=None)
        return JobResult(job=job, state=JobState.FAILED, verdict=None)
    if outcome.result_path is None:
        return JobResult(job=job, state=JobState.FAILED, verdict=None)
    try:
        payload = json.loads(Path(outcome.result_path).read_text(encoding="utf-8"))
    except (OSError, ValueError):  # unreadable / not JSON — includes JSONDecodeError
        return JobResult(job=job, state=JobState.FAILED, verdict=None)
    raw_verdict = payload.get("verdict") if isinstance(payload, dict) else None
    verdict = _RESULT_VERDICT_FOLD.get(raw_verdict) if isinstance(raw_verdict, str) else None
    if verdict is None:
        return JobResult(job=job, state=JobState.FAILED, verdict=None)
    return JobResult(job=job, state=JobState.COMPLETED, verdict=verdict)


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
