"""Supervisor-min unit tests (D-2 co-spawn seam) — duck-typed fake docker client, CPU-only.

Proves without docker: start order (runner -> readiness -> SUT, G-19 supply order),
exact container accounting (1 job = 1 runner + 1 SUT — NEG-4 선행), the exactly-one
result.json invariant (REQ-EXEC-013), the bounded SUT restart contract, finally-
teardown on every path incl. exceptions (REQ-EXEC-015 결), and operator runner_env
pass-through (no consent literal anywhere — decision 2026-07-03).
"""

from __future__ import annotations

import json
import re
import shutil
from pathlib import Path

import pytest

from cv_infra.orchestrator.supervisor import (
    CACHE_ROOT_ENV,
    CACHE_SCRATCH_ROOT_ENV,
    JOB_SPEC_MOUNT,
    LABEL_JOB_ID,
    ORACLE_PLUGIN_DIR_ENV,
    RESULT_OUT_MOUNT,
    ROS_DOMAIN_ID_SPACE,
    JobOutcome,
    allocate_ros_domain_id,
    network_name_for,
    run_job,
)

RUNNER_IMAGE = "cv-infra-runner:test"
SUT_IMAGE = "carter-sut:test"
JOB_ID = "job-1"


def make_spec(job_id: str = JOB_ID, adapter_config: dict | None = None) -> dict:
    spec = {"job_id": job_id, "sut_image_ref": SUT_IMAGE, "scenario": {"scene": "warehouse"}}
    if adapter_config is not None:
        spec["interface"] = {"type": "ros2", "adapter_config": adapter_config}
    return spec


def put_result(tmp_path, job_id: str = JOB_ID, rel: str = "result.json"):
    """Pre-create a result file where the runner would have written it.

    The host job dir is the bind-safe ``network_name_for`` slug (p4c4
    colon-bind fix) — NOT the raw job id, which may carry ':'.
    """
    path = tmp_path / network_name_for(job_id) / "result" / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"job_id": job_id, "verdict": "pass"}), encoding="utf-8")
    return path


# --------------------------------------------------------------------------- #
# Duck-typed docker fakes (containers.run / networks.create surface only).
# --------------------------------------------------------------------------- #


class FakeContainer:
    """Scripted container: each reload() advances through `statuses` (last one sticks)."""

    def __init__(self, label, statuses, exit_code, events):
        self.label = label
        self._statuses = list(statuses)
        self.status = "created"
        self._exit_code = exit_code
        self._events = events
        self.stop_calls = 0
        self.remove_calls = 0
        self.restart_calls = 0

    def reload(self):
        if self._statuses:
            self.status = self._statuses.pop(0)

    def wait(self, timeout=None):
        return {"StatusCode": self._exit_code}

    def stop(self, timeout=None):
        self.stop_calls += 1

    def remove(self, force=False):
        self.remove_calls += 1
        self._events.append(("remove", self.label))

    def restart(self, timeout=None):
        self.restart_calls += 1


class FakeNetwork:
    def __init__(self, name, events):
        self.name = name
        self._events = events
        self.remove_calls = 0

    def remove(self):
        self.remove_calls += 1
        self._events.append(("network-remove", self.name))


class _FakeNetworks:
    def __init__(self, client):
        self._client = client

    def create(self, name, driver=None, labels=None):
        self._client.events.append(("network-create", name))
        self._client.network = FakeNetwork(name, self._client.events)
        self._client.network_labels = labels
        return self._client.network


class _FakeContainers:
    def __init__(self, client):
        self._client = client

    def run(self, image, **kwargs):
        c = self._client
        c.events.append(("run", image))
        c.run_calls.append((image, kwargs))
        if c.runner is None:  # first run = runner (order itself is asserted via events)
            c.runner = FakeContainer("runner", c.runner_statuses, c.runner_exit_code, c.events)
            return c.runner
        if c.raise_on_sut_run is not None:
            raise c.raise_on_sut_run
        c.sut = FakeContainer("sut", c.sut_statuses, 0, c.events)
        return c.sut


class FakeClient:
    """Duck-typed docker client — the only docker surface supervisor-min touches."""

    def __init__(
        self,
        runner_statuses=("running", "exited"),
        runner_exit_code=0,
        sut_statuses=("running",),
        raise_on_sut_run=None,
    ):
        self.events = []  # ordered call log — the start-order assertion target
        self.run_calls = []  # (image, kwargs) per containers.run
        self.runner_statuses = runner_statuses
        self.runner_exit_code = runner_exit_code
        self.sut_statuses = sut_statuses
        self.raise_on_sut_run = raise_on_sut_run
        self.runner = None
        self.sut = None
        self.network = None
        self.network_labels = None  # labels passed to networks.create (R14 sweep target)
        self.networks = _FakeNetworks(self)
        self.containers = _FakeContainers(self)


def run_min(tmp_path, client, **kwargs):
    return run_job(
        make_spec(),
        tmp_path,
        RUNNER_IMAGE,
        SUT_IMAGE,
        client,
        poll_interval_s=0.0,
        **kwargs,
    )


# --------------------------------------------------------------------------- #
# (1) start order: runner -> readiness gate -> SUT (G-19 supply order)
# --------------------------------------------------------------------------- #


def test_start_order_runner_then_readiness_then_sut(tmp_path):
    put_result(tmp_path)
    client = FakeClient()

    def probe(container):
        client.events.append(("probe", container.status))
        return container.status == "running"

    outcome = run_min(tmp_path, client, readiness_probe=probe)
    starts = [e for e in client.events if e[0] in ("network-create", "run", "probe")]
    net_name = network_name_for(JOB_ID)
    assert starts == [
        ("network-create", net_name),
        ("run", RUNNER_IMAGE),  # runner (clock source) strictly first
        ("probe", "running"),  # readiness gate between runner and SUT
        ("run", SUT_IMAGE),
    ]
    assert outcome.infra_error is None


# --------------------------------------------------------------------------- #
# (2) exact accounting: 1 job = 1 runner + 1 SUT + 1 network (NEG-4 선행)
# --------------------------------------------------------------------------- #


