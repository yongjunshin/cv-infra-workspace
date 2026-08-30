"""Supervisor EDGE paths — pre-resource guards, salvage boundaries, teardown (p8c2 T2).

The happy-path seams are pinned by ``test_supervisor_min`` (one job) and
``test_supervisor_batch`` (one carrier, n samples); this file drives the branches
those never reach: a guard that must refuse BEFORE any docker resource exists, a
"never raises" boundary at its second failure, and the teardown/sweep paths whose
whole contract is that a failure there is REPORTED but never masks the job outcome.

The docker fakes are ``test_supervisor_min``'s (same discipline as
``test_supervisor_batch``: one fake surface for every seam — a second one would let
the paths drift into two different definitions of a container). CPU-only, no daemon.
"""

from __future__ import annotations

import asyncio
import json
import types
from pathlib import Path

import pytest

from cv_infra.orchestrator import supervisor as supervisor_mod
from cv_infra.orchestrator.allocator import DomainIdAllocator
from cv_infra.orchestrator.models import Job, JobResult, JobState, Verdict
from cv_infra.orchestrator.queue import JobQueue
from cv_infra.orchestrator.scheduler import SlotAccountant
from cv_infra.orchestrator.store import Store
from cv_infra.orchestrator.supervisor import (
    _GATE_READY,
    JobOutcome,
    ParallelSupervisor,
    _assert_runner_writable,
    _cache_volumes,
    _declared_job_id,
    _gate_runner_ready,
    _hostify_artifact_paths,
    _image_present,
    _sweep_stale,
    _teardown,
    network_name_for,
    run_batch,
    run_job,
)
from tests.test_supervisor_min import (
    RUNNER_IMAGE,
    SUT_IMAGE,
    FakeClient,
    make_spec,
    make_two_tier_roots,
)

# --------------------------------------------------------------------------- #
# (a) cache seeding — the two loud guards a silently-disabled cache hides behind
# --------------------------------------------------------------------------- #


def test_a_failed_copy_refuses_the_job_instead_of_running_it_cold(tmp_path):
    """``cp -a`` failing (here: the destination path is already a FILE, e.g. a
    leftover from a crashed run) must RAISE.

    A partial/absent per-job cache runs all-cold at ~47 s/job while every dashboard
    still reads "warm" (T4 §E1) — the exact failure class this guard closes. The
    message has to name the copy and the cp exit code so an operator can tell a full
    disk from an unreadable base.
    """
    base, scratch_root = make_two_tier_roots(tmp_path)
    job_id = "env-cpfail/r0:0"
    blocker = scratch_root / network_name_for(job_id) / "cache" / "kit"
    blocker.parent.mkdir(parents=True)
    blocker.write_text("not a directory", encoding="utf-8")

    with pytest.raises(RuntimeError) as exc:
        _cache_volumes(base, scratch_root, job_id)

    message = str(exc.value)
    assert "cache seed failed for" in message
    assert "cp -a exit" in message
    # The failed seed leaves no ~1 GB orphan behind the loud error.
    assert not (scratch_root / network_name_for(job_id)).exists()


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
    """``cp -a`` preserves ownership only for a privileged copier. GNU cp already
    exits non-zero otherwise, so this second, structural check is the net for a
    NON-GNU cp that reports success — without it the runner (uid 1234) could not
    write its lock/index files and the cache would turn itself OFF silently.

    Driven directly because reproducing a uid change needs root; the guard's
    predicate is the whole content of the branch.
    """
    with pytest.raises(RuntimeError) as exc:
        _assert_runner_writable(
            _StatOnlyPath("/warm/cache/kit", uid=1234),
            _StatOnlyPath("/scratch/job/cache/kit", uid=0),
        )

    message = str(exc.value)
    assert "cache seed did not preserve ownership" in message
    assert "/scratch/job/cache/kit is uid 0" in message  # BOTH uids are named...
    assert "base /warm/cache/kit is uid 1234" in message  # ...so the fix is obvious


# --------------------------------------------------------------------------- #
# (b) image presence — absence vs. a genuine daemon fault
# --------------------------------------------------------------------------- #


class _Images:
    """Duck-typed ``client.images`` whose ``get`` raises the scripted error."""

    def __init__(self, error: Exception | None = None) -> None:
        self._error = error

    def get(self, image: str):
        if self._error is not None:
            raise self._error
        return object()


class ImageNotFound(Exception):
    """Same CLASS NAME docker's SDK uses — matched by name, never by import (D-2)."""


def test_a_daemon_fault_is_not_read_as_an_absent_image():
    """Absence (``ImageNotFound``/``NotFound``) means "pull it"; ANY other error is a
    real daemon fault and must propagate to run_job's infra boundary. Swallowing it
    as "absent" would turn a broken daemon into an endless pull attempt."""
    assert _image_present(_Images(), "runner:test") is True
    assert _image_present(_Images(ImageNotFound("no such image")), "runner:test") is False

    with pytest.raises(PermissionError, match="docker.sock"):
        _image_present(_Images(PermissionError("docker.sock denied")), "runner:test")


