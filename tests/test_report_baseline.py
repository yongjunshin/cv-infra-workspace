"""M4 baseline lifecycle tests (SR-21, C-1) — REQ-REPORT-005/006, NFR-REPORT-002.

Exercises the best-effort establish/advance policy against a REAL store (the
v6 ``request_baselines`` table), the persistence of a baseline across a store
reopen (additive-schema roundtrip), and the C-1 functional proof: the baseline
comes ONLY from the internal store — an empty store yields no match (skip), and
seeding one is what makes a match appear (G-35 positive control). Stdlib + pytest.
"""

from __future__ import annotations

import sqlite3

from cv_infra.orchestrator.store import Store
from cv_infra.report.baseline import find_baseline, update_baseline

_KEY = "sha256:" + "a" * 64


# --------------------------------------------------------------------------- #
# establish / advance / keep-good policy (module docstring)
# --------------------------------------------------------------------------- #


def test_absent_baseline_returns_none(tmp_path):
    with Store(tmp_path / "cv.sqlite3") as store:
        assert find_baseline(store, _KEY) is None  # skip 신호, not an error


def test_first_run_establishes_baseline_for_any_definite_verdict(tmp_path):
    with Store(tmp_path / "cv.sqlite3") as store:
        # a first-run FAIL still establishes (so a later fix reads as 'improved')
        assert update_baseline(store, request_identity_key=_KEY, sut_ref="sut:1", verdict="fail")
        row = find_baseline(store, _KEY)
        assert row is not None
        assert (row.sut_ref, row.verdict) == ("sut:1", "fail")


def test_pass_advances_baseline_last_known_good(tmp_path):
    with Store(tmp_path / "cv.sqlite3") as store:
        update_baseline(store, request_identity_key=_KEY, sut_ref="sut:1", verdict="pass")
        assert update_baseline(store, request_identity_key=_KEY, sut_ref="sut:2", verdict="pass")
        row = find_baseline(store, _KEY)
        assert (row.sut_ref, row.verdict) == ("sut:2", "pass")  # advanced to newer SUT


def test_fail_does_not_overwrite_existing_good_baseline(tmp_path):
    with Store(tmp_path / "cv.sqlite3") as store:
        update_baseline(store, request_identity_key=_KEY, sut_ref="sut:good", verdict="pass")
        # a subsequent fail must NOT overwrite — the regression stays visible.
        assert not update_baseline(
            store, request_identity_key=_KEY, sut_ref="sut:bad", verdict="fail"
        )
        row = find_baseline(store, _KEY)
        assert (row.sut_ref, row.verdict) == ("sut:good", "pass")


def test_errored_verdict_is_never_baselined(tmp_path):
    with Store(tmp_path / "cv.sqlite3") as store:
        assert not update_baseline(store, request_identity_key=_KEY, sut_ref="sut:1", verdict=None)
        assert find_baseline(store, _KEY) is None  # infra outcome is not a reference


def test_key_metrics_and_source_ref_roundtrip(tmp_path):
    with Store(tmp_path / "cv.sqlite3") as store:
        update_baseline(
            store,
            request_identity_key=_KEY,
            sut_ref="sut:1",
            verdict="pass",
            key_metrics={"time_to_goal_s": 12.5, "collision_count": 0},
            source_result_ref="env-1/req-0:0",
            established_at="2026-07-15T00:00:00+00:00",
        )
        row = find_baseline(store, _KEY)
        assert row.key_metrics == {"time_to_goal_s": 12.5, "collision_count": 0}
        assert row.source_result_ref == "env-1/req-0:0"
        assert row.established_at == "2026-07-15T00:00:00+00:00"


# --------------------------------------------------------------------------- #
# persistence: additive v6 table survives a store reopen (C-1 internal home)
# --------------------------------------------------------------------------- #


def test_baseline_persists_across_store_reopen(tmp_path):
    db = tmp_path / "cv.sqlite3"
    with Store(db) as store:
        update_baseline(store, request_identity_key=_KEY, sut_ref="sut:1", verdict="pass")
    with Store(db) as reopened:  # fresh connection = restart
        row = find_baseline(reopened, _KEY)
        assert row is not None and row.verdict == "pass"
    # the baseline lives in cv-infra's OWN sqlite file (C-1), stamped v7
    # (v7 = p5c2 M4 envelope_reports table — additive, version-pin tracks the bump).
    external = sqlite3.connect(str(db))
    try:
        assert external.execute("PRAGMA user_version").fetchone()[0] == 7
        (count,) = external.execute("SELECT COUNT(*) FROM request_baselines").fetchone()
        assert count == 1
    finally:
        external.close()


def test_old_file_upgrades_and_baseline_table_accepts_writes(tmp_path):
    # A pre-v6 file (v1-shaped, unstamped) opens, gains the request_baselines table
    # via CREATE IF NOT EXISTS (additive, no ALTER), and accepts a baseline write.
    db = tmp_path / "cv.sqlite3"
    legacy = sqlite3.connect(str(db))
    try:
        legacy.executescript(
            "CREATE TABLE jobs (request_id TEXT NOT NULL, repeat_index INTEGER NOT NULL,"
            " state TEXT NOT NULL, attempt_count INTEGER NOT NULL,"
            " PRIMARY KEY (request_id, repeat_index));"
        )
        legacy.commit()  # v1 files carry user_version 0 (never stamped)
    finally:
        legacy.close()
    with Store(db) as store:
        assert find_baseline(store, _KEY) is None
        assert update_baseline(store, request_identity_key=_KEY, sut_ref="sut:1", verdict="pass")
        assert find_baseline(store, _KEY).verdict == "pass"
