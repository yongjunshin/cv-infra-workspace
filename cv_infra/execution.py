"""Case execution seam — the ONLY module in the verify pipeline that touches docker.

One CASE RUN = one container: ``/isaac-sim/python.sh <sim_script> --<axis>=<value> ...``
against the checkout, with the case's output directory overlaid rw on top of the
read-only checkout, then the same image again (GPU-free) for the oracle. Everything
else in the package stays docker-free, so the whole pipeline is testable on a CPU host
with a duck-typed fake client (the idiom the orchestrator seam already relied on).

Most of the docker-facing machinery here is LIFTED from
``cv_infra/orchestrator/supervisor.py`` — cache CoW seeding, the image-present gate
with its pull-liveness watchdog, the supervision loop, the finally-teardown — because
those blocks encode measurements, not opinions:

* ``:ro`` on a Kit/CUDA cache does not make it read-only, it turns it OFF (measured
  47 s -> 1.05 s per robot spawn); hence the per-case ``cp -a`` copy-on-write seed
  bound ``rw``, with an ownership guard so a non-preserving copy is loud (G-34/G-15).
* dockerd creates a missing bind source as root, and the stock Isaac image runs as
  uid 1234 — every host path bound here is pre-created and made world-writable first
  (G-15).
* an implicit ``containers.run`` pull can hang forever; the image is made present
  BEFORE the container starts, under a progress-liveness watchdog.
* the sim's EXIT CODE cannot carry pass/fail (G-62: ``SimulationApp.close()`` exits
  the process with status 0, and the stock ``python.sh`` squashes non-zero to 1) — so
  ``rc`` is only ever read as "did the process die badly" (ERROR lane). The verdict
  comes from the oracle's stdout, never from here.

Assumptions surfaced (M1 lands before the contract modules exist): ``spec`` and
``case`` are DUCK-TYPED. ``spec`` must carry ``checkout``, ``sim_output_dir``,
``sim_image``, ``oracle_script``, ``case_timeout_s``, ``oracle_timeout_s``,
``shm_size`` and ``max_zip_mb``; ``case`` must carry ``case_index``, ``case_id``,
``repeat``, ``seed`` and ``argv`` (``argv[0]`` = the sim script). Those are exactly the
fields ``contract.inputs.VerifySpec`` / ``contract.cases.CaseRun`` grow in M2 — this
module deliberately does not import them, so the contract layer stays the lowest layer.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import stat
import subprocess
import sys
import threading
import time
import zipfile
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

# Container-side seam paths. The checkout is bound read-only at CHECKOUT_MOUNT and the
# container's working dir IS that mount, so a sim script's own relative paths
# ("verify/out/trajectory.json") mean the same thing in CI as they do when a consumer
# runs ``./python.sh verify/sim.py --x=1`` from their repo root (local parity).
CHECKOUT_MOUNT = "/cv/checkout"
PYTHON_SH = "/isaac-sim/python.sh"

# The stock Isaac image's ENTRYPOINT swallows ``docker run`` arguments (G-14), so the
# case command is passed as an explicit entrypoint override + command argv.
DEFAULT_SIM_IMAGE = (
    "nvcr.io/nvidia/isaac-sim:5.1.0"
    "@sha256:f3563cb2ba0c18af0b2fb321360dcb73a917b899f879e3213623d6bee484fa54"
)

DEFAULT_SHM_SIZE = "8g"  # Kit's /dev/shm; the docker default (64 MB) is far too small
DEFAULT_CASE_TIMEOUT_S = 1800.0
DEFAULT_ORACLE_TIMEOUT_S = 300.0
DEFAULT_PULL_STALL_TIMEOUT_S = 300.0
DEFAULT_POLL_INTERVAL_S = 1.0

# FU-16 asset cache (ported verbatim from the orchestrator seam): mount the host
# Omniverse/asset cache into the container so the ~680 MB scene closure downloads ONCE.
# Bind paths are the MEASURED Isaac 5.1.0 on-disk layout (differs from 6.0).
CACHE_ROOT_ENV = "CV_ISAAC_CACHE_ROOT"
CACHE_SCRATCH_ROOT_ENV = "CV_ISAAC_CACHE_SCRATCH_ROOT"

# (host subpath relative to the cache/scratch root, container bind path)
# In seeding mode these three are BOTH the base subpaths (copy sources under the shared
# base root) AND the per-case destinations (same subpath under the case scratch dir).
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
CACHE_MOUNTS: tuple[tuple[str, str], ...] = CACHE_BASE_MOUNTS + CACHE_SCRATCH_MOUNTS

# Operator consent is an INPUT, never a literal in this repo: the two env keys are
# passed through verbatim from the operator environment (the CLI refuses to run
# without them, so this module never has to decide what consent means).
CONSENT_ENV_KEYS: tuple[str, ...] = ("ACCEPT_EULA", "PRIVACY_CONSENT")

# Container labels — so a crashed run's leftovers are findable with
# ``docker ps --filter label=cv-infra.case_id=...`` instead of by eyeball.
LABEL_CASE_ID = "cv-infra.case_id"
LABEL_SLUG = "cv-infra.slug"

# The watchdog kill's marker: producer = ``_supervise_until_exit``, consumer = the
# verdict lane fold (a marker-prefixed error is a TIMEOUT, not an unknown fault).
CASE_TIMEOUT_MARKER = "case timeout:"

_TEARDOWN_STOP_TIMEOUT_S = 10  # graceful stop window before force-remove
_EXIT_CODE_WAIT_S = 30  # API wait on an already-exited container (returns immediately)
_PULL_MONITOR_MIN_INTERVAL_S = 0.05  # floor for a 0/negative poll interval (tests pass 0)


class ImagePullStalled(RuntimeError):
    """A registry image pull made no progress within the liveness window.

    Raised inside the per-case infra boundary, so it surfaces as that case's ERROR
    (a finite terminal state) rather than an unbounded hang.
    """


@dataclass(frozen=True)
class SimExecution:
    """What one sim case run produced — the ERROR-lane inputs plus its artifacts.

    ``rc`` is None exactly when the container never exited on its own (timeout, or a
    failure before/while it ran); ``error`` is None exactly when it did. ``zip_path``
    is always written (an empty output dir still yields an empty zip — honest
    collection beats a silently missing artifact), and ``zip_truncated`` says the zip
    holds a manifest instead of the files because the output blew the size cap.
    """

    rc: int | None
    error: str | None
    wall_s: float
    out_dir: Path
    log_path: Path
    zip_path: Path | None
    zip_truncated: bool = False


@dataclass(frozen=True)
class ZipResult:
    """One collected output archive: where it is, how big, and whether it is a stub."""

    path: Path
    bytes: int
    truncated: bool


def slug_for(key: str) -> str:
    """Per-case slug — deterministic, docker-safe, collision-free.

    ``key`` is slugged to docker's allowed charset and suffixed with a short stable
    hash of the FULL key, so distinct keys that slug identically (case ids share a
    ``sha256:`` prefix — they always do) still get distinct names. Used for the
    container name, the case's host output dir, its cache scratch dir, its log and its
    zip, so all five of a case's names line up in a listing. Body is the orchestrator's
    ``network_name_for`` with a ``cvc-`` (case) prefix; NO network is created here —
    one container per case needs no private network.
    """
    slug = re.sub(r"[^a-zA-Z0-9_.-]+", "-", key).strip("-.")[:24] or "case"
    suffix = hashlib.sha256(key.encode("utf-8")).hexdigest()[:8]
    return f"cvc-{slug}-{suffix}"


def run_key(case: Any) -> str:
    """The identity of ONE run = case index + case id + repeat index.

    Repeats of a case must not share a container name, an output dir or a zip, so the
    repeat index is part of the key; the index prefix keeps a directory listing in
    plan order (the array order the budget truncates as a prefix).
    """
    return f"{case.case_index:04d}-{case.case_id}-r{case.repeat}"


def _resolve_docker_client(docker_client: Any) -> Any:
    """The injected duck-typed client, else a lazily-imported real one.

    Lazy so importing this module (and running the whole CPU test suite) needs no
    docker daemon and no SDK import; tests inject a fake.
    """
    if docker_client is not None:
        return docker_client
    import docker  # noqa: PLC0415

    return docker.from_env()


def gpu_device_requests() -> list[Any]:
    """``--gpus all`` as the SDK spells it (all cases time-share one GPU).

    A module-level function on purpose: it is the single line that needs the docker
    SDK's own types, so tests monkeypatch THIS name to keep the fake-client path
    SDK-free, and production keeps one place where the GPU request is defined.
    """
    from docker.types import DeviceRequest  # noqa: PLC0415

    return [DeviceRequest(count=-1, capabilities=[["gpu"]])]


def _env_path(name: str) -> str | None:
    """Read an optional path env: None when unset, LOUD when set-but-empty.

    An empty string is indistinguishable from unset under truthiness and would
    silently mean "0 cache mounts" — the variant that turns every measurement cold
    while everyone believes it warm. Refuse to guess.
    """
    value = os.environ.get(name)
    if value is None:
        return None
    if not value.strip():
        raise ValueError(
            f"{name} is set but empty — unset it (no cache mounts) or set an absolute"
            " host path; an empty value must never silently mean 'unset'"
        )
    return value


def _cache_volumes(
    cache_root: str | os.PathLike[str] | None,
    cache_scratch_root: str | os.PathLike[str] | None,
    slug: str,
) -> tuple[dict[str, dict[str, str]], Path | None]:
    """Resolve cache roots to docker ``volumes`` binds — single-tier or per-case seeded.
    Returns ``(volumes, per-case scratch | None)``.

    Effective roots = the arguments (win) or ``$CV_ISAAC_CACHE_ROOT`` /
    ``$CV_ISAAC_CACHE_SCRATCH_ROOT``; when neither is set there are ZERO cache mounts
    (a cold but correct run — the CI default until a runner is provisioned).

    * base root alone -> one layer: all six binds ``rw`` from the base. Fine for
      ``concurrency=1``; two concurrent cases would share the same lock files.
    * base + scratch roots -> per-case seeding: the three warm cache SETS
      (``CACHE_BASE_MOUNTS``) are COPIED into ``<scratch_root>/<slug>/<same subpath>``
      and bound **rw** from there; the three always-written runtime dirs
      (``CACHE_SCRATCH_MOUNTS``) are created empty in the same tree. The shared base is
      then never bound into any container, so k parallel cases cannot corrupt it. The
      per-case tree is discarded when the case ends (stateless).
    * scratch root without a base root is a loud config error — a half-configured cache
      would silently run all-cold.

    Roots are resolved to host ABSOLUTE paths: binds resolve against the HOST daemon,
    not this process's cwd. A given-but-missing root raises BEFORE any docker resource
    exists.
    """
    root = cache_root or _env_path(CACHE_ROOT_ENV)
    scratch_root = cache_scratch_root or _env_path(CACHE_SCRATCH_ROOT_ENV)
    if scratch_root and not root:
        raise ValueError(
            "cache_scratch_root given without cache_root — a half-configured per-case"
            " cache would silently run all-cold; give both roots or neither"
        )
    if not root:
        return {}, None
    resolved = Path(root).resolve()
    if not resolved.is_dir():
        raise ValueError(
            f"cache_root {resolved} does not exist or is not a directory "
            f"(creating + chown 1234:1234 is scripts/measure/warm_cache.sh's job)"
        )
    if scratch_root is None:
        return {
            str(resolved / subpath): {"bind": container_path, "mode": "rw"}
            for subpath, container_path in CACHE_MOUNTS
        }, None
    scratch_resolved = Path(scratch_root).resolve()
    if not scratch_resolved.is_dir():
        raise ValueError(
            f"cache_scratch_root {scratch_resolved} does not exist or is not a directory "
            f"(the scratch ROOT is host provisioning's job; per-case dirs are created here)"
        )
    return _seeded_cache_volumes(slug, resolved, scratch_resolved)


def _seeded_cache_volumes(
    slug: str, base_root: Path, scratch_root: Path
) -> tuple[dict[str, dict[str, str]], Path]:
    """The per-case layout: seed the warm tiers, then bind all six **rw** from the copy.

    Every bind SOURCE lives under the per-case scratch, so the shared base is never
    bound into any container. Returns ``(volumes, per-case scratch)``; the tree is
    discarded by the caller's finally-teardown.
    """
    case_scratch = scratch_root / slug
    try:
        _seed_cache_tiers(slug, base_root, case_scratch)
    except Exception:
        # A failed seed leaves no ~1 GB orphan behind the loud error (the finally-
        # teardown never runs — this raises pre-resource, before the case's try).
        _discard_scratch(case_scratch)
        raise
    volumes = {
        str(case_scratch / subpath): {"bind": container_path, "mode": "rw"}
        for subpath, container_path in CACHE_BASE_MOUNTS
    }
    for subpath, container_path in CACHE_SCRATCH_MOUNTS:
        host_dir = case_scratch / subpath
        host_dir.mkdir(parents=True, exist_ok=True)
        host_dir.chmod(0o777)  # the stock image runs non-root (uid 1234) — G-15
        volumes[str(host_dir)] = {"bind": container_path, "mode": "rw"}
    return volumes, case_scratch


def _seed_cache_tiers(slug: str, base_root: Path, case_scratch: Path) -> None:
    """Copy the warm base cache tiers into this case's writable scratch (``cp -a``).

    The container needs the warm cache bytes AND the ability to write its lock/index
    files — a shared ``:ro`` mount gives the first and silently kills the second, so
    every case recompiles its CUDA kernels. An eager per-case copy gives both (measured
    at ~1 s / 930 MB on the workstation).

    ``cp -a`` (not ``shutil.copytree``) because ownership must survive the copy: the
    container is uid 1234 and ``warm_cache.sh provision`` chowns the base tree to
    1234:1234, so a preserving copy is writable by it while a copytree (owned by
    whoever runs the CLI) would not be — the same silent cache-off failure in a new
    costume. Preservation is then VERIFIED per tier, so a non-preserving ``cp`` is
    loud, never silent. Emits ONE structured ``cache-seed`` stderr line with the
    measured cost — the operator-visible proof the seeding actually ran.
    """
    started = time.monotonic()
    tiers: list[dict[str, Any]] = []
    for subpath, container_path in CACHE_BASE_MOUNTS:
        source = base_root / subpath
        if not source.is_dir():
            raise ValueError(
                f"cache base tier {source} does not exist or is not a directory — the warm"
                " cache was never provisioned (scripts/measure/warm_cache.sh); refusing to"
                " seed an empty tier, which would run all-cold while measured as warm"
            )
        destination = case_scratch / subpath
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
                " — a partial/absent per-case cache would run all-cold (disk full? base"
                " unreadable?); the case is refused rather than measured wrong"
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
            "slug": slug,
            "seconds": round(seconds, 3),
            "bytes": sum(int(tier["bytes"]) for tier in tiers),
            "tiers": tiers,
        },
        sort_keys=True,
    )
    print(f"[cv-infra] cache-seed {line}", file=sys.stderr, flush=True)


def _assert_runner_writable(source: Path, destination: Path) -> None:
    """Loud guard: the seeded tier must be writable by the same uid as the base (G-15).

    ``cp -a`` preserves ownership only for a privileged copier; GNU cp already exits
    non-zero otherwise, but a non-GNU ``cp`` might not — and a copy the container
    cannot write is a cache that turns itself OFF (silently). Cheap structural check on
    the tier dir: same owner as the base tier + owner-write bit set.
    """
    src_stat = source.stat()
    dst_stat = destination.stat()
    if dst_stat.st_uid != src_stat.st_uid:
        raise RuntimeError(
            f"cache seed did not preserve ownership: {destination} is uid {dst_stat.st_uid},"
            f" base {source} is uid {src_stat.st_uid} — the container (uid 1234) could not"
            " write its cache lock/index files and the cache would be silently DISABLED;"
            " run the CLI with a `cp -a`-capable (root) identity"
        )
    if not dst_stat.st_mode & stat.S_IWUSR:
        raise RuntimeError(
            f"seeded cache tier {destination} is not owner-writable (mode"
            f" {stat.filemode(dst_stat.st_mode)}) — the cache would be silently DISABLED;"
            " the base tier must be writable by its owner (warm_cache.sh chown 1234:1234)"
        )


def _tree_bytes(root: Path) -> int:
    """Sum the file bytes actually on disk under ``root`` (seed-cost evidence)."""
    total = 0
    for dirpath, _dirnames, filenames in os.walk(root):
        for name in filenames:
            total += os.lstat(os.path.join(dirpath, name)).st_size
    return total


def _log_image_ensure(image: str, kind: str, status: str, **extra: Any) -> None:
    """One structured stderr line per image-present step.

    So an operator can assert the pull-present gate actually engaged (present / pulled
    / stalled / skipped) from a log file, never from narration.
    """
    line = json.dumps({"image": image, "kind": kind, "status": status, **extra}, sort_keys=True)
    print(f"[cv-infra] image-ensure {line}", file=sys.stderr, flush=True)


def _image_present(images: Any, image: str) -> bool:
    """Is ``image`` present in the LOCAL image store? (no registry round-trip).

    ``images.get`` raises ``ImageNotFound`` when the tag is absent locally — matched by
    class NAME so this module (and its CPU tests) stay docker-import-free, exactly like
    the duck-typed fake docker CLIENT. Any OTHER error (a genuine daemon fault)
    propagates rather than being read as absent.
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
    ``stall_timeout_s``. A blocking pull cannot be cancelled, so a stall abandons the
    daemon drain thread behind a loud ``ImagePullStalled`` rather than hanging the run
    forever. Progress-based, NOT a total cap: a large but progressing layer keeps
    resetting the window, so only a genuinely wedged connection trips it. A
    registry/daemon error mid-pull is re-raised.
    """
    started = time.monotonic()
    progress = {"at": started, "events": 0}  # dict item assign = atomic under the GIL
    finished = threading.Event()
    box: dict[str, Exception] = {}

    def _drain() -> None:
        try:
            for _event in client.api.pull(image, stream=True, decode=True):
                progress["at"] = time.monotonic()  # any progress event resets the window
                progress["events"] += 1
        except Exception as exc:  # registry/daemon fault mid-pull — carry it back
            box["error"] = exc
        finally:
            finished.set()

    thread = threading.Thread(target=_drain, name=f"cv-pull-{kind}", daemon=True)
    thread.start()
    wait_s = poll_interval_s if poll_interval_s > 0 else _PULL_MONITOR_MIN_INTERVAL_S
    while not finished.wait(wait_s):
        if time.monotonic() - progress["at"] >= stall_timeout_s:
            # The crawl-vs-dead discriminator is retained in the reason: ``events`` > 0
            # with a long elapsed = the registry WAS talking and then went silent;
            # ``events`` == 0 = it never started (auth/manifest wedge).
            raise ImagePullStalled(
                f"{kind} image {image} pull made no progress for {stall_timeout_s}s"
                f" (progress events seen: {progress['events']},"
                f" pull elapsed: {time.monotonic() - started:.1f}s)"
                " — the registry pull is stalled; the run is failed in finite time"
                " instead of hanging forever"
            )
    if "error" in box:
        raise box["error"]


def _ensure_image_present(
    client: Any,
    image: str,
    *,
    kind: str = "sim",
    stall_timeout_s: float = DEFAULT_PULL_STALL_TIMEOUT_S,
    poll_interval_s: float = DEFAULT_POLL_INTERVAL_S,
) -> str:
    """Make ``image`` present locally BEFORE the container starts.

    Returns the outcome (``"present"`` | ``"pulled"`` | ``"unknown"``).
    ``ImagePullStalled`` (or a re-raised registry/daemon error) on failure — caught by
    the per-case infra boundary, so a stalled/failed pull is a finite case ERROR
    instead of the unbounded hang the implicit ``containers.run`` pull produces.

    A duck-typed client with no ``images`` API never touches a registry: nothing to
    pull or gate — logged (not silent), then skipped. Real docker clients always expose
    ``images`` + ``api``, so that branch is CPU-test-only.
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


