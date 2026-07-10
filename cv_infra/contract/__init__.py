"""Contract & schema package (M1): single definition of the verification contract.

Public surface (consumers import from here or the submodules — never redefine):

* ``API_VERSION`` (apiversion.py) + ``resolve_api_version`` (version.py)
* pydantic v2 models (schema.py / adapter_schema.py) — Phase 3 canonical
  (the Phase-2 stdlib models.py / adapter package retired, D-4')
* ``load_request`` 6-stage loader + ``AdmittedRequest`` (loader.py)
* ``ContractError`` friendly error object (errors.py)

Third-party-backed symbols are exported LAZILY (PEP 562): the runner image
installs the wheel ``--no-deps`` — ``import cv_infra.contract`` alone stays
stdlib-only, and host-only control-plane deps (yaml/docker) must never ride
this package import. The runner pulls schema.py explicitly and executes it on
the BUNDLE-SUPPLIED pydantic (D-4', skew asserted at image build).
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