def test_one_job_spawns_exactly_one_runner_and_one_sut(tmp_path):
    put_result(tmp_path)
    client = FakeClient()
    run_min(tmp_path, client)
    assert [image for image, _ in client.run_calls] == [RUNNER_IMAGE, SUT_IMAGE]
    assert len([e for e in client.events if e[0] == "network-create"]) == 1


def test_cospawn_shares_network_and_deterministic_domain(tmp_path):
    put_result(tmp_path)
    client = FakeClient()
    run_min(tmp_path, client)
    (_, runner_kwargs), (_, sut_kwargs) = client.run_calls
    net_name = network_name_for(JOB_ID)
    assert runner_kwargs["network"] == net_name
    assert sut_kwargs["network"] == net_name
    domain = runner_kwargs["environment"]["ROS_DOMAIN_ID"]
    assert sut_kwargs["environment"]["ROS_DOMAIN_ID"] == domain
    assert 0 <= int(domain) < ROS_DOMAIN_ID_SPACE  # LOCKED §7.5 space
    assert int(domain) == allocate_ros_domain_id(JOB_ID)  # deterministic allocation


def test_job_spec_mounted_ro_and_result_dir_precreated_rw(tmp_path):
    put_result(tmp_path)
    client = FakeClient()
    run_min(tmp_path, client)
    _, runner_kwargs = client.run_calls[0]
    job_dir = tmp_path / network_name_for(JOB_ID)  # bind-safe slug dir (p4c4 colon fix)
    spec_path = job_dir / "job_spec.json"
    result_dir = job_dir / "result"
    assert runner_kwargs["volumes"][str(spec_path)] == {"bind": JOB_SPEC_MOUNT, "mode": "ro"}
    assert runner_kwargs["volumes"][str(result_dir)] == {"bind": RESULT_OUT_MOUNT, "mode": "rw"}
    assert runner_kwargs["environment"]["JOB_SPEC"] == JOB_SPEC_MOUNT  # passed BY PATH
    assert runner_kwargs["environment"]["RESULT_OUT"] == RESULT_OUT_MOUNT
    assert result_dir.is_dir()  # G-15: supervisor pre-creates the mount dir, not dockerd
    assert json.loads(spec_path.read_text(encoding="utf-8")) == make_spec()


# --------------------------------------------------------------------------- #
# (2b) bind-safe host paths for fan-out job ids — p4c4 colon-bind fix (룰링 A)
# --------------------------------------------------------------------------- #

# Fan-out job ids carry ':' (store.job_key "<request_id>:<repeat_index>"). The
# docker daemon parses bind specs as colon-delimited "src:dst:mode", so a raw
# out_dir/job_id bind SOURCE is rejected — MEASURED against the real daemon
# (G-28 anchor): `APIError 500 ... invalid volume specification:
# '/.../r0:0/job_spec.json:/cv/job_spec.json:ro'`, with a colon-free control
# running fine. Evidence: workstation
# ~/cv-infra-p2-out/p4c4/T4/L0/colon-bind-repro.txt (T4 L0, 2026-07-13).
_FANOUT_JOB_ID = "env-75c01e93a6af/r0:0"  # the EXACT job_key shape T4 L0 observed


def test_fanout_job_id_yields_colon_free_bind_sources(tmp_path, cache_env_clear):
    """Full 9-mount surface (2 seam + 3 base + 3 scratch + plugin) assembled for
    a REAL fan-out job_key: every bind SOURCE is colon-free, the job dir uses
    the SAME slug idiom as the cache scratch (비대칭 해소), and ONLY the host
    dir name changed — spec content / labels keep the verbatim job id."""
    expected = put_result(tmp_path, job_id=_FANOUT_JOB_ID)
    base, scratch_root = make_two_tier_roots(tmp_path)
    plugin = tmp_path / "scenario-dir"
    plugin.mkdir()
    client = FakeClient()
    outcome = run_job(
        make_spec(job_id=_FANOUT_JOB_ID),
        tmp_path,
        RUNNER_IMAGE,
        SUT_IMAGE,
        client,
        poll_interval_s=0.0,
        cache_root=base,
        cache_scratch_root=scratch_root,
        oracle_plugin_dir=str(plugin),
    )
    (_, runner_kwargs), _ = client.run_calls
    sources = list(runner_kwargs["volumes"])
    assert len(sources) == 9
    for source in sources:
        assert ":" not in source, f"bind source would break the 'src:dst:mode' spec: {source}"
    job_dir = tmp_path / network_name_for(_FANOUT_JOB_ID)  # scratch와 동일 슬러그 관용구
    assert str(job_dir / "job_spec.json") in sources
    assert str(job_dir / "result") in sources
    # Invariants the fix must NOT move: verbatim job id in the JOB_SPEC content,
    # in the reconciliation label, and in the returned outcome — and the job
    # still round-trips to the pre-seeded result.
    assert (
        json.loads((job_dir / "job_spec.json").read_text(encoding="utf-8"))["job_id"]
        == _FANOUT_JOB_ID
    )
    assert runner_kwargs["labels"][LABEL_JOB_ID] == _FANOUT_JOB_ID
    assert outcome == JobOutcome(_FANOUT_JOB_ID, expected, 0, None)


def test_colon_guard_positive_control(tmp_path):
    """변이 프로브 (G-28 ②): the PRE-FIX assembly (raw ``out_dir/job_id``) is
    exactly the colon-carrying source the daemon rejected — so the negative
    above is non-vacuous: reverting the slug would trip it."""
    raw_source = str(tmp_path / _FANOUT_JOB_ID / "job_spec.json")
    assert ":" in raw_source  # what the daemon saw in the T4 L0 repro
    slugged = str(tmp_path / network_name_for(_FANOUT_JOB_ID) / "job_spec.json")
    assert ":" not in slugged


# --------------------------------------------------------------------------- #
# (3) exactly-one result.json invariant — REQ-EXEC-013
# --------------------------------------------------------------------------- #


