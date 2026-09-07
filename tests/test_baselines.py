"""Baseline store + comparison tests — baselines.py (pure sqlite, no docker, no GPU).

What is pinned here is what a WRONG baseline costs, not the SQL:

* a comparison that RAISES takes the whole report down with it — the baseline is an
  added signal, so every failure mode of it must degrade to "skipped";
* a metric that gates makes the platform guess which direction of a number is bad;
* a single-sample ratio that is silently downgraded hides a real regression, so the
  thin evidence is LABELLED and still fires;
* a file written by a newer build, opened blind, loses the rows it does not understand.
"""

from __future__ import annotations

import sqlite3

import pytest

from cv_infra.baselines import (
    KIND_CHECK,
    KIND_METRIC,
    STATUS_CHANGED,
    STATUS_IMPROVED,
    STATUS_NO_BASELINE,
    STATUS_OK,
    STATUS_REGRESSED,
    STATUS_SKIPPED,
    BaselineRow,
    BaselineStore,
    CaseObservation,
    compare_best_effort,
    rows_for,
    source_for,
    upsert_best_effort,
)

CASE = "sha256:aa"
OTHER = "sha256:bb"


def _store(tmp_path, name: str = "baselines.sqlite3") -> BaselineStore:
    return BaselineStore(tmp_path / name)


def _seed(tmp_path, rows, source: str = "abc123") -> None:
    store = _store(tmp_path)
    store.upsert(rows, source)
    store.close()


# --- (1) the file: schema, WAL, version stamp -----------------------------------------


def test_open_stamps_the_schema_version_and_runs_in_wal(tmp_path):
    store = _store(tmp_path)
    try:
        (version,) = store._conn.execute("PRAGMA user_version").fetchone()
        (journal,) = store._conn.execute("PRAGMA journal_mode").fetchone()
        columns = [row[1] for row in store._conn.execute("PRAGMA table_info(baselines)")]
    finally:
        store.close()

    assert version == 1
    assert journal == "wal"  # concurrent cases read while one writes
    assert columns == [
        "case_key",
        "name",
        "kind",
        "pass_ratio",
        "value",
        "established_at",
        "source",
    ]


def test_kind_is_constrained_to_check_or_metric(tmp_path):
    store = _store(tmp_path)
    try:
        with pytest.raises(sqlite3.IntegrityError):
            store.upsert([BaselineRow(case_key=CASE, name="fell", kind="whatever")], "abc")
    finally:
        store.close()


def test_a_file_from_a_newer_build_is_refused_instead_of_written_blind(tmp_path):
    path = tmp_path / "future.sqlite3"
    conn = sqlite3.connect(str(path))
    conn.execute("PRAGMA user_version = 2")
    conn.close()

    with pytest.raises(RuntimeError, match="newer"):
        BaselineStore(path)


# --- (2) upsert / load ----------------------------------------------------------------


def test_upsert_stamps_provenance_and_load_keys_by_case_and_name(tmp_path):
    _seed(
        tmp_path,
        [
            BaselineRow(case_key=CASE, name="fell", kind=KIND_CHECK, pass_ratio=1.0),
            BaselineRow(case_key=CASE, name="z_final", kind=KIND_METRIC, value=0.12),
            BaselineRow(case_key=OTHER, name="fell", kind=KIND_CHECK, pass_ratio=0.5),
        ],
    )

    store = _store(tmp_path)
    try:
        loaded = store.load([CASE])
    finally:
        store.close()

    assert set(loaded) == {(CASE, "fell"), (CASE, "z_final")}  # OTHER not asked for
    assert loaded[(CASE, "fell")].pass_ratio == 1.0
    assert loaded[(CASE, "z_final")].value == 0.12
    assert loaded[(CASE, "fell")].source == "abc123"
    assert loaded[(CASE, "fell")].established_at  # stamped by the store, not the caller


def test_upsert_advances_the_existing_row(tmp_path):
    _seed(tmp_path, [BaselineRow(case_key=CASE, name="fell", kind=KIND_CHECK, pass_ratio=1.0)])
    _seed(
        tmp_path,
        [BaselineRow(case_key=CASE, name="fell", kind=KIND_CHECK, pass_ratio=0.5)],
        source="def456",
    )

    store = _store(tmp_path)
    try:
        loaded = store.load([CASE])
    finally:
        store.close()

    assert len(loaded) == 1  # advanced in place — a baseline is not a history table
    assert loaded[(CASE, "fell")].pass_ratio == 0.5
    assert loaded[(CASE, "fell")].source == "def456"


