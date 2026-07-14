"""M3 REST->실 러너 프로덕션 글루 테스트 (p4c4 T1.5, T1 report §7-1 (a)/(b)/(c)).

CPU-only, fakes are all INJECTED duck-typed seams (G-20 — no module stubs):

* (a) job_spec 승차 — the ADMITTED model materializes into the canonical
  per-job JOB_SPEC (``api._job_spec_for``) riding + persisting with every
  fanned-out job; mechanical parity guard against the M8 producer
  (``cli/main._job_spec_from_request`` — the G-25 anchor named in the builder).
* (b) ``RunJobRunner`` — the frozen ``run_job`` contract driven off
  ``Job.job_spec``: JobOutcome->JobResult fold (verdict outranks exit code;
  watchdog marker -> TIMEOUT), ``oracle_plugin_dir`` hand-off, and the
  duck-typed ``job_timeout_s`` declaration == the value run_job receives.
* (c) ``serve`` boot glue — env contract (loud missing/empty/non-numeric),
  compute_k wiring, boot-time ``reconcile_at_restart``, and the ONE structured
  ``serve-config`` stderr line (G-26 feature-on; consent = key NAMES only).
* (e) E2E: REAL ``load_envelope`` -> ``cv-infra submit --wait`` -> env-built
  production app -> fake ``run_job`` (pass/fail/timeout/crash mix) -> rollup ->
  status/report_outcome 왕복, over a ``ProcessBoundaryTransport`` (관용구 출처:
  tests/test_cli_batch.py — in-process E2E의 공허-통과 차단, G-28).
* (f) 실패 관측성 (p4c5): the runner's container exit code + the infra reason
  survive the fold, the store (v4) and a fresh process's status read — 137
  (OOM-kill) vs 139 (segfault) vs a plain non-zero exit are distinguishable from
  the API alone (p4c4 발견 ②가 막혔던 지점). NEG: the reason is a bounded
  single-line breadcrumb — no stderr dump, no consent value, no domain detail.
"""

from __future__ import annotations

import copy
import io
import json
import sys
import time
from pathlib import Path

import httpx
import pytest
import yaml
from fastapi.testclient import TestClient

from cv_infra.cli import batch
from cv_infra.cli.main import EXIT_INFRA, EXIT_PASS, main
from cv_infra.cli.main import _job_spec_from_request as m8_job_spec_from_request
from cv_infra.contract.loader import load_request
from cv_infra.orchestrator import serve
from cv_infra.orchestrator.api import _job_spec_for, create_app
from cv_infra.orchestrator.fanout import fan_out
from cv_infra.orchestrator.models import Job, JobResult, JobState, Verdict
from cv_infra.orchestrator.queue import JobQueue
from cv_infra.orchestrator.scheduler import SlotAccountant
from cv_infra.orchestrator.store import Store, job_key
from cv_infra.orchestrator.supervisor import (
    _REASON_MAX_CHARS,  # NEG 가드: 사유 문자열 상한 (테스트가 상수를 재타이핑하지 않게)
    _TRUNCATION_SUFFIX,
    DEFAULT_JOB_TIMEOUT_S,
    JOB_TIMEOUT_MARKER,
    JobOutcome,
    ParallelSupervisor,
    RunJobRunner,
)
from tests.test_supervisor_min import FakeClient, run_min

# Canonical M1-valid request document (platform fixture — drift-guarded by
# test_fixture_canonical_guard.py).
_FIXTURE = Path(__file__).parent / "fixtures" / "nova_carter_warehouse_goal.yaml"
_CANONICAL_TEXT = _FIXTURE.read_text(encoding="utf-8")
_CANONICAL_DOC = yaml.safe_load(_CANONICAL_TEXT)

# The EXISTING runner-side plugin fixture dir stands in for a consumer scenario
# dir (test_orchestrator_api section d idiom).
_PLUGIN_DIR = str(Path(__file__).parent / "fixtures")
_PLUGIN_MODULE = "custom_oracle_plugin"


def _request_doc() -> dict:
    return copy.deepcopy(_CANONICAL_DOC)


def _admit(doc: dict, plugin_dir: str | None = None):
    stream = io.StringIO(json.dumps(doc, indent=2, sort_keys=True))
    return load_request(stream, source_path="test-doc", plugin_dir=plugin_dir)


# --------------------------------------------------------------------------- #
# (a) canonical JOB_SPEC builder: parity with the M8 producer (G-25 guard)
# --------------------------------------------------------------------------- #


@pytest.fixture()
def plugin_import_state():
    sys.modules.pop(_PLUGIN_MODULE, None)
    yield
    sys.modules.pop(_PLUGIN_MODULE, None)


