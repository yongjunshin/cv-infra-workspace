"""Sample derivation from a randomized request (M1 — p6 설계 정본 §0-4/§2).

ONE submitted document + a sample index -> the CONCRETE request that sample
runs. Pure: stdlib + ``contract.schema`` only (no I/O, no clock, no module-level
RNG), so it is CPU-testable and stays inside the foundational import layer
(``.importlinter``: ``cv_infra.contract`` imports no sibling package).

Three properties every consumer depends on:

* PER-SAMPLE INDEPENDENT SEEDING — sample ``i`` draws from ``MT(sha256(
  "cv-derive/1:<seed>:<i>"))``, NOT from one stream advanced ``i`` times, so
  materializing sample 3 never requires materializing 0..2 first. The REST
  fan-out materializes each job in a loop and the CLI materializes sample 0
  alone; both must emit the same bytes for the same (document, index).
* STATIC IN, SAME OBJECT OUT — a document that declares no distribution is
  returned UNCHANGED (``materialize_request(req, i) is req``). Every pre-p6
  request therefore keeps its exact JOB_SPEC bytes and its
  ``request_identity_key``: the p6 path is provably not on it.
* FIXED DRAW ORDER — ``RANDOM_FIELDS`` below. A randomizable field added later
  APPENDS to that tuple (never inserts), or every existing seed silently starts
  producing different samples.

WHAT IS NOT DERIVED: ``scenario.seed`` rides the sample UNCHANGED (it is the
sim's determinism seed — the same one every sample of a request uses, exactly
as pre-p6 repeats did) and the ``request_identity_key`` is derived from the
SUBMITTED document, never from a sample: "which request is this" must not move
per sample — the distribution + seed ARE the request (report/regression.py).
"""

from __future__ import annotations

import hashlib
import random

from cv_infra.contract.schema import Choice, Scenario, Uniform, VerificationRequest

#: Derivation algorithm version — stamped onto every materialized sample
#: (``Scenario.derivation.version``). Bump ONLY together with a change that
#: moves draws (seeding, draw order, rounding): a stored sample must be able to
#: say which rule produced it.
DERIVE_VERSION = "cv-derive/1"

#: Uniform draws are rounded to this many decimals. Why round at all: the raw
#: double rides the JOB_SPEC and the result document as text, and 4 decimals is
#: 0.1 mm on the metre axes / 0.006 deg on yaw — below any physical meaning the
#: sim can honour, while keeping the wire readable and diffable by a human
#: reading two samples side by side. ``choice`` values are NEVER rounded: those
#: are the consumer's own literals (verbatim, §2).
UNIFORM_DECIMALS = 4

#: The randomizable fields, in FIXED DRAW ORDER — ``(scenario block, field)``.
#: This is the single definition of "which fields may carry a distribution";
#: ``tests/test_contract_derive.py`` binds it BOTH ways to the schema (every
#: entry is a ``RandomizableFloat`` field, and every ``RandomizableFloat`` field
#: in the schema is an entry), so a field added to one side and not the other
#: fails loudly instead of never being drawn (G-25).
RANDOM_FIELDS: tuple[tuple[str, str], ...] = (
    ("initial_pose", "x"),
    ("initial_pose", "y"),
    ("initial_pose", "yaw"),
    ("goal", "x"),
    ("goal", "y"),
    ("goal", "yaw"),
    ("debug_obstacle", "x"),
    ("debug_obstacle", "y"),
)


def sample_rng(seed: int, index: int) -> random.Random:
    """The RNG for sample ``index`` of a request seeded ``seed``.

    ``random.Random`` (Mersenne Twister) seeded from the first 64 bits of
    ``sha256("<DERIVE_VERSION>:<seed>:<index>")``. The hash — rather than e.g.
    ``seed + index`` — is what makes neighbouring (seed, index) pairs land in
    unrelated states, so sample 0 of seed 42 and sample 1 of seed 41 share
    nothing. stdlib only: reproducible across hosts and interpreter runs
    (MT is a specified algorithm, not a platform detail).
    """
    digest = hashlib.sha256(f"{DERIVE_VERSION}:{seed}:{index}".encode()).hexdigest()
    return random.Random(int(digest[:16], 16))


def distribution_fields(scenario: Scenario) -> tuple[str, ...]:
    """Wire paths of the scenario fields that still carry a DISTRIBUTION.

    Returned paths are ``scenario.<block>.<field>`` — the path in BOTH wires a
    consumer of this function reads (the request document and the JOB_SPEC),
    so the string can go straight into a rejection message.

    Two callers: ``materialize_request`` (nothing to derive -> hand the request
    back untouched) and the runner's ``parse_request`` leak check (§0-5): the
    union extension means ``extra="forbid"`` no longer stops a distribution
    from reaching the execution plane, so the runner rejects one explicitly —
    an unmaterialized ``{uniform: [...]}`` is a platform bug, not a pose.
    """
    return tuple(
        f"scenario.{block_name}.{field_name}"
        for block_name, field_name in RANDOM_FIELDS
        if (block := getattr(scenario, block_name, None)) is not None
        and isinstance(getattr(block, field_name), (Uniform, Choice))
    )


def materialize_request(request: VerificationRequest, index: int) -> VerificationRequest:
    """The submitted request -> the CONCRETE request for sample ``index``.

    Draws ``RANDOM_FIELDS`` in order from ``sample_rng(scenario.seed, index)``,
    consuming a draw ONLY for a field that actually declares a distribution (a
    static field next to a random one must not shift the stream), and stamps
    ``scenario.derivation = {version, index}`` so the sample carries its own
    provenance. Absent blocks (no ``initial_pose`` / no ``debug_obstacle``)
    consume nothing.

    Returns the ORIGINAL object when the document declares no distribution —
    identity (``is``), not equality: the static/repeats=1/self-test paths keep
    byte-identical JOB_SPECs and cannot be broken by a change in here.
    """
    scenario = request.scenario
    if not distribution_fields(scenario):
        return request

    rng = sample_rng(scenario.seed, index)
    materialized = scenario.model_dump()
    for block_name, field_name in RANDOM_FIELDS:
        block = getattr(scenario, block_name, None)
        if block is None:
            continue
        value = getattr(block, field_name)
        if isinstance(value, Uniform):
            low, high = value.uniform
            materialized[block_name][field_name] = round(rng.uniform(low, high), UNIFORM_DECIMALS)
        elif isinstance(value, Choice):
            materialized[block_name][field_name] = rng.choice(value.choice)
    materialized["derivation"] = {"version": DERIVE_VERSION, "index": index}
    # Re-validated (not assigned) so the sample is a genuinely valid document:
    # the drawn values pass every Scenario constraint the submitted one did.
    # ``deep=True`` makes each sample an independent document — n samples of one
    # request never share a mutable sub-model.
    return request.model_copy(update={"scenario": Scenario.model_validate(materialized)}, deep=True)
