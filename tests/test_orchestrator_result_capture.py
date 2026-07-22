"""p5c3 Result 캡처 + trigger_source 와이어 (M3 control-plane fold -> report row 실값).

Two additive seams, all CPU-provable (no GPU, no docker):

* **Result 캡처** — the control-plane fold (``supervisor._job_result_of`` +
  ``_read_result_doc``) reads the runner-emitted result.json ADDITIVELY onto the
  ``JobResult`` (``result_doc`` / ``result_json_path``), and ``api._result_wire``
  emits its declared ``metrics`` + ``artifacts{mcap,mp4}`` paths + host ``result_json`` —
  so a report row shows real values (P5-02/P5-10) instead of the empty ``{}``/None
  placeholders. Absent/unreadable = honest absence, 회귀 0. **p5c5 T2 (c) 참조프레임 해소**:
  the runner writes mcap/mp4 as CONTAINER-frame paths under RESULT_OUT; the fold now
  rewrites them to HOST-resolvable absolute paths (the RESULT_OUT dir is the parent of
  result.json on the host) so T1's uploader can stage them — consistent with
  ``result_json`` (already host). This SUPERSEDES the p5c3 pass-through-verbatim behavior
  (the container path is not host-resolvable → T1 could not find the file); field NAMES
  stay unchanged (mcap/mp4 → aggregate ``rosbag_mcap``/``recording_mp4``).
* **trigger_source** — ``POST /envelopes`` accepts an optional top-level
  ``trigger_source`` (REQ-INTAKE-003): absent -> ``human-manual`` (default), a legal
  M1 literal -> recorded verbatim into the report, an illegal value -> structured 422.

The result.json fixtures are built by the REAL runner producer
(``runner.evaluate.build_result_dict`` / ``runner.main.write_result`` — the exact
surface ``tests/test_result_emission_golden.py`` anchors, so this fixture is anchored
to the emission shape, NOT hand-crafted to match ``_result_wire``: G-28). Stdlib +
pytest + FastAPI TestClient.
"""

from __future__ import annotations

import copy
import json
import time
from pathlib import Path
from typing import Any

import yaml
from fastapi.testclient import TestClient

from cv_infra.contract.errors import ANNOTATION_KEYS
from cv_infra.contract.schema import Artifacts
from cv_infra.orchestrator.api import (
    _DEFAULT_TRIGGER_SOURCE,
    _TRIGGER_SOURCES,
    _result_wire,
    create_app,
)
from cv_infra.orchestrator.fake_runner import FakeRunner
from cv_infra.orchestrator.fanout import fan_out
from cv_infra.orchestrator.models import Job, JobResult, JobState, Verdict
from cv_infra.orchestrator.store import Store, job_key
from cv_infra.orchestrator.supervisor import (
    JOB_TIMEOUT_MARKER,
    RESULT_OUT_MOUNT,
    JobOutcome,
    RunJobRunner,
    _job_result_of,
    _read_result_doc,
)
from cv_infra.runner.evaluate import OracleOutcome, build_result_dict
from cv_infra.runner.main import write_result
from tests.test_result_emission_golden import GOLDEN_PASS

# Platform copy of the consumer scenario — admitted by the REAL 6-stage loader.
_FIXTURE = Path(__file__).parent / "fixtures" / "nova_carter_warehouse_goal.yaml"
_CANONICAL_DOC = yaml.safe_load(_FIXTURE.read_text(encoding="utf-8"))

# Known real-shape payloads: a metrics map with a None value + concrete artifact
# paths. The runner writes mcap/mp4 as CONTAINER-frame paths under RESULT_OUT
# (RESULT_OUT_MOUNT="/cv/out"); the fold rewrites them to HOST paths (p5c5 T2 c), so
# these are the INPUT (what the runner writes), and ``_hostify`` below is the expected
# OUTPUT (what T1 uploads).
_METRICS = {
    "time_to_goal_s": 42.5,
    "min_clearance_m": None,
    "collision_count": 0,
    "path_len_m": 7.3,
}
_MCAP = "/cv/out/bag/job.mcap"  # container-frame (runner-written) — INPUT
_MP4 = "/cv/out/recording.mp4"  # container-frame (runner-written) — INPUT


