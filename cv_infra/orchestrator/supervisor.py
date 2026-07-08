"""Supervisor-min — one job: SUT+runner co-spawn -> exactly one result.json (M3 §3.5, D-2).

Control-plane execution seam behind ``cv-infra run`` (decision 2026-07-07 D-2): the
supervisor is the ONLY component holding docker.sock (the runner/adapter are DDS-only).
Per job it

1. creates a per-job docker bridge network + allocates a deterministic
   ``ROS_DOMAIN_ID`` (dual isolation, LOCKED §7.5 — 0..101 domain space);
2. starts the RUNNER first — the sim is the ``/clock`` source, and a ``use_sim_time``
   SUT started before clock flows freezes and aborts its nav2 bringup (G-19 supply
   order: clock -> TF/odom -> sensors);
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
with ``--no-deps`` (DoD-P2-12). Tests inject a duck-typed fake client. This is the
MIN seam: queue/scheduler/REST/runner-retry around it are Phase 4.
"""

from __future__ import annotations

import hashlib
import json
import re
import sys
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

# Container-side seam paths (M3 -> M2 env contract; 정본 = cv_infra/runner/main.py
# resolve_job_spec_dict / resolve_result_path). JOB_SPEC is bind-mounted read-only
# and passed BY PATH (safe for large specs); RESULT_OUT is a mounted rw directory.
JOB_SPEC_MOUNT = "/cv/job_spec.json"
RESULT_OUT_MOUNT = "/cv/out"

# LOCKED §7.5 dual isolation: per-job docker network + ROS_DOMAIN_ID in 0..101.
ROS_DOMAIN_ID_SPACE = 102

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


def allocate_ros_domain_id(job_id: str, in_use: frozenset[int] = frozenset()) -> int:
    """Deterministically allocate a ``ROS_DOMAIN_ID`` in 0..101 (LOCKED §7.5).

    Derivation is a stable hash of ``job_id`` (sha256, NOT Python's randomized
    ``hash()``) with linear probing over the domain space to skip ``in_use`` ids.
    Supervisor-min runs a single job so ``in_use`` is empty today; the probing seam
    is the deterministic foundation for the Phase-4 multi-job allocator.
    """
    if len(in_use) >= ROS_DOMAIN_ID_SPACE:
        raise ValueError(f"all {ROS_DOMAIN_ID_SPACE} ROS domain ids are in use")
    digest = hashlib.sha256(job_id.encode("utf-8")).digest()
    start = int.from_bytes(digest[:4], "big") % ROS_DOMAIN_ID_SPACE
    for offset in range(ROS_DOMAIN_ID_SPACE):
        candidate = (start + offset) % ROS_DOMAIN_ID_SPACE
        if candidate not in in_use:
            return candidate
    raise AssertionError("unreachable: in_use guard above")  # pragma: no cover


def network_name_for(job_id: str) -> str:
    """Per-job docker bridge network name — deterministic, docker-safe, collision-free.

    ``job_id`` is slugged to docker's allowed charset and suffixed with a short stable
    hash of the FULL id, so distinct job_ids that slug identically still get distinct
    networks. Same-name leftovers are prevented by the finally-teardown, not the name.
    """
    slug = re.sub(r"[^a-zA-Z0-9_.-]+", "-", job_id).strip("-.")[:24] or "job"
    suffix = hashlib.sha256(job_id.encode("utf-8")).hexdigest()[:8]
    return f"cvj-{slug}-{suffix}"


def default_readiness_probe(runner_container: Any) -> bool:
    """Default runner readiness = the container reports ``running`` (post-reload).

    Deliberately weak: it proves the process is up, not that ``/clock`` flows — per
    G-19 flow claims need received-count measurement, so the measured /clock probe is
    injected by workstation glue (Wave-2 task), never defaulted here.
    """
    return getattr(runner_container, "status", None) == "running"


def run_job(
    job_spec: dict[str, Any],
    out_dir: Path,
    runner_image: str,
    sut_image: str,
    docker_client: Any = None,
    *,
    runner_env: dict[str, str] | None = None,
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

    Infra failures (docker/spawn/collection) are returned as ``infra_error``, never
    raised; a missing ``job_id`` is a seam-contract violation and raises ValueError
    before any resource is created.
    """
    job_id = job_spec.get("job_id")
    if not isinstance(job_id, str) or not job_id:
        raise ValueError("job_spec must carry a non-empty job_id (seam contract, D-2)")
    if sut_restart_limit < 0:
        raise ValueError(f"sut_restart_limit must be >= 0, got {sut_restart_limit}")

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
        runner_ct = client.containers.run(
            runner_image,
            detach=True,
            name=f"{net_name}-runner",
            network=net_name,
            environment=environment,
            volumes={
                str(spec_path): {"bind": JOB_SPEC_MOUNT, "mode": "ro"},
                str(result_dir): {"bind": RESULT_OUT_MOUNT, "mode": "rw"},
            },
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
            # command/entrypoint override, no operator env leak (DoD-P2-03).
            sut_ct = client.containers.run(
                sut_image,
                detach=True,
                name=f"{net_name}-sut",
                network=net_name,
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
