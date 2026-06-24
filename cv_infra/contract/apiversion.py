"""apiVersion constant for the cv-infra standard contract (M1).

Cross-team contract: ``from cv_infra.contract.apiversion import API_VERSION``
yields the current contract version string (k8s-style ``<group>/<version>``).
The 3-state compatibility resolver + deprecation window (``version.py``) land in
Phase 3 (modules/M1-contract-and-schema.md §3.1); Phase 0 ships the constant only.
"""

# Current contract apiVersion (NFR-INTAKE-002). Bump/deprecation policy = Phase 3.
API_VERSION = "cv-infra/v1"
