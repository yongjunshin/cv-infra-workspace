"""M4 regression judgement + identity-key tests (SR-20) — REQ-REPORT-002/003/004.

Pins: (1) ``request_identity_key`` stability — SUT/apiVersion/repeats excluded,
scenario/criteria/settings included; keys derived from the REAL M1 model dump
(G-17: consume the real producer shape, not a hand-drift); (1b) the p5c10 D-5
convention "absent == explicit null", guarded by WALKING a real wire dump so the
guard grows with the M1 contract (and the boundary: lists are never pruned);
(2) the regression
3-phase — baseline absent -> skip (normal), present+same -> no regression,
present+pass->fail -> regressed with 식별성; (3) the C-1 structural negative — the
whole ``cv_infra.report`` package imports only stdlib + cv-infra internal (no
network/subprocess/git/consumer-path code path exists). Stdlib + pytest only.
"""

from __future__ import annotations

import ast
import copy
import pathlib
from typing import Any

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

    # fixed_dt (determinism dt) is in the key; repeats (above) is not. Asserted
    # NON-NULL vs NON-NULL: since p5c10 (D-5) absent == explicit null, so a
    # None-vs-value comparison would only re-test the pruning rule, not "fixed_dt
    # discriminates". Both forms are kept — value-vs-value AND set-vs-unset.
    dt_002 = identity_key(_dump(execution_settings={"fixed_dt": 0.02}))
    dt_003 = identity_key(_dump(execution_settings={"fixed_dt": 0.03}))
    assert dt_002 != dt_003  # two different dts = two different requests
    assert dt_002 != base  # ...and pinning a dt differs from leaving it unset


def test_criteria_order_is_significant_conservative_skip_safe():
    # Order not normalized: a reordered criteria list is a DIFFERENT request (keys
    # differ -> baseline miss -> skip), which is skip-safe (NFR-REPORT-002).
    two = [{"oracle": "reached_goal"}, {"oracle": "no_collision", "params": {"chassis_path": "/c"}}]
    key_ab = identity_key(_dump(acceptance_criteria=two))
    key_ba = identity_key(_dump(acceptance_criteria=list(reversed(two))))
    assert key_ab != key_ba


# --------------------------------------------------------------------------- #
# (1b) absent == explicit null — the p5c10 D-5 normalization convention
# --------------------------------------------------------------------------- #

# A request that exercises every nesting shape the pruning walks: an optional
# sub-block partly filled (scenario.debug_obstacle: height set, width/depth null),
# mappings INSIDE list elements (criteria params, adapter_config.sensors — one
# with `frame`, one without), scalar lists, and set/unset optionals side by side.
_RICH_REQUEST: dict[str, Any] = {
    "scenario": {
        "scene": "nova_carter_warehouse",
        "robot": "nova_carter",
        "goal": {"x": -6.0, "y": 5.0, "yaw": 1.5708},
        "seed": 42,
        "timeout_s": 120,
        "debug_obstacle": {"x": -6.0, "y": 2.0, "height": 0.15},
    },
    "sut": {"image_ref": "carter-sut:a"},
    "interface": {
        "type": "ros2",
        "adapter_config": {
            "odom_topics": ["/odom", "/chassis/odom"],
            "sensors": [
                {"topic": "/front_2d_lidar/scan", "type": "sensor_msgs/msg/LaserScan"},
                {
                    "topic": "/front_3d_lidar/lidar_points",
                    "type": "sensor_msgs/msg/PointCloud2",
                    "frame": "front_3d_lidar",
                },
            ],
        },
    },
    "acceptance_criteria": [
        {"oracle": "reached_goal", "params": {"position_tolerance_m": 0.75}},
        {"oracle": "no_collision", "params": {"chassis_path": "/World/Nova_Carter_ROS/chassis"}},
    ],
    "execution_settings": {"repeats": 3, "fixed_dt": 0.016667},
}


def _mapping_paths(node: Any, prefix: tuple = ()) -> list[tuple]:
    """Every mapping-key path in a wire dump (descending into list elements)."""
    paths: list[tuple] = []
    if isinstance(node, dict):
        for key, value in node.items():
            paths.append((*prefix, key))
            paths.extend(_mapping_paths(value, (*prefix, key)))
    elif isinstance(node, list):
        for index, item in enumerate(node):
            paths.extend(_mapping_paths(item, (*prefix, index)))
    return paths