# --------------------------------------------------------------------------- #
# (c) pre-resource argument guards (both seams, same seat as the job_id check)
# --------------------------------------------------------------------------- #


def test_run_job_refuses_a_negative_sut_restart_limit(tmp_path):
    """The limit BOUNDS restarts; a negative one has no meaning, so it is refused
    before a network or container exists rather than being read as "never restart"."""
    client = FakeClient()
    with pytest.raises(ValueError, match="sut_restart_limit must be >= 0, got -1"):
        run_job(make_spec(), tmp_path, RUNNER_IMAGE, SUT_IMAGE, client, sut_restart_limit=-1)
    assert client.events == []  # pre-resource: nothing to clean up


def test_run_batch_refuses_a_negative_sut_restart_limit(tmp_path):
    """Same guard, same seat, on the carrier seam — the two entry points must not
    disagree about what a legal restart budget is."""
    specs = [
        {"job_id": "env-x/r0:0", "sut_image_ref": SUT_IMAGE, "scenario": {"scene": "warehouse"}},
        {"job_id": "env-x/r0:1", "sut_image_ref": SUT_IMAGE, "scenario": {"scene": "warehouse"}},
    ]
    client = FakeClient()
    with pytest.raises(ValueError, match="sut_restart_limit must be >= 0, got -1"):
        run_batch(
            specs,
            tmp_path,
            RUNNER_IMAGE,
            SUT_IMAGE,
            client,
            batch_id="env-x/r0",
            batch_timeout_s=60.0,
            sut_restart_limit=-1,
        )
    assert client.events == []


# --------------------------------------------------------------------------- #
# (d) readiness gate — it WAITS, it does not judge on the first sample
# --------------------------------------------------------------------------- #


class _ScriptedContainer:
    def __init__(self, statuses) -> None:
        self._statuses = list(statuses)
        self.status = "created"
        self.reloads = 0

    def reload(self) -> None:
        self.reloads += 1
        if self._statuses:
            self.status = self._statuses.pop(0)


def test_the_readiness_gate_polls_until_ready(monkeypatch):
    """G-19 supply order: the SUT starts only after the runner is READY, so a runner
    that needs a moment must be WAITED for — a gate that judged the first sample
    would start the SUT against a runner that has not wired DDS yet."""
    slept: list[float] = []
    monkeypatch.setattr(supervisor_mod.time, "sleep", slept.append)
    runner = _ScriptedContainer(["created", "created", "running"])

    gate = _gate_runner_ready(
        runner, lambda c: c.status == "running", timeout_s=30.0, poll_interval_s=0.25
    )

    assert gate == _GATE_READY
    assert runner.reloads == 3  # it kept polling instead of concluding early
    assert slept == [0.25, 0.25]  # ...at the configured interval, between polls


# --------------------------------------------------------------------------- #
# (e) result recovery — a second read must not invent a second failure mode
# --------------------------------------------------------------------------- #


def test_a_declared_job_id_is_absent_rather_than_fatal_when_unreadable(tmp_path):
    """``_declared_job_id`` feeds the slot cross-check only. The slot's own
    classification already handled an unreadable result (FAILED), so this read is
    deliberately soft: absent id, never a second exception."""
    good = tmp_path / "result.json"
    good.write_text(json.dumps({"job_id": "env-a/r0:0", "verdict": "pass"}), encoding="utf-8")
    corrupt = tmp_path / "corrupt.json"
    corrupt.write_text("{not json", encoding="utf-8")

    assert _declared_job_id(good) == "env-a/r0:0"
    assert _declared_job_id(corrupt) is None  # ValueError (JSONDecodeError) path
    assert _declared_job_id(tmp_path / "absent.json") is None  # OSError path


def test_an_artifact_outside_the_result_mount_is_left_verbatim():
    """Only paths under the runner's ``RESULT_OUT`` mount can be mapped to the host
    bind; anything else is left EXACTLY as the runner wrote it (never guess a mapping
    we cannot prove, G-26) while its siblings are still rewritten."""
    doc = {"artifacts": {"mcap": "/somewhere/else/run.mcap", "mp4": "/cv/out/run.mp4"}}

    out = _hostify_artifact_paths(doc, Path("/host/out"))

    assert out["artifacts"]["mcap"] == "/somewhere/else/run.mcap"  # untouched
    assert out["artifacts"]["mp4"] == "/host/out/run.mp4"  # mapped
    assert doc["artifacts"]["mp4"] == "/cv/out/run.mp4"  # input not mutated


# --------------------------------------------------------------------------- #
# (f) teardown / sweep — failures are surfaced, never raised
# --------------------------------------------------------------------------- #


class _BrokenContainer:
    def __init__(self, label: str) -> None:
        self.label = label

    def stop(self, timeout=None):
        raise RuntimeError(f"{self.label} stop refused")

    def remove(self, force=False):
        raise RuntimeError(f"{self.label} remove refused")