def test_exactly_one_result_is_collected(tmp_path):
    expected = put_result(tmp_path)
    client = FakeClient()
    outcome = run_min(tmp_path, client)
    assert outcome == JobOutcome(JOB_ID, expected, 0, None)


def test_zero_results_is_infra_error(tmp_path):
    client = FakeClient()
    outcome = run_min(tmp_path, client)
    assert outcome.result_path is None
    assert outcome.infra_error is not None
    assert "found 0" in outcome.infra_error


def test_two_results_violate_invariant(tmp_path):
    put_result(tmp_path)
    put_result(tmp_path, rel="nested/result.json")
    client = FakeClient()
    outcome = run_min(tmp_path, client)
    assert outcome.result_path is None
    assert outcome.infra_error is not None
    assert "found 2" in outcome.infra_error


# --------------------------------------------------------------------------- #
# (4) bounded SUT restart contract (nav2 bringup abort is terminal — G-19)
# --------------------------------------------------------------------------- #


def test_sut_early_exit_restarts_then_limit_exhaustion_is_infra_error(tmp_path):
    put_result(tmp_path)
    client = FakeClient(
        runner_statuses=("running",) * 10,  # runner keeps running throughout
        sut_statuses=("exited", "exited"),
    )
    outcome = run_min(tmp_path, client)  # default sut_restart_limit=1
    assert client.sut.restart_calls == 1  # bounded: exactly one restart attempted
    assert client.runner.restart_calls == 0  # the runner is never restarted
    assert outcome.result_path is None
    assert outcome.infra_error is not None
    assert "restart limit" in outcome.infra_error


def test_sut_recovers_after_one_restart(tmp_path):
    expected = put_result(tmp_path)
    client = FakeClient(
        runner_statuses=("running", "running", "running", "exited"),
        sut_statuses=("exited", "running", "running"),
    )
    outcome = run_min(tmp_path, client)
    assert client.sut.restart_calls == 1
    assert outcome == JobOutcome(JOB_ID, expected, 0, None)


# --------------------------------------------------------------------------- #
# (5) teardown on every path — REQ-EXEC-015 결
# --------------------------------------------------------------------------- #


def assert_torn_down(client):
    assert client.runner.remove_calls == 1
    assert client.network.remove_calls == 1
    if client.sut is not None:
        assert client.sut.remove_calls == 1
    # network goes last — containers must leave it first
    net_remove = client.events.index(("network-remove", client.network.name))
    assert net_remove > client.events.index(("remove", "runner"))


def test_happy_path_teardown_leaves_nothing(tmp_path):
    put_result(tmp_path)
    client = FakeClient()
    run_min(tmp_path, client)
    assert_torn_down(client)


def test_exception_during_sut_spawn_still_tears_down(tmp_path):
    client = FakeClient(raise_on_sut_run=RuntimeError("docker daemon fell over"))
    outcome = run_min(tmp_path, client)  # must NOT raise — infra boundary
    assert outcome.result_path is None
    assert outcome.infra_error is not None
    assert "RuntimeError" in outcome.infra_error
    assert "docker daemon fell over" in outcome.infra_error
    assert client.runner.remove_calls == 1  # runner + network cleaned despite the raise
    assert client.network.remove_calls == 1


def test_job_timeout_reports_infra_error_and_tears_down(tmp_path):
    client = FakeClient(runner_statuses=("running",) * 10, sut_statuses=("running",) * 10)
    outcome = run_min(tmp_path, client, job_timeout_s=0.0)
    assert outcome.runner_exit_code is None
    assert outcome.infra_error is not None
    assert "timeout" in outcome.infra_error
    assert_torn_down(client)


# --------------------------------------------------------------------------- #
# readiness gate edges (no SUT started on either)
# --------------------------------------------------------------------------- #


def test_runner_exit_before_ready_skips_sut_and_keeps_exit_code(tmp_path):
    client = FakeClient(runner_statuses=("exited",), runner_exit_code=2)
    outcome = run_min(tmp_path, client)
    assert [image for image, _ in client.run_calls] == [RUNNER_IMAGE]  # no SUT
    assert outcome.runner_exit_code == 2
    assert outcome.result_path is None
    assert outcome.infra_error is not None  # 0 result.json -> invariant records it


def test_readiness_timeout_is_infra_error_without_sut(tmp_path):
    client = FakeClient(runner_statuses=("created", "created"))
    outcome = run_min(tmp_path, client, readiness_timeout_s=0.0)
    assert [image for image, _ in client.run_calls] == [RUNNER_IMAGE]  # no SUT
    assert outcome.runner_exit_code is None
    assert outcome.infra_error is not None
    assert "readiness" in outcome.infra_error
    assert client.runner.remove_calls == 1
    assert client.network.remove_calls == 1


# --------------------------------------------------------------------------- #
# (6) operator runner_env pass-through (decision 2026-07-03 — no literal here)
# --------------------------------------------------------------------------- #


def test_runner_env_passes_through_and_seam_keys_win(tmp_path):
    put_result(tmp_path)
    client = FakeClient()
    operator_env = {
        "SOME_OPERATOR_CONSENT_KEY": "operator-value",  # generic — passed through verbatim
        "RESULT_OUT": "/operator/attempted/override",  # seam key — supervisor must win
    }
    run_min(tmp_path, client, runner_env=operator_env)
    (_, runner_kwargs), (_, sut_kwargs) = client.run_calls
    env = runner_kwargs["environment"]
    assert env["SOME_OPERATOR_CONSENT_KEY"] == "operator-value"
    assert env["RESULT_OUT"] == RESULT_OUT_MOUNT
    assert env["JOB_SPEC"] == JOB_SPEC_MOUNT
    assert set(sut_kwargs["environment"]) == {"ROS_DOMAIN_ID"}  # no operator leak to SUT


# --------------------------------------------------------------------------- #
# (6b) FU-14: scenario-derived ROS env (interface.adapter_config -> runner env)
# --------------------------------------------------------------------------- #