def test_job_spec_builder_matches_the_m8_producer(plugin_import_state):
    """``api._job_spec_for`` == ``cli/main._job_spec_from_request`` on the same
    admitted models — the mechanical guard the builder's SOURCE OF TRUTH anchor
    names (drift in either copy fails here loudly)."""
    plain = _admit(_request_doc()).request
    custom_doc = _request_doc()
    custom_doc["acceptance_criteria"].append(
        {
            "oracle": f"{_PLUGIN_MODULE}:ParamVerdictOracle",
            "params": {"custom_should_pass": True, "explicit_null": None},
        }
    )
    custom = _admit(custom_doc, plugin_dir=_PLUGIN_DIR).request
    for request in (plain, custom):
        assert _job_spec_for(request, "jid-1") == m8_job_spec_from_request(request, "jid-1")
    # Positive control (비공허 실증): the frozen P2 seam shape actually came out.
    spec = _job_spec_for(custom, "jid-1")
    assert set(spec) == {"job_id", "scenario", "sut_image_ref", "interface", "acceptance_criteria"}
    assert spec["job_id"] == "jid-1"
    assert spec["sut_image_ref"] == _CANONICAL_DOC["sut"]["image_ref"]  # sut.image_ref flattened
    assert spec["acceptance_criteria"][-1]["params"]["explicit_null"] is None  # user null survives


def test_admitted_spec_rides_fanned_out_jobs_and_persists(tmp_path):
    """Submit path (T1 §7-1 (a)): every fanned-out job carries a self-contained
    canonical JOB_SPEC keyed by its OWN job_key, and the spec survives a
    restart through the store (스키마 v3)."""

    class SpecRecordingRunner:
        def __init__(self) -> None:
            self.specs: dict[str, dict | None] = {}

        def run(self, job: Job) -> JobResult:
            self.specs[job_key(job)] = copy.deepcopy(job.job_spec)
            return JobResult(job=job, state=JobState.COMPLETED, verdict=Verdict.PASS)

    runner = SpecRecordingRunner()
    db = tmp_path / "cv.sqlite3"
    doc = _request_doc()
    doc["execution_settings"] = {"repeats": 2}
    with Store(db) as store:
        app = create_app(store, runner, k=2)
        with TestClient(app) as client:
            response = client.post("/envelopes", json={"requests": [doc, _request_doc()]})
            assert response.status_code == 202, response.text
            envelope_id = response.json()["envelope_id"]
            deadline = time.monotonic() + 10.0
            body = client.get(f"/envelopes/{envelope_id}").json()
            while body["status"] != "completed":
                assert time.monotonic() < deadline, "envelope did not complete in time"
                time.sleep(0.02)
                body = client.get(f"/envelopes/{envelope_id}").json()
    assert len(runner.specs) == 3  # fan-out 2 + 1, each seen by the runner seam WITH a spec
    for key, spec in runner.specs.items():
        assert spec is not None
        assert spec["job_id"] == key  # per-job identity rides inside the spec (self-contained)
        assert spec["sut_image_ref"] == _CANONICAL_DOC["sut"]["image_ref"]
    with Store(db) as reopened:  # 재기동 대비 영속: a fresh store restores the specs verbatim
        persisted = {job_key(j): j.job_spec for j in reopened.load_jobs()}
        assert persisted == runner.specs


# --------------------------------------------------------------------------- #
# (b) RunJobRunner: outcome fold + frozen-seam hand-off + watchdog declaration
# --------------------------------------------------------------------------- #


def _specced_job(job_id: str = "req-a:0") -> Job:
    (job,) = fan_out(["req-a"], repeats=1)
    job.job_spec = {"job_id": job_id, "sut_image_ref": "sut:test", "scenario": {}}
    return job


def _result_outcome(tmp_path: Path, job_id: str, payload: str, exit_code: int) -> JobOutcome:
    result_dir = tmp_path / job_id / "result"
    result_dir.mkdir(parents=True, exist_ok=True)
    path = result_dir / "result.json"
    path.write_text(payload, encoding="utf-8")
    return JobOutcome(job_id, path, exit_code, None)


def _runner_with(outcome: JobOutcome, tmp_path: Path) -> RunJobRunner:
    return RunJobRunner(
        out_dir=tmp_path, runner_image="runner:test", run_job_fn=lambda *a, **kw: outcome
    )


@pytest.mark.parametrize(
    ("payload", "exit_code", "state", "verdict"),
    [
        # The RECOVERED verdict outranks the informational exit code (the real
        # runner exits 1 on a domain FAIL — cli/main._exit_from_outcome 원칙).
        ('{"job_id": "j", "verdict": "pass"}', 0, JobState.COMPLETED, Verdict.PASS),
        ('{"job_id": "j", "verdict": "fail"}', 1, JobState.COMPLETED, Verdict.FAIL),
        # "timeout" = SUT missed the sim-time budget -> domain FAIL fold
        # (schema.py Verdict comment), NOT the wall-clock TIMEOUT state.
        ('{"job_id": "j", "verdict": "timeout"}', 1, JobState.COMPLETED, Verdict.FAIL),
        # "error" / unknown / non-string / non-dict / unreadable: infra outcome,
        # verdict-less — never a fabricated domain judgement.
        ('{"job_id": "j", "verdict": "error"}', 3, JobState.FAILED, None),
        ('{"job_id": "j", "verdict": "wat"}', 0, JobState.FAILED, None),
        ('{"job_id": "j", "verdict": 5}', 0, JobState.FAILED, None),
        ('["not", "a", "dict"]', 0, JobState.FAILED, None),
        ("{not json", 0, JobState.FAILED, None),
    ],
)
def test_recovered_result_folds_by_the_m1_verdict_key_only(
    tmp_path, payload, exit_code, state, verdict
):
    job = _specced_job()
    outcome = _result_outcome(tmp_path, "req-a:0", payload, exit_code)
    result = _runner_with(outcome, tmp_path).run(job)
    assert (result.state, result.verdict) == (state, verdict)
    assert result.job is job


