"""Deployment self-test EXECUTION-PLANE tests (M7 §3.1/§3.2 on M3) — CPU only.

Gate mapping (``DoD-P5-07`` / REQ-SELFTEST-001/002/004 · NFR-SELFTEST-001):

1. **built-in stub input** — the stub request is supplied BY THE PACKAGE, admits
   through the REAL M1 gate (no contract bypass), and never borrows a consumer
   image/file; an unconfigured stub SUT handle refuses LOUDLY instead of guessing
   (``NFR-SELFTEST-001``: 외부 SUT 0 의존).
2. **size-1 self-test envelope** — exactly one request, both M1 envelope markers,
   no host path on the wire.
3. **★ 실행 로직 복제 없음** (the binding constraint of REQ-SELFTEST-002) — asserted
   TWICE, from opposite directions: a RUNTIME spy proves an ordinary submission and
   the self-test traverse the SAME shared producers, and a STRUCTURAL check proves the
   self-test module imports no execution-plane code at all (with a positive control,
   G-35 — the same collector DOES find those imports in ``api.py``).
4. **operational surfacing** (REQ-SELFTEST-004) — the marker reaches the M6
   projection + the HTML dashboard, survives an orchestrator restart, and carries no
   domain value with it (the NEG-3 boundary is untouched — a bool, not free text).
5. **R-shm** (LOCKED, M5 §8) — the runner container gets a sized ``/dev/shm``, the
   SUT keeps the docker default, the knob can be turned OFF back to the pre-p5c15
   call shape, and the effective value is logged + env-overridable.

The GPU round-trip (Isaac 기동 -> 러너 실행 -> result.json, REQ-SELFTEST-003) is NOT
claimed here: these tests use fake runners/docker. Everything below is about the
control plane carrying a self-test the same way it carries real work.
"""

from __future__ import annotations

import ast
import io
import json
import time
from pathlib import Path

import pytest
import yaml
from fastapi.testclient import TestClient

from cv_infra.contract.loader import load_request
from cv_infra.contract.schema import VerificationRequest
from cv_infra.orchestrator import api, selftest, serve
from cv_infra.orchestrator.models import Job, JobResult, JobState, Verdict
from cv_infra.orchestrator.monitor import build_operational_record, render_dashboard_html
from cv_infra.orchestrator.selftest import (
    SELF_TEST_ORIGIN,
    SUT_IMAGE_ENV,
    SelfTestNotConfigured,
    build_self_test_submission,
)
from cv_infra.orchestrator.store import Store
from cv_infra.orchestrator.supervisor import DEFAULT_RUNNER_SHM_SIZE, RunJobRunner, run_job
from cv_infra.runner.evaluate import read_field
from cv_infra.runner.main import criteria_view
from cv_infra.runner.telemetry import PhysicsTelemetrySampler
from tests.test_supervisor_min import RUNNER_IMAGE, SUT_IMAGE, FakeClient, make_spec, put_result

#: A PLATFORM-internal stub SUT handle for the CPU tests (the real one is a deployment
#: artifact built from ``docker/selftest_stub/``). Deliberately NOT ``carter-sut:*``.
STUB_SUT = "cv-infra-selftest-stub:test"

_STUB_ENV = {SUT_IMAGE_ENV: STUB_SUT}

#: The canonical CONSUMER scenario the platform copies for its own tests — the thing a
#: self-test must never depend on (boundary rule ③ / NFR-SELFTEST-001).
_CONSUMER_FIXTURE = Path(__file__).parent / "fixtures" / "nova_carter_warehouse_goal.yaml"

_REPO_ROOT = Path(__file__).resolve().parents[1]

#: Where the stub SUT image COMES FROM (M7 §3.5 option B, built in p5c15) — the
#: destination an unconfigured deployment must be sent to.
_STUB_RECIPE = "docker/selftest_stub/README.md"


class _PassRunner:
    """Fake runner seam: every job completes PASS (CPU, no docker/GPU)."""

    def __init__(self) -> None:
        self.jobs: list[Job] = []

    def run(self, job: Job) -> JobResult:
        self.jobs.append(job)
        return JobResult(job=job, state=JobState.COMPLETED, verdict=Verdict.PASS)