def test_adapter_config_ros_env_injected_to_runner_not_sut(tmp_path):
    put_result(tmp_path)
    client = FakeClient()
    spec = make_spec(adapter_config={"ros_distro": "jazzy", "rmw": "rmw_fastrtps_cpp"})
    run_job(spec, tmp_path, RUNNER_IMAGE, SUT_IMAGE, client, poll_interval_s=0.0)
    (_, runner_kwargs), (_, sut_kwargs) = client.run_calls
    assert runner_kwargs["environment"]["ROS_DISTRO"] == "jazzy"
    assert runner_kwargs["environment"]["RMW_IMPLEMENTATION"] == "rmw_fastrtps_cpp"
    assert set(sut_kwargs["environment"]) == {"ROS_DOMAIN_ID"}  # blackbox: no leak to SUT


def test_absent_adapter_config_keys_are_not_injected(tmp_path):
    put_result(tmp_path)
    client = FakeClient()
    run_min(tmp_path, client)  # default spec: no interface.adapter_config at all
    (_, runner_kwargs), _ = client.run_calls
    assert "ROS_DISTRO" not in runner_kwargs["environment"]
    assert "RMW_IMPLEMENTATION" not in runner_kwargs["environment"]


def test_adapter_config_injection_is_per_key(tmp_path):
    put_result(tmp_path)
    client = FakeClient()
    spec = make_spec(adapter_config={"rmw": "rmw_fastrtps_cpp"})  # ros_distro missing
    run_job(spec, tmp_path, RUNNER_IMAGE, SUT_IMAGE, client, poll_interval_s=0.0)
    (_, runner_kwargs), _ = client.run_calls
    assert "ROS_DISTRO" not in runner_kwargs["environment"]
    assert runner_kwargs["environment"]["RMW_IMPLEMENTATION"] == "rmw_fastrtps_cpp"


def test_scenario_ros_env_wins_over_operator_runner_env(tmp_path):
    put_result(tmp_path)
    client = FakeClient()
    spec = make_spec(adapter_config={"ros_distro": "jazzy", "rmw": "rmw_fastrtps_cpp"})
    operator_env = {"ROS_DISTRO": "humble", "RMW_IMPLEMENTATION": "rmw_cyclonedds_cpp"}
    run_job(
        spec,
        tmp_path,
        RUNNER_IMAGE,
        SUT_IMAGE,
        client,
        poll_interval_s=0.0,
        runner_env=operator_env,
    )
    (_, runner_kwargs), _ = client.run_calls
    assert runner_kwargs["environment"]["ROS_DISTRO"] == "jazzy"  # scenario is SoT (FU-14)
    assert runner_kwargs["environment"]["RMW_IMPLEMENTATION"] == "rmw_fastrtps_cpp"


# --------------------------------------------------------------------------- #
# (7) GPU device request — runner=Isaac gets the GPU by default, SUT never does
# --------------------------------------------------------------------------- #


def test_runner_gets_gpu_device_request_by_default_sut_does_not(tmp_path):
    put_result(tmp_path)
    client = FakeClient()
    run_min(tmp_path, client)  # default runner_gpus=True — `cv-infra run` path
    (_, runner_kwargs), (_, sut_kwargs) = client.run_calls
    (request,) = runner_kwargs["device_requests"]  # exactly one request: all GPUs
    assert request["Count"] == -1  # docker.types.DeviceRequest — `--gpus all` equivalent
    assert request["Capabilities"] == [["gpu"]]
    assert "device_requests" not in sut_kwargs  # carter nav2 is CPU-only


def test_runner_gpus_opt_out_keeps_cpu_spawn_device_free(tmp_path):
    put_result(tmp_path)
    client = FakeClient()
    run_min(tmp_path, client, runner_gpus=False)  # CPU-test opt-out (kw-only)
    (_, runner_kwargs), (_, sut_kwargs) = client.run_calls
    assert "device_requests" not in runner_kwargs
    assert "device_requests" not in sut_kwargs


# --------------------------------------------------------------------------- #
# seam-contract edges + pure helpers
# --------------------------------------------------------------------------- #


def test_missing_job_id_raises_before_any_docker_call(tmp_path):
    client = FakeClient()
    with pytest.raises(ValueError):
        run_job({"scenario": {}}, tmp_path, RUNNER_IMAGE, SUT_IMAGE, client)
    assert client.events == []  # no resource was created


def test_ros_domain_id_is_deterministic_and_in_range():
    first = allocate_ros_domain_id(JOB_ID)
    assert first == allocate_ros_domain_id(JOB_ID)
    assert 0 <= first < ROS_DOMAIN_ID_SPACE


def test_ros_domain_id_probes_past_in_use():
    first = allocate_ros_domain_id(JOB_ID)
    second = allocate_ros_domain_id(JOB_ID, in_use=frozenset({first}))
    assert second != first
    assert 0 <= second < ROS_DOMAIN_ID_SPACE


def test_ros_domain_id_exhausted_space_raises():
    with pytest.raises(ValueError):
        allocate_ros_domain_id(JOB_ID, in_use=frozenset(range(ROS_DOMAIN_ID_SPACE)))


def test_network_name_deterministic_docker_safe_collision_free():
    name = network_name_for("job/a")
    assert name == network_name_for("job/a")  # deterministic
    assert name != network_name_for("job:a")  # same slug, distinct full-id hash
    assert re.fullmatch(r"[a-zA-Z0-9][a-zA-Z0-9_.-]*", name)  # docker-safe charset


def test_package_level_lazy_export_resolves():
    import cv_infra.orchestrator as orch

    assert orch.run_job is run_job
    assert orch.JobOutcome is JobOutcome


# --------------------------------------------------------------------------- #
# (8) FU-16 asset-cache mount seam (decision 2026-07-09 D-1)
# --------------------------------------------------------------------------- #