def test_infra_error_folds_failed_and_watchdog_marker_folds_timeout(tmp_path):
    job = _specced_job()
    generic = JobOutcome("req-a:0", None, None, "APIError: docker daemon exploded")
    assert _runner_with(generic, tmp_path).run(job).state is JobState.FAILED
    watchdog = JobOutcome(
        "req-a:0", None, None, f"{JOB_TIMEOUT_MARKER} runner still running after 5.0s"
    )
    timed_out = _runner_with(watchdog, tmp_path).run(job)
    assert (timed_out.state, timed_out.verdict) == (JobState.TIMEOUT, None)
    # Belt-and-braces: no result and no infra_error still never reads COMPLETED.
    silent = JobOutcome("req-a:0", None, 0, None)
    assert _runner_with(silent, tmp_path).run(job).state is JobState.FAILED


def test_real_run_job_timeout_message_carries_the_marker(tmp_path):
    """Positive control (G-28 anchoring): the REAL run_job watchdog produces a
    marker-prefixed infra_error — the fold above is bound to the actual
    producer, not to a fixture invented to match the consumer."""
    client = FakeClient(runner_statuses=("running",) * 10, sut_statuses=("running",) * 10)
    outcome = run_min(tmp_path, client, job_timeout_s=0.0)
    assert outcome.infra_error is not None
    assert outcome.infra_error.startswith(JOB_TIMEOUT_MARKER)


# --- p4c5 실패 관측성: the fold PRESERVES the diagnostics (유실 지점 ①/②) ------


@pytest.mark.parametrize("exit_code", [137, 139, 1])  # OOM-kill / segfault / plain failure
def test_real_run_job_recovers_a_crashed_runners_exit_code(tmp_path, exit_code):
    """Positive control (G-28): the REAL run_job already recovers the container
    exit code of a runner that died mid-run and pairs it with the REQ-EXEC-013
    collection reason — this is the producer shape the crash fixtures below
    reuse (137 vs 139 vs 1 is exactly what p4c4 발견 ② could not tell apart)."""
    client = FakeClient(
        runner_statuses=("running", "running", "exited"),
        runner_exit_code=exit_code,
        sut_statuses=("running",) * 5,
    )
    outcome = run_min(tmp_path, client)  # no result.json is ever written -> collection violation
    assert outcome.runner_exit_code == exit_code
    assert outcome.result_path is None
    assert outcome.infra_error is not None and "REQ-EXEC-013" in outcome.infra_error


@pytest.mark.parametrize("exit_code", [137, 139, 1])
def test_fold_carries_the_runner_exit_code_and_reason(tmp_path, exit_code):
    """유실 지점 ②: the JobOutcome->JobResult fold no longer drops the crash
    evidence — state/verdict classification is unchanged (FAILED, verdict-less),
    but the exit code + reason now ride the JobResult (and thence the store)."""
    job = _specced_job()
    reason = "expected exactly 1 result.json under /out/req-a-0/result, found 0 (REQ-EXEC-013)"
    crashed = JobOutcome("req-a:0", None, exit_code, reason)
    result = _runner_with(crashed, tmp_path).run(job)
    assert (result.state, result.verdict) == (JobState.FAILED, None)  # fold 의미론 불변
    assert result.runner_exit_code == exit_code
    assert result.infra_error == reason


def test_fold_carries_diagnostics_on_the_verdict_bearing_paths_too(tmp_path):
    """A recovered verdict still OUTRANKS the exit code (frozen), and the exit
    code is now visible ALONGSIDE it: the runner's exit 1 on a domain FAIL is
    informational, never a state change."""
    job = _specced_job()
    passed = _runner_with(_result_outcome(tmp_path, "req-a:0", '{"verdict": "pass"}', 0), tmp_path)
    result = passed.run(job)
    assert (result.state, result.verdict) == (JobState.COMPLETED, Verdict.PASS)
    assert (result.runner_exit_code, result.infra_error) == (0, None)
    failed = _runner_with(_result_outcome(tmp_path, "req-a:0", '{"verdict": "fail"}', 1), tmp_path)
    result = failed.run(job)
    assert (result.state, result.verdict) == (JobState.COMPLETED, Verdict.FAIL)
    assert (result.runner_exit_code, result.infra_error) == (1, None)


def test_folded_reason_is_a_bounded_single_line_diagnostic(tmp_path):
    """NEG (DoD-P4-13 정신): the reason is an operational breadcrumb, NOT a
    channel for a runner stderr dump. A large multi-line payload is collapsed to
    one line and truncated at the cap — the leak is structurally bounded, not
    politely discouraged."""
    dump = "APIError: boom\n" + "\n".join(
        f"  runner stderr line {i} secret-ish" for i in range(200)
    )
    result = _runner_with(JobOutcome("req-a:0", None, 139, dump), tmp_path).run(_specced_job())
    assert result.infra_error is not None
    assert len(result.infra_error) <= _REASON_MAX_CHARS
    assert "\n" not in result.infra_error
    assert result.infra_error.endswith(_TRUNCATION_SUFFIX)
    assert result.infra_error.startswith("APIError: boom")  # the head — the actionable part
    assert "runner stderr line 199" not in result.infra_error  # the dump did NOT ride along