def _wait_completed(client: TestClient, envelope_id: str, timeout_s: float = 10.0) -> dict:
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        body = client.get(f"/envelopes/{envelope_id}").json()
        if body["status"] == "completed":
            return body
        time.sleep(0.02)
    raise AssertionError(f"envelope {envelope_id} never completed")


def _submit(client: TestClient, body: dict) -> str:
    response = client.post("/envelopes", json=body)
    assert response.status_code == 202, response.text
    return response.json()["envelope_id"]


def _admitted(document: dict) -> VerificationRequest:
    """A stub document through the REAL M1 admit gate -> the typed request.

    Same 6-stage loader the server runs, so anything asserted downstream of it is
    asserted on what the execution plane actually receives (no shortcut model build).
    """
    return load_request(
        io.StringIO(json.dumps(document, indent=2, sort_keys=True)),
        source_path="built-in-stub",
    ).request


def _ordinary_body() -> dict:
    """An ORDINARY submission of the same stub document (the contrast group).

    Same document, no markers — so every difference observed below is the self-test
    marking itself, never a different request.
    """
    submission = build_self_test_submission(environ=_STUB_ENV)
    return {"requests": submission.body["requests"]}


# --------------------------------------------------------------------------- #
# (1) built-in stub input — REQ-SELFTEST-001 / NFR-SELFTEST-001
# --------------------------------------------------------------------------- #


def test_built_in_stub_admits_through_the_real_m1_gate():
    """The stub is a DOCUMENT, admitted by the same 6-stage gate as user input.

    A stub that could only be run by bypassing admit would prove nothing about the
    path a real request takes (REQ-SELFTEST-002 경로 동일성).
    """
    (document,) = build_self_test_submission(environ=_STUB_ENV).body["requests"]
    admitted = load_request(
        io.StringIO(json.dumps(document, indent=2, sort_keys=True)),
        source_path="built-in-stub",
    )
    # Both MVP oracles bind through the real stage-5 loader — no_collision is not
    # decoration: it is the only home of the chassis_path the runner's telemetry bind
    # demands (see the dedicated test below), and its params survive extra=forbid.
    assert admitted.oracles == ("reached_goal", "no_collision")
    assert admitted.request.sut.image_ref == STUB_SUT
    # 크기 1 봉투 × 기본 repeats=1 => k=1 single runner (M7 §3.2 footprint).
    assert admitted.request.execution_settings.repeats == 1
    # The trivial criterion is STRUCTURAL: the robot is spawned AT the goal, so the
    # judgement needs no SUT motion and no typed threshold (M7 §3.3).
    scenario = admitted.request.scenario
    assert scenario.initial_pose is not None
    assert (scenario.initial_pose.x, scenario.initial_pose.y) == (scenario.goal.x, scenario.goal.y)
    assert admitted.request.acceptance_criteria[0].params.position_tolerance_m is None


def test_stub_supplies_the_chassis_path_the_telemetry_bind_demands():
    """★ 라이브 차단 회귀: the runner builds the physics telemetry sampler and binds it
    PRE-reset for EVERY job — ``runner/main.py`` never asks whether a collision
    criterion was declared — and ``telemetry.bind()`` raises when it holds no
    ``chassis_path``. A stub declaring ``reached_goal`` alone therefore dies BEFORE the
    SUT readiness barrier, i.e. before the round-trip can prove anything.

    Asserted along the runner's OWN read chain (``criteria_view`` -> ``read_field`` with
    main.py's ``""`` default -> the sampler attribute the raise predicate tests), so the
    regression reddens WITHOUT a GPU. That is the point: this failure was visible only
    on the workstation until now.
    """
    (document,) = build_self_test_submission(environ=_STUB_ENV).body["requests"]
    view = criteria_view(_admitted(document))
    sampler = PhysicsTelemetrySampler(  # main.py's construction, verbatim
        read_field(view, "chassis_path", ""),
        read_field(view, "collision_excluded_paths", []) or [],
    )
    assert sampler.chassis_path, "stub would raise in telemetry.bind() before readiness"

    # 비공허 대조 (G-35): drop the collision criterion and the empty default — the exact
    # value the raise predicate rejects — comes back. So the assert above is sensitive
    # to this one input, not to something that would hold either way.
    stripped = {
        **document,
        "acceptance_criteria": [
            c for c in document["acceptance_criteria"] if c["oracle"] != "no_collision"
        ],
    }
    assert stripped["acceptance_criteria"] == [{"oracle": "reached_goal"}]
    assert read_field(criteria_view(_admitted(stripped)), "chassis_path", "") == ""


