"""Asset closure + cache key (M1) — what a request can open, named once.

The platform cannot know what a USD contains, but it CAN know which ones a
request is able to reach, because the request declares every one of them. That
list is the unit the warm cache is keyed on, and it is the whole mechanism
behind "the first run for an unseen world is slow and every run after it is not"
(measured on this repo: several minutes cold for the asset download, ~25 s warm).

Two properties make the key worth having, and both come from the covering array
rather than from any one case:

* the closure is the UNION over the WHOLE suite. Warming per case would have
  case 1 pull the person asset cold and case 2 pull the chair asset cold; the
  array is enumerated at admit, so the union is knowable before the first runner
  starts.
* the key is content-addressed and order-free, so two requests that reach the
  same assets share one warmed tier no matter how their documents are laid out,
  and a re-verification of the same document is a cache HIT by construction —
  which is the whole point of keeping the tier around.

The Isaac version is part of the key because a warmed tier holds GPU-derived
caches (shader/compute) that a different runtime cannot reuse; leaving it out
would serve a stale tier to a new engine and the failure would look like a
rendering bug.

Stdlib only — the foundational layer imports no sibling package.
"""

from __future__ import annotations

import hashlib
from collections.abc import Iterable, Iterator
from typing import Any

#: Marks a ref the runner resolves against the Isaac assets root rather than
#: fetching directly; both kinds ride the same closure.
_USD_SUFFIXES = (".usd", ".usda", ".usdz")


def asset_closure(*sources: Any) -> tuple[str, ...]:
    """Every distinct asset ref reachable from the given documents, sorted.

    Accepts embodiment profiles, plain dicts (a JOB_SPEC's ``embodiment``) and
    iterables of either, so both planes can call it with what they happen to
    hold. Sorted + de-duplicated so the result is a SET in a stable order: two
    documents that reach the same assets must produce the same tuple, or the
    cache key stops meaning "the same assets".
    """
    found: set[str] = set()
    for source in sources:
        found.update(_refs(source))
    return tuple(sorted(found))


def asset_set_key(refs: Iterable[str], *, engine_version: str) -> str:
    """sha256 over the closure and the engine it was warmed for.

    Newline-joined rather than concatenated: a ref cannot contain a newline, so
    two different closures cannot collide by running into each other.
    """
    payload = "\n".join([engine_version, *sorted(set(refs))])
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _refs(source: Any) -> Iterator[str]:
    """Walk anything document-shaped, yielding values that look like asset refs.

    Structural rather than field-name driven ON PURPOSE. A field list would have
    to grow every time the profile does, and the failure mode of forgetting is
    silent: the asset simply never gets warmed and one case pays cold time that
    nobody can explain. Walking for USD-suffixed strings cannot forget.
    """
    if source is None:
        return
    if hasattr(source, "model_dump"):
        source = source.model_dump(mode="json")
    if isinstance(source, str):
        if source.endswith(_USD_SUFFIXES):
            yield source
        return
    if isinstance(source, dict):
        for value in source.values():
            yield from _refs(value)
        return
    if isinstance(source, (list, tuple, set)):
        for value in source:
            yield from _refs(value)
