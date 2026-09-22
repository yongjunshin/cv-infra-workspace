"""Shared test scaffolding: a duck-typed docker client and factories for the two
documents the pipeline passes around.

``FakeClient`` has the same shape the execution seam expects of the real one —
``containers.run`` records ``(image, kwargs)``, ``reload()`` walks a scripted status
list, teardown calls are counted — plus the three surfaces the case seam adds:
``container.logs()`` (combined for the sim log, stdout-only for the oracle verdict),
``client.images.get`` (the pull-present gate) and ``client.api.pull`` (its stream). That
is what lets the whole verify pipeline be tested on a CPU host with no docker daemon.

``make_report``/``make_case_row`` build a schema-1 report LITERALLY rather than by
calling ``report.aggregate``: the renderer tests must pin the SCHEMA the renderer reads,
not whatever the producer happens to emit today (the two are tied together by the
end-to-end test in ``test_cli_verify.py``, which renders from a real run's report).
"""

from __future__ import annotations

import hashlib
from pathlib import Path
from types import SimpleNamespace

#: Digest-pinned, like every admitted image: the execution seam names this image's
#: cache subtree after the first 12 hex chars of the digest.
SIM_IMAGE_DIGEST12 = "ab12cd34ef56"
SIM_IMAGE = f"isaac-sim:test@sha256:{SIM_IMAGE_DIGEST12}{'0' * 52}"
SIM_SCRIPT = "verify/sim.py"
ORACLE_SCRIPT = "verify/oracle.py"
OUTPUT_DIR = "verify/out"


class ImageNotFound(Exception):
    """Duck-typed ``docker.errors.ImageNotFound`` — matched by class NAME by the module."""


class FakeContainer:
    """Scripted container: each reload() advances through ``statuses`` (last one sticks)."""

    def __init__(
        self,
        label="sim",
        statuses=("running", "exited"),
        exit_code=0,
        logs=b"boot ok\n",
        stdout_logs=b'{"fell": false}\n',
        events=None,
        stop_error=None,
        remove_error=None,
        logs_error=None,
    ):
        self.label = label
        self._statuses = list(statuses)
        self.status = "created"
        self._exit_code = exit_code
        self._logs = logs
        self._stdout_logs = stdout_logs
        self.events = events if events is not None else []
        self._stop_error = stop_error
        self._remove_error = remove_error
        self._logs_error = logs_error
        self.stop_calls = 0
        self.remove_calls = 0
        self.log_calls = []

    def reload(self):
        if self._statuses:
            self.status = self._statuses.pop(0)

    def wait(self, timeout=None):
        return {"StatusCode": self._exit_code}

    def logs(self, stdout=True, stderr=True):
        self.log_calls.append((stdout, stderr))
        if self._logs_error is not None:
            raise self._logs_error
        if stdout and not stderr:
            return self._stdout_logs
        return self._logs

    def stop(self, timeout=None):
        self.stop_calls += 1
        self.events.append(("stop", self.label))
        if self._stop_error is not None:
            raise self._stop_error

    def remove(self, force=False):
        self.remove_calls += 1
        self.events.append(("remove", self.label))
        if self._remove_error is not None:
            raise self._remove_error


class _FakeImages:
    def __init__(self, present):
        self._present = set(present)
        self.get_calls = []

    def get(self, image):
        self.get_calls.append(image)
        if image not in self._present:
            raise ImageNotFound(image)
        return object()


class _FakeApi:
    def __init__(self, pull_events=3):
        self._pull_events = pull_events
        self.pull_calls = []

    def pull(self, image, stream=False, decode=False):
        self.pull_calls.append(image)
        return [{"status": "Downloading", "id": f"layer{i}"} for i in range(self._pull_events)]


class _FakeContainers:
    def __init__(self, client):
        self._client = client

    def run(self, image, **kwargs):
        client = self._client
        client.events.append(("run", image))
        client.run_calls.append((image, kwargs))
        if client.raise_on_run is not None:
            raise client.raise_on_run
        container = (
            client.queued.pop(0)
            if client.queued
            else FakeContainer(events=client.events, logs=client.logs, **client.container)
        )
        container.events = client.events
        client.started.append(container)
        return container


class FakeClient:
    """Duck-typed docker client — the only docker surface the execution seam touches.

    ``present=None`` (default) omits the ``images``/``api`` attributes entirely: that is
    the "no images API" client the pull gate must skip loudly rather than crash on.
    """

    def __init__(
        self, *, queued=None, raise_on_run=None, present=None, logs=b"boot ok\n", **container
    ):
        self.events = []  # ordered call log
        self.run_calls = []  # (image, kwargs) per containers.run
        self.started = []
        self.queued = list(queued or [])
        self.raise_on_run = raise_on_run
        self.logs = logs
        # Defaults for every container this client hands out — a whole-run test scripts
        # its containers once here instead of queueing two per case by hand.
        self.container = container
        self.containers = _FakeContainers(self)
        if present is not None:
            self.images = _FakeImages(present)
            self.api = _FakeApi()