def test_stub_collision_prim_paths_are_the_measured_ones_not_invented():
    """G-64 / CLAUDE §2-4: prim paths cannot be guessed — a WRONG ``chassis_path`` dies
    in ``bind()`` ("not found in the stage") exactly as loudly as a missing one, and CPU
    cannot check a path against a live stage. The one CPU-checkable provenance is that
    the stub reuses the workstation-MEASURED paths whose source of truth is the platform
    fixture; the measurement transfers only because the stub names the SAME scene and
    robot (asserted here), which resolve to the same asset + robot prim.

    Re-syncing the fixture with a new measurement reddens this — the intended coupling:
    the stub must then be re-checked too.
    """
    fixture = yaml.safe_load(_CONSUMER_FIXTURE.read_text(encoding="utf-8"))
    (document,) = build_self_test_submission(environ=_STUB_ENV).body["requests"]
    assert fixture["scenario"]["scene"] == document["scenario"]["scene"]
    assert fixture["scenario"]["robot"] == document["scenario"]["robot"]

    (measured,) = [
        c["params"] for c in fixture["acceptance_criteria"] if c["oracle"] == "no_collision"
    ]
    (stub_params,) = [
        c["params"] for c in document["acceptance_criteria"] if c["oracle"] == "no_collision"
    ]
    assert stub_params["chassis_path"] == measured["chassis_path"]
    # The exclusions are load-bearing, not decoration: the parked robot rests ON the
    # floor, so unfiltered chassis contacts (self-body subtree + ground plane — the
    # measured partners) would false-FAIL a motionless stub.
    assert stub_params["collision_excluded_paths"] == measured["collision_excluded_paths"]
    assert stub_params["collision_excluded_paths"]

    # ...and each built document OWNS its params (the module constant is a nested
    # mutable — a shallow copy would let one submission's edit ride into the next).
    stub_params["collision_excluded_paths"].append("/mutated")
    (nxt,) = build_self_test_submission(environ=_STUB_ENV).body["requests"]
    (next_params,) = [
        c["params"] for c in nxt["acceptance_criteria"] if c["oracle"] == "no_collision"
    ]
    assert "/mutated" not in next_params["collision_excluded_paths"]


def test_stub_origin_marker_is_the_requirement_string_verbatim():
    """REQ-SELFTEST-001 names the value: ``origin=built-in-stub`` (문자열 그대로)."""
    assert SELF_TEST_ORIGIN == "built-in-stub"


def test_stub_sut_handle_is_never_guessed(tmp_path, monkeypatch):
    """No configured stub handle => LOUD refusal, never a fabricated image ref.

    ``NFR-SELFTEST-001`` is about what the self-test may DEPEND on; a guessed ref
    would turn "the platform is not configured" into "the round-trip failed", the
    exact confusion the image-as-artifact policy (FU-10) exists to prevent.
    """
    monkeypatch.chdir(tmp_path)  # no repo, no scenario file, no consumer checkout
    with pytest.raises(SelfTestNotConfigured) as exc:
        build_self_test_submission(environ={})
    assert SUT_IMAGE_ENV in str(exc.value)

    # set-but-empty is not "unset" and never becomes an empty ref (G-26)
    with pytest.raises(SelfTestNotConfigured):
        build_self_test_submission(environ={SUT_IMAGE_ENV: "   "})

    # env resolves; an explicit argument WINS over it (documented priority)
    assert build_self_test_submission(environ=_STUB_ENV).sut_image_ref == STUB_SUT
    submission = build_self_test_submission(sut_image_ref="other:1", environ=_STUB_ENV)
    assert submission.sut_image_ref == "other:1"
    assert submission.body["requests"][0]["sut"]["image_ref"] == "other:1"


