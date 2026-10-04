"""How many cases run at once — decided from the host, not guessed in advance.

Every case container time-shares ONE GPU, and the number of cases that still adds
throughput is a property of the WORKLOAD (how hard each scene renders), not of the
platform. So ``--concurrency auto`` (the default) does not pick a number: it starts one
case, watches the host, and starts the next one only while the GPU still has room.

MEASURED 2026-10-01 on the workstation (RTX PRO 6000 Blackwell 96 GB, 32 cores, 125 GB),
the carter consumer's cases at a FIXED N, each with its own cache slot:

    N                      1      2      4      8
    wall per case [s]      44.5   34.0   26.3   25.4
    GPU util, mean [%]     30     51     62     68
    GPU memory, peak [GB]  13.8   20.2   32.9   58.6   (7.4 GB of it not ours)
    load1 (32 cores)       6.3    7.8    8.1    13.7

Throughput stops rising once the GPU is busy ~60 % of the time; memory, RAM and CPU are
nowhere near their limits at that point (~6.4 GB VRAM and ~5 GB RAM per case). Hence
the policy below. The governor keeps a LEVEL — the most cases it has decided the host
can take. Growing the level by one needs evidence: ``ADMIT_SPACING_S`` since the last
start (so its load is visible) and a recent mean GPU utilisation under
``UTIL_TARGET_PCT``. Refilling up to the level when a case ends needs none of that —
only that the memory a case is seen to take still fits — because a short case would
otherwise cap the run at (case length / spacing) whatever the GPU says (MEASURED: ~40 s
carter cases held 2-3 in flight when every start waited the spacing). A heavier scene
saturates the GPU at a lower level and stays there.

No probe (no ``nvidia-smi``, an unreadable ``/proc/meminfo``) means no evidence, and no
evidence means ONE case at a time — the run is slower, never oversubscribed.

``--concurrency K`` keeps its old meaning: exactly K at once, no probing.

The dispatcher is the same for both: cases START in array order and the budget is read
at each start, so what runs is always a prefix of the array (the coverage the report
claims is the coverage that ran).
"""

from __future__ import annotations

import json
import subprocess
import sys
import time
from collections import deque
from collections.abc import Callable, Sequence
from concurrent.futures import FIRST_COMPLETED, Future, ThreadPoolExecutor, wait
from dataclasses import dataclass
from typing import Any

UTIL_TARGET_PCT = 60.0  # the measured knee (see the table above)
UTIL_WINDOW_S = 30.0  # utilisation is bursty per frame; judge a window, not a sample
ADMIT_SPACING_S = 20.0  # a case's boot load takes this long to show up (measured)
MAX_PARALLEL = 16  # an upper bound on threads, not a target — the probe decides
VRAM_RESERVE_MIB = 4096.0  # never plan the GPU down to its last few GB
RAM_RESERVE_MIB = 8192.0
INITIAL_CASE_VRAM_MIB = 8192.0  # before any case is measured (measured: ~6.4 GB)
INITIAL_CASE_RAM_MIB = 8192.0  # likewise (measured: ~5 GB)
HEADROOM = 1.25  # the next case is assumed to take this much more than the largest seen
TICK_S = 1.0

NVIDIA_SMI_QUERY = (
    "nvidia-smi",
    "--query-gpu=memory.used,memory.total,utilization.gpu",
    "--format=csv,noheader,nounits",
)
MEMINFO = "/proc/meminfo"


@dataclass(frozen=True)
class HostSample:
    """One look at the host: GPU memory (summed over GPUs — a case gets ``--gpus all``),
    the busiest GPU's utilisation, and the RAM the kernel says is available."""

    gpu_used_mib: float
    gpu_total_mib: float
    gpu_util_pct: float
    ram_available_mib: float


def probe_host(
    run: Callable[..., Any] = subprocess.run, meminfo_path: str = MEMINFO
) -> HostSample | None:
    """Read the host, or ``None`` when it cannot be read (which means: run serially)."""
    try:
        completed = run(list(NVIDIA_SMI_QUERY), capture_output=True, text=True, timeout=10)
        if completed.returncode != 0:
            return None
        rows = [
            [float(field) for field in line.split(",")]
            for line in completed.stdout.splitlines()
            if line.strip()
        ]
        with open(meminfo_path, encoding="utf-8") as handle:
            available_kib = next(
                int(line.split()[1]) for line in handle if line.startswith("MemAvailable:")
            )
    except (OSError, subprocess.SubprocessError, ValueError, IndexError, StopIteration):
        return None
    if not rows:
        return None
    return HostSample(
        gpu_used_mib=sum(row[0] for row in rows),
        gpu_total_mib=sum(row[1] for row in rows),
        gpu_util_pct=max(row[2] for row in rows),
        ram_available_mib=available_kib / 1024.0,
    )


def _log(event: str, **fields: Any) -> None:
    print(
        f"[cv-infra] scheduler {json.dumps({'event': event, **fields}, sort_keys=True)}",
        file=sys.stderr,
        flush=True,
    )


class FixedGovernor:
    """``--concurrency K``: K at once, no probing."""

    mode = "fixed"

    def __init__(self, limit: int) -> None:
        self.max_parallel = limit

    def observe(self, now: float) -> None:
        """Nothing to watch."""

    def admit(self, running: int, now: float) -> bool:
        return running < self.max_parallel

    def back_off(self) -> None:
        """A fixed K is the operator's call; a GPU fault does not change it."""


