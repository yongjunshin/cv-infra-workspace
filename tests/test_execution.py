"""Execution seam unit tests (case = container) — duck-typed fake docker, CPU-only.

Proves without docker or a GPU: the mount table (checkout ``:ro`` + the case's host
output dir overlaid rw on the declared output dir, cache tiers seeded / single / none),
the container env (consent passthrough, ``CV_SEED``, driver caps), the GPU device
request present for the sim and ABSENT for the oracle, the executable entrypoint + argv
+ working dir, timeout -> teardown, exception -> teardown + scratch discard, the
honest empty-output zip and its oversize manifest, the image present/pulled/stalled
gate, and the ``cp -a`` seeding guards.
"""

from __future__ import annotations

import stat
import threading
import time
import types
import zipfile
from pathlib import Path

import pytest

from cv_infra.execution import (
    CACHE_BASE_MOUNTS,
    CACHE_MOUNTS,
    CACHE_ROOT_ENV,
    CACHE_SCRATCH_MOUNTS,
    CACHE_SCRATCH_ROOT_ENV,
    CASE_TIMEOUT_MARKER,
    CHECKOUT_MOUNT,
    CONSENT_ENV_KEYS,
    DEFAULT_SIM_IMAGE,
    EXECUTE_ENTRYPOINT,
    EXECUTE_SCRIPT,
    ImagePullStalled,
    _assert_runner_writable,
    _cache_volumes,
    _discard_scratch,
    _ensure_image_present,
    _image_namespace,
    _image_present,
    _pull_with_liveness,
    _teardown,
    gpu_device_requests,
    resolve_docker_client,
    run_key,
    run_oracle,
    run_sim_case,
    slug_for,
    zip_output,
)
from tests.conftest import (
    ORACLE_SCRIPT,
    OUTPUT_DIR,
    SIM_IMAGE,
    SIM_IMAGE_DIGEST12,
    SIM_SCRIPT,
    FakeClient,
    FakeContainer,
    ImageNotFound,
    make_case,
    make_checkout,
    make_spec,
)

OPERATOR_ENV = {"ACCEPT_EULA": "Y", "PRIVACY_CONSENT": "Y", "HOME": "/home/runner"}


def run_case(tmp_path, client, spec=None, case=None, **kwargs):
    """``run_sim_case`` with a zero poll interval (tests must not sleep)."""
    checkout = spec.checkout if spec is not None else make_checkout(tmp_path)
    return run_sim_case(
        spec if spec is not None else make_spec(checkout),
        case if case is not None else make_case(),
        client,
        run_dir=tmp_path / "run",
        operator_env=OPERATOR_ENV,
        poll_interval_s=0.0,
        **kwargs,
    )


# --------------------------------------------------------------------------- #
# (1) naming: one slug names the container, the case dir, the log and the zip
# --------------------------------------------------------------------------- #


def test_slug_is_docker_safe_and_distinguishes_repeats_of_one_case():
    case = make_case()
    first = slug_for(run_key(case))
    second = slug_for(run_key(make_case(repeat=1)))

    assert first.startswith("cvc-") and ":" not in first  # a ':' breaks the bind spec
    assert first != second  # repeats must not share a container name / dir / zip
    assert slug_for(run_key(case)) == first  # deterministic


def test_a_key_with_no_usable_characters_still_yields_a_name():
    """The charset filter can eat the whole key; the hash suffix keeps it unique."""
    assert slug_for("///").startswith("cvc-case-")


# --------------------------------------------------------------------------- #
# (2) the mount table + call shape — what actually reaches dockerd
# --------------------------------------------------------------------------- #