def _prepare_case_dir(run_dir: Path, slug: str) -> Path:
    """Create this run's host output dir and return it.

    Every host path that gets bind-mounted is pre-created here — dockerd would create
    a missing bind source as root, and the stock Isaac image runs non-root (uid 1234),
    so the dir is made world-writable (G-15). Precise chown is host provisioning's job.
    """
    out_dir = Path(run_dir) / "cases" / slug / "out"
    out_dir.mkdir(parents=True, exist_ok=True)
    out_dir.chmod(0o777)
    return out_dir


def _case_environment(operator_env: Mapping[str, str], seed: int) -> dict[str, str]:
    """The container env: operator consent passthrough + the two keys we own.

    Consent (``ACCEPT_EULA`` / ``PRIVACY_CONSENT``) is passed through VERBATIM from the
    operator's environment — no consent literal lives in this repo, and a missing key
    is the CLI's refusal to make, not ours. ``CV_SEED`` is the case's derived seed (the
    contract with the sim script: same case + same repeat = same seed).
    ``NVIDIA_DRIVER_CAPABILITIES=all`` is what the stock image needs to see the GPU
    (the platform's own runner image used to bake it in; the stock one does not).
    """
    environment = {key: operator_env[key] for key in CONSENT_ENV_KEYS if key in operator_env}
    environment["NVIDIA_DRIVER_CAPABILITIES"] = "all"
    environment["CV_SEED"] = str(seed)
    return environment


