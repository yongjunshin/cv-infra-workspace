"""Duck-typed docker fakes + spec/case stubs for the execution seam (CPU-only).

Same shape as the frozen orchestrator fake (``tests/test_supervisor_min.py``:
``containers.run`` records ``(image, kwargs)``, ``reload()`` walks a scripted status
list, teardown calls are counted), extended with the three surfaces the case seam adds:
``container.logs()`` (combined for the sim log, stdout-only for the oracle verdict),
``client.images.get`` (the pull-present gate) and ``client.api.pull`` (its stream).

NOT named ``conftest.py`` on purpose: it is imported explicitly by the test module, so
the existing suite-wide ``tests/conftest.py`` stays untouched (M1 is additive).

The spec/case stubs are the duck-typed contract ``cv_infra.execution`` documents —
``contract.inputs.VerifySpec`` / ``contract.cases.CaseRun`` land in M2 and must satisfy
exactly these attribute names.
"""

from __future__ import annotations

import hashlib
from pathlib import Path
from types import SimpleNamespace

SIM_IMAGE = "isaac-sim:test"
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
            else FakeContainer(events=client.events, logs=client.logs)
        )
        container.events = client.events
        client.started.append(container)
        return container


class FakeClient:
    """Duck-typed docker client — the only docker surface the execution seam touches.

    ``present=None`` (default) omits the ``images``/``api`` attributes entirely: that is
    the "no images API" client the pull gate must skip loudly rather than crash on.
    """

    def __init__(self, *, queued=None, raise_on_run=None, present=None, logs=b"boot ok\n"):
        self.events = []  # ordered call log
        self.run_calls = []  # (image, kwargs) per containers.run
        self.started = []
        self.queued = list(queued or [])
        self.raise_on_run = raise_on_run
        self.logs = logs
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
    """The duck-typed VerifySpec surface ``cv_infra.execution`` reads (M2 fills it in)."""
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
    """The duck-typed CaseRun surface (M2's ``contract.cases`` owns the real derivation)."""
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