def test_sim_case_call_shape_is_the_documented_contract(tmp_path):
    checkout = make_checkout(tmp_path)
    spec = make_spec(checkout)
    case = make_case()
    client = FakeClient()

    execution = run_case(tmp_path, client, spec=spec, case=case)

    image, kwargs = client.run_calls[0]
    assert image == SIM_IMAGE
    # G-14: the stock image's ENTRYPOINT would swallow the arguments — override it.
    assert kwargs["entrypoint"] == EXECUTE_ENTRYPOINT
    assert kwargs["command"] == ["-lc", EXECUTE_SCRIPT, SIM_SCRIPT, "--lighting=dim"]
    assert kwargs["working_dir"] == CHECKOUT_MOUNT  # local parity with ./verify/sim.py
    assert kwargs["shm_size"] == "8g"
    assert kwargs["detach"] is True
    assert kwargs["name"].endswith("-sim")
    assert kwargs["labels"]["cv-infra.case_id"] == case.case_id
    volumes = kwargs["volumes"]
    assert volumes[str(checkout.resolve())] == {"bind": CHECKOUT_MOUNT, "mode": "ro"}
    assert volumes[str(execution.out_dir.resolve())] == {
        "bind": f"{CHECKOUT_MOUNT}/{OUTPUT_DIR}",
        "mode": "rw",
    }
    assert execution.rc == 0 and execution.error is None
    assert execution.wall_s >= 0.0


def test_case_env_passes_consent_through_and_adds_seed_and_driver_caps(tmp_path):
    client = FakeClient()
    run_case(tmp_path, client)

    environment = client.run_calls[0][1]["environment"]
    assert [environment[key] for key in CONSENT_ENV_KEYS] == ["Y", "Y"]
    assert environment["CV_SEED"] == "123456"
    assert environment["NVIDIA_DRIVER_CAPABILITIES"] == "all"
    assert "HOME" not in environment  # only the two consent keys pass through


def test_case_env_omits_consent_keys_the_operator_did_not_set(tmp_path):
    """Refusing on missing consent is the CLI's job (exit 3) — this seam never bakes it."""
    client = FakeClient()
    run_sim_case(
        make_spec(make_checkout(tmp_path)),
        make_case(),
        client,
        run_dir=tmp_path / "run",
        operator_env={},
        poll_interval_s=0.0,
    )

    environment = client.run_calls[0][1]["environment"]
    assert not [key for key in CONSENT_ENV_KEYS if key in environment]


def test_sim_gets_the_gpu_and_the_oracle_does_not(tmp_path, monkeypatch):
    """One GPU is time-shared by the cases; a GPU-holding oracle would halve throughput."""
    monkeypatch.setattr("cv_infra.execution.gpu_device_requests", lambda: ["ALL-GPUS"])
    checkout = make_checkout(tmp_path)
    spec = make_spec(checkout)
    client = FakeClient()

    run_case(tmp_path, client, spec=spec)
    rc, stdout, error = run_oracle(
        spec,
        make_case(),
        client,
        run_dir=tmp_path / "run",
        operator_env=OPERATOR_ENV,
        case_out=tmp_path / "run" / "out",
        poll_interval_s=0.0,
    )

    assert client.run_calls[0][1]["device_requests"] == ["ALL-GPUS"]
    assert "device_requests" not in client.run_calls[1][1]
    assert rc == 0 and error is None and stdout == '{"fell": false}\n'


def test_gpu_device_request_is_the_sdk_all_gpus_shape():
    """The one line that needs the docker SDK's own type (monkeypatched everywhere else)."""
    requests = gpu_device_requests()

    assert requests[0]["Count"] == -1
    assert requests[0]["Capabilities"] == [["gpu"]]


def test_oracle_reruns_the_same_image_and_argv_read_only(tmp_path):
    checkout = make_checkout(tmp_path)
    spec = make_spec(checkout)
    case_out = tmp_path / "run" / "cases" / "c" / "out"
    case_out.mkdir(parents=True)
    client = FakeClient()

    rc, stdout, error = run_oracle(
        spec,
        make_case(),
        client,
        run_dir=tmp_path / "run",
        operator_env=OPERATOR_ENV,
        case_out=case_out,
        poll_interval_s=0.0,
    )

    image, kwargs = client.run_calls[0]
    assert image == SIM_IMAGE  # same image: one dependency story, not two
    assert kwargs["command"] == [
        "-lc",
        EXECUTE_SCRIPT,
        ORACLE_SCRIPT,
        "--lighting=dim",
    ]  # same axes as the sim
    assert {bind["mode"] for bind in kwargs["volumes"].values()} == {"ro"}
    assert kwargs["volumes"][str(case_out.resolve())]["bind"] == f"{CHECKOUT_MOUNT}/{OUTPUT_DIR}"
    # stdout ONLY — a chatty stderr must not be able to inject a verdict line.
    assert client.started[0].log_calls == [(True, False)]
    assert (rc, stdout, error) == (0, '{"fell": false}\n', None)