def _hostify(container_path: str, result_json_path) -> str:
    """The expected HOST path the fold produces: the container path under
    RESULT_OUT_MOUNT re-rooted at the host RESULT_OUT dir (= result.json's parent).
    Independently computed here — NOT imported from the module under test."""
    rel = container_path[len(RESULT_OUT_MOUNT) :].lstrip("/")
    return str(Path(result_json_path).parent / rel)


def _doc() -> dict:
    return copy.deepcopy(_CANONICAL_DOC)


def _real_result_doc(
    job_id: str,
    verdict: str,
    *,
    metrics: dict | None = None,
    mcap: str | None = _MCAP,
    mp4: str | None = _MP4,
) -> dict[str, Any]:
    """A result.json doc from the ACTUAL runner producer (G-28 anchor)."""
    return build_result_dict(
        job_id,
        verdict,
        [OracleOutcome(name="reached_goal", passed=verdict == "pass")],
        metrics if metrics is not None else _METRICS,
        artifacts=Artifacts(mcap=mcap, mp4=mp4),
    )


def _outcome_with_result(tmp_path: Path, job_id: str, verdict: str, **kw) -> JobOutcome:
    """Write a real-shape result.json to disk and point a JobOutcome at it (the
    shape ``run_job`` hands ``_job_result_of``)."""
    doc = _real_result_doc(job_id, verdict, **kw)
    rdir = tmp_path / job_id.replace("/", "_").replace(":", "_")
    rdir.mkdir(parents=True, exist_ok=True)
    path = write_result(doc, rdir / "result.json")
    exit_code = {"pass": 0, "fail": 1, "error": 3}[verdict]
    return JobOutcome(job_id, path, exit_code, None)


def _job() -> Job:
    (job,) = fan_out(["req-a"], repeats=1)
    return job


# --------------------------------------------------------------------------- #
# G-28 anchor: the fixture IS the real runner emission shape (not hand-crafted)
# --------------------------------------------------------------------------- #


def test_fixture_shape_is_the_real_runner_emission(tmp_path):
    doc = _real_result_doc("job-0001", "pass")
    # same top-level key tree + artifacts sub-keys as the emission golden.
    assert set(doc) == set(GOLDEN_PASS)
    assert set(doc["artifacts"]) == set(GOLDEN_PASS["artifacts"])  # {mcap, mp4}
    # and the bytes on disk are exactly what M3 reads back (sorted, indent=2).
    path = write_result(doc, tmp_path / "result.json")
    assert json.loads(path.read_text(encoding="utf-8")) == doc


# --------------------------------------------------------------------------- #
# (1) real result.json -> fold captures doc+path -> wire emits verbatim
# --------------------------------------------------------------------------- #