def test_run_job_receives_the_frozen_seam_args_off_the_job(tmp_path):
    """The wrapper hands run_job EXACTLY the frozen contract: canonical spec by
    value, sut image off the spec, the job's stage-5 anchor, and the SAME
    ``job_timeout_s`` it declares to the coherence gate (single source)."""
    calls: list[tuple[tuple, dict]] = []

    def recording_run_job(*args, **kwargs):
        calls.append((args, kwargs))
        return JobOutcome(args[0]["job_id"], None, None, "stop here")

    runner = RunJobRunner(
        out_dir=tmp_path / "out",
        runner_image="ghcr.io/x/runner@sha256:pin",
        docker_client="fake-docker-client",
        runner_env={"ACCEPT_EULA": "operator-consent-token"},
        cache_root="/host/cache",
        cache_scratch_root="/host/scratch",
        job_timeout_s=123.0,
        run_job_fn=recording_run_job,
    )
    job = _specced_job()
    job.oracle_plugin_dir = "/abs/consumer/scenarios"
    runner.run(job)
    ((args, kwargs),) = calls
    assert args == (
        job.job_spec,
        tmp_path / "out",
        "ghcr.io/x/runner@sha256:pin",
        "sut:test",  # job_spec["sut_image_ref"] — the cli/main fold, off the riding spec
        "fake-docker-client",
    )
    assert kwargs["oracle_plugin_dir"] == "/abs/consumer/scenarios"
    assert kwargs["job_timeout_s"] == runner.job_timeout_s == 123.0  # 이중 정의 금지
    assert kwargs["runner_env"] == {"ACCEPT_EULA": "operator-consent-token"}
    assert kwargs["cache_root"] == "/host/cache"
    assert kwargs["cache_scratch_root"] == "/host/scratch"
    # No-anchor discrimination: an anchor-less job hands run_job None (the
    # pinned kw-only default semantics — no mount, no env).
    runner.run(_specced_job("req-a:0"))
    assert calls[-1][1]["oracle_plugin_dir"] is None


def test_spec_less_job_is_loud_never_a_silent_noop(tmp_path):
    runner = RunJobRunner(out_dir=tmp_path, runner_image="r:t", run_job_fn=lambda *a, **kw: None)
    with pytest.raises(ValueError, match="job_spec"):
        runner.run(Job(request_id="req-a", repeat_index=0))


def test_wrapper_declares_its_watchdog_to_the_coherence_gate(tmp_path):
    """T1 워치독 정합 게이트 계약 (b): the production wrapper's ``job_timeout_s``
    attribute IS the duck-typed declaration — a shorter outer watchdog is
    rejected at construction, the default equals run_job's single constant."""
    runner = RunJobRunner(out_dir=tmp_path, runner_image="r:t")
    assert runner.job_timeout_s == DEFAULT_JOB_TIMEOUT_S
    queue = JobQueue(fan_out(["r"], repeats=1))
    with pytest.raises(ValueError, match="watchdog"):
        ParallelSupervisor(
            queue, SlotAccountant(k=1), runner, job_timeout_s=DEFAULT_JOB_TIMEOUT_S - 1
        )
    ParallelSupervisor(  # outer >= inner (or None, serve.py's choice) is accepted
        JobQueue(fan_out(["r2"], repeats=1)),
        SlotAccountant(k=1),
        runner,
        job_timeout_s=DEFAULT_JOB_TIMEOUT_S,
    )


# --------------------------------------------------------------------------- #
# (c) serve: env contract -> config (loud) -> production composition + boot log
# --------------------------------------------------------------------------- #

_FULL_ENV = {
    "CV_STORE_PATH": "/data/cv.sqlite3",
    "CV_OUT_DIR": "/data/out",
    "CV_RUNNER_IMAGE": "ghcr.io/x/runner@sha256:pin",
    "CV_MAX_CONCURRENT": "8",
}


def test_config_from_env_reads_the_full_contract():
    environ = {
        **_FULL_ENV,
        "CV_VRAM_PER_INSTANCE_MB": "8000",
        "CV_BIND_HOST": "0.0.0.0",
        "CV_BIND_PORT": "9000",
        "CV_ISAAC_CACHE_ROOT": "/host/cache",
        "CV_ISAAC_CACHE_SCRATCH_ROOT": "/host/scratch",
        "ACCEPT_EULA": "operator-consent-token",
    }
    config = serve.config_from_env(environ)
    assert config.store_path == "/data/cv.sqlite3"
    assert config.out_dir == "/data/out"
    assert config.runner_image == "ghcr.io/x/runner@sha256:pin"
    assert config.max_concurrent == 8
    assert config.vram_per_instance_mb == 8000.0
    assert (config.host, config.port) == ("0.0.0.0", 9000)
    assert (config.cache_root, config.cache_scratch_root) == ("/host/cache", "/host/scratch")
    assert config.consent_env == {"ACCEPT_EULA": "operator-consent-token"}  # verbatim, presence


def test_config_defaults_match_the_m8_client_default():
    config = serve.config_from_env(dict(_FULL_ENV))
    assert (config.host, config.port) == ("127.0.0.1", 8000)  # batch._DEFAULT_API counterpart
    assert config.vram_per_instance_mb is None  # VRAM guard off until MEASURED value given
    assert config.consent_env == {}  # absent consent = runner boot guard refuses (LOCKED §7.8)


