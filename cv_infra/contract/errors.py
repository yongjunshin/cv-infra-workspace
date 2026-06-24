"""Friendly contract-error object (M1) — Phase 0 shape stub.

Carries the structured, self-correcting validation error surfaced on schema
violations (REQ-INTAKE-005, NFR-INTAKE-001): field path + expected + got +
fixable example, plus machine-readable source location for M8 GitHub-Actions
inline annotations (M1 object fields <-> M8 file/line/col, 1:1).

Principle: a raw pydantic/Python traceback is NEVER leaked to stderr —
violations are rendered as this object and rejected with exit 2, not as a
stack trace (DoD-P3-02; exit-code contract LOCKED §7-9).

The ``ValidationError.errors()`` -> ContractError post-processing and the
CI-annotation serializer are built in Phase 3 (§3.4); Phase 0 ships the
attribute-slot stub only.
"""


class ContractError(Exception):
    """Structured, friendly contract/validation error (shape stub).

    Phase-3 attribute slots (placeholder):
      field_path   # dotted/indexed path, e.g. requests[0].acceptance_criteria.timeout_s
      expected     # expected value/type, e.g. "positive number (seconds)"
      got          # actual input, e.g. "-5"
      example      # fixable example, e.g. "timeout_s: 120"
      doc_link     # contract-doc anchor
      source_path  # consumer-repo-root-relative path (GitHub annotation file)
      source_line  # CI annotation line (if available)
      source_col   # CI annotation col (if available)
    """

    # Field-path + expected + example formatting (and the CI-annotation
    # serializer) are formalized in Phase 3. No raw traceback is ever surfaced.
    ...
