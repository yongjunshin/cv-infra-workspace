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
from pathlib import Path

import pytest

from cv_infra.orchestrator.supervisor import (
    CACHE_ROOT_ENV,
    JOB_SPEC_MOUNT,
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
    """Pre-create a result file where the runner would have written it."""
    path = tmp_path / job_id / "result" / rel
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

    def create(self, name, driver=None):
        self._client.events.append(("network-create", name))
        self._client.network = FakeNetwork(name, self._client.events)
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
    spec_path = tmp_path / JOB_ID / "job_spec.json"
    result_dir = tmp_path / JOB_ID / "result"
    assert runner_kwargs["volumes"][str(spec_path)] == {"bind": JOB_SPEC_MOUNT, "mode": "ro"}
    assert runner_kwargs["volumes"][str(result_dir)] == {"bind": RESULT_OUT_MOUNT, "mode": "rw"}
    assert runner_kwargs["environment"]["JOB_SPEC"] == JOB_SPEC_MOUNT  # passed BY PATH
    assert runner_kwargs["environment"]["RESULT_OUT"] == RESULT_OUT_MOUNT
    assert result_dir.is_dir()  # G-15: supervisor pre-creates the mount dir, not dockerd
    assert json.loads(spec_path.read_text(encoding="utf-8")) == make_spec()


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