def _parent(dump: Any, path: tuple) -> Any:
    """The container holding ``path``'s last key."""
    node = dump
    for part in path[:-1]:
        node = node[part]
    return node


def test_explicit_null_equals_absent_for_every_dump_path():
    """D-5: for EVERY key path of a real dump, "set to null" == "not there at all".

    Walking the dump — instead of listing today's optional fields — is what makes
    this guard grow with the M1 contract: a field added tomorrow is covered the
    day it lands. Positive control: pre-p5c10 code (no null pruning) fails this.
    """
    dump = VerificationRequest.model_validate(_RICH_REQUEST).model_dump(mode="json", by_alias=True)
    paths = _mapping_paths(dump)
    assert len(paths) > 30, "the rich request must exercise a deeply nested dump"
    for path in paths:
        nulled = copy.deepcopy(dump)
        _parent(nulled, path)[path[-1]] = None
        dropped = copy.deepcopy(dump)
        del _parent(dropped, path)[path[-1]]
        assert identity_key(nulled) == identity_key(dropped), f"absent != null at {path}"


def test_omitted_block_and_explicit_defaults_are_one_key():
    """The 4 shapes a caller can hand us for the SAME request converge (D-5).

    A: ``execution_settings`` absent (a hand-built mapping, or a dump taken with
    ``exclude_none=True`` — the idiom orchestrator/api.py:335-339 already uses
    elsewhere) · B: repeats only · C: full dump, repeats=1 · D: full dump,
    repeats=3. Pre-p5c10, A/B forked away from C/D = a latent baseline splitter.
    """
    full = _dump()  # C — repeats defaulted to 1, fixed_dt null
    case_a = {key: value for key, value in full.items() if key != "execution_settings"}
    case_b = {**full, "execution_settings": {"repeats": 3}}
    case_d = _dump(execution_settings={"repeats": 3})
    assert len({identity_key(m) for m in (case_a, case_b, full, case_d)}) == 1


def test_a_new_optional_null_field_does_not_move_the_key():
    """Why D-2 (``scenario.initial_pose``) was baseline-safe — now with the REAL field.

    An optional field added to the contract dumps as ``null`` on every existing
    request; pruning drops it, so pre-existing identity keys — and the baseline
    rows behind them — stand. Since p5c11 the D-2 field EXISTS, so the first half
    is no longer simulated: deleting it from a genuine model dump reproduces the
    pre-growth wire exactly. The synthetic half keeps covering growth shapes the
    contract does not have yet (a nested knob, a whole new top-level block).
    """
    grown = _dump()  # real dump — carries scenario.initial_pose: null since p5c11
    assert grown["scenario"]["initial_pose"] is None, "D-2's field must be in the dump"
    pre_growth = copy.deepcopy(grown)
    del pre_growth["scenario"]["initial_pose"]  # byte-for-byte the pre-p5c11 wire
    assert identity_key(grown) == identity_key(pre_growth)

    grown["execution_settings"]["some_future_knob"] = None  # a nested one
    grown["future_top_level_block"] = None  # and a whole new block
    assert identity_key(grown) == identity_key(pre_growth)


# The canonical consumer request's key AFTER the p5c10 D-5 one-off reset. Measured
# (p5c11 T2) on both sides of the ``scenario.initial_pose`` addition — identical —
# and equal to the value the ``regression.py`` header records.
CANONICAL_FIXTURE_KEY = "sha256:3ed4011ca34c5a1c4f1a32ded31a93812ec2a9d8664fb42a8d4cccd8da8db487"


def test_canonical_fixture_key_is_pinned_to_the_live_baseline_value():
    """The key live baseline rows for the canonical request carry — it must stand.

    Derived exactly as production does (``orchestrator/api.py``: full
    ``model_dump(mode="json", by_alias=True)`` of the ADMITTED request). A red here
    means every baseline row for this request just became ``no-baseline``: legitimate
    ONLY as a conscious contract/fixture change (re-pin, and expect one no-baseline
    run), never as a side effect of adding an optional field.
    """
    from cv_infra.contract.loader import load_request

    fixture = pathlib.Path(__file__).parent / "fixtures" / "nova_carter_warehouse_goal.yaml"
    dump = load_request(fixture).request.model_dump(mode="json", by_alias=True)
    assert dump["scenario"]["initial_pose"] is None  # optional, omitted by the consumer
    assert identity_key(dump) == CANONICAL_FIXTURE_KEY


