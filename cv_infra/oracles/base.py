"""Oracle plugin interface / base (M1) — Phase 0 stub.

"Acceptance criteria are also input": the criteria-named oracle is loaded as a
plugin and bound to an evaluatable state (REQ-INTAKE-007/008). M1 owns the
interface + base + loader; the concrete oracles (reached_goal / no_collision)
are owned by the evaluation engine (M2).

The entry-point / explicit-path loader and the friendly load-failure rejection
(exit 2) are built in Phase 3 (§3.6); Phase 0 ships the abstract interface stub
only. The interface is kept open to a future user-supplied checker (post-MVP).
"""

from abc import ABC, abstractmethod


class OracleBase(ABC):
    """Abstract acceptance-criteria oracle (stub).

    ``name``/``version`` identify the plugin; ``validate_params`` checks criteria
    at contract time; ``evaluate`` produces verdict + metrics at evaluation time
    (concrete impl = M2). Signatures are finalized in Phase 3.
    """

    #: Oracle plugin name (set by subclass).
    name: str
    #: Oracle plugin version (set by subclass).
    version: str

    @abstractmethod
    def validate_params(self, criteria: object) -> None:
        """Contract-time check of acceptance criteria — formalized in Phase 3."""
        ...

    @abstractmethod
    def evaluate(self, telemetry: object, criteria: object) -> object:
        """Evaluation-time verdict + metrics (impl = M2) — formalized in Phase 3."""
        ...