def test_load_of_no_cases_touches_the_db_not_at_all(tmp_path):
    store = _store(tmp_path)
    try:
        assert store.load([]) == {}
    finally:
        store.close()


# --- (3) the rules --------------------------------------------------------------------


def test_a_dropped_pass_ratio_regresses_and_a_risen_one_improves(tmp_path):
    _seed(
        tmp_path,
        [
            BaselineRow(case_key=CASE, name="fell", kind=KIND_CHECK, pass_ratio=1.0),
            BaselineRow(case_key=CASE, name="upright", kind=KIND_CHECK, pass_ratio=0.5),
            BaselineRow(case_key=CASE, name="moved", kind=KIND_CHECK, pass_ratio=1.0),
        ],
    )

    outcome = compare_best_effort(
        tmp_path / "baselines.sqlite3",
        [
            CaseObservation(
                case_key=CASE,
                checks={"fell": 0.66, "upright": 1.0, "moved": 1.0},
                repeats_run=3,
            )
        ],
    )

    assert outcome.available is True
    assert (outcome.compared, outcome.regressed, outcome.improved) == (3, 1, 1)
    statuses = {d["name"]: d["status"] for d in outcome.cases[CASE].details}
    assert statuses == {
        "fell": STATUS_REGRESSED,
        "upright": STATUS_IMPROVED,
        "moved": STATUS_OK,
    }
    assert outcome.cases[CASE].status == STATUS_REGRESSED  # one regressed key reddens it
    assert [d["single_sample"] for d in outcome.cases[CASE].details] == [False, False, False]


def test_a_single_sample_regression_is_labelled_but_still_gates(tmp_path):
    _seed(tmp_path, [BaselineRow(case_key=CASE, name="fell", kind=KIND_CHECK, pass_ratio=1.0)])

    outcome = compare_best_effort(
        tmp_path / "baselines.sqlite3",
        [CaseObservation(case_key=CASE, checks={"fell": 0.0}, repeats_run=1)],
    )

    (detail,) = outcome.cases[CASE].details
    assert detail["single_sample"] is True  # the evidence is thin and says so
    assert detail["status"] == STATUS_REGRESSED
    assert outcome.regressed == 1  # ...and the gate still fires


def test_a_changed_metric_is_reported_with_its_delta_and_never_regresses(tmp_path):
    _seed(
        tmp_path,
        [
            BaselineRow(case_key=CASE, name="z_final", kind=KIND_METRIC, value=0.10),
            BaselineRow(case_key=CASE, name="steady", kind=KIND_METRIC, value=2.0),
        ],
    )

    outcome = compare_best_effort(
        tmp_path / "baselines.sqlite3",
        [CaseObservation(case_key=CASE, metrics={"z_final": 0.13, "steady": 2.0})],
    )

    details = {d["name"]: d for d in outcome.cases[CASE].details}
    assert details["z_final"]["status"] == STATUS_CHANGED
    assert details["z_final"]["delta"] == pytest.approx(0.03)
    assert details["steady"]["status"] == STATUS_OK
    assert (outcome.metric_changes, outcome.regressed) == (1, 0)
    assert outcome.cases[CASE].status == STATUS_OK  # a moved number is not a failure


def test_an_absent_or_retyped_key_skips_instead_of_failing(tmp_path):
    _seed(
        tmp_path,
        [
            # 'fell' was a METRIC last time and is a check today — no comparable history.
            BaselineRow(case_key=CASE, name="fell", kind=KIND_METRIC, value=1.0),
            BaselineRow(case_key=CASE, name="z_final", kind=KIND_CHECK, pass_ratio=1.0),
        ],
    )

    outcome = compare_best_effort(
        tmp_path / "baselines.sqlite3",
        [
            CaseObservation(
                case_key=CASE, checks={"fell": 0.0, "new": 0.0}, metrics={"z_final": 1.0}
            )
        ],
    )

    assert {d["status"] for d in outcome.cases[CASE].details} == {STATUS_NO_BASELINE}
    assert [d["baseline"] for d in outcome.cases[CASE].details] == [None, None, None]
    assert (outcome.absent, outcome.compared, outcome.regressed) == (3, 0, 0)
    assert outcome.cases[CASE].status == STATUS_NO_BASELINE  # absent = skip, not red


