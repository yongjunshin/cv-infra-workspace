"""Contract data models (M1) — Phase 0 skeleton.

Envelope / VerificationRequest / VerificationResult placeholders. The formal
pydantic v2 models (fields, validators, JSON-schema export) are finalized in
Phase 3 (modules/M1-contract-and-schema.md §3.2). Phase 0 ships import-able
skeletons only: stdlib only, NO pydantic at this phase. These are the single
definition of the contract models — consumers (M2/M3/M4) import, never redefine.
"""


class RequestEnvelope:
    """N>=1 VerificationRequest container (REQ-INTAKE-001).

    Phase-3 fields (placeholder): apiVersion, trigger_source (human-manual|ci-cd,
    REQ-INTAKE-003), is_self_test/origin (M7 marker), requests (list).
    """

    # Formalized as a pydantic v2 model in Phase 3.
    ...


class VerificationRequest:
    """Self-contained verification instance (REQ-INTAKE-002/006).

    Phase-3 fields (placeholder): sut_image_ref, scenario, acceptance_criteria,
    interface (adapter), execution_settings.
    """

    # Formalized as a pydantic v2 model in Phase 3.
    ...


class VerificationResult:
    """Exactly one result per job (REQ-EXEC-013); spec §3.2 names it "Result".

    Phase-3 fields (placeholder): verdict (pass|fail), declared metrics map
    (time-to-goal / min-clearance / collision_count / path_len, REQ-EXEC-012),
    artifact refs, request_identity_key (field only; key derivation = M4).
    """

    # Formalized as a pydantic v2 model in Phase 3.
    ...
