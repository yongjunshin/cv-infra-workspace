"""Abstract SUT adapter interface (M2, NFR-EXEC-003).

The verification / orchestration plane is target-agnostic; only the sim<->SUT
interface swaps behind this adapter (NFR-EXEC-003). Four methods, per M2 §2.2:
``wire`` / ``await_ready`` / ``drive_mission`` / ``teardown``. A concrete adapter
NEVER spawns the SUT and holds NO docker.sock — M3 co-spawns SUT+runner onto a
shared per-job bridge network + ROS_DOMAIN_ID (D-2/D-D); the adapter only joins
that transport at the DDS level and drives/measures the SUT as a blackbox
(REQ-EXEC-005: SUT internals unmodified).

This is the M2 runner-plane adapter base and is distinct from ``cv_infra.adapter``
(M1's contract-side ``SUTAdapter`` interface + wire schema).
"""

from __future__ import annotations

from abc import ABC, abstractmethod


class SimAdapter(ABC):
    """Target-agnostic adapter: drive + measure a blackbox SUT (no spawn, no sock)."""

    #: Adapter discriminator matching request ``interface.type`` (e.g. "ros2").
    interface_type: str

    @abstractmethod
    def wire(self, simulation_app: object, adapter_config: object) -> None:
        """Join the SUT transport + remap topics/QoS/goal interface (no SUT edits)."""
        ...

    @abstractmethod
    def await_ready(self, timeout_s: float) -> bool:
        """Readiness barrier — block until declared conditions hold (REQ-EXEC-007).

        Returns True only once ready; the mission/timeout (sim-time) clock must not
        start before this, or a not-ready send causes a false FAIL.
        """
        ...

    @abstractmethod
    def drive_mission(self, goal: object, *, timeout_s: float | None = None) -> object:
        """Send the mission goal and monitor completion/failure/timeout.

        ``timeout_s`` is the scenario's SIM-time budget (D-F — measured on the
        /clock domain, never wall-clock; the wall-clock runaway watchdog is
        M3's). Returns an adapter-specific terminal outcome for logging; the
        verdict itself always comes from the oracles over telemetry.
        """
        ...

    @abstractmethod
    def teardown(self) -> None:
        """Leave the transport and release adapter resources."""
        ...