def test_fold_captures_result_json_and_wire_emits_metrics_paths_verbatim(tmp_path):
    outcome = _outcome_with_result(tmp_path, "req-a:0", "pass")
    result = _job_result_of(_job(), outcome)

    # captured ADDITIVELY (the whole doc + its host path). The doc is the runner's
    # result.json with its artifact paths hostified (T2 c) — metrics/verdict untouched.
    expected_doc = _real_result_doc(
        "req-a:0",
        "pass",
        mcap=_hostify(_MCAP, outcome.result_path),
        mp4=_hostify(_MP4, outcome.result_path),
    )
    assert result.result_doc == expected_doc
    assert result.result_json_path == str(outcome.result_path)
    # classification is UNCHANGED (verdict outranks the informational exit code).
    assert (result.state, result.verdict) == (JobState.COMPLETED, Verdict.PASS)

    wire = _result_wire(result)
    assert wire["metrics"] == _METRICS  # verbatim, incl. the None value, no key mangling
    # p5c5 T2 (c): mcap/mp4 are now HOST-resolvable — the container /cv/out prefix
    # re-rooted at the host RESULT_OUT dir (result.json's parent). Field names unchanged.
    host_mcap = _hostify(_MCAP, outcome.result_path)
    host_mp4 = _hostify(_MP4, outcome.result_path)
    assert wire["artifacts"] == {"mcap": host_mcap, "mp4": host_mp4}
    assert Path(host_mcap).is_absolute() and Path(host_mp4).is_absolute()
    # frame consistency: all three ride the SAME host result dir as result_json.
    assert Path(host_mcap).parent.parent == Path(outcome.result_path).parent  # /bag/ subdir
    assert Path(host_mp4).parent == Path(outcome.result_path).parent
    assert wire["result_json"] == str(outcome.result_path)  # host RESULT_OUT path
    assert wire["verdict"] == "pass"


def test_fail_result_metrics_and_paths_ride_through(tmp_path):
    outcome = _outcome_with_result(
        tmp_path, "req-a:0", "fail", metrics={"collision_count": 3}, mcap="/cv/out/f.mcap", mp4=None
    )
    wire = _result_wire(_job_result_of(_job(), outcome))
    assert wire["verdict"] == "fail"
    assert wire["metrics"]["collision_count"] == 3
    # mcap hostified (T2 c), mp4 None stays None (정직한 부재 — T1 skips).
    assert wire["artifacts"] == {
        "mcap": _hostify("/cv/out/f.mcap", outcome.result_path),
        "mp4": None,
    }
    assert wire["result_json"] == str(outcome.result_path)


# --------------------------------------------------------------------------- #
# (2) doc 부재 (fake runner) -> byte-identical empty wire (회귀 0 증거)
# --------------------------------------------------------------------------- #


def test_fake_runner_result_keeps_byte_identical_empty_wire():
    job = _job()
    # exactly what FakeRunner / the CPU-skeleton path returns (no doc, no path).
    result = JobResult(job=job, state=JobState.COMPLETED, verdict=Verdict.PASS)
    wire = _result_wire(result)
    # 4-key shape, no result_json ride-along — identical to the pre-p5c3 output the
    # existing 658 suite is green against (optional key absent, not None).
    assert wire == {"job_id": job_key(job), "verdict": "pass", "metrics": {}, "artifacts": {}}
    assert "result_json" not in wire


# --------------------------------------------------------------------------- #
# (3) error/timeout -> verdict 날조 0 (_classify 불변), 캡처는 정보 탑재일 뿐
# --------------------------------------------------------------------------- #


def test_error_result_captured_but_verdict_stays_none(tmp_path):
    outcome = _outcome_with_result(
        tmp_path, "req-a:0", "error", metrics={"collision_count": 1}, mcap="/cv/out/e.mcap"
    )
    result = _job_result_of(_job(), outcome)
    # _classify unchanged: verdict "error" is an infra outcome -> FAILED, verdict-less.
    assert (result.state, result.verdict) == (JobState.FAILED, None)
    wire = _result_wire(result)
    assert wire["verdict"] is None  # never fabricated from the doc's own "error"
    # ...but the doc STILL rides so the failure job's artifacts are uploadable — and the
    # mcap is host-resolvable (T2 c), which is exactly what makes a failure-class job's
    # bag uploadable rather than a dead container path.
    assert wire["metrics"]["collision_count"] == 1
    assert wire["artifacts"]["mcap"] == _hostify("/cv/out/e.mcap", outcome.result_path)
    assert wire["result_json"] == str(outcome.result_path)