class _BrokenNetwork:
    def remove(self):
        raise RuntimeError("network still has endpoints")


def test_teardown_reports_every_failure_and_still_finishes(capsys):
    """Teardown must not mask the job outcome: every step is attempted regardless of
    earlier failures and each failure goes to stderr, so a leaked container is
    visible in the log instead of becoming an exception that replaces the verdict."""
    _teardown((_BrokenContainer("sut"), None, _BrokenContainer("runner")), _BrokenNetwork())

    err = capsys.readouterr().err
    assert err.count("[cv-supervisor] teardown stop failed:") == 2  # None skipped, both tried
    assert err.count("[cv-supervisor] teardown remove failed:") == 2  # remove tried after stop
    assert "[cv-supervisor] teardown network remove failed:" in err


class _SweepListing:
    def __init__(self, items) -> None:
        self._items = list(items)

    def list(self, all: bool = False, filters: dict | None = None):  # noqa: A002 — SDK name
        return list(self._items)


def test_the_boot_sweep_counts_what_it_found_even_when_a_removal_fails(capsys):
    """R14 §3.9: the restart sweep reports how many labeled leftovers it FOUND. A
    network that refuses removal (an endpoint still attached) is logged and counted —
    returning a smaller number would tell the operator the host is cleaner than it is."""
    client = types.SimpleNamespace(
        containers=_SweepListing([_BrokenContainer("stale")]),
        networks=_SweepListing([_BrokenNetwork(), _BrokenNetwork()]),
    )

    assert _sweep_stale(client) == (1, 2)
    assert capsys.readouterr().err.count("[cv-supervisor] sweep network remove failed:") == 2


# --------------------------------------------------------------------------- #
# (g) carrier salvage — the LAST resort of a "never raises" boundary
# --------------------------------------------------------------------------- #


def test_a_carrier_whose_salvage_read_also_fails_still_returns_one_outcome_per_sample(
    tmp_path, monkeypatch
):
    """``run_batch`` NEVER raises and NEVER returns fewer than ``len(job_specs)``
    outcomes (P5-13). This drives both failures at once — the carrier dies (the SUT
    spawn is refused) AND the disk fold that would salvage it also fails — and pins
    that the seam still charges every slot, carrying BOTH reasons so the operator can
    tell "the carrier died" from "we could not even read what landed".
    """
    monkeypatch.setattr(
        supervisor_mod,
        "_fold_batch_outcomes",
        lambda *args, **kwargs: (_ for _ in ()).throw(OSError("result dir unreadable")),
    )
    specs = [
        {"job_id": "env-s/r0:0", "sut_image_ref": SUT_IMAGE, "scenario": {"scene": "warehouse"}},
        {"job_id": "env-s/r0:1", "sut_image_ref": SUT_IMAGE, "scenario": {"scene": "warehouse"}},
    ]
    client = FakeClient(raise_on_sut_run=RuntimeError("daemon gone"))

    outcomes = run_batch(
        specs,
        tmp_path,
        RUNNER_IMAGE,
        SUT_IMAGE,
        client,
        batch_id="env-s/r0",
        batch_timeout_s=60.0,
        poll_interval_s=0.0,
    )

    assert [o.job_id for o in outcomes] == ["env-s/r0:0", "env-s/r0:1"]  # every slot charged
    for outcome in outcomes:
        assert isinstance(outcome, JobOutcome)
        assert outcome.result_path is None
        assert "daemon gone" in outcome.infra_error  # why the carrier died
        # ...and why we cannot say more than that:
        assert "batch result salvage also failed" in outcome.infra_error
        assert "result dir unreadable" in outcome.infra_error
    # The boundary still tore its resources down (teardown runs in `finally`).
    assert ("network-remove", network_name_for("env-s/r0")) in client.events


# --------------------------------------------------------------------------- #
# (h) ParallelSupervisor — the admission deadlock guard
# --------------------------------------------------------------------------- #


class _PassRunner:
    def run(self, job: Job) -> JobResult:
        return JobResult(job=job, state=JobState.COMPLETED, verdict=Verdict.PASS)


def test_an_admission_that_produces_no_task_is_loud_instead_of_hanging(tmp_path):
    """Positive control for the supervision loop's deadlock guard.

    With correct slot/allocator accounting a slot is always free when nothing is in
    flight, so this cannot happen — which is exactly why it needs a control: a
    regression in ``_admit`` would otherwise spin the loop forever with jobs pending
    and no task to wait on, and a hung control plane reports nothing at all.
    """
    with Store(tmp_path / "cv.sqlite3") as store:
        queue = JobQueue([Job(request_id="env-q/r0", repeat_index=0)], store=store)
        supervisor = ParallelSupervisor(
            queue, SlotAccountant(k=1), _PassRunner(), allocator=DomainIdAllocator(store)
        )
        supervisor._admit = lambda loop, in_flight: None  # the regression, injected

        with pytest.raises(RuntimeError, match="admission produced no task while jobs are pending"):
            asyncio.run(supervisor.run())