def _case_volumes(spec: Any, case_out: Path, *, mode: str) -> dict[str, dict[str, str]]:
    """Checkout ``:ro`` + this run's host output dir overlaid on the declared output dir.

    The output bind is DEEPER than the checkout bind, and docker applies the deeper
    mount last, so the case writes into a per-run host dir even though its parent tree
    is read-only. That is the whole reason ``sim_output_dir`` must exist in the
    checkout (a ``.gitkeep``): a mount point under a read-only bind cannot be created.
    """
    checkout = Path(spec.checkout).resolve()
    return {
        str(checkout): {"bind": CHECKOUT_MOUNT, "mode": "ro"},
        str(Path(case_out).resolve()): {
            "bind": f"{CHECKOUT_MOUNT}/{spec.sim_output_dir}",
            "mode": mode,
        },
    }


def _supervise_until_exit(
    container: Any, *, timeout_s: float, poll_interval_s: float
) -> tuple[int | None, str | None]:
    """Wait for the container to exit, or kill the case on the wall-clock deadline.

    Returns ``(exit_code, error)`` — exactly one side is set. On timeout the kill
    itself happens in the caller's finally-teardown (stop + force-remove), so there is
    one place that removes containers, not two.
    """
    deadline = time.monotonic() + timeout_s
    while True:
        container.reload()
        if container.status == "exited":
            return _exit_code(container), None
        if time.monotonic() >= deadline:
            return None, (
                f"{CASE_TIMEOUT_MARKER} container still running after {timeout_s}s"
                " (teardown kills it)"
            )
        time.sleep(poll_interval_s)