def test_timeout_and_collection_violation_are_honest_absence():
    # timeout: run_job never collects a result (result_path is None) -> honest absence.
    timed_out = JobOutcome("req-a:0", None, None, f"{JOB_TIMEOUT_MARKER} runner still running")
    result = _job_result_of(_job(), timed_out)
    assert (result.state, result.verdict) == (JobState.TIMEOUT, None)
    assert result.result_doc is None and result.result_json_path is None
    wire = _result_wire(result)
    assert wire["metrics"] == {} and wire["artifacts"] == {} and "result_json" not in wire


def test_unreadable_collected_result_is_honest_absence(tmp_path):
    # rare: a result.json was collected but is corrupt -> doc None (honest), path kept.
    path = tmp_path / "result.json"
    path.write_text("{ not json", encoding="utf-8")
    assert _read_result_doc(path) is None  # loud-free, honest
    result = _job_result_of(_job(), JobOutcome("req-a:0", path, 0, None))
    assert result.result_doc is None  # unparseable -> empty metrics downstream
    assert result.result_json_path == str(path)  # the file WAS collected
    wire = _result_wire(result)
    assert wire["metrics"] == {} and wire["artifacts"] == {}
    assert wire["result_json"] == str(path)


def test_non_dict_result_is_honest_absence(tmp_path):
    path = tmp_path / "result.json"
    path.write_text('["not", "a", "dict"]', encoding="utf-8")
    assert _read_result_doc(path) is None


# --------------------------------------------------------------------------- #
# (1'/5) E2E through the api: report row real values + restart survival
# --------------------------------------------------------------------------- #


def _real_run_job(spec, out_dir, runner_image, sut_image_ref, docker_client=None, **kwargs):
    """A fake ``run_job`` that writes a REAL-shape result.json per job and returns a
    JobOutcome pointing at it — so the full api stack (fold capture -> _result_wire ->
    build_report) sees real metrics/paths exactly like a live runner would produce."""
    job_id = spec["job_id"]
    doc = _real_result_doc(job_id, "pass")
    rdir = Path(out_dir) / job_id.replace("/", "_").replace(":", "_")
    rdir.mkdir(parents=True, exist_ok=True)
    path = write_result(doc, rdir / "result.json")
    return JobOutcome(job_id, path, 0, None)


def _real_runner(tmp_path: Path) -> RunJobRunner:
    return RunJobRunner(
        out_dir=tmp_path / "out", runner_image="runner:test", run_job_fn=_real_run_job
    )


def _submit(client: TestClient, documents: list[dict], **body_extra) -> str:
    response = client.post("/envelopes", json={"requests": documents, **body_extra})
    assert response.status_code == 202, response.text
    return response.json()["envelope_id"]


def _wait_report(client: TestClient, envelope_id: str, timeout_s: float = 10.0) -> dict:
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        if client.get(f"/envelopes/{envelope_id}").json()["status"] == "completed":
            return client.get(f"/envelopes/{envelope_id}/report").json()
        time.sleep(0.02)
    raise AssertionError(f"envelope {envelope_id} did not complete within {timeout_s}s")


def test_report_row_carries_real_metrics_and_artifact_paths(tmp_path):
    with Store(tmp_path / "cv.sqlite3") as store:
        app = create_app(store, _real_runner(tmp_path), k=2)
        with TestClient(app) as client:
            report = _wait_report(client, _submit(client, [_doc()]))
    (row,) = report["matrix"]
    # metrics are the result.json map VERBATIM (재계산·키 가공 0) — the empty {} placeholder
    # from the fake-runner report-seam test is now the real declared metrics.
    assert row["metrics"] == _METRICS
    (selected,) = row["artifacts"]["selected"]
    assert selected["role"] == "representative-pass"
    # p5c5 T2 (c): the report row's mcap/mp4 are HOST-resolvable (rewritten from the
    # runner's /cv/out container frame), consistent with result_json — so T1 can stage
    # them. All three share the same host result dir; the container /cv/out is GONE.
    assert selected["rosbag_mcap"] == _hostify(_MCAP, selected["result_json"])
    assert selected["recording_mp4"] == _hostify(_MP4, selected["result_json"])
    for path in (selected["rosbag_mcap"], selected["recording_mp4"], selected["result_json"]):
        assert Path(path).is_absolute()
        assert not path.startswith(RESULT_OUT_MOUNT)  # no container-frame leak
    # result_json is a real host path to an on-disk result.json (실 경로 verbatim).
    assert Path(selected["result_json"]).name == "result.json"
    assert Path(selected["result_json"]).is_file()