def test_missing_required_envs_are_reported_together():
    with pytest.raises(ValueError) as exc:
        serve.config_from_env({"CV_STORE_PATH": "/data/cv.sqlite3"})
    message = str(exc.value)
    for name in ("CV_OUT_DIR", "CV_RUNNER_IMAGE", "CV_MAX_CONCURRENT"):
        assert name in message  # ALL missing envs in ONE error (one-pass fix)
    assert "CV_STORE_PATH" not in message.split("missing required env(s):")[1]


@pytest.mark.parametrize(
    "overrides",
    [
        {"CV_STORE_PATH": ""},  # set-but-empty is loud, never 'unset' (G-26, T1 관례)
        {"CV_MAX_CONCURRENT": "eight"},
        {"CV_VRAM_PER_INSTANCE_MB": " "},
        {"CV_BIND_PORT": "http"},
    ],
)
def test_bad_env_values_are_loud(overrides):
    with pytest.raises(ValueError):
        serve.config_from_env({**_FULL_ENV, **overrides})


class _FakeGauge:
    def __init__(self, free_mb: float) -> None:
        self._free_mb = free_mb

    def available_vram_mb(self) -> float:
        return self._free_mb


class _StaleContainer:
    def __init__(self) -> None:
        self.removed = 0

    def stop(self, timeout=None) -> None:
        pass

    def remove(self, force=False) -> None:
        self.removed += 1


class _StaleNetwork:
    def __init__(self) -> None:
        self.removed = 0

    def remove(self) -> None:
        self.removed += 1


class _Listing:
    def __init__(self, items: list) -> None:
        self._items = items

    def list(self, all: bool = False, filters: dict | None = None):  # noqa: A002 — SDK name
        return list(self._items)


class _FakeSweepDocker:
    """Duck-typed docker client for the boot sweep (label list/stop/remove only)."""

    def __init__(self) -> None:
        self.stale_container = _StaleContainer()
        self.stale_network = _StaleNetwork()
        self.containers = _Listing([self.stale_container])
        self.networks = _Listing([self.stale_network])


def _serve_config(tmp_path: Path, **overrides) -> serve.ServeConfig:
    environ = {
        "CV_STORE_PATH": str(tmp_path / "cv.sqlite3"),
        "CV_OUT_DIR": str(tmp_path / "out"),
        "CV_RUNNER_IMAGE": "runner:test",
        "CV_MAX_CONCURRENT": "8",
        **overrides,
    }
    return serve.config_from_env(environ)


def test_build_app_computes_k_and_emits_the_serve_config_line(tmp_path, capsys):
    """Feature-on gate (G-26): ONE structured boot line carrying the COMPUTED k
    (LOCKED §7.4 — operator cap 8 capped to 4 by the VRAM 2nd guard) and the
    consent key NAMES with the value NEVER logged (G-21)."""
    config = _serve_config(
        tmp_path,
        CV_VRAM_PER_INSTANCE_MB="8000",
        ACCEPT_EULA="operator-consent-token",
    )
    serve.build_app(config, vram_gauge=_FakeGauge(free_mb=35000.0))
    err = capsys.readouterr().err
    (line,) = [ln for ln in err.splitlines() if ln.startswith("[cv-orchestrator] serve-config ")]
    body = json.loads(line.removeprefix("[cv-orchestrator] serve-config "))
    assert body["k"] == 4  # min(max_concurrent 8, floor(35000/8000)=4)
    assert body["max_concurrent"] == 8
    assert body["runner_image"] == "runner:test"
    assert body["job_timeout_s"] == DEFAULT_JOB_TIMEOUT_S  # the wrapper's declaration, 1 source
    assert body["consent_env_present"] == ["ACCEPT_EULA"]
    assert "operator-consent-token" not in err  # value never rides any log (G-21)


def test_build_app_reconciles_the_store_at_boot(tmp_path, capsys):
    """serve is the production caller of reconcile_at_restart (R14): stale
    labeled docker resources are swept, RUNNING orphans go terminal via the
    retry policy (max_attempts=1 defaults), in-flight envelopes read loud."""
    db = tmp_path / "cv.sqlite3"
    with Store(db) as store:
        store.record_envelope("env-live", ["env-live/r0"], [None])
        job = Job(request_id="env-live/r0", repeat_index=0)
        store.upsert_job(job)
        job.state = JobState.RUNNING
        store.upsert_job(job)
        store.record_domain_id(7, "env-live/r0:0")
    client = _FakeSweepDocker()
    app = serve.build_app(_serve_config(tmp_path), docker_client=client)
    assert client.stale_container.removed == 1
    assert client.stale_network.removed == 1
    body = json.loads(
        capsys.readouterr().err.split("[cv-orchestrator] serve-config ", 1)[1].splitlines()[0]
    )
    assert body["reconciliation"] == {
        "containers_removed": 1,
        "networks_removed": 1,
        "orphans_requeued": 0,
        "orphans_failed": 1,  # max_attempts=1: the interrupted attempt is terminal
        "domain_ids_cleared": 1,
        "envelopes_failed": 1,
    }
    with TestClient(app) as tc:
        response = tc.get("/envelopes/env-live")
        assert response.status_code == 500  # loud marker, never silent forever-'running'
        assert "restarted" in response.json()["detail"]