def test_refusal_points_at_the_stub_build_recipe(tmp_path, monkeypatch):
    """A refusal must be ACTIONABLE — and this one went stale for two cycles.

    Its last sentence used to call the stub image "the open M7 §3.5 A/B decision".
    That decision landed on option B in p5c15 and the image was BUILT
    (``docker/selftest_stub/``), so the one surface that mentions the stub told an
    operator to WAIT where the truth was BUILD. Nothing pinned the message, so the
    suite stayed green while it rotted.

    Pinned = the two load-bearing facts, not the prose: the refusal fires for BOTH
    resolution failures, and it names a build recipe that actually exists.
    """
    monkeypatch.chdir(tmp_path)  # no repo checkout under the CWD either
    for environ in ({}, {SUT_IMAGE_ENV: "   "}):  # unset / set-but-empty (G-26)
        with pytest.raises(SelfTestNotConfigured) as exc:
            build_self_test_submission(environ=environ)
        message = str(exc.value)
        assert SUT_IMAGE_ENV in message  # which knob carries the ref...
        assert _STUB_RECIPE in message  # ...and where a deployment gets a value for it

    # 비공허: a pointer at something that does not exist is a NEW dead end, so the
    # recipe the message names is asserted to be a real artifact of this tree.
    assert (_REPO_ROOT / _STUB_RECIPE).is_file()
    assert (_REPO_ROOT / "docker/selftest_stub/Dockerfile").is_file()


def test_stub_needs_no_consumer_asset_at_all(tmp_path, monkeypatch):
    """비공허 대조: the consumer fixture EXISTS and carries a real SUT ref — and the
    stub references neither it nor any other consumer artifact.

    The stub is built with the working directory somewhere empty, so a hidden file
    read would raise rather than silently succeed.
    """
    consumer_sut = "carter-sut:p2"
    assert consumer_sut in _CONSUMER_FIXTURE.read_text(encoding="utf-8")  # 대조군 실재

    monkeypatch.chdir(tmp_path)
    body = build_self_test_submission(environ=_STUB_ENV).body
    wire = json.dumps(body)
    assert consumer_sut not in wire
    assert consumer_sut not in Path(selftest.__file__).read_text(encoding="utf-8")
    # G-69: the self-test sends NO host path (the control plane may be containerized;
    # a client-side path would become a deployment constraint). Entry-point oracles
    # need no stage-5 anchor, so the key is ABSENT, not null-filled.
    assert "oracle_plugin_dirs" not in body


# --------------------------------------------------------------------------- #
# (2) size-1 self-test envelope — REQ-SELFTEST-002
# --------------------------------------------------------------------------- #


def test_self_test_envelope_is_size_one_and_carries_both_markers():
    body = build_self_test_submission(environ=_STUB_ENV).body
    assert len(body["requests"]) == 1  # 크기 1 (REQ-SELFTEST-002)
    assert body["is_self_test"] is True
    assert body["origin"] == SELF_TEST_ORIGIN
    assert "trigger_source" not in body  # absent => the server's documented default


def test_trigger_source_rides_only_when_the_caller_declares_one():
    """Provenance (REQ-INTAKE-003) stays the caller's decision — a platform CI
    self-test records ci-cd, an operator's records nothing (server default)."""
    body = build_self_test_submission(environ=_STUB_ENV, trigger_source="ci-cd").body
    assert body["trigger_source"] == "ci-cd"


def test_submitted_self_test_fans_out_to_exactly_one_job(tmp_path):
    with Store(tmp_path / "cv.sqlite3") as store:
        app = api.create_app(store, _PassRunner(), k=2)
        with TestClient(app) as client:
            envelope_id = _submit(client, build_self_test_submission(environ=_STUB_ENV).body)
            body = _wait_completed(client, envelope_id)
    assert len(body["jobs"]) == 1  # 봉투 1 × repeats 1
    assert body["jobs"][0]["state"] == "completed"
    assert body["report_outcome"] == "pass"


