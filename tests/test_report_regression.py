"""M4 regression judgement + identity-key tests (SR-20) — REQ-REPORT-002/003/004.

Pins: (1) ``request_identity_key`` stability — SUT/apiVersion/repeats excluded,
scenario/criteria/settings included; keys derived from the REAL M1 model dump
(G-17: consume the real producer shape, not a hand-drift); (2) the regression
3-phase — baseline absent -> skip (normal), present+same -> no regression,
present+pass->fail -> regressed with 식별성; (3) the C-1 structural negative — the
whole ``cv_infra.report`` package imports only stdlib + cv-infra internal (no
network/subprocess/git/consumer-path code path exists). Stdlib + pytest only.
"""

from __future__ import annotations

import ast
import pathlib

import cv_infra.report
from cv_infra.contract.schema import VerificationRequest
from cv_infra.orchestrator.store import BaselineRow
from cv_infra.report.regression import (
    STATUS_IMPROVED,
    STATUS_NO_BASELINE,
    STATUS_REGRESSED,
    STATUS_UNCHANGED,
    identity_key,
    judge_regression,
)

_BASE_REQUEST = {
    "scenario": {
        "scene": "nova_carter_warehouse",
        "robot": "nova_carter",
        "goal": {"x": -6.0, "y": 5.0, "yaw": 1.5708},
        "seed": 42,
        "timeout_s": 120,
    },
    "sut": {"image_ref": "carter-sut:a"},
    "acceptance_criteria": [{"oracle": "reached_goal"}],
}


def _dump(**overrides) -> dict:
    """A REAL M1 Verification Request wire dump (by_alias) with field overrides."""
    payload = {**_BASE_REQUEST, **overrides}
    return VerificationRequest.model_validate(payload).model_dump(mode="json", by_alias=True)


# --------------------------------------------------------------------------- #
# (1) request_identity_key — normalization scope (surfaced assumption, tested)
# --------------------------------------------------------------------------- #


def test_identity_key_is_prefixed_sha256_and_stable():
    key = identity_key(_dump())
    assert key.startswith("sha256:") and len(key) == len("sha256:") + 64
    assert identity_key(_dump()) == key  # deterministic across fresh dumps


def test_sut_only_change_keeps_same_key():
    # The whole point of REQ-REPORT-002: "same request, only SUT differs".
    key_a = identity_key(_dump(sut={"image_ref": "carter-sut:a"}))
    key_b = identity_key(
        _dump(
            sut={
                "image_ref": "carter-sut:b",
                "image_id": "sha256:" + "b" * 64,
            }
        )
    )
    assert key_a == key_b


def test_apiversion_and_repeats_excluded_from_key():
    base = identity_key(_dump())
    assert identity_key(_dump(apiVersion="cv-infra/v1")) == base  # explicit == default
    # repeats is a fan-out orchestration knob, NOT part of request identity.
    assert identity_key(_dump(execution_settings={"repeats": 5})) == base
    assert identity_key(_dump(execution_settings={"repeats": 1})) == base


def test_scenario_criteria_and_fixed_dt_change_the_key():
    base = identity_key(_dump())
    changed_scene = _dump()
    changed_scene["scenario"]["seed"] = 43
    assert identity_key(changed_scene) != base  # scenario (determinism seed) in key

    changed_criteria = _dump(
        acceptance_criteria=[{"oracle": "reached_goal", "params": {"position_tolerance_m": 0.5}}]
    )
    assert identity_key(changed_criteria) != base  # criteria in key

    # fixed_dt (determinism dt) is in the key; repeats (above) is not.
    assert identity_key(_dump(execution_settings={"fixed_dt": 0.02})) != base


def test_criteria_order_is_significant_conservative_skip_safe():
    # Order not normalized: a reordered criteria list is a DIFFERENT request (keys
    # differ -> baseline miss -> skip), which is skip-safe (NFR-REPORT-002).
    two = [{"oracle": "reached_goal"}, {"oracle": "no_collision", "params": {"chassis_path": "/c"}}]
    key_ab = identity_key(_dump(acceptance_criteria=two))
    key_ba = identity_key(_dump(acceptance_criteria=list(reversed(two))))
    assert key_ab != key_ba