def _exit_code(container: Any) -> int:
    """Fetch an exited container's exit code (``wait`` returns immediately post-exit)."""
    return int(container.wait(timeout=_EXIT_CODE_WAIT_S)["StatusCode"])


def _teardown(containers: tuple[Any, ...]) -> None:
    """Best-effort stop/remove of every spawned container — no leftover on any path.

    Every step is attempted regardless of earlier failures; failures are surfaced on
    stderr but never raised (teardown must not mask the case outcome). No network to
    remove: one container per case needs no private network.
    """
    for container in containers:
        if container is None:
            continue
        try:
            container.stop(timeout=_TEARDOWN_STOP_TIMEOUT_S)
        except Exception as exc:
            print(f"[cv-infra] teardown stop failed: {exc!r}", file=sys.stderr)
        try:
            container.remove(force=True)
        except Exception as exc:
            print(f"[cv-infra] teardown remove failed: {exc!r}", file=sys.stderr)


def _discard_scratch(scratch_dir: Path | None) -> None:
    """Best-effort removal of the per-case scratch tree (it dies with the case).

    Removes BOTH halves of the tree — the seeded cache copies and the runtime scratch
    dirs (they share one root) — so a case leaves ~1 GB of disk behind for exactly as
    long as it runs. Same discipline as ``_teardown``: failures surface on stderr but
    never mask the case outcome. None (no cache configured) is a no-op.
    """
    if scratch_dir is None:
        return
    try:
        shutil.rmtree(scratch_dir)
    except Exception as exc:
        print(f"[cv-infra] scratch discard failed: {exc!r}", file=sys.stderr)


