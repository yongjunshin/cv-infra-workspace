"""apiVersion 3-state resolver + deprecation window (M1 §3.1 — NFR-INTAKE-002).

3-state contract (LOCKED — consistent with the exit-code contract §7-9):

    supported, current     -> accept                     (normal flow)
    supported, deprecated  -> accept + WARNING           (sunset date + migration link)
    unknown / unsupported  -> reject                     (exit-2-ELIGIBLE object;
                                                          ``sys.exit`` is the consumer's)

Deprecation policy (NFR-INTAKE-002, documentation target): breaking changes
only on a MAJOR bump; at least N-1 minor supported; sunset window >= 2
releases. The 3 version axes (Action tag / CLI package / contract apiVersion)
are INDEPENDENT — no single hardcoded version (R17); the compat matrix is
surfaced to users by M8.

The apiVersion CONSTANT is reused from ``apiversion.py`` (single definition —
never redefined here). An ABSENT apiVersion is a STRICT reject (D-1'
2026-07-10, supersedes the cycle-1 accept-as-current assumption): silently
treating absence as current would hollow out the versioned contract
(NFR-INTAKE-002; loud > silent). The friendly error tells the user exactly
which line to add (``apiVersion: cv-infra/v1``).

``cv-infra/v1`` has no deprecated predecessors yet, so ``DEPRECATED`` is empty
— tests exercise the warn path by injecting a deprecation table (no fake
version is baked into the module constants).
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Literal

from cv_infra.contract.apiversion import API_VERSION
from cv_infra.contract.errors import ContractError

#: apiVersions the current code accepts as-is.
SUPPORTED: frozenset[str] = frozenset({API_VERSION})

#: Citation anchor rendered into rejects/warnings (a real user-facing doc URL
#: is an M8 surface — this stays an honest citation, not a fabricated link).
DOC_LINK = "M1-contract-and-schema.md §3.1 (apiVersion policy)"


@dataclass(frozen=True)
class DeprecatedVersion:
    """Deprecation metadata surfaced in the WARNING (sunset >= 2 releases)."""

    sunset: str  # e.g. "removed after release vX+2"
    migration_link: str


#: apiVersions still accepted but deprecated (warn). Empty for cv-infra/v1 —
#: populated when a v2 lands and v1 enters its sunset window.
DEPRECATED: Mapping[str, DeprecatedVersion] = {}


@dataclass(frozen=True)
class VersionResolution:
    """Outcome of the 3-state resolve — a value, never a control-flow effect.

    ``state == "reject"`` carries the friendly ``error`` (exit-2-eligible);
    ``state == "warn"`` carries the deprecation ``warning`` prose. The consumer
    (CLI/M3) maps reject -> exit 2 / 422 — this module never exits.
    """

    api_version: str
    state: Literal["accept", "warn", "reject"]
    warning: str | None = None
    error: ContractError | None = None


def resolve_api_version(
    value: object,
    *,
    supported: frozenset[str] | None = None,
    deprecated: Mapping[str, DeprecatedVersion] | None = None,
    source_path: str | None = None,
) -> VersionResolution:
    """Resolve a document's ``apiVersion`` value through the 3-state table.

    ``value`` is the raw (pre-validation) document value: absent (``None``)
    is a STRICT reject with add-the-key guidance (D-1'); a non-string value
    rejects with a friendly error. ``supported``/``deprecated`` default to the
    module tables (overridable so tests/tools can evaluate hypothetical
    windows).
    """
    supported = SUPPORTED if supported is None else supported
    deprecated = DEPRECATED if deprecated is None else deprecated

    if value is None:
        return VersionResolution(
            api_version="(missing)",
            state="reject",
            error=_reject_error(None, supported, deprecated, source_path),
        )

    if not isinstance(value, str):
        return VersionResolution(
            api_version=repr(value),
            state="reject",
            error=_reject_error(repr(value), supported, deprecated, source_path),
        )

    if value in deprecated:
        info = deprecated[value]
        return VersionResolution(
            api_version=value,
            state="warn",
            warning=(
                f"apiVersion {value!r} is DEPRECATED (sunset: {info.sunset}) — "
                f"migrate to {API_VERSION!r}: {info.migration_link}"
            ),
        )

    if value in supported:
        return VersionResolution(api_version=value, state="accept")

    return VersionResolution(
        api_version=value,
        state="reject",
        error=_reject_error(value, supported, deprecated, source_path),
    )


def _reject_error(
    got: str | None,
    supported: frozenset[str],
    deprecated: Mapping[str, DeprecatedVersion],
    source_path: str | None,
) -> ContractError:
    """``got is None`` = the key is ABSENT (D-1' strict): guide the user to add
    it (``ContractError``'s default ``got`` renders the "(missing)" sentinel)."""
    known = sorted(supported | set(deprecated))
    expected = f"a supported contract apiVersion, one of {known}"
    got_kwargs: dict[str, str] = {"got": repr(got)}
    if got is None:
        expected += " — the key is required; add it at the top level of the document"
        got_kwargs = {}
    return ContractError(
        field_path="apiVersion",
        expected=expected,
        **got_kwargs,
        example=f"apiVersion: {API_VERSION}",
        doc_link=DOC_LINK,
        source_path=source_path,
    )
