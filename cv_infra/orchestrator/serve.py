"""Production boot glue (p4c4, T1 report §7-1 (c)) — env-configured REST server.

Wires the REAL store / ``RunJobRunner`` / ``create_app`` into one uvicorn
process so ``cv-infra submit --wait`` (M8 batch CLI) can drive real runners
over REST. ONE command on the workstation / pinned CLI container::

    CV_STORE_PATH=/host/p/cv-infra.sqlite3 \\
    CV_OUT_DIR=/host/p/cv-infra-out \\
    CV_RUNNER_IMAGE=<runner image ref> \\
    CV_MAX_CONCURRENT=<operator budget k cap> \\
        python3 -m cv_infra.orchestrator.serve

Env contract (T1 관례: set-but-EMPTY is always loud — G-26 변종; unset optional
= documented default). Required: ``CV_STORE_PATH`` (SQLite file, created on
first boot), ``CV_OUT_DIR`` (job artifact root — per-job subdirs are created by
``run_job``), ``CV_RUNNER_IMAGE`` (image-as-artifact pin, FU-10: NO hardcoded
default), ``CV_MAX_CONCURRENT`` (operator's AUTHORITATIVE cap, LOCKED §7.4).
Optional: ``CV_VRAM_PER_INSTANCE_MB`` (Phase-2/4 MEASURED per-instance VRAM —
setting it turns ON the NVML 2nd guard via ``PynvmlVramGauge``; never a
constant, CLAUDE §2-4), ``CV_BIND_HOST``/``CV_BIND_PORT`` (default
127.0.0.1:8000 — matches the M8 client default, M8 §8 cicd-g5 same-host MVP),
``CV_ISAAC_CACHE_ROOT``/``CV_ISAAC_CACHE_SCRATCH_ROOT`` (existing FU-16 / D-B
cache contract, resolved once at boot and passed explicitly), plus the operator
consent keys ``ACCEPT_EULA``/``PRIVACY_CONSENT`` forwarded VERBATIM to
``runner_env`` only when present (decision 2026-07-03 — values are never
committed/logged; absent = the runner boot guard honestly refuses, LOCKED §7.8).

Boot order: config -> ``reconcile_at_restart`` (R14: label sweep + domain-id
clear + RUNNING-orphan re-label + envelope loud markers — this module is the
production caller of the T1 reconciliation) -> app -> ONE structured
``serve-config`` stderr line (G-26 feature-on: k, mounts roots, consent key
NAMES, reconciliation counts — assertable, never silent) -> uvicorn.

The OUTER ``ParallelSupervisor`` wall-clock watchdog IS configured here (p5c7 T2,
decision 2026-07-24 D-2): ``build_app`` passes ``job_timeout_s=config.outer_wallclock_s``
(default ``DEFAULT_OUTER_WALLCLOCK_S``, env ``CV_OUTER_WALLCLOCK_S``) to ``create_app`` — it
is the ONLY bound that spans the image pull, which the inner ``run_job`` container watchdog
does not cover (it starts only after both containers exist). Leaving it None used to make a
legitimately crawling GHCR pull unbounded, hard-killed only by the CI job timeout's
UNCLASSIFIED kill. The default is strictly > the inner watchdog (its measured derivation +
surfaced assumptions live on the constant), so it satisfies the strict coherence gate; an
env override that is <= the inner watchdog is refused loud by that gate (an equal outer
wait_for fires first and strands the executor thread — the p4c1 coherence hazard).
"""

from __future__ import annotations

import json
import os
import sys
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Any

from fastapi import FastAPI

from cv_infra.orchestrator.api import create_app
from cv_infra.orchestrator.monitor import ResourceHealthSampler, attach_sampler
from cv_infra.orchestrator.scheduler import PynvmlVramGauge, VramGauge, compute_k
from cv_infra.orchestrator.store import Store
from cv_infra.orchestrator.supervisor import (
    CACHE_ROOT_ENV,
    CACHE_SCRATCH_ROOT_ENV,
    DEFAULT_OUTER_WALLCLOCK_S,
    RestartReconciliation,
    RunJobRunner,
    reconcile_at_restart,
)