def _write_logs(container: Any, log_path: Path) -> None:
    """Persist the container's combined stdout+stderr next to the case's zip.

    The container has no display, so a GUI script boot-crashes or hangs there and
    NOWHERE else: the log is the only place that says why. Always collected, for a
    passing case too.
    """
    log_path.parent.mkdir(parents=True, exist_ok=True)
    log_path.write_bytes(container.logs())


def zip_output(out_dir: Path, dest: Path, max_bytes: int) -> ZipResult:
    """Zip the case's output dir, replacing it with a manifest when it is too big.

    An EMPTY (or absent) output dir still produces a zip: "the case wrote nothing" is
    a finding, and a missing artifact would read as an infrastructure failure instead.
    The size cap is measured on the UNCOMPRESSED source bytes — conservative on
    purpose (it is a "did the script dump a 40 GB video" guard, not an accounting
    exercise), and when it trips the zip carries a manifest of what was dropped so the
    operator can see the shape of the output that blew the budget.
    """
    source = Path(out_dir)
    files = sorted(path for path in source.rglob("*") if path.is_file()) if source.is_dir() else []
    total = sum(path.stat().st_size for path in files)
    dest = Path(dest)
    dest.parent.mkdir(parents=True, exist_ok=True)
    truncated = total > max_bytes
    with zipfile.ZipFile(dest, "w", zipfile.ZIP_DEFLATED) as archive:
        if truncated:
            manifest = [
                f"output dir {source} holds {total} bytes in {len(files)} file(s),"
                f" over the {max_bytes}-byte cap (--max-zip-mb) — files NOT collected:",
                *(f"{path.relative_to(source)}\t{path.stat().st_size}" for path in files),
            ]
            archive.writestr("MANIFEST.txt", "\n".join(manifest) + "\n")
        else:
            for path in files:
                archive.write(path, arcname=str(path.relative_to(source)))
    return ZipResult(dest, dest.stat().st_size, truncated)


