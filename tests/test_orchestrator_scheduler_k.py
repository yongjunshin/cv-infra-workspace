"""compute_k + SlotAccountant + NVML seam tests (M3 §3.4, LOCKED §7.4) —
REQ-ORCH-004/005/006, REQ-DEPLOY-012, NFR-ORCH-003.

CPU-only: the k = min(...) rule with an injected fake gauge (no NVML), the
half-configured-guard error (R-NV: a silently skipped guard is the hazard),
slot-token accounting (over-launch 0 / reclaim-leak loud), and pynvml import
laziness (GPU-free hosts must import the module harmlessly).
"""

from __future__ import annotations

import sys

import pytest

from cv_infra.orchestrator.scheduler import (
    PynvmlVramGauge,
    SlotAccountant,
    compute_k,
)


class FakeGauge:
    """Injected VramGauge test double — available VRAM in MiB."""

    def __init__(self, free_mb: float) -> None:
        self._free_mb = free_mb

    def available_vram_mb(self) -> float:
        return self._free_mb


# --------------------------------------------------------------------------- #
# (a) compute_k — LOCKED §7.4 min rule (no hardcoded k anywhere)
# --------------------------------------------------------------------------- #


def test_k_is_the_operator_authoritative_cap_by_default():
    # Injected budget in, same k out — nothing hardcoded (NFR-ORCH-001 규율).
    for budget in (1, 3, 7):
        assert compute_k(budget) == budget


def test_vram_second_guard_floors_k():
    assert compute_k(8, vram_gauge=FakeGauge(10240), vram_per_instance_mb=4096) == 2


def test_authoritative_cap_wins_over_plentiful_vram():
    assert compute_k(2, vram_gauge=FakeGauge(1 << 20), vram_per_instance_mb=4096) == 2


def test_render_cap_is_an_independent_cap_term():
    assert compute_k(8, render_cap=3) == 3


def test_k_is_min_of_all_three_terms():
    assert (
        compute_k(8, vram_gauge=FakeGauge(3 * 4096), vram_per_instance_mb=4096, render_cap=5) == 3
    )


def test_insufficient_vram_yields_zero_capacity():
    # Guard leaves no room -> 0 (admission stays closed); never negative.
    assert compute_k(4, vram_gauge=FakeGauge(1000), vram_per_instance_mb=4096) == 0


def test_half_configured_vram_guard_is_a_loud_config_error():
    # R-NV: a silently skipped guard would neuter NFR-ORCH-003 — both halves
    # or neither.
    with pytest.raises(ValueError):
        compute_k(4, vram_gauge=FakeGauge(8192))
    with pytest.raises(ValueError):
        compute_k(4, vram_per_instance_mb=4096)


@pytest.mark.parametrize(
    "kwargs",
    [
        {"max_concurrent": 0},
        {"max_concurrent": 4, "vram_gauge": FakeGauge(8192), "vram_per_instance_mb": 0},
        {"max_concurrent": 4, "render_cap": 0},
    ],
)
def test_invalid_inputs_raise(kwargs):
    with pytest.raises(ValueError):
        compute_k(**kwargs)


# --------------------------------------------------------------------------- #
# (b) SlotAccountant — admission gate + reclaim accounting (REQ-ORCH-006)
# --------------------------------------------------------------------------- #


def test_slots_admit_up_to_k_then_gate_closes():
    slots = SlotAccountant(k=2)
    assert slots.try_acquire()
    assert slots.try_acquire()
    assert not slots.try_acquire()  # gate closed — the launch never happens
    assert slots.in_use == 2
    assert slots.over_launch_count == 0  # NFR-ORCH-003
    assert slots.max_concurrent_observed == 2


def test_release_returns_the_slot_for_reassignment():
    slots = SlotAccountant(k=1)
    assert slots.try_acquire()
    assert not slots.try_acquire()
    slots.release()
    assert slots.try_acquire()  # freed slot immediately re-acquirable
    slots.release()
    assert slots.acquired_total == 2
    assert slots.released_total == 2  # balanced: reclaim-leak 0
    assert slots.in_use == 0


def test_release_without_acquire_is_loud():
    with pytest.raises(RuntimeError):
        SlotAccountant(k=1).release()


def test_k_below_one_is_rejected():
    with pytest.raises(ValueError):
        SlotAccountant(k=0)


# --------------------------------------------------------------------------- #
# (c) PynvmlVramGauge — lazy import (GPU-free host import 무해, D-A/R-NV)
# --------------------------------------------------------------------------- #


def test_pynvml_import_is_lazy_and_failure_is_loud(monkeypatch):
    # Block `import pynvml` entirely: construction must still succeed (proof
    # the import is lazy — GPU-free hosts import/construct fine), and only the
    # actual gauge CALL surfaces the failure loudly.
    monkeypatch.setitem(sys.modules, "pynvml", None)
    gauge = PynvmlVramGauge()  # must not raise
    with pytest.raises(ImportError):
        gauge.available_vram_mb()
