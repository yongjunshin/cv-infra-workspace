"""Canonical JOB_SPEC assembly (M1) — the SINGLE producer of the M3->M2 wire.

The admitted request document is what a user writes; the JOB_SPEC is what one
runner container is handed. Both submission planes materialize the second from
the first — M8 ``cv-infra run`` (envelope-less, one job) and the M3 REST submit
fan-out — and until p8c1 each carried its OWN copy of the assembly, kept equal
only by a parity guard (the G-25 anchor comment + the rest-glue test). Two
copies of a wire shape is exactly the drift G-17 describes, so the shape now
lives HERE, in the layer both planes already depend on: the contract owns the
wire, the planes call it. (The duplication existed because M3 must not import
the M8 CLI plane — layer direction: M8 wraps M3. Moving the definition DOWN,
not sideways, keeps that direction intact; ``.importlinter`` still forbids the
contract any sibling.)

STDLIB-ONLY and duck-typed on purpose: the request is consumed through
``model_dump`` alone (no pydantic import — same form the two originals already
had), so this module rides the runner wheel's ``--no-deps`` install like the
rest of the package. It is deliberately NOT eager-exported from
``cv_infra.contract.__init__`` — consumers import the submodule directly.
"""

from __future__ import annotations

from typing import Any


def build_job_spec(
    request: Any, job_id: str, *, locomotion_policy_path: str | None = None
) -> dict[str, Any]:
    """Admitted M1 ``schema.VerificationRequest`` -> canonical JOB_SPEC dict.

    The wire shape is the frozen Phase-2 M3->M2 seam (supervisor JOB_SPEC file
    -> runner ``resolve_job_spec_dict``): top-level key set ``{job_id, scenario,
    sut_image_ref, interface, acceptance_criteria}`` with ``sut.image_ref``
    flattened (REQ-INTAKE-006), plus ``execution_settings`` when it carries a
    runner-actionable knob (below). ``apiVersion`` (resolved at admit) and
    ``sut.image_id`` stay OFF the wire — no execution-plane consumer exists.

    ``exclude_none=True`` keeps "None = downstream default applies" fields
    ABSENT exactly as the raw-YAML pass-through did: a present-but-``null``
    known-key param (e.g. ``goal_orientation_wxyz``) would defeat the oracle's
    ``read_field(name, default)`` fallback. Free-form dict values (custom
    criterion params) are NOT filtered by pydantic's ``exclude_none`` —
    explicit nulls a user wrote survive verbatim (measured 2026-07-10).
    ``scenario.debug_obstacle`` (D-2') rides the wire only when declared.

    ``execution_settings`` (decision 2026-08-04 D-8): the knobs the RUNNER can
    act on ride the wire, ``repeats`` does NOT — it is M3's own fan-out axis and
    each fanned-out job IS one repeat (the CLI path runs exactly one), so
    shipping it would tell the runner something false about the job it holds
    (one home per field). ``min_pass_ratio`` is excluded for the same reason
    (p6 §0-14): it judges the REQUEST's samples as a set, which is not a fact
    about the one sample the runner holds. The subtree is dumped mechanically
    (``exclude={"repeats", "min_pass_ratio"}`` + ``exclude_none``) so a future
    M1 knob rides without an allowlist to drift (G-25), and it is OMITTED when
    nothing survives that filter — an undeclared ``fixed_dt`` leaves the frozen
    key set byte-identical. Nesting (not flattening like ``sut_image_ref``) is
    what the seam actually admits: the runner re-validates the spec as a whole
    ``VerificationRequest`` (``runner/main.py::parse_request``, ``extra=forbid``)
    — measured 2026-08-05, a top-level ``fixed_dt`` is rejected with exit 2.
    Consuming the value is M2's; this producer only stops swallowing it.

    ``locomotion_policy_path`` (D2 2026-08-31, wired 2026-09-01) is the 2nd SUT
    artifact's RESOLVED absolute path — the value stage 5 computed at the only
    place that knows the ride-along anchor (``loader.AdmittedRequest``), passed
    in because this function is handed a request MODEL, which carries only the
    relative ``file`` the user wrote. Both halves come from that one admission,
    so the pair rides only when BOTH are present: the declaration
    (``sut.locomotion_policy``, which supplies the digest — the document is the
    single definition of what is pinned) and the resolved path. A caller with no
    path emits nothing and the runner then says so loudly rather than running a
    declared artifact silently (``runner/go2_wiring.check_firmware_slot``);
    an undeclared policy leaves the wire byte-identical (the carter plane).
    Flat keys, not a nested ``sut`` block, for the same reason ``sut_image_ref``
    is flat, and named verbatim after the consumer's constants
    (``go2_wiring.POLICY_PATH_KEY`` / ``POLICY_SHA_KEY`` — pinned equal in
    ``tests/test_contract_locomotion_policy.py``; the contract may not import a
    sibling, so the two literals are tied by a test instead, G-25).

    Callers (both are thin aliases of this function, no second assembly):
    ``cli/main._job_spec_from_request`` (M8 ``cv-infra run``) and
    ``orchestrator/api._job_spec_for`` (M3 REST submit) — both forward their
    ``AdmittedRequest.locomotion_policy_path``, which is what makes the two
    planes emit the same spec for the same document. That they resolve to
    THIS definition is pinned structurally by
    ``tests/test_contract_job_spec.py``; the behavioural parity guard over both
    handles stays in ``tests/test_orchestrator_rest_glue.py``.
    """
    spec = {
        "job_id": job_id,
        "scenario": request.scenario.model_dump(exclude_none=True),
        "sut_image_ref": request.sut.image_ref,  # flattened canonical field (REQ-INTAKE-006)
        "interface": request.interface.model_dump(exclude_none=True),
        "acceptance_criteria": [
            criterion.model_dump(exclude_none=True) for criterion in request.acceptance_criteria
        ],
    }
    policy = request.sut.locomotion_policy
    if policy is not None and locomotion_policy_path:
        spec["locomotion_policy_path"] = locomotion_policy_path
        spec["locomotion_policy_sha256"] = policy.sha256
    runner_knobs = request.execution_settings.model_dump(
        exclude_none=True, exclude={"repeats", "min_pass_ratio"}
    )
    if runner_knobs:
        spec["execution_settings"] = runner_knobs
    return spec
