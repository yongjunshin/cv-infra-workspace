"""The envelope-status vocabulary has ONE definition home (p8c2 T2, T5 승계).

``"running"`` / ``"completed"`` are a WIRE vocabulary that lives on two planes: the
orchestrator mints it (``GET /envelopes/{id}`` -> ``status``) and the CLI folds a
terminal envelope off it (``cv-infra wait`` / ``submit --wait``). Until p8c2 BOTH
sides carried their own string literal, so the two planes agreed only by habit —
renaming the value on the server would have left the client polling forever with a
green suite (the client's literal still matched nothing, and nothing tested that).

Empirically the definition home is ``orchestrator/store.ENVELOPE_RUNNING`` /
``ENVELOPE_COMPLETED``: they are the ONLY writers of the ``envelopes.status`` column
(store.py INSERT/UPDATE), and ``api._status_from_store`` puts that persisted value on
the wire verbatim — so the store's constant already WAS the value on one of the two
producer paths. The other producer (``api._envelope_status``, the live record) and the
consumer now read the same constant.

These tests are POSITIVE CONTROLS: each renames the definition and asserts the
dependent side follows. Renaming the definition is exactly the change that used to be
silent.
"""

from __future__ import annotations

import asyncio
import copy
import threading
import time
from pathlib import Path

import httpx
import pytest
import yaml
from fastapi.testclient import TestClient

from cv_infra.cli import batch as cli_batch
from cv_infra.cli.exit_codes import EXIT_INFRA, EXIT_PASS
from cv_infra.orchestrator import api as orchestrator_api
from cv_infra.orchestrator import store as orchestrator_store
from cv_infra.orchestrator.api import create_app
from cv_infra.orchestrator.models import Job, JobResult, JobState, Verdict
from cv_infra.orchestrator.store import ENVELOPE_COMPLETED, ENVELOPE_RUNNING, Store

_FIXTURE = Path(__file__).parent / "fixtures" / "nova_carter_warehouse_goal.yaml"


def _request_doc() -> dict:
    return copy.deepcopy(yaml.safe_load(_FIXTURE.read_text(encoding="utf-8")))


# --------------------------------------------------------------------------- #
# (1) the values themselves are a frozen wire contract
# --------------------------------------------------------------------------- #


def test_the_vocabulary_bytes_are_frozen_and_shared_by_both_planes():
    """The single-sourcing must not have MOVED the bytes (M8 clients in the wild
    poll for these exact two strings), and both planes must bind the one object —
    an equal-but-separate copy is the drift hole this closes."""
    assert (ENVELOPE_RUNNING, ENVELOPE_COMPLETED) == ("running", "completed")
    assert orchestrator_api.ENVELOPE_COMPLETED is orchestrator_store.ENVELOPE_COMPLETED
    assert orchestrator_api.ENVELOPE_RUNNING is orchestrator_store.ENVELOPE_RUNNING
    assert cli_batch.ENVELOPE_COMPLETED is orchestrator_store.ENVELOPE_COMPLETED


# --------------------------------------------------------------------------- #
# (2) producer: the LIVE status body follows the definition
# --------------------------------------------------------------------------- #


class _GatedRunner:
    """Runner that parks in the executor thread until the test releases it.

    The live 'running' window is otherwise a race against a fake runner that
    finishes in microseconds; parking makes BOTH status values observable
    deterministically (the event loop stays free to serve the status GET —
    that is exactly what ``loop.run_in_executor`` buys, M3 §3.5 R-DS).
    """

    def __init__(self) -> None:
        self.entered = threading.Event()
        self.release = threading.Event()

    def run(self, job: Job) -> JobResult:
        self.entered.set()
        assert self.release.wait(timeout=10.0), "the test never released the runner"
        return JobResult(job=job, state=JobState.COMPLETED, verdict=Verdict.PASS)