def test_oracle_infra_failure_is_this_cases_error_not_a_crash(tmp_path):
    client = FakeClient(raise_on_run=RuntimeError("daemon gone"))

    rc, stdout, error = run_oracle(
        make_spec(make_checkout(tmp_path)),
        make_case(),
        client,
        run_dir=tmp_path / "run",
        operator_env=OPERATOR_ENV,
        case_out=tmp_path / "out",
        poll_interval_s=0.0,
    )

    assert rc is None and stdout == ""
    assert error == "RuntimeError: daemon gone"


def test_oracle_timeout_reports_the_marker_and_tears_the_container_down(tmp_path):
    container = FakeContainer(label="oracle", statuses=("running",))
    client = FakeClient(queued=[container])
    spec = make_spec(make_checkout(tmp_path), oracle_timeout_s=0.0)

    rc, stdout, error = run_oracle(
        spec,
        make_case(),
        client,
        run_dir=tmp_path / "run",
        operator_env=OPERATOR_ENV,
        case_out=tmp_path / "out",
        poll_interval_s=0.0,
    )

    # Whatever the killed oracle managed to print is still returned (diagnostics), but
    # ``error`` is what routes the case: a timed-out oracle NEVER yields a verdict.
    assert rc is None
    assert error.startswith(CASE_TIMEOUT_MARKER)
    assert (container.stop_calls, container.remove_calls) == (1, 1)


# --------------------------------------------------------------------------- #
# (3) artifacts: the log and the zip are collected on EVERY path
# --------------------------------------------------------------------------- #


def test_container_log_is_saved_next_to_the_zip(tmp_path):
    """The container has no display: a GUI script's boot crash is visible ONLY here."""
    client = FakeClient(logs=b"[Error] no display\n")

    execution = run_case(tmp_path, client)

    assert execution.log_path.read_bytes() == b"[Error] no display\n"
    assert execution.log_path.parent.name == "logs"


def test_empty_output_still_produces_a_zip(tmp_path):
    """ "the case wrote nothing" is a finding; a missing artifact reads as infra failure."""
    execution = run_case(tmp_path, FakeClient())

    assert execution.zip_path.exists()
    assert execution.zip_truncated is False
    with zipfile.ZipFile(execution.zip_path) as archive:
        assert archive.namelist() == []


def test_output_files_are_collected_with_checkout_relative_names(tmp_path):
    out = tmp_path / "out"
    (out / "nested").mkdir(parents=True)
    (out / "trajectory.json").write_text('{"z": 0.1}', encoding="utf-8")
    (out / "nested" / "frame.txt").write_text("x", encoding="utf-8")

    result = zip_output(out, tmp_path / "zips" / "case.zip", max_bytes=1024)

    assert result.truncated is False and result.bytes > 0
    with zipfile.ZipFile(result.path) as archive:
        assert sorted(archive.namelist()) == ["nested/frame.txt", "trajectory.json"]


def test_an_oversize_output_is_replaced_by_a_manifest(tmp_path):
    out = tmp_path / "out"
    out.mkdir()
    (out / "huge.bin").write_bytes(b"x" * 4096)

    result = zip_output(out, tmp_path / "case.zip", max_bytes=100)

    assert result.truncated is True
    with zipfile.ZipFile(result.path) as archive:
        assert archive.namelist() == ["MANIFEST.txt"]
        manifest = archive.read("MANIFEST.txt").decode("utf-8")
    assert "huge.bin\t4096" in manifest  # the shape that blew the budget is named
    assert "100-byte cap" in manifest


def test_a_missing_output_dir_is_an_empty_zip_not_an_exception(tmp_path):
    result = zip_output(tmp_path / "never-created", tmp_path / "case.zip", max_bytes=10)

    assert result.truncated is False
    with zipfile.ZipFile(result.path) as archive:
        assert archive.namelist() == []