# --- env names (module docstring is the contract prose) ----------------------
STORE_PATH_ENV = "CV_STORE_PATH"
OUT_DIR_ENV = "CV_OUT_DIR"
RUNNER_IMAGE_ENV = "CV_RUNNER_IMAGE"
MAX_CONCURRENT_ENV = "CV_MAX_CONCURRENT"
VRAM_PER_INSTANCE_ENV = "CV_VRAM_PER_INSTANCE_MB"
BIND_HOST_ENV = "CV_BIND_HOST"
BIND_PORT_ENV = "CV_BIND_PORT"
#: Outer wall-clock cap over the WHOLE job (pull(s) + mission) — the ONLY bound that spans the
#: image pull (p5c7 T2, D-2). Unset = ``DEFAULT_OUTER_WALLCLOCK_S`` (MEASURED derivation on the
#: constant); an override must stay strictly > the inner watchdog or the coherence gate refuses.
OUTER_WALLCLOCK_ENV = "CV_OUTER_WALLCLOCK_S"

_REQUIRED_ENVS = (STORE_PATH_ENV, OUT_DIR_ENV, RUNNER_IMAGE_ENV, MAX_CONCURRENT_ENV)

_DEFAULT_BIND_HOST = "127.0.0.1"  # M8 client default counterpart (batch._DEFAULT_API)
_DEFAULT_BIND_PORT = 8000

#: Operator consent env keys forwarded verbatim to ``runner_env`` when present
#: (decision 2026-07-03; same key set as cv_infra/cli/main.py ``_CONSENT_ENV_KEYS``
#: — names are the cross-team wire, VALUES are operator-provided at runtime and
#: never committed or logged, G-21).
CONSENT_ENV_KEYS = ("ACCEPT_EULA", "PRIVACY_CONSENT")


@dataclass(frozen=True)
class ServeConfig:
    """Boot-time snapshot of the env contract (one read — no mid-run re-reads)."""

    store_path: str
    out_dir: str
    runner_image: str
    max_concurrent: int
    vram_per_instance_mb: float | None
    cache_root: str | None
    cache_scratch_root: str | None
    consent_env: dict[str, str]
    host: str
    port: int
    outer_wallclock_s: float  # outer wall-clock cap over pull(s)+mission (default on unset)


def _get(environ: Mapping[str, str], name: str) -> str | None:
    """Optional env read: None when unset, LOUD when set-but-empty (T1 관례, G-26)."""
    value = environ.get(name)
    if value is None:
        return None
    if not value.strip():
        raise ValueError(
            f"{name} is set but empty — unset it or set a real value; an empty"
            " string must never silently mean 'unset' (G-26)"
        )
    return value


def _number(environ: Mapping[str, str], name: str, parse: Callable[[str], Any]) -> Any:
    """Optional numeric env: parsed via ``parse``, LOUD on a non-numeric value."""
    raw = _get(environ, name)
    if raw is None:
        return None
    try:
        return parse(raw)
    except ValueError as exc:
        raise ValueError(f"{name}={raw!r} is not a valid number") from exc


def config_from_env(environ: Mapping[str, str]) -> ServeConfig:
    """Read the module env contract into a ``ServeConfig`` — all-or-nothing loud.

    Every missing required env is reported in ONE error (an operator fixes the
    whole list in one pass, not one boot-crash at a time).
    """
    required = {name: _get(environ, name) for name in _REQUIRED_ENVS}
    missing = [name for name, value in required.items() if value is None]
    if missing:
        raise ValueError(
            f"missing required env(s): {', '.join(missing)} — the production server"
            " needs the full boot contract (module docstring); refusing defaults"
            " for store/artifacts/image/budget"
        )
    port = _number(environ, BIND_PORT_ENV, int)
    outer_wallclock_s = _number(environ, OUTER_WALLCLOCK_ENV, float)
    return ServeConfig(
        store_path=required[STORE_PATH_ENV],
        out_dir=required[OUT_DIR_ENV],
        runner_image=required[RUNNER_IMAGE_ENV],
        max_concurrent=_number(environ, MAX_CONCURRENT_ENV, int),
        vram_per_instance_mb=_number(environ, VRAM_PER_INSTANCE_ENV, float),
        cache_root=_get(environ, CACHE_ROOT_ENV),
        cache_scratch_root=_get(environ, CACHE_SCRATCH_ROOT_ENV),
        consent_env={k: environ[k] for k in CONSENT_ENV_KEYS if k in environ},
        host=_get(environ, BIND_HOST_ENV) or _DEFAULT_BIND_HOST,
        port=port if port is not None else _DEFAULT_BIND_PORT,
        # unset optional = documented default (T1 관례); set-but-empty/non-numeric = loud above.
        outer_wallclock_s=(
            outer_wallclock_s if outer_wallclock_s is not None else DEFAULT_OUTER_WALLCLOCK_S
        ),
    )


