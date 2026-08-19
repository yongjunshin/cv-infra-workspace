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
    FIFO_POLICY,
    MIB_PER_GIB,
    PynvmlVramGauge,
    SlotAccountant,
    budget_vram_per_instance_mb,
    compute_k,
    to_resource_budget,
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


def test_insufficient_vram_is_a_loud_config_error():
    # p4c1 follow-up ① (PM 룰링, cycle-plan 2026-07-13): the VRAM guard leaving
    # no capacity is an operator misconfiguration surfaced LOUDLY — a silent 0
    # would either crash SlotAccountant later or park admission forever.
    with pytest.raises(ValueError, match="computed k = 0"):
        compute_k(4, vram_gauge=FakeGauge(1000), vram_per_instance_mb=4096)


def test_vram_guard_k_of_exactly_one_is_still_valid():
    # Boundary: one instance fits — no error, k floors to 1 (not over-loud).
    assert compute_k(4, vram_gauge=FakeGauge(4096), vram_per_instance_mb=4096) == 1


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
# (a2) Resource Budget seam (REQ-DEPLOY-012, DoD-P5-11) — the operator knobs
# reflected into the M1 ``ResourceBudget`` and back into compute_k's MiB unit.
# The rule/signature of compute_k is untouched (LOCKED §7.4); only the SOURCE of
# its inputs moved.
# --------------------------------------------------------------------------- #

#: MEASURED per-instance VRAM of the deployment this seam was wired on (p5c17 boot
#: log, ``CV_VRAM_PER_INSTANCE_MB=3785``). Used instead of a round number because
#: 3785 MiB has fraction bits in GiB (3.6962890625) — a round example would hide a
#: conversion mistake (CLAUDE §2-4: the value is measured, never invented here).
MEASURED_VRAM_PER_INSTANCE_MB = 3785.0

#: MiB values whose DECIMAL (1000) round trip is INEXACT in IEEE-754 binary64.
#: DERIVED by that very property over a realistic MiB range — a hand list would go
#: stale silently; the non-empty assert below keeps the counter-example honest.
_DECIMAL_LOSSY_MB = tuple(float(mb) for mb in range(1, 40001) if mb / 1000 * 1000 != mb)[:3]


def test_operator_knobs_reflect_into_the_contract_budget():
    budget = to_resource_budget(2, vram_per_instance_mb=MEASURED_VRAM_PER_INSTANCE_MB)
    assert budget.max_concurrent == 2  # authoritative cap, verbatim
    assert budget.scheduling_policy == FIFO_POLICY  # the one policy that exists (queue.py FIFO)
    assert budget.vram_per_instance_gb == MEASURED_VRAM_PER_INSTANCE_MB / MIB_PER_GIB


@pytest.mark.parametrize(
    "mb",
    [MEASURED_VRAM_PER_INSTANCE_MB, 8000.0, 6000.0, 12288.0, 1.0, *_DECIMAL_LOSSY_MB],
)
def test_mib_gib_round_trip_is_exact(mb):
    """mb -> budget -> mb is BIT-identical: the reflection cannot perturb k.

    Exactness is why ``MIB_PER_GIB`` is the binary 1024 and not a decimal 1000 —
    a power-of-two divisor only shifts the exponent in IEEE-754 binary64.
    """
    budget = to_resource_budget(2, vram_per_instance_mb=mb)
    assert budget_vram_per_instance_mb(budget) == mb  # ==, not approx


def test_a_decimal_divisor_would_not_round_trip():
    """양성 대조: the constant choice is load-bearing, not decoration.

    If ``MIB_PER_GIB`` were the decimal 1000, these same operator values would
    come back CHANGED — the guard above would then be measuring luck.
    """
    assert MIB_PER_GIB == 1024
    assert _DECIMAL_LOSSY_MB, "counter-example 집합이 비었다 — 대조가 공허하다"
    for mb in _DECIMAL_LOSSY_MB:
        assert mb / 1000 * 1000 != mb  # the divisor we did NOT pick loses bits
        assert mb / MIB_PER_GIB * MIB_PER_GIB == mb  # the one we did picks none


def test_k_from_the_budget_equals_k_from_the_raw_operator_scalars():
    """Wiring the budget in front of ``compute_k`` changes WHERE the inputs come
    from, never WHAT k is (LOCKED §7.4 rule untouched)."""
    gauge = FakeGauge(16376.0)  # this host's free VRAM order of magnitude
    budget = to_resource_budget(8, vram_per_instance_mb=MEASURED_VRAM_PER_INSTANCE_MB)
    via_budget = compute_k(
        budget.max_concurrent,
        vram_gauge=gauge,
        vram_per_instance_mb=budget_vram_per_instance_mb(budget),
    )
    via_scalars = compute_k(8, vram_gauge=gauge, vram_per_instance_mb=MEASURED_VRAM_PER_INSTANCE_MB)
    assert via_budget == via_scalars == 4  # floor(16376/3785) = 4 < cap 8


@pytest.mark.parametrize(
    "kwargs",
    [
        {"max_concurrent": 0, "vram_per_instance_mb": 4096.0},  # contract: cap >= 1
        {"max_concurrent": 2, "vram_per_instance_mb": 0.0},  # contract: vram > 0
        {"max_concurrent": 2, "vram_per_instance_mb": -4096.0},
        {"max_concurrent": 2, "vram_per_instance_mb": 4096.0, "scheduling_policy": ""},
    ],
)
def test_a_misconfigured_budget_is_refused_at_reflection_time(kwargs):
    """양성 대조: a wrong budget is caught EARLIER than before (boot, by the
    contract bounds) instead of surviving as a scalar until admission. pydantic's
    ValidationError IS a ValueError, so the existing loud-boot contract holds."""
    max_concurrent = kwargs.pop("max_concurrent")
    with pytest.raises(ValueError):
        to_resource_budget(max_concurrent, **kwargs)


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