def test_output_over_the_spec_cap_is_reported_as_truncated(tmp_path):
    spec = make_spec(make_checkout(tmp_path), max_zip_mb=0)  # any output trips the cap
    case = make_case()
    # Stand in for what the sim writes: the case's host output dir is deterministic,
    # and _prepare_case_dir adopts an existing one.
    out_dir = tmp_path / "run" / "cases" / slug_for(run_key(case)) / "out"
    out_dir.mkdir(parents=True)
    (out_dir / "trajectory.json").write_text("{}", encoding="utf-8")

    execution = run_case(tmp_path, FakeClient(), spec=spec, case=case)

    assert execution.zip_truncated is True
    with zipfile.ZipFile(execution.zip_path) as archive:
        assert archive.namelist() == ["MANIFEST.txt"]


# --------------------------------------------------------------------------- #
# (4) failure lanes: timeout / infra exception — always torn down
# --------------------------------------------------------------------------- #


def test_a_case_that_never_exits_is_killed_at_the_deadline(tmp_path):
    container = FakeContainer(statuses=("running",))
    client = FakeClient(queued=[container])
    spec = make_spec(make_checkout(tmp_path), case_timeout_s=0.0)

    execution = run_case(tmp_path, client, spec=spec)

    assert execution.rc is None
    assert execution.error.startswith(CASE_TIMEOUT_MARKER)
    assert (container.stop_calls, container.remove_calls) == (1, 1)
    assert execution.zip_path.exists()  # collection happens on the failing path too


def test_an_exit_code_is_reported_but_never_read_as_a_verdict(tmp_path):
    """G-62: after boot the sim's status cannot carry pass/fail — rc only separates
    "died badly" (ERROR lane) from "ran to completion" (ask the oracle)."""
    client = FakeClient(queued=[FakeContainer(exit_code=1)])

    execution = run_case(tmp_path, client)

    assert (execution.rc, execution.error) == (1, None)


def test_a_docker_failure_is_this_cases_error_and_leaves_nothing_behind(tmp_path):
    client = FakeClient(raise_on_run=RuntimeError("no such image"))

    execution = run_case(tmp_path, client)

    assert execution.rc is None
    assert execution.error == "RuntimeError: no such image"
    assert execution.zip_path.exists()
    assert client.started == []


def test_a_seeded_run_discards_its_scratch_even_when_the_container_fails(tmp_path, capsys):
    base, scratch_root = _warm_cache(tmp_path)
    client = FakeClient(raise_on_run=RuntimeError("boom"))

    execution = run_case(tmp_path, client, cache_root=base, cache_scratch_root=scratch_root)

    assert execution.error == "RuntimeError: boom"
    assert list(scratch_root.iterdir()) == []  # ~1 GB does not outlive the case
    assert "cache-seed" in capsys.readouterr().err


def test_a_log_read_failure_does_not_lose_the_exit_code(tmp_path):
    container = FakeContainer(logs_error=OSError("stream closed"))
    client = FakeClient(queued=[container])

    execution = run_case(tmp_path, client)

    assert execution.rc == 0  # the run's outcome survives the collection failure
    assert execution.error == "OSError: stream closed"


def test_teardown_never_masks_the_outcome(capsys):
    """A stop/remove that itself fails is surfaced on stderr, never raised."""
    container = FakeContainer(
        stop_error=RuntimeError("already gone"), remove_error=RuntimeError("still gone")
    )

    _teardown((None, container))

    err = capsys.readouterr().err
    assert "teardown stop failed" in err and "teardown remove failed" in err
    assert container.remove_calls == 1  # remove is attempted even after stop failed


# --------------------------------------------------------------------------- #
# (5) cache mounts — none / single tier / per-case seeded
# --------------------------------------------------------------------------- #


def _warm_cache(tmp_path) -> tuple[Path, Path]:
    """A provisioned warm base + an empty scratch root.

    The tiers live under the cache root's PER-IMAGE subtree (``<root>/<digest12>``) —
    the shape ``warm_cache.sh <root>/<digest12> provision`` leaves behind.
    """
    base = tmp_path / "warm"
    for subpath, _bind in CACHE_BASE_MOUNTS:
        tier = base / SIM_IMAGE_DIGEST12 / subpath
        tier.mkdir(parents=True)
        (tier / "shader.bin").write_bytes(b"cached")
    scratch_root = tmp_path / "scratch"
    scratch_root.mkdir()
    return base, scratch_root