def run_sim_case(
    spec: Any,
    case: Any,
    client: Any = None,
    *,
    run_dir: Path,
    operator_env: Mapping[str, str],
    poll_interval_s: float = DEFAULT_POLL_INTERVAL_S,
    pull_stall_timeout_s: float = DEFAULT_PULL_STALL_TIMEOUT_S,
    cache_root: str | os.PathLike[str] | None = None,
    cache_scratch_root: str | os.PathLike[str] | None = None,
) -> SimExecution:
    """Run ONE case+repeat in one GPU container and collect its output + log.

    Everything after the container starts is best-effort collection: a docker/OS
    failure, a stalled pull or a timeout all land as ``error`` (the case's ERROR lane)
    rather than as an exception, because one broken case must not take the run down.
    The output zip is produced on EVERY path — including the failing ones, where its
    emptiness is itself the evidence.

    ``rc`` is reported but never read as pass/fail (G-62): after boot, the sim's exit
    status cannot travel out of the container. It only separates "died badly" (ERROR)
    from "ran to completion" (ask the oracle).
    """
    client = _resolve_docker_client(client)
    slug = slug_for(run_key(case))
    run_dir = Path(run_dir)
    log_path = run_dir / "logs" / f"{slug}.sim.log"
    zip_path = run_dir / "zips" / f"{slug}.zip"
    out_dir = run_dir / "cases" / slug / "out"
    container = None
    scratch_dir = None
    rc: int | None = None
    error: str | None = None
    started = time.monotonic()
    try:
        out_dir = _prepare_case_dir(run_dir, slug)
        volumes, scratch_dir = _cache_volumes(cache_root, cache_scratch_root, slug)
        _ensure_image_present(
            client,
            spec.sim_image,
            kind="sim",
            stall_timeout_s=pull_stall_timeout_s,
            poll_interval_s=poll_interval_s,
        )
        volumes.update(_case_volumes(spec, out_dir, mode="rw"))
        container = client.containers.run(
            spec.sim_image,
            entrypoint=PYTHON_SH,
            command=list(case.argv),
            working_dir=CHECKOUT_MOUNT,
            environment=_case_environment(operator_env, case.seed),
            volumes=volumes,
            device_requests=gpu_device_requests(),
            shm_size=spec.shm_size,
            detach=True,
            name=f"{slug}-sim",
            labels={LABEL_CASE_ID: case.case_id, LABEL_SLUG: slug},
        )
        rc, error = _supervise_until_exit(
            container, timeout_s=spec.case_timeout_s, poll_interval_s=poll_interval_s
        )
        _write_logs(container, log_path)
    except Exception as exc:  # infra boundary: this case ERRORs, the run continues
        error = f"{type(exc).__name__}: {exc}"
    finally:
        _teardown((container,))
        _discard_scratch(scratch_dir)
    zipped = zip_output(out_dir, zip_path, int(spec.max_zip_mb) * 1024 * 1024)
    return SimExecution(
        rc=rc,
        error=error,
        wall_s=time.monotonic() - started,
        out_dir=out_dir,
        log_path=log_path,
        zip_path=zipped.path,
        zip_truncated=zipped.truncated,
    )


