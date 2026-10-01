"""The scheduler: how many cases are in flight, and in which order they start.

The governors are pure policy over injected samples and an injected clock, so every
admission rule is asserted without a GPU. ``dispatch`` runs real threads on trivial
work with a tiny tick.
"""

from __future__ import annotations

import subprocess
import threading
from types import SimpleNamespace

import pytest

from cv_infra import scheduler
from cv_infra.scheduler import (
    ADMIT_SPACING_S,
    UTIL_TARGET_PCT,
    UTIL_WINDOW_S,
    AdaptiveGovernor,
    FixedGovernor,
    HostSample,
    dispatch,
)

# The real probe, captured before the autouse fixture (conftest) patches the module name.
REAL_PROBE = scheduler.probe_host

GiB = 1024.0


def sample(used=8 * GiB, total=96 * GiB, util=10.0, ram=100 * GiB):
    return HostSample(gpu_used_mib=used, gpu_total_mib=total, gpu_util_pct=util,
                      ram_available_mib=ram)  # fmt: skip


def meminfo(tmp_path, text="MemTotal: 131072000 kB\nMemAvailable: 104857600 kB\n"):
    path = tmp_path / "meminfo"
    path.write_text(text, encoding="utf-8")
    return str(path)


def smi(stdout="", returncode=0):
    return lambda *args, **kwargs: SimpleNamespace(stdout=stdout, returncode=returncode)


# --- probe_host -------------------------------------------------------------------------


def test_the_probe_sums_gpu_memory_takes_the_busiest_gpu_and_reads_memavailable(tmp_path):
    host = REAL_PROBE(run=smi("7396, 97887, 12\n1000, 24000, 80\n"), meminfo_path=meminfo(tmp_path))
    assert host == HostSample(8396.0, 121887.0, 80.0, 102400.0)


@pytest.mark.parametrize(
    "run",
    [
        smi(returncode=9),  # nvidia-smi present but failing
        smi(stdout="\n"),  # no GPU rows at all
        smi(stdout="7396, n/a, 12\n"),  # an unparseable field
    ],
)
def test_a_probe_without_evidence_is_none(tmp_path, run):
    assert REAL_PROBE(run=run, meminfo_path=meminfo(tmp_path)) is None


def test_a_missing_nvidia_smi_is_none(tmp_path):
    def missing(*args, **kwargs):
        raise FileNotFoundError("nvidia-smi")

    assert REAL_PROBE(run=missing, meminfo_path=meminfo(tmp_path)) is None


def test_a_hung_nvidia_smi_is_none(tmp_path):
    def hung(*args, **kwargs):
        raise subprocess.TimeoutExpired("nvidia-smi", 10)

    assert REAL_PROBE(run=hung, meminfo_path=meminfo(tmp_path)) is None


def test_a_meminfo_without_memavailable_is_none(tmp_path):
    path = meminfo(tmp_path, "MemTotal: 1 kB\n")
    assert REAL_PROBE(run=smi("1, 2, 3\n"), meminfo_path=path) is None


# --- FixedGovernor ----------------------------------------------------------------------


def test_a_fixed_governor_holds_exactly_k():
    governor = FixedGovernor(2)
    governor.observe(0.0)
    governor.back_off()  # the operator's K is not the scheduler's to change
    assert [governor.admit(running, 0.0) for running in (0, 1, 2)] == [True, True, False]


# --- AdaptiveGovernor -------------------------------------------------------------------


def adaptive(samples, **kwargs):
    feed = iter(samples)
    return AdaptiveGovernor(probe=lambda: next(feed), **kwargs)


def test_the_first_case_always_starts_even_with_no_evidence(capsys):
    governor = adaptive([None, None])
    governor.observe(0.0)
    governor.observe(1.0)
    assert governor.admit(0, 1.0)
    assert not governor.admit(1, 100.0)  # blind: one at a time
    assert capsys.readouterr().err.count('"event": "blind"') == 1  # said once, not per tick


def test_the_default_probe_is_the_module_probe(monkeypatch):
    monkeypatch.setattr(scheduler, "probe_host", lambda: sample())
    governor = AdaptiveGovernor()
    governor.observe(0.0)
    assert governor.admit(0, 0.0)
    assert governor.admit(1, ADMIT_SPACING_S)


def test_a_probe_that_comes_back_ends_blindness():
    governor = adaptive([None, sample()])
    governor.observe(0.0)
    governor.observe(1.0)
    assert governor.admit(0, 1.0)
    assert governor.admit(1, 1.0 + ADMIT_SPACING_S)


def test_room_on_the_gpu_admits_the_next_case_once_the_spacing_has_passed(capsys):
    governor = adaptive([sample(), sample(used=14 * GiB, util=30.0)])
    governor.observe(0.0)
    assert governor.admit(0, 0.0)
    governor.observe(ADMIT_SPACING_S - 1)
    assert not governor.admit(1, ADMIT_SPACING_S - 1)  # its load is not visible yet
    assert governor.admit(1, ADMIT_SPACING_S)
    assert '"event": "grow"' in capsys.readouterr().err
    assert governor.level == 2


def test_a_case_that_ends_is_replaced_at_once_up_to_the_level_reached():
    governor = adaptive([sample(), sample(util=10.0), sample(util=90.0)])
    governor.observe(0.0)
    governor.admit(0, 0.0)
    governor.observe(ADMIT_SPACING_S)
    assert governor.admit(1, ADMIT_SPACING_S)  # grown to 2
    governor.observe(ADMIT_SPACING_S + 1)  # one ended; the GPU looks busy right now
    assert governor.admit(1, ADMIT_SPACING_S + 1)  # refilled without waiting or util
    assert not governor.admit(2, ADMIT_SPACING_S + 1)  # but not grown past 2 like this