def test_no_cache_configured_means_zero_cache_mounts(tmp_path, monkeypatch):
    monkeypatch.delenv(CACHE_ROOT_ENV, raising=False)
    monkeypatch.delenv(CACHE_SCRATCH_ROOT_ENV, raising=False)
    client = FakeClient()

    execution = run_case(tmp_path, client)

    volumes = client.run_calls[0][1]["volumes"]
    assert len(volumes) == 2  # checkout + this case's output overlay, nothing else
    assert execution.error is None


def test_a_single_tier_cache_binds_all_six_dirs_rw_under_the_image_namespace(tmp_path, monkeypatch):
    base = tmp_path / "warm"
    (base / SIM_IMAGE_DIGEST12).mkdir(parents=True)
    monkeypatch.setenv(CACHE_ROOT_ENV, str(base))
    monkeypatch.delenv(CACHE_SCRATCH_ROOT_ENV, raising=False)

    volumes, scratch = _cache_volumes(None, None, "cvc-x", SIM_IMAGE)

    assert scratch is None
    assert len(volumes) == len(CACHE_MOUNTS)
    assert {bind["mode"] for bind in volumes.values()} == {"rw"}  # :ro turns caches OFF
    # Kit/CUDA caches belong to one Isaac BUILD — a second image gets its own subtree.
    assert set(volumes) == {
        str(base / SIM_IMAGE_DIGEST12 / subpath) for subpath, _bind in CACHE_MOUNTS
    }


def test_a_seeded_cache_copies_the_warm_tiers_and_never_binds_the_base(tmp_path, capsys):
    base, scratch_root = _warm_cache(tmp_path)
    client = FakeClient()

    execution = run_case(tmp_path, client, cache_root=base, cache_scratch_root=scratch_root)

    volumes = client.run_calls[0][1]["volumes"]
    cache_binds = [source for source in volumes if str(base) in source]
    assert cache_binds == []  # the shared base is never bound into any container
    assert len([source for source in volumes if str(scratch_root) in source]) == len(CACHE_MOUNTS)
    assert "[cv-infra] cache-seed" in capsys.readouterr().err  # the feature-on proof
    assert execution.error is None
    assert list(scratch_root.iterdir()) == []  # discarded with the case


def test_the_seeded_runtime_dirs_are_world_writable(tmp_path):
    """dockerd would create a missing bind source as root; the image runs as uid 1234."""
    base, scratch_root = _warm_cache(tmp_path)

    volumes, scratch = _cache_volumes(base, scratch_root, "cvc-x", SIM_IMAGE)
    try:
        for subpath, _bind in CACHE_SCRATCH_MOUNTS:
            mode = (scratch / subpath).stat().st_mode
            assert mode & stat.S_IWOTH
    finally:
        _discard_scratch(scratch)
    assert set(volumes) == {str(scratch / subpath) for subpath, _bind in CACHE_MOUNTS}


def test_a_scratch_root_without_a_base_is_refused(tmp_path):
    with pytest.raises(ValueError, match="without cache_root"):
        _cache_volumes(None, tmp_path, "cvc-x", SIM_IMAGE)


def test_a_cache_root_without_this_images_subtree_names_the_command_to_run(tmp_path):
    """Never silently cold, and never created here: the tree must be owned by uid 1234."""
    base = tmp_path / "warm"
    base.mkdir()

    with pytest.raises(ValueError) as exc:
        _cache_volumes(base, None, "cvc-x", SIM_IMAGE)

    message = str(exc.value)
    assert str(base / SIM_IMAGE_DIGEST12) in message
    assert "warm_cache.sh" in message and "provision" in message


def test_an_image_without_a_digest_cannot_name_a_cache_subtree(tmp_path):
    """The admit gate refuses one already; the seam refuses to guess if it ever slips."""
    with pytest.raises(ValueError, match="not digest-pinned"):
        _image_namespace("nvcr.io/nvidia/isaac-sim:5.1.0")


def test_a_missing_scratch_root_is_loud(tmp_path):
    base, _scratch = _warm_cache(tmp_path)
    with pytest.raises(ValueError, match="scratch ROOT is host provisioning"):
        _cache_volumes(base, tmp_path / "absent", "cvc-x", SIM_IMAGE)