# The D-1 정본 six binds (host subpath -> container path), asserted verbatim so a wrong
# CACHE_MOUNTS constant fails here (not derived from the module under test).
EXPECTED_CACHE_BINDS = {
    "cache/kit": "/isaac-sim/kit/cache",
    "cache/home": "/isaac-sim/.cache",
    "cache/computecache": "/isaac-sim/.nv/ComputeCache",
    "logs": "/isaac-sim/.nvidia-omniverse/logs",
    "data": "/isaac-sim/.local/share/ov/data",
    "documents": "/isaac-sim/Documents",
}


def test_no_cache_root_no_env_keeps_exactly_two_volumes(tmp_path, monkeypatch):
    monkeypatch.delenv(CACHE_ROOT_ENV, raising=False)
    put_result(tmp_path)
    client = FakeClient()
    run_min(tmp_path, client)  # cache_root defaults to None (backward compat)
    (_, runner_kwargs), _ = client.run_calls
    assert len(runner_kwargs["volumes"]) == 2  # spec(ro) + result(rw) only — regression guard
    assert all("/isaac-sim" not in v["bind"] for v in runner_kwargs["volumes"].values())


def test_cache_root_adds_six_rw_binds_exactly(tmp_path, monkeypatch):
    monkeypatch.delenv(CACHE_ROOT_ENV, raising=False)
    put_result(tmp_path)
    cache = tmp_path / "isaac-cache"
    cache.mkdir()  # supervisor requires the root to exist; subdirs it does NOT
    client = FakeClient()
    run_min(tmp_path, client, cache_root=cache)
    (_, runner_kwargs), _ = client.run_calls
    volumes = runner_kwargs["volumes"]
    assert len(volumes) == 8  # 2 seam + 6 cache
    resolved = cache.resolve()
    for subpath, bind in EXPECTED_CACHE_BINDS.items():
        host = str(resolved / subpath)
        assert volumes[host] == {"bind": bind, "mode": "rw"}  # exact bind + rw mode


def test_env_fallback_then_arg_precedence(tmp_path, monkeypatch):
    put_result(tmp_path)
    env_cache = tmp_path / "env-cache"
    env_cache.mkdir()
    arg_cache = tmp_path / "arg-cache"
    arg_cache.mkdir()

    # env alone -> env used
    monkeypatch.setenv(CACHE_ROOT_ENV, str(env_cache))
    client = FakeClient()
    run_min(tmp_path, client)
    (_, runner_kwargs), _ = client.run_calls
    assert str(env_cache.resolve() / "cache/kit") in runner_kwargs["volumes"]

    # arg + env -> arg wins, env absent
    client2 = FakeClient()
    run_min(tmp_path, client2, cache_root=arg_cache)
    (_, runner_kwargs2), _ = client2.run_calls
    assert str(arg_cache.resolve() / "cache/kit") in runner_kwargs2["volumes"]
    assert str(env_cache.resolve() / "cache/kit") not in runner_kwargs2["volumes"]


def test_missing_cache_root_raises_before_any_resource(tmp_path, monkeypatch):
    monkeypatch.delenv(CACHE_ROOT_ENV, raising=False)
    client = FakeClient()
    with pytest.raises(ValueError):
        run_min(tmp_path, client, cache_root=tmp_path / "does-not-exist")
    assert client.events == []  # no network/container created before the raise


def test_file_cache_root_raises_before_any_resource(tmp_path, monkeypatch):
    monkeypatch.delenv(CACHE_ROOT_ENV, raising=False)
    a_file = tmp_path / "not-a-dir"
    a_file.write_text("x", encoding="utf-8")
    client = FakeClient()
    with pytest.raises(ValueError):
        run_min(tmp_path, client, cache_root=a_file)
    assert client.events == []  # non-directory root is loud, pre-resource


def test_cache_mounts_never_leak_to_sut(tmp_path, monkeypatch):
    monkeypatch.delenv(CACHE_ROOT_ENV, raising=False)
    put_result(tmp_path)
    cache = tmp_path / "isaac-cache"
    cache.mkdir()
    client = FakeClient()
    run_min(tmp_path, client, cache_root=cache)
    (_, _runner_kwargs), (_, sut_kwargs) = client.run_calls
    assert "volumes" not in sut_kwargs  # SUT is a blackbox: no cache, no GPU
    assert "device_requests" not in sut_kwargs


def test_cache_root_resolved_to_host_absolute_path(tmp_path, monkeypatch):
    """Relative cache_root must still be passed as a host ABSOLUTE path (D-O/F5)."""
    monkeypatch.delenv(CACHE_ROOT_ENV, raising=False)
    put_result(tmp_path)
    (tmp_path / "rel-cache").mkdir()
    monkeypatch.chdir(tmp_path)
    client = FakeClient()
    run_min(tmp_path, client, cache_root="rel-cache")  # relative arg
    (_, runner_kwargs), _ = client.run_calls
    cache_binds = [
        host for host, spec in runner_kwargs["volumes"].items() if spec["bind"].startswith("/isaac")
    ]
    assert len(cache_binds) == 6
    for host in cache_binds:
        assert Path(host).is_absolute()  # never a relative bind
        assert host.startswith(str((tmp_path / "rel-cache").resolve()))


# --------------------------------------------------------------------------- #
# (8b) D-B per-job cache seeding: warm base = copy SOURCE only, per-job copies rw
#      (DoD-P4-15 — repaired p4c5)
# --------------------------------------------------------------------------- #

