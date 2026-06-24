"""SUT adapter interface (M1) — Phase 0 stub.

Defines the blackbox-SUT wiring boundary: a request's ``interface.type`` selects
an adapter and its ``adapter_config`` sub-schema (REQ-EXEC-004/005/006). The
adapter drives + measures the SUT as a blackbox and, by contract, carries NO
field that modifies the SUT container internals (REQ-EXEC-005).

The concrete ros2 adapter implementation lives in the execution plane (M2);
M1 owns the interface + schema only. Methods are finalized in Phase 3.
"""

from abc import ABC, abstractmethod


class SUTAdapter(ABC):
    """Blackbox SUT adapter interface (stub).

    ``interface.type`` (currently ``ros2``) selects the adapter; ``adapter_config``
    (see adapter_schema.py) configures the wiring. Adapter = drive + measure only.
    """

    #: Adapter type discriminator, e.g. "ros2" (interface.type). Set by subclass.
    interface_type: str

    @abstractmethod
    def connect(self) -> None:
        """Join the SUT transport (e.g. DDS domain) — formalized in Phase 3."""
        ...
