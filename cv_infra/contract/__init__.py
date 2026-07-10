"""Contract & schema package (M1): single definition of the verification contract.

Public surface (consumers import from here or the submodules — never redefine):

* ``API_VERSION`` (apiversion.py) + ``resolve_api_version`` (version.py)
* pydantic v2 models (schema.py / adapter_schema.py) — Phase 3 canonical
* ``load_request`` 6-stage loader + ``AdmittedRequest`` (loader.py)
* ``ContractError`` friendly error object (errors.py)
* Phase-2 stdlib models (models.py) — DEPRECATED, migrate in P3 cycle-2

Third-party-backed symbols are exported LAZILY (PEP 562): the runner image
installs the wheel ``--no-deps`` (no pydantic/yaml) and must keep importing
``cv_infra.contract.models`` — which triggers this package __init__ — without
pulling the host-only control-plane deps (D-C/R20, DoD-P2-12 direction).
"""

from cv_infra.contract.apiversion import API_VERSION  # stdlib-safe, eager
from cv_infra.contract.errors import ContractError  # stdlib-safe, eager

_LAZY = {
    # schema.py (pydantic)
    "RequestEnvelope": "cv_infra.contract.schema",
    "VerificationRequest": "cv_infra.contract.schema",
    "Result": "cv_infra.contract.schema",
    "ExecutionSettings": "cv_infra.contract.schema",
    "ResourceBudget": "cv_infra.contract.schema",
    # adapter_schema.py (pydantic)
    "Interface": "cv_infra.contract.adapter_schema",
    "Ros2AdapterConfig": "cv_infra.contract.adapter_schema",
    # version.py / loader.py
    "resolve_api_version": "cv_infra.contract.version",
    "VersionResolution": "cv_infra.contract.version",
    "load_request": "cv_infra.contract.loader",
    "AdmittedRequest": "cv_infra.contract.loader",
}

__all__ = ["API_VERSION", "ContractError", *sorted(_LAZY)]


def __getattr__(name: str):
    if name in _LAZY:
        import importlib

        return getattr(importlib.import_module(_LAZY[name]), name)
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