# MEASURED ANCHOR (G-28 — the external system's real behavior, not our code's guess):
# a ``:ro`` bind of the warm base does NOT make the CUDA/Kit caches read-only, it
# DISABLES them (they cannot open their lock/index files for write, so every job
# recompiles its CUDA kernels). Identical warm bytes, mount flag the only variable:
# robot_spawn 47s (ro) vs 1.05s (rw); k=4 job wall 318s -> 104s, 8/8 pass; the
# runner's own cache_delta showed entries_added=0 (the warm CONTENT was always
# sufficient — only WRITABILITY was missing). Seeding cost: 1.07s / 930MB per job.
# Evidence: agent-comms/reports/runner-2026-07-14-p4c5-experiments.md §E1/E2/E4
# (workstation ~/cv-infra-p2-out/p4c5/{E1,E2,E4}/).
#
# So: the three warm tiers are COPIED into the per-job scratch and bound rw from
# there, the three runtime dirs are created empty and bound rw, and the shared base
# is never bound into any container — DoD-P4-15's "공유 캐시 쓰기/손상 0" becomes
# structural instead of mount-flag-dependent. Asserted verbatim below (never derived
# from the module under test).
EXPECTED_SEEDED_BINDS = {
    "cache/kit": "/isaac-sim/kit/cache",
    "cache/home": "/isaac-sim/.cache",
    "cache/computecache": "/isaac-sim/.nv/ComputeCache",
}
EXPECTED_SCRATCH_BINDS = {
    "logs": "/isaac-sim/.nvidia-omniverse/logs",
    "data": "/isaac-sim/.local/share/ov/data",
    "documents": "/isaac-sim/Documents",
}
WARM_BYTES = b"warm-cache-bytes"  # one file per warm tier — makes the seed-cost log exact


@pytest.fixture()
def cache_env_clear(monkeypatch):
    """No ambient cache env bleeds into the assertions (both knobs)."""
    monkeypatch.delenv(CACHE_ROOT_ENV, raising=False)
    monkeypatch.delenv(CACHE_SCRATCH_ROOT_ENV, raising=False)
    return monkeypatch


def make_two_tier_roots(tmp_path):
    """A PROVISIONED warm base (what M5 ``warm_cache.sh warm`` leaves) + a scratch root.

    The base tiers now carry content because the supervisor READS them (copy source);
    an empty/absent tier is a loud config error, not a silent cold run.
    """
    base = tmp_path / "cache-base"
    scratch_root = tmp_path / "cache-scratch"
    scratch_root.mkdir()
    for subpath in EXPECTED_SEEDED_BINDS:
        tier = base / subpath
        tier.mkdir(parents=True)
        (tier / "warm.bin").write_bytes(WARM_BYTES)
    return base, scratch_root


def tree_snapshot(root: Path) -> dict[str, bytes]:
    """Content snapshot of a tree (P4-15: the shared base must be byte-identical after)."""
    return {
        str(path.relative_to(root)): path.read_bytes()
        for path in sorted(root.rglob("*"))
        if path.is_file()
    }


def test_seeding_binds_six_rw_copies_and_never_the_shared_base(tmp_path, cache_env_clear):
    put_result(tmp_path)
    base, scratch_root = make_two_tier_roots(tmp_path)
    client = FakeClient()
    seen = {}

    def probe(container):  # what the runner sees WHILE it runs (seed + mkdir are pre-spawn)
        job_scratch = scratch_root / network_name_for(JOB_ID)
        seen["tiers"] = sorted(p.name for p in (job_scratch / "cache").iterdir() if p.is_dir())
        seen["runtime"] = sorted(
            p.name for p in job_scratch.iterdir() if p.is_dir() and p.name != "cache"
        )
        seen["warm"] = (job_scratch / "cache" / "kit" / "warm.bin").read_bytes()
        return container.status == "running"

    run_min(
        tmp_path, client, cache_root=base, cache_scratch_root=scratch_root, readiness_probe=probe
    )
    (_, runner_kwargs), (_, sut_kwargs) = client.run_calls
    volumes = runner_kwargs["volumes"]
    job_scratch = scratch_root.resolve() / network_name_for(JOB_ID)
    assert len(volumes) == 8  # 2 seam + 3 seeded cache + 3 runtime scratch
    for subpath, bind in {**EXPECTED_SEEDED_BINDS, **EXPECTED_SCRATCH_BINDS}.items():
        assert volumes[str(job_scratch / subpath)] == {"bind": bind, "mode": "rw"}
    # P4-15, structurally: the shared base is a copy SOURCE — it appears in the
    # container spec neither rw nor ro. And NO cache bind may be read-only: a cache
    # the runner cannot write is a cache that turns itself OFF (measured anchor above).
    assert not any(source.startswith(str(base.resolve())) for source in volumes)
    assert not any(spec["mode"] == "ro" for spec in volumes.values() if "isaac-sim" in spec["bind"])
    assert seen["tiers"] == ["computecache", "home", "kit"]  # seeded BEFORE the spawn
    assert seen["warm"] == WARM_BYTES  # ...carrying the warm content, not an empty tier
    assert seen["runtime"] == ["data", "documents", "logs"]  # pre-created (G-15)
    assert not job_scratch.exists()  # per-job tree discarded when the job ended (stateless)
    assert "volumes" not in sut_kwargs  # SUT stays a blackbox — no cache at all


def test_runner_writes_hit_the_copy_and_leave_the_shared_base_byte_identical(
    tmp_path, cache_env_clear
):
    """DoD-P4-15 실질 불변식: the job writes its cache freely; the shared base is unchanged.

    The fake runner does what the REAL one does (T4 cache_delta: in-place updates of
    lock/index/material_cache.json, entries_added=0) — write into the mounted cache.
    Pre-repair this write attempt hit a ``:ro`` mount and the cache silently disabled
    itself; now it lands in the per-job copy and the base never sees it.
    """
    put_result(tmp_path)
    base, scratch_root = make_two_tier_roots(tmp_path)
    before = tree_snapshot(base)
    client = FakeClient()
    wrote = {}

    def probe(container):
        job_scratch = scratch_root / network_name_for(JOB_ID)
        (job_scratch / "cache" / "kit" / "warm.bin").write_bytes(b"rewritten-in-place")
        (job_scratch / "cache" / "computecache" / "kernel.cubin").write_bytes(b"jit-output")
        # Read back INSIDE the job — the per-job tree is gone by the time we assert.
        wrote["kit"] = (job_scratch / "cache" / "kit" / "warm.bin").read_bytes()
        wrote["cc"] = (job_scratch / "cache" / "computecache" / "kernel.cubin").read_bytes()
        return container.status == "running"

    outcome = run_min(
        tmp_path, client, cache_root=base, cache_scratch_root=scratch_root, readiness_probe=probe
    )
    # Both halves, or the assertion is vacuous: the write LANDED (the mount the runner
    # got is writable — the whole repair) ...
    assert outcome.infra_error is None
    assert wrote == {"kit": b"rewritten-in-place", "cc": b"jit-output"}
    # ... and it landed in the COPY: the shared base is byte-identical (DoD-P4-15
    # 공유 캐시 쓰기/손상 0 — structural, since the base is never even mounted).
    assert tree_snapshot(base) == before