def build_app(
    config: ServeConfig,
    *,
    docker_client: Any = None,
    vram_gauge: VramGauge | None = None,
    run_job_fn: Callable[..., Any] | None = None,
    start_sampler: bool = False,
) -> FastAPI:
    """Compose the production app: store -> reconcile (R14) -> k -> runner -> api.

    The three kw-only seams are CPU-test injection points (G-20 주입식 fakes);
    production (``main``) passes the real docker client and leaves the rest
    None (= ``PynvmlVramGauge`` when the VRAM guard is configured, the real
    ``run_job``). ``docker_client=None`` skips only the label sweep half of
    reconciliation (docker-free CPU tests) — ``reconcile_at_restart`` 계약.

    ``start_sampler`` (default False — the CPU-test path stays poller-free) wires
    the M6 resource/health sampler as a background task; ``main`` passes True so
    the resident deployment populates VRAM/util (NVML absent -> loud-once degrade).
    """
    store = Store(config.store_path)  # process-lifetime (the app owns it from here)
    _, reconciliation = reconcile_at_restart(store, docker_client)
    if config.vram_per_instance_mb is not None:
        gauge = vram_gauge if vram_gauge is not None else PynvmlVramGauge()
        k = compute_k(
            config.max_concurrent,
            vram_gauge=gauge,
            vram_per_instance_mb=config.vram_per_instance_mb,
        )
    else:
        k = compute_k(config.max_concurrent)
    runner = RunJobRunner(
        out_dir=config.out_dir,
        runner_image=config.runner_image,
        docker_client=docker_client,
        runner_env=config.consent_env or None,
        cache_root=config.cache_root,
        cache_scratch_root=config.cache_scratch_root,
        run_job_fn=run_job_fn,
    )
    # p5c7 T2 (D-2): wire the OUTER wall-clock cap — the only bound spanning the image pull.
    # It rides create_app -> ParallelSupervisor, whose strict coherence gate refuses an
    # outer_wallclock_s that is not > the runner's inner watchdog (loud at first submit).
    app = create_app(store, runner, k=k, job_timeout_s=config.outer_wallclock_s)
    if start_sampler:
        # M6 §3.4: the resident deployment's SOLE periodic NVML poller.
        attach_sampler(app, ResourceHealthSampler(store))
    _log_serve_config(
        config,
        k=k,
        inner_watchdog_s=runner.job_timeout_s,
        outer_wallclock_s=config.outer_wallclock_s,
        recon=reconciliation,
    )
    return app


def _log_serve_config(
    config: ServeConfig,
    *,
    k: int,
    inner_watchdog_s: float,
    outer_wallclock_s: float,
    recon: RestartReconciliation,
) -> None:
    """Feature-on gate (G-26): ONE structured stderr line summarizing the boot.

    Consent env surfaces as key NAMES only — values are never logged (G-21).
    Same idiom as the supervisor's ``runner-mounts`` line.
    """
    line = json.dumps(
        {
            "bind": f"{config.host}:{config.port}",
            "store_path": config.store_path,
            "out_dir": config.out_dir,
            "runner_image": config.runner_image,
            "k": k,
            "max_concurrent": config.max_concurrent,
            "vram_per_instance_mb": config.vram_per_instance_mb,
            "cache_root": config.cache_root,
            "cache_scratch_root": config.cache_scratch_root,
            "consent_env_present": sorted(config.consent_env),
            "job_timeout_s": inner_watchdog_s,  # inner container watchdog (run_job / RunJobRunner)
            "outer_wallclock_s": outer_wallclock_s,  # outer cap over pull(s)+mission (p5c7 T2)
            "reconciliation": {
                "containers_removed": recon.containers_removed,
                "networks_removed": recon.networks_removed,
                "orphans_requeued": recon.orphans_requeued,
                "orphans_failed": recon.orphans_failed,
                "domain_ids_cleared": recon.domain_ids_cleared,
                "envelopes_failed": recon.envelopes_failed,
            },
        },
        sort_keys=True,
    )
    print(f"[cv-orchestrator] serve-config {line}", file=sys.stderr, flush=True)


def main() -> int:
    """``python3 -m cv_infra.orchestrator.serve`` — the ONE production command.

    Config/env errors raise loud before any socket binds. Lazy third-party
    imports keep ``import cv_infra.orchestrator.serve`` docker/uvicorn-free
    (same discipline as ``run_job``'s lazy ``import docker``).
    """
    config = config_from_env(os.environ)
    import uvicorn  # noqa: PLC0415

    import docker  # noqa: PLC0415

    app = build_app(config, docker_client=docker.from_env(), start_sampler=True)
    uvicorn.run(app, host=config.host, port=config.port)
    return 0


if __name__ == "__main__":  # pragma: no cover — process entrypoint
    sys.exit(main())