def test_captured_real_values_survive_orchestrator_restart(tmp_path):
    db = tmp_path / "cv.sqlite3"
    with Store(db) as store:
        app = create_app(store, _real_runner(tmp_path), k=1)
        with TestClient(app) as client:
            envelope_id = _submit(client, [_doc()])
            before = _wait_report(client, envelope_id)
    assert before["matrix"][0]["metrics"] == _METRICS  # real values were persisted
    # Full restart: a NEW store + app that never saw the envelope serves the SAME
    # report (store v7 durable twin) — the captured real values ride through unchanged.
    with Store(db) as reopened:
        restarted = create_app(reopened, FakeRunner(), k=1)
        with TestClient(restarted) as client:
            response = client.get(f"/envelopes/{envelope_id}/report")
            assert response.status_code == 200
            assert response.json() == before  # byte-identical durable twin


# --------------------------------------------------------------------------- #
# (4) trigger_source: absent -> human-manual · ci-cd verbatim · illegal -> 422
# --------------------------------------------------------------------------- #


def test_default_is_a_legal_m1_trigger_source():
    # non-drift: the default is one of the values derived from the M1 Literal.
    assert _DEFAULT_TRIGGER_SOURCE == "human-manual"
    assert _DEFAULT_TRIGGER_SOURCE in _TRIGGER_SOURCES
    assert set(_TRIGGER_SOURCES) == {"human-manual", "ci-cd"}


def test_trigger_source_absent_records_human_manual(tmp_path):
    with Store(tmp_path / "cv.sqlite3") as store:
        app = create_app(store, _real_runner(tmp_path), k=1)
        with TestClient(app) as client:
            report = _wait_report(client, _submit(client, [_doc()]))
    assert report["trigger_source"] == "human-manual"


def test_trigger_source_ci_cd_recorded_verbatim(tmp_path):
    with Store(tmp_path / "cv.sqlite3") as store:
        app = create_app(store, _real_runner(tmp_path), k=1)
        with TestClient(app) as client:
            report = _wait_report(client, _submit(client, [_doc()], trigger_source="ci-cd"))
    assert report["trigger_source"] == "ci-cd"  # submitted value wins, verbatim


def test_explicit_null_trigger_source_falls_back_to_default(tmp_path):
    with Store(tmp_path / "cv.sqlite3") as store:
        app = create_app(store, _real_runner(tmp_path), k=1)
        with TestClient(app) as client:
            report = _wait_report(client, _submit(client, [_doc()], trigger_source=None))
    assert report["trigger_source"] == "human-manual"


def test_illegal_trigger_source_is_structured_422_eight_key(tmp_path):
    with Store(tmp_path / "cv.sqlite3") as store:
        app = create_app(store, FakeRunner(), k=1)
        with TestClient(app) as client:
            response = client.post(
                "/envelopes", json={"requests": [_doc()], "trigger_source": "jenkins"}
            )
            assert response.status_code == 422  # never a 500, never a raw traceback
            assert "Traceback" not in response.text
            (error,) = response.json()["detail"]["errors"]
            assert set(error) == set(ANNOTATION_KEYS)  # the M1 8-key annotation shape
            assert error["field_path"] == "trigger_source"
            assert "jenkins" in error["got"]
            # all-or-nothing: an illegal wrapper key created zero jobs (비전파).
            assert store.load_jobs() == []
