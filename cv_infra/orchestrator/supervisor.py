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
``DomainIdAllocator``. The single-runner ``run_job`` path above stays frozen
(P2/P3 ``cv-infra run`` 계약). The pure isolation helpers
(``allocate_ros_domain_id`` / ``network_name_for`` / ``ROS_DOMAIN_ID_SPACE``)
moved verbatim to ``allocator.py`` (M3 §3.6 home) and are re-exported here.
"""

from __future__ import annotations

import asyncio
import json
import os
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
from cv_infra.orchestrator.models import Job, JobResult, JobState
from cv_infra.orchestrator.queue import JobQueue
from cv_infra.orchestrator.scheduler import SlotAccountant
from cv_infra.orchestrator.store import job_key

# Container-side seam paths (M3 -> M2 env contract; 정본 = cv_infra/runner/main.py
# resolve_job_spec_dict / resolve_result_path). JOB_SPEC is bind-mounted read-only
# and passed BY PATH (safe for large specs); RESULT_OUT is a mounted rw directory.
JOB_SPEC_MOUNT = "/cv/job_spec.json"
RESULT_OUT_MOUNT = "/cv/out"

# FU-16 asset cache (decision 2026-07-09 D-1): mount the host Omniverse/asset cache into
# the runner so the ~680 MB / 241-file scene closure downloads ONCE, not every job (T0
# probe: 2nd receive 688 MB -> 1.29 MB — reports/deployment-2026-07-09-fu16-probe.md).
# Bind paths are the MEASURED Isaac 5.1.0 on-disk layout (differs from 6.0, R2). All rw
# single-layer: shared-RO base + per-job writable scratch (D-B, 2-tier) is deferred to
# DoD-P4-15 (Phase 2 is single-job; the warm write is measured +117 KB).
CACHE_ROOT_ENV = "CV_ISAAC_CACHE_ROOT"

# (host subpath relative to cache root, container bind path)
CACHE_MOUNTS: tuple[tuple[str, str], ...] = (
    ("cache/kit", "/isaac-sim/kit/cache"),
    ("cache/home", "/isaac-sim/.cache"),
    ("cache/computecache", "/isaac-sim/.nv/ComputeCache"),
    ("logs", "/isaac-sim/.nvidia-omniverse/logs"),
    ("data", "/isaac-sim/.local/share/ov/data"),
    ("documents", "/isaac-sim/Documents"),
)

# D-1 custom-oracle plugin dir (decision 2026-07-11, wiring contract #3): the scenario
# directory holding consumer oracle .py files is bind-mounted read-only at the SAME
# absolute path inside the RUNNER (G-26 idiom — the runner sys.path's that very string,
# so host/container paths must agree verbatim) and announced via this env. Runner-only:
# the SUT never sees the mount or the env (blackbox no-leak invariant).
ORACLE_PLUGIN_DIR_ENV = "CV_ORACLE_PLUGIN_DIR"

_TEARDOWN_STOP_TIMEOUT_S = 10  # graceful stop window before force-remove
_EXIT_CODE_WAIT_S = 30  # API wait on an already-exited container (returns immediately)

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


def _cache_volumes(cache_root: str | os.PathLike[str] | None) -> dict[str, dict[str, str]]:
    """Resolve the Isaac asset-cache root to docker ``volumes`` binds (FU-16 / D-1).

    Effective root = the ``cache_root`` argument (wins) or ``$CV_ISAAC_CACHE_ROOT``;
    when neither is set there are ZERO cache mounts (today's behavior, backward compat).
    A given-but-missing / non-directory root is a loud ``ValueError`` — the caller runs
    this in the same seat as the ``job_id`` check, so it raises BEFORE any resource.

    The root is resolved to a host ABSOLUTE path (``Path.resolve()``): the runner is
    spawned via docker.sock, so ``-v`` binds resolve against the HOST daemon, not this
    process's cwd (sibling-container hazard D-O/F5). Subdir existence is NOT required —
    creating them + ``chown 1234:1234`` is M5 ``warm_cache.sh``'s job (G-15), never this
    module's. All six binds are ``mode: rw`` single-layer (2-tier RO/scratch = P4-15).
    """
    root = cache_root or os.environ.get(CACHE_ROOT_ENV)
    if not root:
        return {}
    resolved = Path(root).resolve()
    if not resolved.is_dir():
        raise ValueError(
            f"cache_root {resolved} does not exist or is not a directory "
            f"(create + chown 1234:1234 is M5 warm_cache.sh's job, D-1)"
        )
    return {
        str(resolved / subpath): {"bind": container_path, "mode": "rw"}
        for subpath, container_path in CACHE_MOUNTS
    }


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
    oracle_plugin_dir: str | None = None,
    runner_gpus: bool = True,
    readiness_probe: ReadinessProbe | None = None,
    readiness_timeout_s: float = 120.0,
    job_timeout_s: float = 1800.0,
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
    same seat as ``job_id``. RO/2-tier caching (D-B) is deferred to DoD-P4-15 — the
    ``rw`` single layer here is what Phase 2's single job needs (see ``_cache_volumes``).

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
    cache_volumes = _cache_volumes(cache_root)
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
    job_dir = Path(out_dir) / job_id
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
        net_name = network_name_for(job_id)
        network = client.networks.create(net_name, driver="bridge")

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
        runner_ct = client.containers.run(
            runner_image,
            detach=True,
            name=f"{net_name}-runner",
            network=net_name,
            labels=labels,
            environment=environment,
            volumes={
                # Cache/plugin binds first so the seam mounts below win on any
                # host-path collision (same principle as the seam env keys above).
                # Plugin bind = SAME absolute path host->container, read-only (D-1).
                **cache_volumes,
                **({plugin_dir: {"bind": plugin_dir, "mode": "ro"}} if plugin_dir else {}),
                str(spec_path): {"bind": JOB_SPEC_MOUNT, "mode": "ro"},
                str(result_dir): {"bind": RESULT_OUT_MOUNT, "mode": "rw"},
            },
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
                f"job timeout: runner still running after {job_timeout_s}s (teardown kills it)"
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