def _both_live_status_values(tmp_path: Path) -> list[str]:
    """(status while a job is in flight, status once supervision is done)."""
    runner = _GatedRunner()
    with Store(tmp_path / "cv.sqlite3") as store:
        app = create_app(store, runner, k=1)
        with TestClient(app) as client:
            envelope_id = client.post("/envelopes", json={"requests": [_request_doc()]}).json()[
                "envelope_id"
            ]
            try:
                assert runner.entered.wait(timeout=10.0), "supervision never admitted the job"
                in_flight = client.get(f"/envelopes/{envelope_id}").json()["status"]
            finally:
                runner.release.set()
            deadline = time.monotonic() + 10.0
            while time.monotonic() < deadline:
                terminal = client.get(f"/envelopes/{envelope_id}").json()["status"]
                if terminal != in_flight:
                    return [in_flight, terminal]
                time.sleep(0.005)
    raise AssertionError(f"the live record never left {in_flight!r}")


def test_todays_live_producer_really_emits_both_vocabulary_values(tmp_path):
    """Non-vacuity premise for the test below (G-59): the live path is what serves an
    envelope this process owns, and it emits BOTH values — so renaming them is a
    change with an observable target."""
    assert _both_live_status_values(tmp_path) == [ENVELOPE_RUNNING, ENVELOPE_COMPLETED]


def test_the_live_status_producer_follows_the_vocabulary_definition(tmp_path, monkeypatch):
    """Rename the definition -> the live wire follows. Before p8c2 this was a
    hard-coded ``"completed" if record.done else "running"`` and this test failed
    (the wire kept the old words while the store row moved)."""
    monkeypatch.setattr(orchestrator_api, "ENVELOPE_RUNNING", "in-flight")
    monkeypatch.setattr(orchestrator_api, "ENVELOPE_COMPLETED", "finished")

    assert _both_live_status_values(tmp_path) == ["in-flight", "finished"]


# --------------------------------------------------------------------------- #
# (3) consumer: the CLI's terminal test follows the same definition
# --------------------------------------------------------------------------- #


def _wait_exit(status_value: str) -> int:
    """``cv-infra wait`` against a server that always answers with this status."""
    body = {
        "envelope_id": "env-1",
        "status": status_value,
        "jobs": [],
        "rollups": [],
        "report_outcome": "pass",
    }
    transport = httpx.MockTransport(lambda request: httpx.Response(200, json=body))

    async def run() -> int:
        async with httpx.AsyncClient(
            transport=transport, base_url="http://cv-infra.test"
        ) as client:
            # timeout 0 = poll once, then give up: a NON-terminal status must fold to
            # EXIT_INFRA, so the two outcomes below are distinguishable.
            return await cli_batch._poll_until_terminal(client, "wait", "env-1", 0.0)

    return asyncio.run(run())


def test_todays_consumer_really_discriminates_on_the_vocabulary(capsys):
    """Non-vacuity premise: with the shipped words, ``completed`` folds the outcome
    (exit 0) and ``running`` runs out the clock (exit 3)."""
    assert _wait_exit(ENVELOPE_COMPLETED) == EXIT_PASS
    assert _wait_exit(ENVELOPE_RUNNING) == EXIT_INFRA
    capsys.readouterr()


@pytest.mark.parametrize("shipped_word", ["completed"])
def test_the_cli_terminal_test_follows_the_vocabulary_definition(monkeypatch, capsys, shipped_word):
    """Rename the definition -> the CLI's terminal test follows it, and the OLD word
    stops being terminal. Without the single source the CLI would keep waiting for a
    word the server no longer sends — i.e. ``cv-infra wait`` hanging until --timeout
    and reporting infra (3) for a perfectly finished batch."""
    monkeypatch.setattr(cli_batch, "ENVELOPE_COMPLETED", "finished")

    assert _wait_exit("finished") == EXIT_PASS
    assert _wait_exit(shipped_word) == EXIT_INFRA
    capsys.readouterr()