# --------------------------------------------------------------------------- #
# (2) regression 3-phase — REQ-REPORT-003/004, NFR-REPORT-001/002
# --------------------------------------------------------------------------- #


def _baseline(verdict: str, sut_ref: str = "carter-sut:a") -> BaselineRow:
    return BaselineRow(
        request_identity_key="sha256:" + "0" * 64,
        sut_ref=sut_ref,
        verdict=verdict,
        established_at="2026-07-15T00:00:00+00:00",
    )


def test_absent_baseline_skips_as_normal():
    reg = judge_regression("req-1", "fail", None)
    assert reg.status == STATUS_NO_BASELINE
    assert reg.baseline_sut_ref is None
    assert "skip" in reg.detail and "실패 아님" in reg.detail  # normal, not a failure


def test_same_verdict_is_unchanged_no_regression():
    reg = judge_regression("req-1", "pass", _baseline("pass"))
    assert reg.status == STATUS_UNCHANGED
    assert reg.baseline_verdict == "pass"


def test_pass_to_fail_is_regressed_with_identification():
    reg = judge_regression("req-1", "fail", _baseline("pass", sut_ref="carter-sut:a@sha256:xyz"))
    assert reg.status == STATUS_REGRESSED
    # NFR-REPORT-001: which request, which SUT version, when established.
    assert reg.baseline_sut_ref == "carter-sut:a@sha256:xyz"
    assert reg.baseline_established_at == "2026-07-15T00:00:00+00:00"
    assert "req-1" in reg.detail and "carter-sut:a@sha256:xyz" in reg.detail
    assert "pass→fail" in reg.detail


def test_fail_to_pass_is_improved():
    reg = judge_regression("req-1", "pass", _baseline("fail"))
    assert reg.status == STATUS_IMPROVED


def test_errored_current_skips_without_comparing():
    reg = judge_regression("req-1", None, _baseline("pass"))
    assert reg.status == STATUS_NO_BASELINE  # no domain verdict to compare
    assert reg.baseline_sut_ref is None  # baseline not attached — no contradiction
    assert "errored" in reg.detail


# --------------------------------------------------------------------------- #
# (3) C-1 structural negative — no network/subprocess/git/consumer-path code path
# --------------------------------------------------------------------------- #

# The report package may import ONLY these stdlib modules + cv-infra internal
# (cv_infra.*). Absence of socket/urllib/http/subprocess/git/os proves — at the
# import graph level — that no baseline/report code can reach a consumer's CI/git
# history or the network (REQ-REPORT-006, C-1). An allow-list flags any new import
# for review rather than blessing a deny-list that a rename could slip past (G-21).
_ALLOWED_STDLIB = frozenset(
    {
        "__future__",
        "hashlib",
        "json",
        "copy",
        "collections",
        "typing",
        "dataclasses",
        "datetime",
        "enum",
        "math",
    }
)


def _imported_top_modules(path: pathlib.Path) -> set[str]:
    tree = ast.parse(path.read_text())
    tops: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            tops.update(alias.name.split(".")[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
            tops.add(node.module.split(".")[0])
    return tops


def test_report_package_imports_only_stdlib_and_internal():
    package_dir = pathlib.Path(cv_infra.report.__file__).parent
    sources = sorted(package_dir.glob("*.py"))
    assert sources, "report package has no modules to audit"
    for source in sources:
        for module in _imported_top_modules(source):
            assert (
                module == "cv_infra" or module in _ALLOWED_STDLIB
            ), f"{source.name} imports {module!r} — C-1 forbids network/subprocess/git/os"


def test_report_source_has_no_remote_endpoint_literals():
    # Complements the import audit: a hardcoded consumer-repo/network endpoint
    # would surface as a URL-scheme literal. These schemes cannot appear in
    # legitimate report/ code (no endpoints — C-1), and — unlike bare words such
    # as "git"/"socket" that our OWN negative-contract docstrings use — they do
    # not collide with prose (G-21: scan the real injection form, not a word).
    package_dir = pathlib.Path(cv_infra.report.__file__).parent
    forbidden = ("://", "git@", "GITHUB_TOKEN")
    for source in package_dir.glob("*.py"):
        text = source.read_text()
        for token in forbidden:
            assert token not in text, f"{source.name} contains forbidden literal {token!r} (C-1)"