def test_a_refill_still_has_to_fit_in_memory():
    governor = adaptive([sample(), sample(), sample(used=90 * GiB)])
    governor.observe(0.0)
    governor.admit(0, 0.0)
    governor.observe(ADMIT_SPACING_S)
    governor.admit(1, ADMIT_SPACING_S)
    governor.observe(ADMIT_SPACING_S + 1)
    assert not governor.admit(1, ADMIT_SPACING_S + 1)


def test_a_lost_gpu_drops_a_level_and_never_climbs_back_to_it(capsys):
    governor = adaptive([sample()] * 6)
    governor.observe(0.0)
    governor.admit(0, 0.0)
    governor.observe(ADMIT_SPACING_S)
    assert governor.admit(1, ADMIT_SPACING_S)  # level 2
    governor.back_off()
    assert (governor.level, governor.ceiling) == (1, 1)
    governor.observe(3 * ADMIT_SPACING_S)
    assert not governor.admit(1, 3 * ADMIT_SPACING_S)  # room, util and time — but capped
    governor.back_off()
    assert governor.level == 1  # never below one
    assert '"event": "back-off"' in capsys.readouterr().err


def test_a_busy_gpu_admits_nothing_more():
    governor = adaptive([sample(util=UTIL_TARGET_PCT), sample(util=UTIL_TARGET_PCT)])
    governor.observe(0.0)
    governor.admit(0, 0.0)
    governor.observe(ADMIT_SPACING_S)
    assert not governor.admit(1, ADMIT_SPACING_S)


def test_utilisation_is_judged_over_the_window_only():
    governor = adaptive([sample(util=100.0), sample(util=10.0)])
    governor.observe(0.0)
    governor.admit(0, 0.0)
    governor.observe(UTIL_WINDOW_S + 1)  # the busy sample has aged out
    assert governor.admit(1, UTIL_WINDOW_S + 1)


def test_the_largest_footprint_seen_is_what_the_next_case_must_fit_beside():
    # Two cases took 40 GiB of GPU and 30 GiB of RAM between them: 20 / 15 per case.
    governor = adaptive([sample(), sample(used=48 * GiB, ram=70 * GiB)])
    governor.observe(0.0)
    governor.admit(0, 0.0)
    governor.observe(ADMIT_SPACING_S)
    assert governor.admit(2, ADMIT_SPACING_S)
    assert (governor.vram_per_case_mib, governor.ram_per_case_mib) == (20 * GiB, 15 * GiB)


def test_a_gpu_without_room_for_one_more_case_admits_nothing():
    governor = adaptive([sample(), sample(used=88 * GiB)])
    governor.observe(0.0)
    governor.admit(0, 0.0)
    governor.observe(ADMIT_SPACING_S)
    assert not governor.admit(1, ADMIT_SPACING_S)


def test_ram_without_room_for_one_more_case_admits_nothing():
    governor = adaptive([sample(), sample(ram=12 * GiB)])
    governor.observe(0.0)
    governor.admit(0, 0.0)
    governor.observe(ADMIT_SPACING_S)
    assert not governor.admit(1, ADMIT_SPACING_S)


def test_the_thread_ceiling_holds_whatever_the_host_says():
    governor = adaptive([sample(), sample()], max_parallel=2)
    governor.observe(0.0)
    governor.admit(0, 0.0)
    governor.observe(ADMIT_SPACING_S)
    assert not governor.admit(2, ADMIT_SPACING_S)


# --- dispatch ---------------------------------------------------------------------------


def test_dispatch_keeps_item_order_and_reports_the_peak():
    gate = threading.Event()

    def work(item):
        gate.wait(5)
        return item * 10

    threading.Timer(0.2, gate.set).start()  # both workers are busy until then
    done = dispatch([1, 2, 3], work, FixedGovernor(2), tick_s=0.01)
    assert done.results == [10, 20, 30]
    assert done.peak == 2
    assert not done.truncated


def test_the_budget_is_read_once_per_start_and_stops_starting_at_the_deadline():
    reads = iter([0.0, 1.0, 5.0])
    done = dispatch(
        ["a", "b", "c", "d"],
        str.upper,
        FixedGovernor(1),
        deadline=2.0,
        deadline_clock=lambda: next(reads),
        tick_s=0.01,
    )
    assert done.results == ["A", "B"]  # the prefix that started in time
    assert done.truncated


def test_a_spent_budget_starts_nothing():
    done = dispatch(["a"], str.upper, FixedGovernor(1), deadline=0.0, clock=lambda: 1.0)
    assert (done.results, done.truncated, done.peak) == ([], True, 0)


def test_a_worker_fault_propagates():
    def boom(item):
        raise RuntimeError(item)

    with pytest.raises(RuntimeError, match="x"):
        dispatch(["x"], boom, FixedGovernor(1), tick_s=0.01)


def test_the_adaptive_governor_ramps_up_on_a_host_with_room():
    gate = threading.Event()
    now = iter(float(t) for t in range(0, 10_000, 7))  # every look is 7 s later

    def work(item):
        gate.wait(5)
        return item

    governor = adaptive(iter(lambda: sample(), None))
    threading.Timer(0.3, gate.set).start()
    done = dispatch(list(range(4)), work, governor, clock=lambda: next(now), tick_s=0.01)
    assert done.results == [0, 1, 2, 3]
    assert done.peak >= 2