def make_checkout(tmp_path: Path) -> Path:
    """A consumer checkout: the sim script plus the committed output mount point."""
    checkout = tmp_path / "checkout"
    (checkout / OUTPUT_DIR).mkdir(parents=True)
    (checkout / SIM_SCRIPT).parent.mkdir(parents=True, exist_ok=True)
    (checkout / SIM_SCRIPT).write_text("# sim\n", encoding="utf-8")
    return checkout


def make_spec(checkout: Path, **overrides):
    """The duck-typed VerifySpec surface ``cv_infra.execution`` reads (the contract fills it in)."""
    fields = {
        "checkout": checkout,
        "sim_output_dir": OUTPUT_DIR,
        "sim_image": SIM_IMAGE,
        "oracle_script": ORACLE_SCRIPT,
        "case_timeout_s": 60.0,
        "oracle_timeout_s": 30.0,
        "shm_size": "8g",
        "max_zip_mb": 512,
    }
    fields.update(overrides)
    return SimpleNamespace(**fields)


def make_case(case_index: int = 3, axes: dict[str, str] | None = None, repeat: int = 0):
    """The duck-typed CaseRun surface (``contract.cases`` owns the real derivation)."""
    axes = {"lighting": "dim"} if axes is None else axes
    case_id = "sha256:" + hashlib.sha256(repr(sorted(axes.items())).encode()).hexdigest()
    return SimpleNamespace(
        case_index=case_index,
        case_id=case_id,
        axes=axes,
        repeat=repeat,
        seed=123456,
        argv=[SIM_SCRIPT, *(f"--{name}={value}" for name, value in axes.items())],
    )


# --- report factories (schema 1 — see the module docstring) ---------------------------

CASE_ID = "sha256:4b227777d4dd1fc61c6f884f48641d02b4d121d3fd328cb08b5531fcacdabf8a"


def make_case_row(**overrides):
    """One ``matrix[]`` row: a passing single-repeat case with one check and one metric."""
    row = {
        "case_id": CASE_ID,
        "axes": {"lighting": "dim"},
        "result": "pass",
        "repeats_planned": 1,
        "repeats_run": 1,
        "runs": [
            {
                "repeat": 0,
                "seed": 123456,
                "rc_sim": 0,
                "rc_oracle": 0,
                "wall_s": 41.2,
                "zip": "zips/cvc-0000.zip",
                "log": "logs/cvc-0000.sim.log",
                "error": None,
                "verdict": {"upright": True, "z_final": 0.12},
            }
        ],
        "checks": {"upright": {"pass_ratio": 1.0, "n": 1}},
        "metrics": {"z_final": {"values": [0.12], "mean": 0.12}},
        "nulls": [],
        "regression": {"status": "ok", "details": []},
    }
    row.update(overrides)
    return row


def make_report(*, rows=None, summary=None, inputs=None, baseline=None, **overrides):
    """A schema-1 report dict: one passing gate case, no truncation, baseline present."""
    report = {
        "schema": 1,
        "generated_at": "2026-09-07T00:00:00+00:00",
        "mode": "gate",
        "inputs": {
            "sim_script": "verify/sim.py",
            "sim_input_space": "verify/param_space.pict",
            "sim_output_dir": "verify/out",
            "oracle_script": "verify/oracle.py",
            "pict_k": 2,
            "repeats": 1,
            "budget_s": None,
            "sim_image": "isaac-sim:test",
            "concurrency": 1,
            "report_only": False,
            "checkout_sha": "abc123",
        },
        "summary": {
            "exit_code": 0,
            "report_outcome": "pass",
            "cases_planned": 1,
            "cases_run": 1,
            "cases_errored": 0,
            "runs_total": 1,
            "checks_failed": 0,
            "regressions": 0,
            "coverage": {"requested_k": 2, "achieved": 1.0, "truncated_after_case": None},
        },
        "matrix": [make_case_row()] if rows is None else rows,
        "baseline": {
            "db": "/tmp/baselines.sqlite3",
            "available": True,
            "compared": 1,
            "absent": 0,
            "regressed": 0,
            "improved": 0,
            "metric_changes": 0,
            "updated": False,
        },
        "artifacts": {
            "zips": ["zips/cvc-0000.zip"],
            "logs": ["logs/cvc-0000.sim.log"],
            "oversize_replaced": [],
        },
    }
    report["summary"].update(summary or {})
    report["inputs"].update(inputs or {})
    report["baseline"].update(baseline or {})
    report.update(overrides)
    return report
