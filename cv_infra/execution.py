"""Case execution seam — the ONLY module in the verify pipeline that touches docker.

One CASE RUN = one container: ``<sim_script> --<axis>=<value> ...``
against the checkout, with the case's output directory overlaid rw on top of the
read-only checkout, then the same image again (GPU-free) for the oracle. Everything
else in the package stays docker-free, so the whole pipeline is testable on a CPU host
with a duck-typed fake client (the idiom the orchestrator seam already relied on).

Most of the docker-facing machinery here is LIFTED from
``cv_infra/orchestrator/supervisor.py`` (since removed; see git history) —
cache CoW seeding, the image-present gate with its pull-liveness watchdog, the
supervision loop, the finally-teardown — because those blocks encode measurements, not
opinions:

* ``:ro`` on a Kit/CUDA cache does not make it read-only, it turns it OFF (measured
  47 s -> 1.05 s per robot spawn); hence the per-case ``cp -a`` copy-on-write seed
  bound ``rw``, with an ownership guard so a non-preserving copy is loud.
* dockerd creates a missing bind source as root, and the stock Isaac image runs as
  uid 1234 — every host path bound here is pre-created and made world-writable first.
* an implicit ``containers.run`` pull can hang forever; the image is made present
  BEFORE the container starts, under a progress-liveness watchdog.
* the sim's EXIT CODE cannot carry pass/fail (``SimulationApp.close()`` exits
  the process with status 0, and the stock ``python.sh`` squashes non-zero to 1) — so
  ``rc`` is only ever read as "did the process die badly" (ERROR lane). The verdict
  comes from the oracle's stdout, never from here.

Assumptions surfaced (written before the contract modules existed): ``spec`` and
``case`` are DUCK-TYPED. ``spec`` must carry ``checkout``, ``sim_output_dir``,
``sim_image``, ``oracle_script``, ``case_timeout_s``, ``oracle_timeout_s``,
``shm_size`` and ``max_zip_mb``; ``case`` must carry ``case_index``, ``case_id``,
``repeat``, ``seed`` and ``argv`` (``argv[0]`` = the sim script). Those are exactly the
fields ``contract.inputs.VerifySpec`` / ``contract.cases.CaseRun`` grew later — this
module deliberately does not import them, so the contract layer stays the lowest layer.
"""

from __future__ import annotations

import fcntl
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
# runs ``./verify/sim.py --x=1`` from their repo root (local parity).
CHECKOUT_MOUNT = "/cv/checkout"

# The stock Isaac image's ENTRYPOINT swallows ``docker run`` arguments, so the
# case command is passed through a minimal shell wrapper. The user entrypoint itself is
# executable and owns its interpreter via its shebang: Python, Bash, or another binary.
EXECUTE_ENTRYPOINT = "/bin/sh"
EXECUTE_SCRIPT = 'exec "$0" "$@"'
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

# CACHE SLOTS (base root alone, no scratch root). Two Isaac instances must not share one
# Kit cache tree — it holds per-instance lock files (``kit/DerivedDataCache/
# app_instance_lock0`` — seen in the provisioned tree). So every run LEASES a slot for
# its lifetime: slot 0 is the provisioned ``<root>/<digest12>`` itself (a serial run is
# exactly what it was), slot k>=1 is ``<root>/<digest12>.slots/<k>``, copied ONCE from
# slot 0's warm tiers and then kept — it stays warm for every later run. The lease is an
# ``flock`` on ``<root>/<digest12>.slot<k>.lock``, so it also holds across two CLI
# processes (two jobs on two runners of one host) and dies with a crashed process.
#
# The copy runs INSIDE the case image as its own user (uid 1234), because the warm tree
# is uid 1234's ``0700`` and the CLI is not root (measured: the host-side ``cp -a`` of the
# scratch mode fails exactly there). MEASURED 2026-10-01: ~1.5 GB per slot, 8 slots in
# 5 s.
MAX_CACHE_SLOTS = 32
SLOT_READY_MARKER = ".cv-infra-slot-ready"
SLOT_COPY_TIMEOUT_S = 600.0
SLOT_COPY_SCRIPT = (
    'for tier in "$@"; do rm -rf "/dst/$tier" && cp -a "/src/$tier" "/dst/$tier" || exit 1; done'
)

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

# A GPU fault inside one case. MEASURED 2026-10-01 (5 carter cases in flight): Kit's
# Vulkan backend logged ``VkResult: ERROR_DEVICE_LOST`` 30 s into one case and then hung
# — it never exits — until the 1800 s case timeout; the other cases ran on, unaffected.
# So the sim's own log is watched while it runs, and that line ends the case at once
# with this marker (consumer: the CLI retries such a run once and backs off concurrency).
GPU_LOST_MARKER = "gpu device lost:"
GPU_LOST_SIGNATURE = b"ERROR_DEVICE_LOST"
GPU_WATCH_PERIOD_S = 10.0
GPU_WATCH_TAIL_LINES = 200

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
    container name, the case's private network, its host output dir, its cache scratch
    dir, its log and its zip, so all of a case's names line up in a listing. Body is the
    orchestrator's ``network_name_for`` with a ``cvc-`` (case) prefix.
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