def run_oracle(
    spec: Any,
    case: Any,
    client: Any = None,
    *,
    run_dir: Path,
    operator_env: Mapping[str, str],
    case_out: Path,
    poll_interval_s: float = DEFAULT_POLL_INTERVAL_S,
) -> tuple[int | None, str, str | None]:
    """Re-run the SAME image on the case's output, GPU-free, and return its stdout.

    Same image, same argv (only the script changes), same env — so the oracle sees
    exactly the axis values the sim saw and needs no second dependency story. It gets
    NO device request (judging is CPU work; a GPU-holding oracle would halve the
    machine's case throughput) and both binds are ``:ro`` (an oracle that edits the
    evidence it judges is a bug we can make structurally impossible).

    Returns ``(rc, stdout, error)``; a non-zero rc or an error is the case's ERROR
    lane. The image is already local — the sim run just pulled it — so no pull gate
    here.
    """
    client = _resolve_docker_client(client)
    slug = slug_for(run_key(case))
    container = None
    rc: int | None = None
    stdout = ""
    error: str | None = None
    try:
        container = client.containers.run(
            spec.sim_image,
            entrypoint=PYTHON_SH,
            command=[spec.oracle_script, *list(case.argv)[1:]],
            working_dir=CHECKOUT_MOUNT,
            environment=_case_environment(operator_env, case.seed),
            volumes=_case_volumes(spec, case_out, mode="ro"),
            detach=True,
            name=f"{slug}-oracle",
            labels={LABEL_CASE_ID: case.case_id, LABEL_SLUG: slug},
        )
        rc, error = _supervise_until_exit(
            container, timeout_s=spec.oracle_timeout_s, poll_interval_s=poll_interval_s
        )
        # stdout ONLY: the verdict is parsed out of it, and a chatty stderr (Kit banners,
        # warnings) must not be able to inject a line the parser would read as a verdict.
        stdout = container.logs(stdout=True, stderr=False).decode("utf-8", errors="replace")
    except Exception as exc:  # same infra boundary as the sim half
        error = f"{type(exc).__name__}: {exc}"
    finally:
        _teardown((container,))
    return rc, stdout, error