@pytest.mark.parametrize(
    "markers",
    [
        {"is_self_test": "yes"},  # not a bool
        {"origin": 7},  # not a string
    ],
)
def test_malformed_markers_are_a_structured_422_not_a_silent_coercion(tmp_path, markers):
    """Provenance that silently coerces is worse than none — the wrapper rejects
    with the same 8-key contract-error shape the admit gate uses."""
    with Store(tmp_path / "cv.sqlite3") as store:
        app = api.create_app(store, _PassRunner(), k=1)
        with TestClient(app) as client:
            response = client.post("/envelopes", json={**_ordinary_body(), **markers})
    assert response.status_code == 422
    (error,) = response.json()["detail"]["errors"]
    assert error["field_path"] in markers


# --------------------------------------------------------------------------- #
# (3) ★ 실행 로직 복제 없음 — the same path, proven twice
# --------------------------------------------------------------------------- #


def _imported_modules(path: Path) -> set[str]:
    """Every module name imported by a source file (ast — no execution)."""
    names: set[str] = set()
    for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
        if isinstance(node, ast.Import):
            names |= {alias.name for alias in node.names}
        elif isinstance(node, ast.ImportFrom) and node.module:
            names.add(node.module)
            names |= {f"{node.module}.{alias.name}" for alias in node.names}
    return names


def test_self_test_module_holds_no_execution_logic():
    """STRUCTURAL half of "실행 로직 복제 없음": the self-test module is INPUT ONLY.

    It may not import (nor therefore re-implement) any part of the execution plane —
    fan-out, queue, scheduler, supervisor, allocator, rollup, store or the API. If a
    future edit grows a private submit/drive path inside it, this reddens.

    Positive control (G-35): the SAME collector applied to ``api.py`` DOES find those
    imports — so "zero matches" above means absence, not a blind collector.
    """
    plane = {
        "cv_infra.orchestrator.api",
        "cv_infra.orchestrator.fanout",
        "cv_infra.orchestrator.queue",
        "cv_infra.orchestrator.scheduler",
        "cv_infra.orchestrator.supervisor",
        "cv_infra.orchestrator.allocator",
        "cv_infra.orchestrator.rollup",
        "cv_infra.orchestrator.store",
        "cv_infra.orchestrator.monitor",
    }
    imported = _imported_modules(Path(selftest.__file__))
    assert imported & plane == set(), f"self-test module reaches into: {sorted(imported & plane)}"
    assert not any(name.startswith("httpx") for name in imported)  # nor its own client

    control = _imported_modules(Path(api.__file__)) & plane
    assert len(control) >= 5, f"collector saw only {sorted(control)} in api.py — blind?"


def test_self_test_and_ordinary_submission_traverse_the_same_producers(tmp_path, monkeypatch):
    """RUNTIME half: ONE fan-out producer, ONE job-spec producer, ONE runner seam
    serve both submissions — observed by spies on the SHARED call sites.

    If the self-test ever grew its own path, the spies would see it only once (or
    with a different job shape), which is precisely what the assertion forbids.
    """
    fan_out_calls: list[tuple] = []
    spec_calls: list[str] = []
    real_fan_out, real_spec_for = api.fan_out_requests, api._job_spec_for

    def spy_fan_out(pairs):
        pairs = list(pairs)
        fan_out_calls.append(tuple(pairs))
        return real_fan_out(pairs)

    def spy_spec_for(request, job_id):
        spec_calls.append(job_id)
        return real_spec_for(request, job_id)

    monkeypatch.setattr(api, "fan_out_requests", spy_fan_out)
    monkeypatch.setattr(api, "_job_spec_for", spy_spec_for)

    runner = _PassRunner()
    with Store(tmp_path / "cv.sqlite3") as store:
        app = api.create_app(store, runner, k=2)
        with TestClient(app) as client:
            ordinary_id = _submit(client, _ordinary_body())
            _wait_completed(client, ordinary_id)
            self_test_id = _submit(client, build_self_test_submission(environ=_STUB_ENV).body)
            _wait_completed(client, self_test_id)

    # Both submissions went through the SAME shared producers, once each.
    assert len(fan_out_calls) == 2 and all(len(call) == 1 for call in fan_out_calls)
    assert len(spec_calls) == 2
    assert {job_id.split("/")[0] for job_id in spec_calls} == {ordinary_id, self_test_id}
    # ... and the SAME runner seam ran both jobs with byte-identical job specs (the
    # documents are identical — only the envelope markers differ).
    assert len(runner.jobs) == 2
    ordinary_job, self_test_job = runner.jobs
    assert ordinary_job.job_spec is not None
    assert {**ordinary_job.job_spec, "job_id": ""} == {**self_test_job.job_spec, "job_id": ""}