def test_an_unprovisioned_warm_tier_is_refused_rather_than_seeded_empty(tmp_path):
    base = tmp_path / "warm"
    (base / SIM_IMAGE_DIGEST12).mkdir(parents=True)
    scratch_root = tmp_path / "scratch"
    scratch_root.mkdir()

    with pytest.raises(ValueError, match="never provisioned"):
        _cache_volumes(base, scratch_root, "cvc-x", SIM_IMAGE)


def test_a_failed_copy_is_loud_and_leaves_no_orphan(tmp_path):
    base, scratch_root = _warm_cache(tmp_path)
    # A regular file where `cp -a` must create the tier dir -> cp exits non-zero.
    blocker = scratch_root / "cvc-x" / CACHE_BASE_MOUNTS[0][0]
    blocker.parent.mkdir(parents=True)
    blocker.write_text("not a directory", encoding="utf-8")

    with pytest.raises(RuntimeError) as exc:
        _cache_volumes(base, scratch_root, "cvc-x", SIM_IMAGE)

    assert "cache seed failed for" in str(exc.value)
    assert not (scratch_root / "cvc-x").exists()  # no ~1 GB orphan behind the error


def test_an_empty_cache_env_is_loud_instead_of_meaning_unset(tmp_path, monkeypatch):
    monkeypatch.setenv(CACHE_ROOT_ENV, "   ")

    with pytest.raises(ValueError, match="set but empty"):
        _cache_volumes(None, None, "cvc-x", SIM_IMAGE)


class _StatOnlyPath:
    """Minimal ``Path`` stand-in: ``_assert_runner_writable`` only stats + renders it."""

    def __init__(self, path: str, *, uid: int, mode: int = 0o40755) -> None:
        self._path = path
        self._stat = types.SimpleNamespace(st_uid=uid, st_mode=mode)

    def stat(self):
        return self._stat

    def __str__(self) -> str:
        return self._path


def test_a_copy_that_lost_ownership_is_loud_not_a_silently_disabled_cache():
    """Driven directly because reproducing a uid change needs root; a non-GNU ``cp``
    that reports success would otherwise hand the container a cache it cannot write."""
    with pytest.raises(RuntimeError) as exc:
        _assert_runner_writable(
            _StatOnlyPath("/warm/cache/kit", uid=1234),
            _StatOnlyPath("/scratch/case/cache/kit", uid=0),
        )

    message = str(exc.value)
    assert "/scratch/case/cache/kit is uid 0" in message  # BOTH uids are named...
    assert "base /warm/cache/kit is uid 1234" in message  # ...so the fix is obvious


def test_a_tier_the_owner_cannot_write_is_loud():
    with pytest.raises(RuntimeError, match="not owner-writable"):
        _assert_runner_writable(
            _StatOnlyPath("/warm/cache/kit", uid=1234),
            _StatOnlyPath("/scratch/case/cache/kit", uid=1234, mode=0o40555),
        )


def test_discarding_scratch_is_best_effort(tmp_path, capsys):
    _discard_scratch(None)  # no cache configured — a no-op, not a crash
    victim = tmp_path / "not-a-dir"
    victim.write_text("x", encoding="utf-8")

    _discard_scratch(victim)

    assert "scratch discard failed" in capsys.readouterr().err


# --------------------------------------------------------------------------- #
# (6) image-present gate — an implicit pull can hang forever
# --------------------------------------------------------------------------- #


def test_a_present_image_is_not_pulled(capsys):
    client = FakeClient(present={SIM_IMAGE})

    assert _ensure_image_present(client, SIM_IMAGE, poll_interval_s=0.0) == "present"
    assert client.api.pull_calls == []
    assert '"status": "present"' in capsys.readouterr().err


def test_an_absent_image_is_pulled_before_the_container_starts(capsys):
    client = FakeClient(present=set())

    assert _ensure_image_present(client, SIM_IMAGE, poll_interval_s=0.01) == "pulled"
    assert client.api.pull_calls == [SIM_IMAGE]
    assert '"status": "pulled"' in capsys.readouterr().err


def test_a_client_without_an_images_api_is_skipped_loudly(capsys):
    """The CPU fake never touches a registry — nothing to gate, but say so."""
    assert _ensure_image_present(FakeClient(), SIM_IMAGE, poll_interval_s=0.0) == "unknown"
    assert "no-images-api" in capsys.readouterr().err