# --------------------------------------------------------------------------- #
# (e) E2E: submit --wait -> env-built production app -> fake run_job -> rollup
# --------------------------------------------------------------------------- #


class ProcessBoundaryTransport(httpx.ASGITransport):
    """In-process ASGI transport that models the REAL process boundary by
    dropping the custom-oracle module before every server-side request
    (관용구 출처: tests/test_cli_batch.py, p4c3 — G-28: without this, the
    server's stage-5 re-admit silently reuses the CLIENT's import and the
    anchor plumbing claim is vacuous)."""

    def __init__(self, app, module_name: str) -> None:
        super().__init__(app=app)
        self.requests: list[httpx.Request] = []
        self._module_name = module_name

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        sys.modules.pop(self._module_name, None)
        return await super().handle_async_request(request)


def _wire_batch(monkeypatch: pytest.MonkeyPatch, transport: httpx.ASGITransport) -> None:
    monkeypatch.setattr(
        batch,
        "_make_client",
        lambda api_base: httpx.AsyncClient(transport=transport, base_url="http://cv-infra.test"),
    )
    monkeypatch.setattr(batch, "_POLL_INTERVAL_S", 0.01)


def _write_envelope_tree(tmp_path: Path, scenario_texts: dict[str, str], envelope_text: str):
    """Consumer-shaped tree: batch.yaml + scenarios/ beside it (test_cli_batch 관용구)."""
    scenarios = tmp_path / "scenarios"
    scenarios.mkdir(exist_ok=True)
    for name, text in scenario_texts.items():
        (scenarios / name).write_text(text, encoding="utf-8")
    envelope = tmp_path / "batch.yaml"
    envelope.write_text(envelope_text, encoding="utf-8")
    return envelope


def _scripted_run_job(behaviors: dict[str, str], calls: dict[str, dict]):
    """Injected fake of the frozen run_job contract (G-20): records every
    hand-off, then scripts the outcome per request suffix — result.json files
    are REALLY written so the wrapper's verdict extraction runs for real."""

    def fake_run_job(job_spec, out_dir, runner_image, sut_image, docker_client=None, **kwargs):
        job_id = job_spec["job_id"]
        calls[job_id] = {
            "job_spec": copy.deepcopy(job_spec),
            "runner_image": runner_image,
            "sut_image": sut_image,
            **kwargs,
        }
        suffix = job_id.rsplit("/", 1)[-1].split(":")[0]
        behavior = behaviors.get(suffix, "pass")
        if behavior.startswith("hard-crash:"):
            # The runner CONTAINER died mid-run (137 OOM-kill / 139 segfault):
            # non-zero exit + no result.json. Producer-anchored shape (G-28) —
            # test_real_run_job_recovers_a_crashed_runners_exit_code pins that the
            # REAL run_job returns exactly this pair.
            return JobOutcome(
                job_id,
                None,
                int(behavior.split(":", 1)[1]),
                f"expected exactly 1 result.json under {out_dir}/result, found 0 (REQ-EXEC-013)",
            )
        if behavior == "crash":
            raise RuntimeError("simulated runner-seam crash (P4-11 mix)")
        if behavior == "watchdog-timeout":
            return JobOutcome(
                job_id,
                None,
                None,
                f"{JOB_TIMEOUT_MARKER} runner still running after 1800.0s (teardown kills it)",
            )
        verdict = {"pass": "pass", "fail": "fail"}[behavior]
        result_dir = Path(out_dir) / job_id / "result"
        result_dir.mkdir(parents=True, exist_ok=True)
        result_path = result_dir / "result.json"
        result_path.write_text(json.dumps({"job_id": job_id, "verdict": verdict}), encoding="utf-8")
        return JobOutcome(job_id, result_path, 0 if verdict == "pass" else 1, None)

    return fake_run_job


_E2E_ORACLE_MODULE = "p4c4_glue_e2e_oracle"

_E2E_ORACLE_SRC = """\
from cv_infra.oracles.base import OracleBase


class GlueE2EOracle(OracleBase):
    name = "glue_e2e_fixture"
    version = "0.0.1"

    def validate_params(self, criteria):
        return None

    def evaluate(self, telemetry, criteria):
        return {"passed": True}
"""


def _custom_scenario_text() -> str:
    doc = _request_doc()
    doc["acceptance_criteria"].append(
        {"oracle": f"{_E2E_ORACLE_MODULE}:GlueE2EOracle", "params": {"anything": "goes"}}
    )
    return yaml.safe_dump(doc)


