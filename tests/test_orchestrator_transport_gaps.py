"""p5c5 T2 — transport-gap repairs in the job lifecycle (M3 supervisor), CPU-only.

Three surgical fixes, all provable without docker/GPU against duck-typed fakes:

* **(a) pull-stall liveness watchdog** — ``containers.run`` pulls a missing image
  implicitly and BLOCKING, upstream of every existing watchdog (``job_timeout_s`` only
  starts after the SUT container exists), so a wedged GHCR layer pull hung a live E2E
  37 min+ (history 2026-07-21 놀란점 3). ``run_job`` now makes both images present first,
  bounding the pull by a progress-liveness watchdog: no progress within the window ->
  ``ImagePullStalled`` -> finite ``infra_error`` (FAILED), never an unbounded hang.
* **(b) SUT-present order gate** — the SUT image is made present BEFORE the runner
  container starts, so the runner never begins its mission against a still-pulling SUT; a
  SUT pull stall fails explicitly with NO runner container started (no mission-timeout
  masquerade).
* **(c) reference-frame** — the hostification helper unit edges (the full fold->wire->
  report path lives in ``test_orchestrator_result_capture.py``).

The pull watchdog runs a daemon drain thread; a stalling fake blocks on an Event the test
releases in teardown so no thread lingers. Stdlib + pytest only (no docker import — the
image-absent exception is duck-typed by class NAME, exactly like the fake docker client).
"""

from __future__ import annotations

import inspect
import json
import threading
from pathlib import Path

import pytest

from cv_infra.orchestrator.supervisor import (
    DEFAULT_PULL_STALL_TIMEOUT_S,
    RESULT_OUT_MOUNT,
    ImagePullStalled,
    _container_to_host_path,
    _ensure_image_present,
    _hostify_artifact_paths,
    _pull_with_liveness,
    run_job,
)

# Reuse the frozen single-job fake (containers/networks/events/run_calls) — READ-only
# import, the same cross-test reuse test_orchestrator_rest_glue.py already does.
from tests.test_supervisor_min import (
    RUNNER_IMAGE,
    SUT_IMAGE,
    FakeClient,
    make_spec,
    put_result,
)

# --------------------------------------------------------------------------- #
# Duck-typed image/registry surface (the ONLY new docker surface T2 touches).
# --------------------------------------------------------------------------- #


class ImageNotFound(Exception):
    """Duck-typed docker.errors.ImageNotFound — matched by class NAME by the module."""


class _Images:
    def __init__(self, present):
        self._present = set(present)
        self.get_calls = []

    def get(self, image):
        self.get_calls.append(image)
        if image not in self._present:
            raise ImageNotFound(image)
        return object()


def _healthy_pull(layers=3):
    """A pull that emits `layers` progress events then completes (drain finishes fast)."""

    def _make():
        return [{"status": "Downloading", "id": f"layer{i}"} for i in range(layers)]

    return _make


def _stalling_pull(release: threading.Event):
    """A pull that emits NO progress and blocks until `release` is set (never in a healthy
    window) — the drain thread wedges in next(), progress never advances -> stall."""

    def _make():
        def _gen():
            release.wait()  # blocks the first next(); test releases it in teardown
            return
            yield  # unreachable — makes this a generator

        return _gen()

    return _make


def _erroring_pull(exc: Exception):
    """A pull that raises mid-stream (registry/daemon fault) — surfaced as infra_error."""

    def _make():
        def _gen():
            raise exc
            yield  # unreachable

        return _gen()

    return _make


class _ApiPull:
    def __init__(self, script):
        self._script = script
        self.pull_calls = []

    def pull(self, image, stream=False, decode=False):
        self.pull_calls.append(image)
        return self._script[image]()


class TransportFakeClient(FakeClient):
    """FakeClient + a duck-typed ``images``/``api`` surface for the pull-present gate."""

    def __init__(self, *, present=(), pull_script=None, **kw):
        super().__init__(**kw)
        self.images = _Images(present)
        self.api = _ApiPull(pull_script or {})


def _run(tmp_path, client, **kw):
    """run_job with a >0 pull poll (the pull monitor floors 0 anyway) and small stall."""
    return run_job(
        make_spec(),
        tmp_path,
        RUNNER_IMAGE,
        SUT_IMAGE,
        client,
        poll_interval_s=0.0,
        **kw,
    )