def test_a_daemon_fault_is_not_read_as_image_absence():
    class _Broken:
        def get(self, image):
            raise RuntimeError("daemon fault")

    with pytest.raises(RuntimeError, match="daemon fault"):
        _image_present(_Broken(), SIM_IMAGE)


def test_image_not_found_is_matched_by_class_name_alone():
    class _Images:
        def get(self, image):
            raise ImageNotFound(image)  # a duck-typed twin, NOT docker.errors

    assert _image_present(_Images(), SIM_IMAGE) is False


def test_a_wedged_pull_fails_in_finite_time_instead_of_hanging():
    release = threading.Event()

    class _Api:
        def pull(self, image, stream=False, decode=False):
            def _gen():
                release.wait()  # never within the window; released in teardown
                return
                yield  # unreachable — makes this a generator

            return _gen()

    client = types.SimpleNamespace(api=_Api())
    try:
        with pytest.raises(ImagePullStalled) as exc:
            _pull_with_liveness(
                client, SIM_IMAGE, kind="sim", stall_timeout_s=0.05, poll_interval_s=0.0
            )
    finally:
        release.set()
    # The crawl-vs-dead discriminator is retained: 0 events = the registry never spoke.
    assert "progress events seen: 0" in str(exc.value)


def test_a_slow_but_progressing_pull_is_not_false_killed():
    """The twin of the stall test: a CRAWLING but alive pull must survive the window.

    Progress-based, not a total cap — every event resets the window, so a big-but-moving
    layer is never killed. Non-vacuous: the pull outlives several monitor wakeups.
    """

    class _Api:
        def pull(self, image, stream=False, decode=False):
            def _gen():
                for i in range(4):
                    time.sleep(0.02)
                    yield {
                        "status": "Downloading",
                        "id": "layer0",
                        "progressDetail": {"current": i},
                    }

            return _gen()

    client = types.SimpleNamespace(api=_Api())
    started = time.monotonic()
    _pull_with_liveness(client, SIM_IMAGE, kind="sim", stall_timeout_s=5.0, poll_interval_s=0.01)

    assert time.monotonic() - started > 0.05  # it really did span several wakeups


def test_a_registry_error_mid_pull_is_surfaced():
    class _Api:
        def pull(self, image, stream=False, decode=False):
            def _gen():
                raise RuntimeError("manifest unknown")
                yield  # unreachable

            return _gen()

    client = types.SimpleNamespace(api=_Api())
    with pytest.raises(RuntimeError, match="manifest unknown"):
        _pull_with_liveness(
            client, SIM_IMAGE, kind="sim", stall_timeout_s=5.0, poll_interval_s=0.01
        )


def test_a_stalled_pull_is_the_cases_error_and_starts_no_container(tmp_path):
    release = threading.Event()

    class _StallingApi:
        def pull(self, image, stream=False, decode=False):
            def _gen():
                release.wait()
                return
                yield  # unreachable

            return _gen()

    client = FakeClient(present=set())
    client.api = _StallingApi()
    try:
        execution = run_case(tmp_path, client, pull_stall_timeout_s=0.0)
    finally:
        release.set()  # let the abandoned drain thread exit cleanly

    assert execution.rc is None
    assert execution.error.startswith("ImagePullStalled:")
    assert client.started == []  # a wedged pull never reaches containers.run


# --------------------------------------------------------------------------- #
# (7) the docker client seam itself
# --------------------------------------------------------------------------- #


def test_an_injected_client_is_used_as_is():
    client = FakeClient()

    assert resolve_docker_client(client) is client


def test_without_an_injected_client_the_sdk_is_imported_lazily(monkeypatch):
    """Lazy so importing this module (and the whole CPU suite) needs no daemon."""
    import docker

    monkeypatch.setattr(docker, "from_env", lambda: "REAL-CLIENT")

    assert resolve_docker_client(None) == "REAL-CLIENT"


def test_the_default_image_is_the_pinned_stock_digest():
    assert DEFAULT_SIM_IMAGE.startswith("nvcr.io/nvidia/isaac-sim:5.1.0@sha256:")
