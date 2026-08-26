"""Batch job wire contract (M1 — p6 설계 정본 §0-6/§2).

The p6 execution granularity is "1 container = n samples of ONE request"
(CEO decision 2026-08-26): the sample stays the logical job (``Job(request_id,
repeat_index)``, one ``JobResult`` each — M3/M4 unchanged), and the container
becomes a CARRIER for n of them. This module is the whole wire between the
carrier's producer (M3 supervisor) and its consumer (M2 batch runner).

Shape = a WRAPPER document, not an array overload of ``JOB_SPEC``: a bare array
would make "one spec" and "n specs" the same env var with two shapes, and the
carrier needs a place for carrier-level identity (``request_id``) that no
single spec owns. The existing single-job seam (``JOB_SPEC`` +
``runner.main.resolve_job_spec_dict``) is UNTOUCHED — a runner image that
predates batching fails loudly on the new command instead of half-running it.

WIRE INVARIANT (the one thing both sides must agree on):

    specs[i]  <->  results/<i>/  <->  repeat_index i

i.e. the i-th spec's outputs land under the i-th results directory, and that i
IS the ``repeat_index`` of the logical job M3 folds the result back into. The
``results/<i>`` path is rendered by the runner (``runner.batch``), which owns
the out-dir layout; the INDEX AGREEMENT is owned here.

``batch_summary.json`` is the carrier's own report: it is flushed atomically
after every iteration, so "did sample i run?" is answerable even when the
carrier dies mid-batch (item present = it ran; item AND result absent = it did
not, and M3 charges that slot an infra error — P5-13).
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, ConfigDict, Field

#: Env var naming the batch wrapper FILE (a path, never inline JSON — same
#: convention as the single-job ``JOB_SPEC``: n specs do not fit an env value,
#: and a file is what a read-only mount can carry).
JOB_SPEC_BATCH_ENV = "JOB_SPEC_BATCH"

#: Carrier-level summary, written at the ROOT of the batch out-dir (sibling of
#: ``results/``), flushed after every iteration.
BATCH_SUMMARY_FILENAME = "batch_summary.json"

#: ``batch_summary.json``'s ``schema`` value — the reader's version handle
#: (same 3-state idea as ``apiVersion``: a reader that does not know this string
#: must say so instead of guessing at the keys).
BATCH_SUMMARY_SCHEMA = "cv-batch-summary/1"


class JobSpecBatch(BaseModel):
    """The ``JOB_SPEC_BATCH`` document: n canonical JOB_SPECs + carrier identity.

    ``specs`` are the JOB_SPEC dicts EXACTLY as the single-job seam defines them
    (``orchestrator/api._job_spec_for`` / ``cli/main._job_spec_from_request``) —
    kept as opaque mappings here on purpose: this contract is about the CARRIER,
    and re-declaring the spec shape would fork the frozen seam into two
    definitions (blueprint §8). The runner re-validates each one through
    ``VerificationRequest`` as it always has.

    ``min_length=1``: an empty batch is a producer bug, and "boot Isaac to do
    nothing" is the most expensive way to discover it (pre-boot reject, exit 2).

    ``request_id`` is the carrier's identity (all n samples belong to ONE
    request — that is what makes them shareable in one container). Optional so a
    hand-written batch can be run without inventing one.
    """

    model_config = ConfigDict(extra="forbid")

    specs: list[dict[str, Any]] = Field(min_length=1)
    request_id: str | None = None