def resolve_docker_client(docker_client: Any) -> Any:
    """The injected duck-typed client, else a lazily-imported real one.

    Lazy so importing this module (and running the whole CPU test suite) needs no
    docker daemon and no SDK import; tests inject a fake. Public because the CLI
    resolves the client ONCE for a whole run (a dead daemon is then one exit-3 line
    instead of one ERROR per case).
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


def _image_namespace(sim_image: str) -> str:
    """``<digest12>`` — the cache subtree that belongs to THIS image, and only it.

    Every tier under the cache root is derived from the Isaac BUILD that wrote it: Kit's
    shader cache, the CUDA ComputeCache and the asset closure are all keyed to that
    build's own formats. Pointing two different images at one tree is not a shared warm
    cache, it is corruption — and the shape it takes (a stale shader blob loaded by a
    different Kit) is a crash nobody can trace back to a directory.

    The admit contract already refuses an image that is not digest-pinned
    (``contract.inputs._digest_pinned_image``), so the 12 hex chars always exist here;
    they are short enough for an operator to type and wide enough to be unique.
    """
    match = re.search(r"@sha256:([0-9a-f]{12})", sim_image)
    if match is None:
        raise ValueError(
            f"sim image {sim_image!r} is not digest-pinned, so its cache subtree cannot be"
            " named — the per-image cache namespace is what keeps one image's Kit/CUDA"
            " caches out of another's (pass `--sim-image <name>@sha256:...`)"
        )
    return match.group(1)


@dataclass
class CacheLease:
    """A held cache slot: its tree, its index, and the open lock that holds it."""

    path: Path
    slot: int
    lock_fd: int

    def release(self) -> None:
        """Best effort, like every teardown step: closing the fd drops the flock."""
        try:
            fcntl.flock(self.lock_fd, fcntl.LOCK_UN)
        finally:
            os.close(self.lock_fd)


def _lease_cache_slot(
    client: Any, sim_image: str, base: Path, *, poll_interval_s: float
) -> CacheLease:
    """The lowest free slot, provisioned if it is new (see CACHE SLOTS above)."""
    namespace = base.name
    for slot in range(MAX_CACHE_SLOTS):
        lock_path = base.parent / f"{namespace}.slot{slot}.lock"
        fd = os.open(lock_path, os.O_RDWR | os.O_CREAT, 0o666)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            os.close(fd)
            continue
        lease = CacheLease(
            base if slot == 0 else base.parent / f"{namespace}.slots" / str(slot), slot, fd
        )
        try:
            if slot:
                _provision_slot(client, sim_image, base, lease, poll_interval_s=poll_interval_s)
        except Exception:
            lease.release()
            raise
        return lease
    raise RuntimeError(
        f"all {MAX_CACHE_SLOTS} cache slots of {base} are leased — more Isaac instances"
        " than this host can hold are running against one image"
    )


def _provision_slot(
    client: Any, sim_image: str, base: Path, lease: CacheLease, *, poll_interval_s: float
) -> None:
    """Make slot k a warm copy of slot 0 — once. A slot without its ready marker (new,
    or a copy that died half-way) is (re)copied; the copy replaces each tier whole."""
    slot_dir = lease.path
    for subpath in ("cache", *(sub for sub, _ in CACHE_SCRATCH_MOUNTS)):
        (slot_dir / subpath).mkdir(parents=True, exist_ok=True)
        (slot_dir / subpath).chmod(0o777)  # the image runs non-root (uid 1234)
    marker = slot_dir / SLOT_READY_MARKER
    if marker.is_file():
        return
    tiers = [subpath.split("/", 1)[1] for subpath, _ in CACHE_BASE_MOUNTS]  # "cache/kit" -> "kit"
    started = time.monotonic()
    container = None
    try:
        container = client.containers.run(
            sim_image,
            entrypoint="/bin/sh",
            command=["-c", SLOT_COPY_SCRIPT, "cv-slot-copy", *tiers],
            volumes={
                str(base / "cache"): {"bind": "/src", "mode": "ro"},
                str(slot_dir / "cache"): {"bind": "/dst", "mode": "rw"},
            },
            detach=True,
            name=f"cv-cache-{base.name}-slot{lease.slot}",
            labels={LABEL_SLUG: f"cache-slot-{lease.slot}"},
        )
        rc, error = _supervise_until_exit(
            container, timeout_s=SLOT_COPY_TIMEOUT_S, poll_interval_s=poll_interval_s
        )
    finally:
        _teardown((container,))
    if error is not None or rc != 0:
        raise RuntimeError(
            f"cache slot {lease.slot} copy into {slot_dir} failed ({error or f'exit {rc}'})"
            " — a slot that is not a warm copy would run all-cold while measured as warm"
        )
    marker.write_text(
        f"copied from {base} in {time.monotonic() - started:.1f}s\n", encoding="utf-8"
    )
    line = json.dumps(
        {
            "slot": lease.slot,
            "path": str(slot_dir),
            "seconds": round(time.monotonic() - started, 3),
        },
        sort_keys=True,
    )
    print(f"[cv-infra] cache-slot {line}", file=sys.stderr, flush=True)


def _cache_volumes(
    cache_root: str | os.PathLike[str] | None,
    cache_scratch_root: str | os.PathLike[str] | None,
    slug: str,
    sim_image: str,
    *,
    client: Any = None,
    poll_interval_s: float = DEFAULT_POLL_INTERVAL_S,
) -> tuple[dict[str, dict[str, str]], Path | None, CacheLease | None]:
    """Resolve cache roots to docker ``volumes`` binds — slotted or per-case seeded.
    Returns ``(volumes, per-case scratch | None, held slot | None)``.

    Effective roots = the arguments (win) or ``$CV_ISAAC_CACHE_ROOT`` /
    ``$CV_ISAAC_CACHE_SCRATCH_ROOT``; when neither is set there are ZERO cache mounts
    (a cold but correct run — the CI default until a runner is provisioned).

    The base root is namespaced PER IMAGE: the six tiers live under
    ``<cache_root>/<digest12>`` (see ``_image_namespace``). The scratch root is not —
    its children are already per-case slugs, and nothing warm is shared there.

    * base root alone -> a LEASED SLOT (see CACHE SLOTS above): all six binds ``rw``
      from the slot this run holds — slot 0 (the base itself) when nothing else runs.
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
        return {}, None, None
    resolved = Path(root).resolve() / _image_namespace(sim_image)
    if not resolved.is_dir():
        # Neither silently cold (the failure everyone believes is a warm run) nor created
        # here: the tree has to be owned by uid 1234 for the container to write it,
        # and this process is not root. Name the exact command instead.
        raise ValueError(
            f"cache subtree {resolved} does not exist or is not a directory — this image's"
            " cache was never provisioned. Run: bash scripts/measure/warm_cache.sh"
            f" {resolved} provision (it creates the 6-way tree and chowns it to 1234:1234;"
            " the CLI must not, it is not root)"
        )
    if scratch_root is None:
        lease = _lease_cache_slot(client, sim_image, resolved, poll_interval_s=poll_interval_s)
        volumes = {
            str(lease.path / subpath): {"bind": container_path, "mode": "rw"}
            for subpath, container_path in CACHE_MOUNTS
        }
        return volumes, None, lease
    scratch_resolved = Path(scratch_root).resolve()
    if not scratch_resolved.is_dir():
        raise ValueError(
            f"cache_scratch_root {scratch_resolved} does not exist or is not a directory "
            f"(the scratch ROOT is host provisioning's job; per-case dirs are created here)"
        )
    volumes, scratch = _seeded_cache_volumes(slug, resolved, scratch_resolved)
    return volumes, scratch, None


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
        host_dir.chmod(0o777)  # the stock image runs non-root (uid 1234)
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
    """Loud guard: the seeded tier must be writable by the same uid as the base (uid 1234).

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
    so the dir is made world-writable. Precise chown is host provisioning's job.
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
    container: Any, *, timeout_s: float, poll_interval_s: float, watch_gpu: bool = False
) -> tuple[int | None, str | None]:
    """Wait for the container to exit, or kill the case on the wall-clock deadline —
    or, with ``watch_gpu``, as soon as its log says the GPU device was lost.

    Returns ``(exit_code, error)`` — exactly one side is set. On timeout the kill
    itself happens in the caller's finally-teardown (stop + force-remove), so there is
    one place that removes containers, not two.
    """
    deadline = time.monotonic() + timeout_s
    next_watch = time.monotonic()
    while True:
        container.reload()
        if container.status == "exited":
            return _exit_code(container), None
        if time.monotonic() >= deadline:
            return None, (
                f"{CASE_TIMEOUT_MARKER} container still running after {timeout_s}s"
                " (teardown kills it)"
            )
        if watch_gpu and time.monotonic() >= next_watch:
            next_watch = time.monotonic() + GPU_WATCH_PERIOD_S
            if GPU_LOST_SIGNATURE in _log_tail(container):
                return None, (
                    f"{GPU_LOST_MARKER} the renderer reported "
                    f"{GPU_LOST_SIGNATURE.decode()} — Kit hangs after that, so the case"
                    " was ended at once instead of at its timeout"
                )
        time.sleep(poll_interval_s)


def _log_tail(container: Any) -> bytes:
    """The last lines of a RUNNING container's log — best effort: a log read that fails
    mid-run is not a reason to end the case (the final collection still reports it)."""
    try:
        return container.logs(tail=GPU_WATCH_TAIL_LINES)
    except Exception:
        return b""


def _exit_code(container: Any) -> int:
    """Fetch an exited container's exit code (``wait`` returns immediately post-exit)."""
    return int(container.wait(timeout=_EXIT_CODE_WAIT_S)["StatusCode"])