def test_e2e_submit_wait_drives_fake_run_job_to_pass_with_anchors(monkeypatch, tmp_path, capsys):
    """The full glue seam: REAL load_envelope -> wire v2 -> env-built production
    app (serve.build_app) -> fan-out -> k-parallel RunJobRunner -> fake run_job
    -> rollup -> terminal exit 0. Asserts the spec/anchor/timeout hand-offs the
    fake recorded AND the store's restart-safe copy."""
    envelope = _write_envelope_tree(
        tmp_path,
        {"plain.yaml": _CANONICAL_TEXT, "custom.yaml": _custom_scenario_text()},
        "apiVersion: cv-infra/v1\n"
        "requests:\n"
        "  - scenario: scenarios/plain.yaml\n"
        "    repeats: 2\n"
        "  - scenario: scenarios/custom.yaml\n",
    )
    (tmp_path / "scenarios" / f"{_E2E_ORACLE_MODULE}.py").write_text(
        _E2E_ORACLE_SRC, encoding="utf-8"
    )
    calls: dict[str, dict] = {}
    config = _serve_config(tmp_path, CV_MAX_CONCURRENT="2")
    app = serve.build_app(config, run_job_fn=_scripted_run_job({}, calls))
    _wire_batch(monkeypatch, ProcessBoundaryTransport(app, _E2E_ORACLE_MODULE))
    try:
        rc = main(["submit", str(envelope), "--wait"])
    finally:
        sys.modules.pop(_E2E_ORACLE_MODULE, None)  # no import residue (G-29 정신)

    out_lines = capsys.readouterr().out.strip().splitlines()
    assert rc == EXIT_PASS
    envelope_id = out_lines[0]
    assert envelope_id.startswith("env-")
    assert "report_outcome=pass" in out_lines[-1]

    # Fan-out N×r reached the runner seam: 2 (repeats override) + 1 jobs.
    assert sorted(calls) == [
        f"{envelope_id}/r0:0",
        f"{envelope_id}/r0:1",
        f"{envelope_id}/r1:0",
    ]
    anchor = str((tmp_path / "scenarios").resolve())
    for job_id, call in calls.items():
        spec = call["job_spec"]
        assert set(spec) == {
            "job_id",
            "scenario",
            "sut_image_ref",
            "interface",
            "acceptance_criteria",
        }
        assert spec["job_id"] == job_id
        assert call["sut_image"] == spec["sut_image_ref"] == _CANONICAL_DOC["sut"]["image_ref"]
        assert call["runner_image"] == "runner:test"
        assert call["job_timeout_s"] == DEFAULT_JOB_TIMEOUT_S  # wrapper 단일 소스
        # D-1 anchor 전달 단정: on the batch path the M8 CLI anchors EVERY
        # request with its scenario's parent dir (test_cli_batch E2E pins
        # ``[anchor, anchor]``), so all jobs hand run_job the REAL dir — the
        # None (no-anchor) discrimination is unit-pinned in
        # test_run_job_receives_the_frozen_seam_args_off_the_job and the api
        # seam test (test_orchestrator_api section d).
        assert call["oracle_plugin_dir"] == anchor

    # status/report_outcome 왕복 (fresh CLI invocation over the same app).
    assert main(["status", envelope_id]) == EXIT_PASS
    status_body = json.loads(capsys.readouterr().out)
    assert len(status_body["jobs"]) == 3
    assert all(j["state"] == "completed" for j in status_body["jobs"])
    assert [r["verdict"] for r in status_body["rollups"]] == ["pass", "pass"]
    assert status_body["report_outcome"] == "pass"

    # 재기동 대비: the specs the seam ran are ALSO the persisted ones.
    with Store(config.store_path) as reopened:
        persisted = {job_key(j): j.job_spec for j in reopened.load_jobs()}
        assert persisted == {job_id: call["job_spec"] for job_id, call in calls.items()}


def test_e2e_mixed_outcomes_with_a_crash_runner_stay_isolated(monkeypatch, tmp_path, capsys):
    """pass/fail/timeout/crash mix (task 요구 4 + P4-11 CPU 사전 검증): one
    crashing runner seam fails ONLY its own job; siblings complete, the
    watchdog marker classifies TIMEOUT, and errored 우선 folds exit 3."""
    envelope = _write_envelope_tree(
        tmp_path,
        {f"s{i}.yaml": _CANONICAL_TEXT for i in range(4)},
        "apiVersion: cv-infra/v1\n"
        "requests:\n" + "".join(f"  - scenario: scenarios/s{i}.yaml\n" for i in range(4)),
    )
    calls: dict[str, dict] = {}
    behaviors = {"r0": "pass", "r1": "fail", "r2": "watchdog-timeout", "r3": "crash"}
    config = _serve_config(tmp_path, CV_MAX_CONCURRENT="2")
    app = serve.build_app(config, run_job_fn=_scripted_run_job(behaviors, calls))
    _wire_batch(monkeypatch, ProcessBoundaryTransport(app, _E2E_ORACLE_MODULE))

    rc = main(["submit", str(envelope), "--wait"])
    out_lines = capsys.readouterr().out.strip().splitlines()
    assert rc == EXIT_INFRA  # errored 우선: infra noise never reads as a self-regression
    envelope_id = out_lines[0]
    assert "report_outcome=errored" in out_lines[-1]
    assert len(calls) == 4  # every job reached the seam exactly once (k=2 supervision)

    assert main(["status", envelope_id]) == EXIT_PASS
    body = json.loads(capsys.readouterr().out)
    states = {j["request_id"].rsplit("/", 1)[-1]: j["state"] for j in body["jobs"]}
    # Crash isolation (P4-11): r3's raising seam failed ONLY r3 — r0/r1
    # completed with verdicts, r2 is the watchdog TIMEOUT classification.
    assert states == {"r0": "completed", "r1": "completed", "r2": "timeout", "r3": "failed"}
    verdicts = {r["request_id"].rsplit("/", 1)[-1]: r["verdict"] for r in body["rollups"]}
    assert verdicts == {"r0": "pass", "r1": "fail", "r2": None, "r3": None}
    # p4c5: the two failure paths now SAY WHY — the crashed seam's exception
    # message survived the crash boundary, the watchdog kill carries its marker.
    jobs = {j["request_id"].rsplit("/", 1)[-1]: j for j in body["jobs"]}
    assert "simulated runner-seam crash" in jobs["r3"]["infra_error"]
    assert jobs["r2"]["infra_error"].startswith(JOB_TIMEOUT_MARKER)


