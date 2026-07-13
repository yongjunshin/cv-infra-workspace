"""Oracle plugin interface / base / loader (M1 §3.6 — REQ-INTAKE-007/008).

"Acceptance criteria are also input": the criteria-named oracle is loaded as a
plugin and bound to an evaluatable state. M1 owns the interface + base +
loader; the concrete oracles (reached_goal / no_collision) are owned by the
evaluation engine (M2) and register through the ``cv_infra.oracles``
entry-point group (pyproject). Custom oracles either register in that group
from their own distribution or are addressed by an explicit ``module:Class``
path — both via stdlib ``importlib`` machinery (M1 §2 reuse rule: no custom
import machine).

Load failure (unknown name / import error / not an ``OracleBase``) raises a
friendly ``ContractError`` — a rejection-eligible object (exit 2 is the
consumer's mapping, LOCKED §7-9) — so a bad criteria reference is stopped at
contract time, before the execution plane (NFR-INTAKE-003). Loading proves
BINDABILITY only; evaluation stays M2's.

Import-time this module stays stdlib-only (abc / importlib +
``cv_infra.contract.errors``, itself stdlib-only), so the runner image (wheel
installed ``--no-deps``) keeps importing the concrete oracles unchanged.
"""

from __future__ import annotations

import importlib
from abc import ABC, abstractmethod
from importlib import metadata

from cv_infra.contract.errors import ContractError

#: Entry-point group the loader discovers oracle plugins from (pyproject
#: ``[project.entry-points."cv_infra.oracles"]``).
ENTRY_POINT_GROUP = "cv_infra.oracles"

_EXPECTED = (
    "the name of an oracle registered in the "
    f"'{ENTRY_POINT_GROUP}' entry-point group, or an explicit "
    "'package.module:ClassName' path to an OracleBase subclass"
)
_EXAMPLE = "oracle: reached_goal"
_DOC_LINK = "M1-contract-and-schema.md §3.6 (oracle plugins)"


class OracleBase(ABC):
    """Abstract acceptance-criteria oracle (the plugin contract).

    ``name``/``version`` identify the plugin; ``validate_params`` is the
    runner-plane pre-boot check of the (merged) criteria view — the runner
    calls it on every composed oracle right after ``build_oracles``, before
    sim boot, and a raise rejects the job with contract-error semantics
    (exit 2; D-1 2026-07-13); ``evaluate`` produces the verdict + metrics at
    evaluation time (concrete impl = M2 engine). The interface is kept open
    to a future user-supplied checker (post-MVP).
    """

    #: Oracle plugin name (set by subclass).
    name: str
    #: Oracle plugin version (set by subclass).
    version: str

    @abstractmethod
    def validate_params(self, criteria: object) -> None:
        """Runner-plane pre-boot check of the merged criteria view.

        Raise (any exception type) on invalid params: the runner calls this
        for every composed oracle right after ``build_oracles`` and maps a
        raise onto the contract-error rejection (exit 2) BEFORE sim boot, so
        bad params never spend GPU time (D-1 2026-07-13, NFR-INTAKE-003).
        """
        ...

    @abstractmethod
    def evaluate(self, telemetry: object, criteria: object) -> object:
        """Evaluation-time verdict + metrics (impl = M2 engine)."""
        ...


def _reject(name: str, reason: str) -> ContractError:
    return ContractError(
        expected=f"{_EXPECTED} ({reason})",
        got=repr(name),
        example=_EXAMPLE,
        doc_link=_DOC_LINK,
    )


def _load_explicit_path(name: str) -> type:
    module_name, _, attr = name.partition(":")
    try:
        module = importlib.import_module(module_name)
    except ImportError as exc:
        raise _reject(name, f"module {module_name!r} could not be imported: {exc}") from exc
    try:
        return getattr(module, attr)
    except AttributeError as exc:
        raise _reject(name, f"module {module_name!r} has no attribute {attr!r}") from exc


def _load_entry_point(name: str) -> type:
    matches = metadata.entry_points(group=ENTRY_POINT_GROUP, name=name)
    if not matches:
        registered = sorted(ep.name for ep in metadata.entry_points(group=ENTRY_POINT_GROUP))
        raise _reject(name, f"no such entry point; registered oracles: {registered}")
    try:
        return next(iter(matches)).load()
    except Exception as exc:  # import/attr errors inside the plugin
        raise _reject(name, f"entry point failed to load: {exc}") from exc


def load_oracle(name: str) -> OracleBase:
    """Load + bind the oracle plugin ``name`` (REQ-INTAKE-007).

    ``name`` with a ``:`` is an explicit ``module:Class`` path; anything else
    is looked up in the ``cv_infra.oracles`` entry-point group. Returns a
    ready (bound) oracle INSTANCE; any failure raises a friendly
    ``ContractError`` so the request is rejected pre-execution
    (REQ-INTAKE-008, NFR-INTAKE-003).
    """
    loaded = _load_explicit_path(name) if ":" in name else _load_entry_point(name)
    if not (isinstance(loaded, type) and issubclass(loaded, OracleBase)):
        raise _reject(name, f"loaded object {loaded!r} is not an OracleBase subclass")
    try:
        return loaded()
    except TypeError as exc:  # abstract / non-nullary constructor
        raise _reject(name, f"could not instantiate: {exc}") from exc