def _teardown(containers: tuple[Any, ...]) -> None:
    """Best-effort stop/remove of every spawned container — no leftover on any path.

    Every step is attempted regardless of earlier failures; failures are surfaced on
    stderr but never raised (teardown must not mask the case outcome). The case's
    private network goes in ``_remove_network``, after its container.
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


def _create_network(client: Any, slug: str, case_id: str) -> Any:
    """The case's OWN bridge network. Concurrent cases on docker's default bridge share
    one broadcast domain, so a sim that speaks multicast discovery (ROS 2 / DDS) would
    hear its neighbour case on the same domain id. A private bridge still routes out
    (asset downloads) and resolves names; it only stops cases from seeing each other."""
    return client.networks.create(
        f"{slug}-net", driver="bridge", labels={LABEL_CASE_ID: case_id, LABEL_SLUG: slug}
    )


def _remove_network(network: Any) -> None:
    """Best effort, after the container is gone (a network with an endpoint refuses)."""
    if network is None:
        return
    try:
        network.remove()
    except Exception as exc:
        print(f"[cv-infra] teardown network remove failed: {exc!r}", file=sys.stderr)


def _release_lease(lease: CacheLease | None) -> None:
    if lease is None:
        return
    try:
        lease.release()
    except Exception as exc:
        print(f"[cv-infra] cache slot release failed: {exc!r}", file=sys.stderr)


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

    ``rc`` is reported but never read as pass/fail: after boot, the sim's exit
    status cannot travel out of the container. It only separates "died badly" (ERROR)
    from "ran to completion" (ask the oracle).
    """
    client = resolve_docker_client(client)
    slug = slug_for(run_key(case))
    run_dir = Path(run_dir)
    log_path = run_dir / "logs" / f"{slug}.sim.log"
    zip_path = run_dir / "zips" / f"{slug}.zip"
    out_dir = run_dir / "cases" / slug / "out"
    container = None
    network = None
    scratch_dir = None
    lease = None
    rc: int | None = None
    error: str | None = None
    started = time.monotonic()
    try:
        out_dir = _prepare_case_dir(run_dir, slug)
        _ensure_image_present(
            client,
            spec.sim_image,
            kind="sim",
            stall_timeout_s=pull_stall_timeout_s,
            poll_interval_s=poll_interval_s,
        )
        # After the image gate: a new cache slot is copied by a container of this image.
        volumes, scratch_dir, lease = _cache_volumes(
            cache_root,
            cache_scratch_root,
            slug,
            spec.sim_image,
            client=client,
            poll_interval_s=poll_interval_s,
        )
        volumes.update(_case_volumes(spec, out_dir, mode="rw"))
        network = _create_network(client, slug, case.case_id)
        container = client.containers.run(
            spec.sim_image,
            entrypoint=EXECUTE_ENTRYPOINT,
            command=["-lc", EXECUTE_SCRIPT, *list(case.argv)],
            working_dir=CHECKOUT_MOUNT,
            environment=_case_environment(operator_env, case.seed),
            volumes=volumes,
            device_requests=gpu_device_requests(),
            shm_size=spec.shm_size,
            detach=True,
            name=f"{slug}-sim",
            labels={LABEL_CASE_ID: case.case_id, LABEL_SLUG: slug},
            network=network.name,
        )
        rc, error = _supervise_until_exit(
            container,
            timeout_s=spec.case_timeout_s,
            poll_interval_s=poll_interval_s,
            watch_gpu=True,
        )
        _write_logs(container, log_path)
    except Exception as exc:  # infra boundary: this case ERRORs, the run continues
        error = f"{type(exc).__name__}: {exc}"
    finally:
        _teardown((container,))
        _remove_network(network)
        _release_lease(lease)
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
    client = resolve_docker_client(client)
    slug = slug_for(run_key(case))
    container = None
    rc: int | None = None
    stdout = ""
    error: str | None = None
    try:
        container = client.containers.run(
            spec.sim_image,
            entrypoint=EXECUTE_ENTRYPOINT,
            command=["-lc", EXECUTE_SCRIPT, spec.oracle_script, *list(case.argv)[1:]],
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