def test_seeded_and_scratch_paths_are_unique_per_job(tmp_path, cache_env_clear):
    base, scratch_root = make_two_tier_roots(tmp_path)
    scratch_hosts: dict[str, set] = {}
    for job_id in ("job-1", "job-2"):
        put_result(tmp_path, job_id=job_id)
        client = FakeClient()
        run_job(
            make_spec(job_id=job_id),
            tmp_path,
            RUNNER_IMAGE,
            SUT_IMAGE,
            client,
            poll_interval_s=0.0,
            cache_root=base,
            cache_scratch_root=scratch_root,
        )
        (_, runner_kwargs), _ = client.run_calls
        scratch_hosts[job_id] = {
            host
            for host, spec in runner_kwargs["volumes"].items()
            if spec["mode"] == "rw" and host.startswith(str(scratch_root.resolve()))
        }
        assert len(scratch_hosts[job_id]) == 6  # 3 seeded + 3 runtime, all per-job
    assert scratch_hosts["job-1"].isdisjoint(scratch_hosts["job-2"])  # 잡별 고유 경로


def test_two_tier_scratch_discarded_even_when_spawn_raises(tmp_path, cache_env_clear):
    base, scratch_root = make_two_tier_roots(tmp_path)
    client = FakeClient(raise_on_sut_run=RuntimeError("docker daemon fell over"))
    outcome = run_min(tmp_path, client, cache_root=base, cache_scratch_root=scratch_root)
    assert outcome.infra_error is not None
    # finally-discard held — the ~1GB seeded copy dies with the job on the exception path
    assert not (scratch_root / network_name_for(JOB_ID)).exists()


def test_missing_base_tier_is_loud_and_leaves_no_partial_scratch(tmp_path, cache_env_clear):
    # The seeding mode READS the base tiers, so an unprovisioned tier (M5
    # warm_cache.sh never ran) must be loud: seeding an empty tier would run
    # all-cold while every dashboard says warm (G-26 — the exact failure class
    # this repair closes).
    base, scratch_root = make_two_tier_roots(tmp_path)
    shutil.rmtree(base / "cache" / "computecache")
    client = FakeClient()
    with pytest.raises(ValueError, match="cache base tier"):
        run_min(tmp_path, client, cache_root=base, cache_scratch_root=scratch_root)
    assert client.events == []  # loud + pre-resource (no network, no container)
    assert not (scratch_root / network_name_for(JOB_ID)).exists()  # partial seed discarded


def test_unwritable_seed_is_loud_not_a_silently_disabled_cache(tmp_path, cache_env_clear):
    # G-15 + the repair's whole point: a copy the runner (uid 1234) cannot WRITE is a
    # cache that turns itself off — 47s of CUDA JIT per job, and nothing in the logs
    # but a 1-line EROFS (T4 §E1). ``cp -a`` preserves the base's ownership/mode, so an
    # unwritable base tier yields an unwritable copy: refuse loudly instead of running a
    # cold job that every dashboard reports as warm.
    base, scratch_root = make_two_tier_roots(tmp_path)
    (base / "cache" / "kit").chmod(0o500)  # r-x: readable (copyable) but not writable
    client = FakeClient()
    try:
        with pytest.raises(RuntimeError, match="not owner-writable"):
            run_min(tmp_path, client, cache_root=base, cache_scratch_root=scratch_root)
        assert client.events == []  # loud + pre-resource
    finally:
        (base / "cache" / "kit").chmod(0o700)  # let tmp_path teardown remove the tree


def test_cache_seed_cost_is_logged_per_job(tmp_path, cache_env_clear, capsys):
    # G-26 feature-on gate #2: a per-job copy is invisible in the mount spec (it
    # looks like any rw bind), so the seeding emits its own structured line with the
    # MEASURED cost — operators see the price (1.07s/930MB on the workstation) and QA
    # verifies "the seeding actually ran" from a file, never from narration.
    put_result(tmp_path)
    base, scratch_root = make_two_tier_roots(tmp_path)
    client = FakeClient()
    run_min(tmp_path, client, cache_root=base, cache_scratch_root=scratch_root)
    lines = [
        line
        for line in capsys.readouterr().err.splitlines()
        if line.startswith("[cv-supervisor] cache-seed ")
    ]
    assert len(lines) == 1  # one job, one seed
    logged = json.loads(lines[0].removeprefix("[cv-supervisor] cache-seed "))
    assert logged["job_id"] == JOB_ID
    assert logged["seconds"] >= 0.0
    assert logged["bytes"] == 3 * len(WARM_BYTES)  # bytes measured ON DISK after the copy
    assert [tier["target"] for tier in logged["tiers"]] == list(EXPECTED_SEEDED_BINDS.values())
    for tier in logged["tiers"]:
        assert tier["source"].startswith(str(base.resolve()))  # base = copy SOURCE only
        assert tier["bytes"] == len(WARM_BYTES)


def test_single_tier_never_seeds_and_never_logs_a_seed(tmp_path, cache_env_clear, capsys):
    # 동결 P2 계약: base root ALONE keeps the six rw binds pointing AT the base — no
    # copy, no seed line, no scratch dir. The repair must not leak into that path.
    put_result(tmp_path)
    base, _ = make_two_tier_roots(tmp_path)
    client = FakeClient()
    run_min(tmp_path, client, cache_root=base)
    (_, runner_kwargs), _ = client.run_calls
    volumes = runner_kwargs["volumes"]
    assert len(volumes) == 8  # 2 seam + 6 cache, all from the base itself
    for subpath, bind in EXPECTED_CACHE_BINDS.items():
        assert volumes[str(base.resolve() / subpath)] == {"bind": bind, "mode": "rw"}
    assert "cache-seed" not in capsys.readouterr().err