def test_empty_list_is_kept_not_treated_as_absent():
    """The pruning BOUNDARY (keeps the module comment honest): mappings shrink,
    lists never do. An empty list is identity-bearing — only null-valued keys and
    mappings emptied BY the pruning are equivalent to absence."""
    dump = _dump(acceptance_criteria=[{"oracle": "no_collision", "params": {"chassis_path": "/c"}}])
    assert dump["acceptance_criteria"][0]["params"]["collision_excluded_paths"] == []
    without = copy.deepcopy(dump)
    del without["acceptance_criteria"][0]["params"]["collision_excluded_paths"]
    assert identity_key(without) != identity_key(dump)


# --------------------------------------------------------------------------- #
# (1c) p6 randomness (p6c3) — a request that DECLARES distributions has its own
# key, and the axis that decides which samples get drawn is INSIDE it.
# --------------------------------------------------------------------------- #

# Same request as ``_BASE_REQUEST`` with two axes randomized + a spawn window.
_DISTRIBUTION_SCENARIO: dict[str, Any] = {
    "scene": "nova_carter_warehouse",
    "robot": "nova_carter",
    "goal": {"x": {"choice": [-6.0, 5.0]}, "y": 5.0, "yaw": {"uniform": [0.0, 3.1416]}},
    "seed": 42,
    "timeout_s": 120,
    "initial_pose": {"x": {"uniform": [-6.5, -5.5]}, "y": -1.0, "yaw": 3.1416},
}

# The key a randomized request carries. Measured 2026-08-26 (p6c3 T1) — the
# sibling of CANONICAL_FIXTURE_KEY for the p6 wire: it exists so that the
# baseline of a randomized request cannot be silently invalidated by a change to
# the notation's SERIALIZATION (the distribution rides the key verbatim), the
# way the canonical pin protects static requests.
DISTRIBUTION_FIXTURE_KEY = "sha256:cc66d90e256cd60ba6e3deff92d92182f7c9780ab864ade5abd0b7d05628ae85"


def test_distribution_request_key_is_pinned():
    assert identity_key(_dump(scenario=_DISTRIBUTION_SCENARIO)) == DISTRIBUTION_FIXTURE_KEY


def test_seed_moves_a_randomized_requests_key_but_repeats_still_does_not():
    """The two halves of "what identifies a randomized request".

    seed IS in the key: with distributions declared, the seed decides WHICH
    samples get drawn, so two seeds are two different verification requests and
    must not share a baseline. repeats is still NOT: it decides HOW MANY samples
    are drawn — the rolled-up verdict already collapses them (header block).
    """
    other_seed = {**_DISTRIBUTION_SCENARIO, "seed": 43}
    assert identity_key(_dump(scenario=other_seed)) != DISTRIBUTION_FIXTURE_KEY
    for repeats in (1, 5, 60):
        assert (
            identity_key(
                _dump(scenario=_DISTRIBUTION_SCENARIO, execution_settings={"repeats": repeats})
            )
            == DISTRIBUTION_FIXTURE_KEY
        )


def test_min_pass_ratio_is_part_of_request_identity():
    """Unlike repeats, min_pass_ratio says WHAT COUNTS AS PASS — a judgement
    policy, i.e. part of what is being verified (p6 design §0-14). Absent it is
    null and prunes away, so no existing key moved when the field landed."""
    assert (
        identity_key(_dump(scenario=_DISTRIBUTION_SCENARIO, execution_settings={"repeats": 5}))
        == DISTRIBUTION_FIXTURE_KEY
    )
    with_ratio = _dump(
        scenario=_DISTRIBUTION_SCENARIO, execution_settings={"repeats": 5, "min_pass_ratio": 0.8}
    )
    assert identity_key(with_ratio) != DISTRIBUTION_FIXTURE_KEY


def test_the_key_belongs_to_the_submitted_document_not_to_a_derived_sample():
    """Why the producers derive the key from the ADMITTED request and never from
    the materialized sample (orchestrator/api.py, cli/main.py): each sample has
    different drawn values and its own derivation stamp, so keying on a sample
    would give one request N baselines that never match again."""
    from cv_infra.contract.derive import materialize_request

    request = VerificationRequest.model_validate(
        {**_BASE_REQUEST, "scenario": _DISTRIBUTION_SCENARIO}
    )
    keys = {
        identity_key(materialize_request(request, i).model_dump(mode="json", by_alias=True))
        for i in range(3)
    }
    assert len(keys) == 3 and DISTRIBUTION_FIXTURE_KEY not in keys


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