def test_a_null_stored_number_reads_as_absent(tmp_path):
    _seed(
        tmp_path,
        [
            BaselineRow(case_key=CASE, name="fell", kind=KIND_CHECK, pass_ratio=None),
            BaselineRow(case_key=CASE, name="z", kind=KIND_METRIC, value=None),
        ],
    )

    outcome = compare_best_effort(
        tmp_path / "baselines.sqlite3",
        [CaseObservation(case_key=CASE, checks={"fell": 1.0}, metrics={"z": 1.0})],
    )

    assert outcome.absent == 2  # no number stored = nothing to compare against


def test_an_errored_case_is_skipped_entirely(tmp_path):
    _seed(tmp_path, [BaselineRow(case_key=CASE, name="fell", kind=KIND_CHECK, pass_ratio=1.0)])

    outcome = compare_best_effort(
        tmp_path / "baselines.sqlite3",
        [CaseObservation(case_key=CASE, errored=True)],
    )

    assert outcome.cases[CASE].status == STATUS_SKIPPED
    assert outcome.cases[CASE].details == ()
    assert (outcome.compared, outcome.regressed) == (0, 0)  # infra fault != regression


# --- (4) best effort ------------------------------------------------------------------


def test_a_missing_directory_skips_every_case_with_one_stderr_line(tmp_path, capsys):
    outcome = compare_best_effort(
        tmp_path / "no-such-dir" / "baselines.sqlite3",
        [CaseObservation(case_key=CASE, checks={"fell": 1.0}), CaseObservation(case_key=OTHER)],
    )

    assert outcome.available is False
    assert outcome.error
    assert {case.status for case in outcome.cases.values()} == {STATUS_SKIPPED}
    assert len(capsys.readouterr().err.strip().splitlines()) == 1


def test_a_corrupt_file_skips_rather_than_taking_the_report_down(tmp_path, capsys):
    path = tmp_path / "corrupt.sqlite3"
    path.write_bytes(b"this is not a database" * 100)

    outcome = compare_best_effort(path, [CaseObservation(case_key=CASE, checks={"fell": 1.0})])

    assert outcome.available is False
    assert outcome.cases[CASE].status == STATUS_SKIPPED
    assert "unavailable" in capsys.readouterr().err


def test_update_creates_the_db_directory_on_a_fresh_runner(tmp_path):
    path = tmp_path / "home" / ".cv-infra" / "baselines.sqlite3"

    assert upsert_best_effort(
        path,
        [BaselineRow(case_key=CASE, name="fell", kind=KIND_CHECK, pass_ratio=1.0)],
        source="abc123",
    )

    store = BaselineStore(path)
    try:
        assert store.load([CASE])[(CASE, "fell")].pass_ratio == 1.0
    finally:
        store.close()


def test_a_failed_update_reports_false_instead_of_raising(tmp_path, capsys):
    blocker = tmp_path / "not-a-dir"
    blocker.write_text("", encoding="utf-8")

    assert not upsert_best_effort(
        blocker / "baselines.sqlite3",
        [BaselineRow(case_key=CASE, name="fell", kind=KIND_CHECK, pass_ratio=1.0)],
        source="abc123",
    )
    assert "not updated" in capsys.readouterr().err


# --- (5) what gets written ------------------------------------------------------------


def test_rows_for_writes_checks_and_metrics_but_never_an_errored_case():
    rows = rows_for(
        [
            CaseObservation(case_key=CASE, checks={"fell": 0.5}, metrics={"z_final": 0.12}),
            CaseObservation(case_key=OTHER, checks={"fell": 1.0}, errored=True),
        ]
    )

    assert [(r.case_key, r.name, r.kind, r.pass_ratio, r.value) for r in rows] == [
        (CASE, "fell", KIND_CHECK, 0.5, None),
        (CASE, "z_final", KIND_METRIC, None, 0.12),
    ]


def test_source_is_the_ci_commit_when_there_is_one():
    assert source_for({"GITHUB_SHA": "deadbeef"}) == "deadbeef"
    assert source_for({}) == "manual"  # a local run is traceable as such