def _ensure_lines(capsys):
    return [
        line
        for line in capsys.readouterr().err.splitlines()
        if line.startswith("[cv-supervisor] image-ensure ")
    ]


# --------------------------------------------------------------------------- #
# (a) pull-stall liveness watchdog — finite termination, never an unbounded hang
# --------------------------------------------------------------------------- #


def test_pull_with_liveness_raises_on_stall_in_finite_time():
    release = threading.Event()
    client = TransportFakeClient(pull_script={SUT_IMAGE: _stalling_pull(release)})
    try:
        with pytest.raises(ImagePullStalled, match="no progress"):
            # tiny window -> the stall is detected almost immediately (finite).
            _pull_with_liveness(
                client, SUT_IMAGE, kind="sut", stall_timeout_s=0.05, poll_interval_s=0.01
            )
    finally:
        release.set()  # let the daemon drain thread exit cleanly


def test_pull_with_liveness_completes_when_progress_flows():
    client = TransportFakeClient(pull_script={SUT_IMAGE: _healthy_pull()})
    # generous window: a healthy (progressing) pull must NOT be false-killed.
    _pull_with_liveness(client, SUT_IMAGE, kind="sut", stall_timeout_s=5.0, poll_interval_s=0.01)
    assert client.api.pull_calls == [SUT_IMAGE]


def test_pull_registry_error_mid_stream_is_surfaced():
    boom = RuntimeError("manifest unknown")
    client = TransportFakeClient(pull_script={SUT_IMAGE: _erroring_pull(boom)})
    with pytest.raises(RuntimeError, match="manifest unknown"):
        _pull_with_liveness(
            client, SUT_IMAGE, kind="sut", stall_timeout_s=5.0, poll_interval_s=0.01
        )


def test_sut_pull_stall_fails_job_finite_and_starts_no_container(tmp_path):
    # runner present (its ensure passes), SUT absent + stalling pull -> the job fails in
    # finite time with an infra_error, and (b) NO container was ever started.
    release = threading.Event()
    client = TransportFakeClient(
        present={RUNNER_IMAGE}, pull_script={SUT_IMAGE: _stalling_pull(release)}
    )
    try:
        outcome = _run(tmp_path, client, pull_stall_timeout_s=0.05)
    finally:
        release.set()
    assert outcome.result_path is None
    assert outcome.infra_error is not None
    assert "ImagePullStalled" in outcome.infra_error and SUT_IMAGE in outcome.infra_error
    # (b) order gate: the runner mission never ran — no containers, no network.
    assert client.run_calls == []
    assert client.network is None
    assert client.events == []


def test_runner_pull_stall_fails_job_before_sut_ensure(tmp_path):
    release = threading.Event()
    client = TransportFakeClient(present=set(), pull_script={RUNNER_IMAGE: _stalling_pull(release)})
    try:
        outcome = _run(tmp_path, client, pull_stall_timeout_s=0.05)
    finally:
        release.set()
    assert outcome.infra_error is not None and RUNNER_IMAGE in outcome.infra_error
    assert client.run_calls == []
    # SUT image was never even probed — runner ensure raised first.
    assert SUT_IMAGE not in client.images.get_calls
    assert client.api.pull_calls == [RUNNER_IMAGE]


# --------------------------------------------------------------------------- #
# (b) SUT-present order gate + happy paths (present / pulled)
# --------------------------------------------------------------------------- #


def test_both_images_present_skips_pull_and_runs_normally(tmp_path):
    put_result(tmp_path)
    client = TransportFakeClient(present={RUNNER_IMAGE, SUT_IMAGE})
    outcome = _run(tmp_path, client)
    assert outcome.infra_error is None
    assert outcome.result_path is not None
    assert client.api.pull_calls == []  # both present -> no registry round-trip
    # both images probed present BEFORE either container started (order gate).
    assert client.images.get_calls == [RUNNER_IMAGE, SUT_IMAGE]
    assert [image for image, _ in client.run_calls] == [RUNNER_IMAGE, SUT_IMAGE]


def test_absent_sut_is_pulled_then_job_proceeds(tmp_path):
    put_result(tmp_path)
    client = TransportFakeClient(present={RUNNER_IMAGE}, pull_script={SUT_IMAGE: _healthy_pull()})
    outcome = _run(tmp_path, client)
    assert outcome.infra_error is None
    assert outcome.result_path is not None
    assert client.api.pull_calls == [SUT_IMAGE]  # SUT pulled once, healthy
    assert [image for image, _ in client.run_calls] == [RUNNER_IMAGE, SUT_IMAGE]