# --------------------------------------------------------------------------- #
# (4) operational surfacing — REQ-SELFTEST-004 (↔ REQ-MONITOR-003)
# --------------------------------------------------------------------------- #


def test_self_test_is_identifiable_on_the_operational_view_and_survives_restart(tmp_path):
    """The operator sees WHICH envelope was the deployment self-test — on the JSON
    projection and on the one-glance HTML, from the SAME store row.

    비공허 대조: an ordinary submission of the SAME document sits next to it reading
    false, so the marker is carrying information rather than being always-true.
    """
    database = tmp_path / "cv.sqlite3"
    with Store(database) as store:
        app = api.create_app(store, _PassRunner(), k=2)
        with TestClient(app) as client:
            ordinary_id = _submit(client, _ordinary_body())
            _wait_completed(client, ordinary_id)
            self_test_id = _submit(client, build_self_test_submission(environ=_STUB_ENV).body)
            _wait_completed(client, self_test_id)
            raw_json = client.get("/monitor.json").text
            raw_html = client.get("/monitor").text

    body = json.loads(raw_json)
    marked = {req["envelope_id"]: req["is_self_test"] for req in body["requests"]}
    assert marked == {self_test_id: True, ordinary_id: False}
    outcomes = {req["envelope_id"]: req["report_outcome"] for req in body["requests"]}
    assert outcomes[self_test_id] == "pass"  # the round-trip result IS on the view
    assert "<th>self_test</th>" in raw_html and "True" in raw_html

    # Store-backed, so a restarted orchestrator still tells self-test from user work
    # (the projection reads envelopes, and the process that submitted them is gone).
    with Store(database) as restarted:
        record = build_operational_record(restarted)
    assert {req.envelope_id: req.is_self_test for req in record.requests} == marked
    assert "<th>self_test</th>" in render_dashboard_html(record)


def test_self_test_surfacing_leaks_no_domain_value(tmp_path):
    """The self-test introduces a NEW document source — re-assert the M6 boundary on
    it (the NEG-3 suite covers the consumer fixture; this covers the stub).

    Positive control: the stub's own scene/robot/SUT strings ARE in the store, and
    still none of them reaches either operational surface. The free-text sibling
    ``origin`` deliberately stays off the view — only the bool crosses.
    """
    with Store(tmp_path / "cv.sqlite3") as store:
        app = api.create_app(store, _PassRunner(), k=1)
        with TestClient(app) as client:
            envelope_id = _submit(client, build_self_test_submission(environ=_STUB_ENV).body)
            _wait_completed(client, envelope_id)
            raw_json = client.get("/monitor.json").text
            raw_html = client.get("/monitor").text

        spec_text = json.dumps([job.job_spec for job in store.load_jobs()])
        stub = build_self_test_submission(environ=_STUB_ENV).body["requests"][0]
        # The criteria params are domain values too (G-59 ④: the guard's input grows
        # with the contract) — the chassis prim path joined the stub in p5c15.
        (collision,) = [c for c in stub["acceptance_criteria"] if c["oracle"] == "no_collision"]
        domain_values = (
            stub["scenario"]["scene"],
            stub["scenario"]["robot"],
            STUB_SUT,
            collision["params"]["chassis_path"],
        )
        for value in domain_values:
            assert value in spec_text, f"양성 대조 실패 — store에 {value!r}가 없다"

        # ... and the store DID record the audit-side origin (M7-DoD-3 출처 식별).
        assert store.load_envelope(envelope_id).origin == SELF_TEST_ORIGIN

    for value in (*domain_values, SELF_TEST_ORIGIN):
        assert value not in raw_json, f"operational leak: {value!r}"
        assert value not in raw_html, f"operational leak: {value!r}"


# --------------------------------------------------------------------------- #
# (5) R-shm — runner /dev/shm sizing (LOCKED risk, M5 §8)
# --------------------------------------------------------------------------- #