class AdaptiveGovernor:
    """``--concurrency auto``: admit while the host has room (see the module docstring).

    ``observe`` is called once per dispatcher tick (and right before every decision), so
    the baseline — the host BEFORE the first case — is the first sample taken.
    """

    mode = "auto"

    def __init__(
        self,
        probe: Callable[[], HostSample | None] | None = None,
        max_parallel: int = MAX_PARALLEL,
    ) -> None:
        self.max_parallel = max_parallel
        self._probe = probe or (lambda: probe_host())  # looked up per call: patchable
        self._baseline: HostSample | None = None
        self._latest: HostSample | None = None
        self._utils: deque[tuple[float, float]] = deque()
        self._last_admit = float("-inf")
        self._blind = False
        self.level = 1
        self.ceiling = max_parallel
        self.vram_per_case_mib = INITIAL_CASE_VRAM_MIB
        self.ram_per_case_mib = INITIAL_CASE_RAM_MIB

    def observe(self, now: float) -> None:
        sample = self._probe()
        if sample is None:
            if not self._blind:
                _log(
                    "blind",
                    reason="host probe unavailable (nvidia-smi / /proc/meminfo)",
                    consequence="one case at a time",
                )
            self._blind = True
            return
        self._blind = False
        self._latest = sample
        if self._baseline is None:
            self._baseline = sample
        self._utils.append((now, sample.gpu_util_pct))
        while self._utils and self._utils[0][0] < now - UTIL_WINDOW_S:
            self._utils.popleft()

    def admit(self, running: int, now: float) -> bool:
        if running == 0:  # nothing in flight: always start one (no evidence needed)
            self._last_admit = now
            return True
        if self._blind or self._latest is None or self._baseline is None:
            return False
        if running >= self.max_parallel:
            return False
        self._learn(running)
        latest = self._latest
        vram_left = latest.gpu_total_mib - latest.gpu_used_mib - HEADROOM * self.vram_per_case_mib
        ram_left = latest.ram_available_mib - HEADROOM * self.ram_per_case_mib
        if vram_left < VRAM_RESERVE_MIB or ram_left < RAM_RESERVE_MIB:
            return False
        if running < self.level:  # a refill: the host already took this many
            self._last_admit = now
            return True
        util = sum(value for _, value in self._utils) / len(self._utils)
        if (
            now - self._last_admit < ADMIT_SPACING_S
            or util >= UTIL_TARGET_PCT
            or running + 1 > self.ceiling
        ):
            return False
        self._last_admit = now
        self.level = running + 1
        _log(
            "grow",
            level=self.level,
            gpu_util_mean_pct=round(util, 1),
            vram_free_mib=round(latest.gpu_total_mib - latest.gpu_used_mib),
            vram_per_case_mib=round(self.vram_per_case_mib),
            ram_per_case_mib=round(self.ram_per_case_mib),
        )
        return True

    def back_off(self) -> None:
        """A case lost its GPU: one fewer in flight from now on, and never back up to
        the level it happened at (thread-safe enough: two int stores under the GIL)."""
        self.level = max(1, self.level - 1)
        self.ceiling = self.level
        _log("back-off", level=self.level, reason="a case lost its GPU device")

    def _learn(self, running: int) -> None:
        """The largest per-case footprint seen so far. Never shrinks: a case's peak is
        what the next one has to fit beside (a just-started case that has not allocated
        yet only makes one sample low, never the estimate)."""
        baseline, latest = self._baseline, self._latest
        assert baseline is not None and latest is not None  # guarded by admit()
        self.vram_per_case_mib = max(
            self.vram_per_case_mib, (latest.gpu_used_mib - baseline.gpu_used_mib) / running
        )
        self.ram_per_case_mib = max(
            self.ram_per_case_mib,
            (baseline.ram_available_mib - latest.ram_available_mib) / running,
        )


@dataclass
class Live:
    """What is in flight right now — read by anyone watching the run (the dashboard
    sampler); written only by ``dispatch``."""

    running: int = 0


@dataclass(frozen=True)
class Dispatched:
    """What the dispatcher ran: one result per STARTED item, in item order, the most
    items it had in flight at once, and whether it stopped starting before the end."""

    results: list[Any]
    peak: int
    truncated: bool


def dispatch(
    items: Sequence[Any],
    run_one: Callable[[Any], Any],
    governor: FixedGovernor | AdaptiveGovernor,
    *,
    deadline: float | None = None,
    clock: Callable[[], float] = time.monotonic,
    deadline_clock: Callable[[], float] | None = None,
    tick_s: float = TICK_S,
    live: Live | None = None,
) -> Dispatched:
    """Start ``items`` in order as the governor allows; stop STARTING at ``deadline``.

    ``deadline_clock`` (default: ``clock``) is read exactly once per item, at the moment
    the governor has said yes to it — the budget is checked when a case would start,
    never while one waits for room.

    A worker exception propagates (after every started item finished): a platform fault
    must not be folded into a result.
    """
    deadline_clock = deadline_clock or clock
    live = live or Live()
    futures: list[Future[Any]] = []
    running: set[Future[Any]] = set()
    peak = 0
    truncated = False
    with ThreadPoolExecutor(max_workers=governor.max_parallel) as pool:
        while True:  # leaves when nothing is running and nothing more will start
            running = {future for future in running if not future.done()}
            live.running = len(running)
            if len(futures) < len(items) and not truncated:
                now = clock()
                governor.observe(now)
                if governor.admit(len(running), now):
                    if deadline is not None and deadline_clock() >= deadline:
                        truncated = True
                        continue
                    future = pool.submit(run_one, items[len(futures)])
                    futures.append(future)
                    running.add(future)
                    live.running = len(running)
                    peak = max(peak, len(running))
                    continue
            if not running:
                break
            wait(running, timeout=tick_s, return_when=FIRST_COMPLETED)
    return Dispatched(
        results=[future.result() for future in futures], peak=peak, truncated=truncated
    )