# --------------------------------------------------------------------------- #
# (f) 실패 관측성 (p4c5): the crash evidence reaches the store + the status API
#     (p4c4 발견 ②의 추적 장애 — history 2026-07-14 놀란 점 7)
# --------------------------------------------------------------------------- #

#: The status wire's job entry — EXACT key set (operational breadcrumbs only; no
#: job_spec / scenario / consent / domain detail rides this view).
_STATUS_JOB_KEYS = {
    "request_id",
    "repeat_index",
    "state",
    "attempt_count",
    "runner_exit_code",
    "infra_error",
}


def test_e2e_runner_crash_reaches_the_status_api_and_a_fresh_store(monkeypatch, tmp_path, capsys):
    """The whole seam a p4c4 크래시 추적이 필요로 했던 것: two runner containers
    die with DIFFERENT exit codes (137 OOM-kill vs 139 segfault) + no result;
    the codes and reasons land on the status API AND in SQLite, so a fresh
    process (new Store, new app) still reads why. NEG: the operator consent
    value never rides this view (positive control below proves it WAS configured).
    """
    envelope = _write_envelope_tree(
        tmp_path,
        {f"s{i}.yaml": _CANONICAL_TEXT for i in range(3)},
        "apiVersion: cv-infra/v1\n"
        "requests:\n" + "".join(f"  - scenario: scenarios/s{i}.yaml\n" for i in range(3)),
    )
    calls: dict[str, dict] = {}
    behaviors = {"r0": "hard-crash:137", "r1": "hard-crash:139", "r2": "pass"}
    config = _serve_config(tmp_path, CV_MAX_CONCURRENT="2", ACCEPT_EULA="operator-consent-token")
    app = serve.build_app(config, run_job_fn=_scripted_run_job(behaviors, calls))
    _wire_batch(monkeypatch, ProcessBoundaryTransport(app, _E2E_ORACLE_MODULE))

    assert main(["submit", str(envelope), "--wait"]) == EXIT_INFRA  # errored 우선 (불변)
    envelope_id = capsys.readouterr().out.strip().splitlines()[0]
    # Positive control (비공허): the consent value REALLY was handed to the runner
    # seam — so its absence from the operational view below is a guard, not luck.
    assert calls[f"{envelope_id}/r0:0"]["runner_env"] == {"ACCEPT_EULA": "operator-consent-token"}

    assert main(["status", envelope_id]) == EXIT_PASS
    raw_status = capsys.readouterr().out
    body = json.loads(raw_status)
    jobs = {j["request_id"].rsplit("/", 1)[-1]: j for j in body["jobs"]}
    assert all(set(job) == _STATUS_JOB_KEYS for job in body["jobs"])  # shape drift 0
    # 137 vs 139 vs a clean exit are now DISTINGUISHABLE from the API alone.
    assert (jobs["r0"]["state"], jobs["r0"]["runner_exit_code"]) == ("failed", 137)
    assert (jobs["r1"]["state"], jobs["r1"]["runner_exit_code"]) == ("failed", 139)
    assert "REQ-EXEC-013" in jobs["r0"]["infra_error"]  # ... and they say why
    assert (jobs["r2"]["state"], jobs["r2"]["runner_exit_code"]) == ("completed", 0)
    assert jobs["r2"]["infra_error"] is None  # a clean job carries no reason
    # NEG (DoD-P4-13 정신): no consent value, no secret, no domain detail on the
    # operational view — and no runner-log payload smuggled through infra_error.
    assert "operator-consent-token" not in raw_status
    assert all(len(j["infra_error"] or "") <= _REASON_MAX_CHARS for j in body["jobs"])

    # 새 Store 객체 복원 (파일이 기록이다): a fresh process reads the same evidence,
    # both from the store directly and through the store-served status path.
    with Store(config.store_path) as reopened:
        persisted = {job_key(j).rsplit("/", 1)[-1]: j for j in reopened.load_jobs()}
    assert (persisted["r0:0"].runner_exit_code, persisted["r1:0"].runner_exit_code) == (137, 139)
    assert "REQ-EXEC-013" in persisted["r0:0"].infra_error
    assert "operator-consent-token" not in (persisted["r0:0"].infra_error or "")

    restarted = serve.build_app(_serve_config(tmp_path, CV_MAX_CONCURRENT="2"))
    with TestClient(restarted) as client:
        restored = client.get(f"/envelopes/{envelope_id}").json()  # _status_from_store path
    restored_jobs = {j["request_id"].rsplit("/", 1)[-1]: j for j in restored["jobs"]}
    assert restored_jobs == jobs  # 동일 shape·동일 증거, 다른 프로세스 (단일 조립기)