def test_runner_gets_a_sized_dev_shm_and_the_sut_keeps_the_docker_default(tmp_path):
    """Kit needs more than docker's 64m default; the SUT is not a Kit process.

    The value itself is ``DEFAULT_RUNNER_SHM_SIZE`` (its measured in-repo source is
    documented on the constant) — asserted through the constant, never re-typed here.
    """
    put_result(tmp_path)
    client = FakeClient()
    run_job(make_spec(), tmp_path, RUNNER_IMAGE, SUT_IMAGE, client, poll_interval_s=0.0)
    (_, runner_kwargs), (_, sut_kwargs) = client.run_calls
    assert runner_kwargs["shm_size"] == DEFAULT_RUNNER_SHM_SIZE
    assert "shm_size" not in sut_kwargs  # blackbox SUT: unchanged docker default
    assert isinstance(DEFAULT_RUNNER_SHM_SIZE, str) and DEFAULT_RUNNER_SHM_SIZE.strip()


def test_shm_knob_off_restores_the_pre_p5c15_call_shape(tmp_path):
    """``None`` passes NOTHING to docker — the knob is as reversible as it is tunable."""
    put_result(tmp_path)
    client = FakeClient()
    run_job(
        make_spec(), tmp_path, RUNNER_IMAGE, SUT_IMAGE, client, poll_interval_s=0.0, shm_size=None
    )
    (_, runner_kwargs), _ = client.run_calls
    assert "shm_size" not in runner_kwargs


def test_spawn_log_records_the_effective_shm_size(tmp_path, capsys):
    """G-26 feature-on gate: an operator (and Wave-C evidence) can read the value
    that was actually applied off ONE structured line, per job."""
    put_result(tmp_path)
    run_job(
        make_spec(),
        tmp_path,
        RUNNER_IMAGE,
        SUT_IMAGE,
        FakeClient(),
        poll_interval_s=0.0,
        shm_size="512m",
    )
    (line,) = [
        ln
        for ln in capsys.readouterr().err.splitlines()
        if ln.startswith("[cv-supervisor] runner-mounts ")
    ]
    assert json.loads(line.removeprefix("[cv-supervisor] runner-mounts "))["shm_size"] == "512m"


def test_run_job_runner_hands_its_shm_size_to_every_job():
    """One construction-time source (the production seam), not a per-call literal."""
    seen: list[object] = []

    def fake_run_job(*args, **kwargs):
        seen.append(kwargs["shm_size"])
        return None

    runner = RunJobRunner(
        out_dir="/out", runner_image=RUNNER_IMAGE, shm_size="2g", run_job_fn=fake_run_job
    )
    assert runner.shm_size == "2g"
    with pytest.raises(AttributeError):  # fake outcome -> the fold raises; the call happened
        runner.run(Job(request_id="r0", repeat_index=0, job_spec=make_spec()))
    assert seen == ["2g"]


def test_serve_env_knob_overrides_the_default_and_is_logged(tmp_path, capsys):
    """``CV_RUNNER_SHM_SIZE`` is the operator's dial (unset = documented default),
    and the boot line carries whichever value took effect."""
    base = {
        "CV_STORE_PATH": str(tmp_path / "cv.sqlite3"),
        "CV_OUT_DIR": str(tmp_path / "out"),
        "CV_RUNNER_IMAGE": RUNNER_IMAGE,
        "CV_MAX_CONCURRENT": "2",
    }
    assert serve.config_from_env(dict(base)).runner_shm_size == DEFAULT_RUNNER_SHM_SIZE
    config = serve.config_from_env({**base, "CV_RUNNER_SHM_SIZE": "4g"})
    assert config.runner_shm_size == "4g"
    with pytest.raises(ValueError):  # set-but-empty is loud, never silently 'unset' (G-26)
        serve.config_from_env({**base, "CV_RUNNER_SHM_SIZE": ""})

    serve.build_app(config)
    (line,) = [
        ln
        for ln in capsys.readouterr().err.splitlines()
        if ln.startswith("[cv-orchestrator] serve-config ")
    ]
    assert json.loads(line.removeprefix("[cv-orchestrator] serve-config "))["runner_shm_size"] == (
        "4g"
    )