def test_empty_cache_env_values_are_loud_not_silent_unset(tmp_path, cache_env_clear):
    # G-26 변종: CV_ISAAC_CACHE_ROOT="" must never silently mean '0 cache
    # mounts' (an all-cold run measured as warm); same for the scratch env.
    client = FakeClient()
    cache_env_clear.setenv(CACHE_ROOT_ENV, "")
    with pytest.raises(ValueError, match="empty"):
        run_min(tmp_path, client)
    assert client.events == []  # loud + pre-resource

    cache_env_clear.delenv(CACHE_ROOT_ENV, raising=False)
    base, _ = make_two_tier_roots(tmp_path)
    cache_env_clear.setenv(CACHE_SCRATCH_ROOT_ENV, "")
    with pytest.raises(ValueError, match="empty"):
        run_min(tmp_path, client, cache_root=base)
    assert client.events == []


def test_scratch_root_without_base_root_is_loud(tmp_path, cache_env_clear):
    _, scratch_root = make_two_tier_roots(tmp_path)
    client = FakeClient()
    with pytest.raises(ValueError, match="without cache_root"):
        run_min(tmp_path, client, cache_scratch_root=scratch_root)
    assert client.events == []  # half-configured 2-tier never launches anything


def test_missing_scratch_root_raises_before_any_resource(tmp_path, cache_env_clear):
    base, _ = make_two_tier_roots(tmp_path)
    client = FakeClient()
    with pytest.raises(ValueError, match="cache_scratch_root"):
        run_min(tmp_path, client, cache_root=base, cache_scratch_root=tmp_path / "nope")
    assert client.events == []


def test_runner_mounts_structured_log_is_assertable(tmp_path, cache_env_clear, capsys):
    # G-26 feature-on gate: the spawn emits ONE structured line carrying the
    # full mount spec (count / ro flags / paths) — tests and operators assert
    # the cache actually engaged instead of trusting a silent no-op.
    put_result(tmp_path)
    base, scratch_root = make_two_tier_roots(tmp_path)
    plugin = tmp_path / "scenario-dir"
    plugin.mkdir()
    client = FakeClient()
    run_min(
        tmp_path,
        client,
        cache_root=base,
        cache_scratch_root=scratch_root,
        oracle_plugin_dir=str(plugin),
    )
    lines = [
        line
        for line in capsys.readouterr().err.splitlines()
        if line.startswith("[cv-supervisor] runner-mounts ")
    ]
    assert len(lines) == 1  # one spawn, one line
    logged = json.loads(lines[0].removeprefix("[cv-supervisor] runner-mounts "))
    assert logged["job_id"] == JOB_ID
    mounts = {entry["target"]: entry for entry in logged["mounts"]}
    assert len(mounts) == 9  # 3 seeded cache + 3 runtime scratch + plugin + spec + result
    for bind in {**EXPECTED_SEEDED_BINDS, **EXPECTED_SCRATCH_BINDS}.values():
        # every cache bind: rw, sourced from the PER-JOB tree (never the shared base)
        assert mounts[bind]["mode"] == "rw"
        assert mounts[bind]["source"].startswith(str(scratch_root.resolve()))
        assert not mounts[bind]["source"].startswith(str(base.resolve()))
    assert mounts[str(plugin.resolve())]["mode"] == "ro"  # D-1 plugin bind rides the log too
    assert mounts[JOB_SPEC_MOUNT]["mode"] == "ro"
    assert mounts[RESULT_OUT_MOUNT]["mode"] == "rw"


# --------------------------------------------------------------------------- #
# (9) D-1 custom-oracle plugin dir (decision 2026-07-11 — wiring contract #3)
# --------------------------------------------------------------------------- #


def test_oracle_plugin_dir_none_keeps_container_spec_unchanged(tmp_path, monkeypatch):
    monkeypatch.delenv(CACHE_ROOT_ENV, raising=False)
    put_result(tmp_path)
    client = FakeClient()
    run_min(tmp_path, client, oracle_plugin_dir=None)  # explicit None == the default
    (_, runner_kwargs), (_, sut_kwargs) = client.run_calls
    assert ORACLE_PLUGIN_DIR_ENV not in runner_kwargs["environment"]
    assert len(runner_kwargs["volumes"]) == 2  # spec(ro) + result(rw) only — unchanged
    assert "volumes" not in sut_kwargs


def test_oracle_plugin_dir_mounts_ro_same_absolute_path_plus_env_on_runner(tmp_path):
    put_result(tmp_path)
    plugin = tmp_path / "scenario-dir"
    plugin.mkdir()
    client = FakeClient()
    run_min(tmp_path, client, oracle_plugin_dir=str(plugin))
    (_, runner_kwargs), _ = client.run_calls
    host = str(plugin.resolve())
    # SAME absolute path on both sides, read-only (G-26 idiom — runner sys.path's it),
    # and the env value is that very string (D-1 wiring contract #3).
    assert runner_kwargs["volumes"][host] == {"bind": host, "mode": "ro"}
    assert runner_kwargs["environment"][ORACLE_PLUGIN_DIR_ENV] == host


def test_oracle_plugin_dir_never_leaks_to_sut(tmp_path):
    put_result(tmp_path)
    plugin = tmp_path / "scenario-dir"
    plugin.mkdir()
    client = FakeClient()
    run_min(tmp_path, client, oracle_plugin_dir=str(plugin))
    _, (_, sut_kwargs) = client.run_calls
    assert "volumes" not in sut_kwargs  # no mount on the SUT (blackbox)
    assert set(sut_kwargs["environment"]) == {"ROS_DOMAIN_ID"}  # no env leak either


def test_missing_oracle_plugin_dir_raises_before_any_resource(tmp_path):
    client = FakeClient()
    with pytest.raises(ValueError):
        run_min(tmp_path, client, oracle_plugin_dir=str(tmp_path / "does-not-exist"))
    assert client.events == []  # loud + pre-resource — never a silent no-op (G-26)