def test_ensure_emits_structured_feature_on_line(tmp_path, capsys):
    put_result(tmp_path)
    client = TransportFakeClient(present={RUNNER_IMAGE}, pull_script={SUT_IMAGE: _healthy_pull()})
    _run(tmp_path, client)
    statuses = {}
    for line in _ensure_lines(capsys):
        payload = json.loads(line.removeprefix("[cv-supervisor] image-ensure "))
        statuses[payload["image"]] = (payload["kind"], payload["status"])
    assert statuses[RUNNER_IMAGE] == ("runner", "present")
    assert statuses[SUT_IMAGE] == ("sut", "pulled")


def test_legacy_fake_without_images_api_is_backward_compatible(tmp_path, capsys):
    # The frozen FakeClient (test_supervisor_min) has no images/api surface — the gate is
    # a logged no-op, and the existing single-job behavior is byte-for-byte unchanged.
    put_result(tmp_path)
    client = FakeClient()  # no .images, no .api
    outcome = _run(tmp_path, client)
    assert outcome.result_path is not None and outcome.infra_error is None
    assert [image for image, _ in client.run_calls] == [RUNNER_IMAGE, SUT_IMAGE]
    lines = _ensure_lines(capsys)
    assert len(lines) == 2 and all("no-images-api" in line for line in lines)


def test_default_pull_stall_timeout_is_a_documented_placeholder():
    # non-drift guard: run_job's default is the single module constant (실측-후-기입).
    assert inspect.signature(run_job).parameters["pull_stall_timeout_s"].default == (
        DEFAULT_PULL_STALL_TIMEOUT_S
    )
    assert DEFAULT_PULL_STALL_TIMEOUT_S > 0


# --------------------------------------------------------------------------- #
# (c) hostification helper edges (frame translation; field names unchanged)
# --------------------------------------------------------------------------- #


def test_container_paths_under_mount_are_rerooted_at_host():
    host_root = Path("/host/out/result")
    doc = {
        "verdict": "pass",
        "metrics": {"x": 1},
        "artifacts": {"mcap": f"{RESULT_OUT_MOUNT}/bag/x.mcap", "mp4": f"{RESULT_OUT_MOUNT}/r.mp4"},
    }
    out = _hostify_artifact_paths(doc, host_root)
    assert out["artifacts"]["mcap"] == str(host_root / "bag" / "x.mcap")
    assert out["artifacts"]["mp4"] == str(host_root / "r.mp4")
    assert out["metrics"] == {"x": 1} and out["verdict"] == "pass"  # nothing else touched
    assert doc["artifacts"]["mcap"] == f"{RESULT_OUT_MOUNT}/bag/x.mcap"  # input not mutated


def test_path_not_under_mount_is_left_verbatim():
    # honest passthrough — never guess a mapping we cannot prove (G-26).
    assert _container_to_host_path("/somewhere/else/x.mcap", Path("/host/out")) == (
        "/somewhere/else/x.mcap"
    )


def test_none_and_missing_artifacts_pass_through_unchanged():
    assert _hostify_artifact_paths({"artifacts": {"mcap": None, "mp4": None}}, Path("/h")) == {
        "artifacts": {"mcap": None, "mp4": None}
    }
    no_artifacts = {"verdict": "pass"}
    assert _hostify_artifact_paths(no_artifacts, Path("/h")) is no_artifacts  # no-op returns input


def test_ensure_image_present_return_values():
    present = TransportFakeClient(present={SUT_IMAGE})
    assert (
        _ensure_image_present(
            present, SUT_IMAGE, kind="sut", stall_timeout_s=5.0, poll_interval_s=0.01
        )
        == "present"
    )
    pulled = TransportFakeClient(present=set(), pull_script={SUT_IMAGE: _healthy_pull()})
    assert (
        _ensure_image_present(
            pulled, SUT_IMAGE, kind="sut", stall_timeout_s=5.0, poll_interval_s=0.01
        )
        == "pulled"
    )
    assert (
        _ensure_image_present(
            FakeClient(), SUT_IMAGE, kind="sut", stall_timeout_s=5.0, poll_interval_s=0.01
        )
        == "unknown"
    )
