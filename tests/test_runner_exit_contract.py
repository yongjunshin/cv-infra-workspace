"""CPU tests for the exit-code DELIVERY path (0/1/2/3 — DoD-P2-07, G-62).

``exit_code_for_verdict`` (verdict -> code) was already covered in
test_runner_core.py; what was NOT covered is whether the decided code ever
reaches the process boundary. It did not: ``SimulationApp.close()`` never
returns — it ends the process with status 0 — so ``run``'s ``finally`` erased
every post-boot code (measured p5c13, die codes 16/16 = 0). The repair is
structural (decide -> our own cleanup -> ``hard_exit``/``os._exit``), and only
the structure is CPU-checkable, so this module pins it in three ways:

* behaviour — ``hard_exit`` flushes, then delivers exactly the code it was given;
* the real process boundary — ``python3 -m cv_infra.runner.main`` (the container
  ENTRYPOINT's module) exits 2 / 3 on the two paths reachable without a GPU;
* structure — ``run`` closes no vendor object on its terminal path and the module
  entrypoint delivers with ``hard_exit``. A source-level assert is the only test
  available for a ``# pragma: no cover - GPU path`` body, and it is exactly the
  invariant that broke (something ran between the decision and the status).

The 4 values at the CONTAINER boundary stay a live claim — scripts/
exit_contract_probe.sh measures them on GPU (Wave B); nothing here may be read
as evidence for that.
"""

import ast
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from cv_infra.runner import main

_SRC = Path(main.__file__).read_text(encoding="utf-8")
_TREE = ast.parse(_SRC)


def _valid_spec() -> dict:
    """A canonical JOB_SPEC dict — same Phase-2 wire as test_runner_wiring."""
    return {
        "job_id": "job-exit-0001",
        "scenario": {
            "scene": "omniverse://assets/warehouse.usd",
            "robot": "omniverse://assets/nova_carter_ros.usd",
            "goal": {"x": 3.0, "y": -1.5, "yaw": 0.0},
            "seed": 7,
            "timeout_s": 120.0,
        },
        "sut_image_ref": "carter-sut:p2",
        "interface": {"type": "ros2", "adapter_config": {}},
        "acceptance_criteria": [
            {"oracle": "reached_goal", "params": {"position_tolerance_m": 0.3}},
            {"oracle": "no_collision", "params": {"chassis_path": "/World/carter/chassis"}},
        ],
    }


def _run_entrypoint(job_spec: str | None, result_out: Path) -> subprocess.CompletedProcess:
    """Run the ENTRYPOINT module in a child process and return the real status.

    Isaac-free by construction: both arms terminate at or before ``boot()``'s
    first statement (the EULA guard runs BEFORE ``from isaacsim import ...``).
    """
    env = {k: v for k, v in os.environ.items() if k not in ("PYTHONPATH", "ACCEPT_EULA")}
    if job_spec is not None:
        env["JOB_SPEC"] = job_spec
    env["RESULT_OUT"] = str(result_out)
    return subprocess.run(
        [sys.executable, "-m", "cv_infra.runner.main"],
        env=env,
        capture_output=True,
        text=True,
        timeout=120,
    )


# --------------------------------------------------------------------------- #
# hard_exit — the delivery itself.
# --------------------------------------------------------------------------- #
def test_hard_exit_flushes_both_streams_before_delivering_the_code(monkeypatch):
    events: list[str] = []

    class _Stream:
        def __init__(self, name: str) -> None:
            self.name = name

        def flush(self) -> None:
            events.append(f"flush:{self.name}")

    monkeypatch.setattr(sys, "stdout", _Stream("stdout"))
    monkeypatch.setattr(sys, "stderr", _Stream("stderr"))
    main.hard_exit(main.EXIT_FAIL, exit_process=lambda code: events.append(f"exit:{code}"))
    # os._exit drops whatever is still buffered, so the flushes must come FIRST —
    # the runner's evidence (boot trace / cache delta / tolerance audit) is its log.
    assert events == ["flush:stdout", "flush:stderr", "exit:1"]


@pytest.mark.parametrize("code", [0, 1, 2, 3])
def test_hard_exit_status_reaches_the_process_boundary(code):
    """The default path really is ``os._exit`` — status observed from outside."""
    proc = subprocess.run(
        [sys.executable, "-c", f"from cv_infra.runner.main import hard_exit; hard_exit({code})"],
        env={k: v for k, v in os.environ.items() if k != "PYTHONPATH"},
        capture_output=True,
        timeout=60,
    )
    assert proc.returncode == code


# --------------------------------------------------------------------------- #
# The ENTRYPOINT module's own status — the two codes reachable without a GPU.
# --------------------------------------------------------------------------- #
def test_entrypoint_bad_job_spec_exits_2(tmp_path):
    proc = _run_entrypoint("{not json", tmp_path)
    assert proc.returncode == main.EXIT_USAGE
    assert "bad job spec" in proc.stderr


def test_entrypoint_boot_guard_without_consent_exits_3(tmp_path):
    """Pre-boot platform 3 (EULA) — the one code that survived p5c13 unaided.

    Regression guard: it must keep surviving now that the delivery changed. It
    also positively controls the arm above (same runner, valid spec -> the parse
    is NOT what stopped it).
    """
    proc = _run_entrypoint(json.dumps(_valid_spec()), tmp_path)
    assert proc.returncode == main.EXIT_PLATFORM
    assert "EULA not accepted" in proc.stderr


# --------------------------------------------------------------------------- #
# Structure: nothing may run between the decision and the status (G-62).
# --------------------------------------------------------------------------- #
def _function(name: str) -> ast.FunctionDef:
    for node in _TREE.body:
        if isinstance(node, ast.FunctionDef) and node.name == name:
            return node
    raise AssertionError(f"cv_infra/runner/main.py has no top-level def {name}")


def test_run_closes_no_vendor_object_on_its_terminal_path():
    """``run`` must not call ``.close()`` on anything — above all ``sim.close()``.

    That call is what took the process (status 0) before the decided code could
    be delivered. Placing a code AFTER it does not help either: p5c13 measured
    that nothing after ``close()`` runs, marker included.
    """
    closes = [
        ast.unparse(node.func)
        for node in ast.walk(_function("run"))
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr == "close"
    ]
    assert not closes, (
        f"run() calls {closes} — a vendor close() ends the process with status 0 "
        "and erases the job's exit code (G-62). Resource return happens by process "
        "death via main.hard_exit; see the finally block's rationale."
    )


def test_live_probe_job_spec_is_valid_and_fails_only_after_boot():
    """The GPU probe's own spec, checked on CPU (a broken probe costs a GPU session).

    scripts/measure/exit_contract_probe.sh drives its two boot arms with an embedded
    JOB_SPEC. If that spec were invalid the runner would exit 2 at the parse and the
    arm would silently stop measuring what it claims to (post-boot 3). So: it must
    parse, AND its scene must be one ``resolve_scene`` rejects — that rejection is
    what makes the failure happen inside ``load_scene``, i.e. AFTER SimulationApp.
    """
    from cv_infra.runner.sim_runtime import resolve_scene

    script = Path(__file__).resolve().parents[1] / "scripts/measure/exit_contract_probe.sh"
    body = script.read_text(encoding="utf-8")
    block = body.split("# --- BEGIN PROBE_JOB_SPEC ---")[1].split("# --- END PROBE_JOB_SPEC ---")[0]
    payload = block.split("<<'JSON' || true", 1)[1].rsplit("JSON", 1)[0]

    spec = json.loads(payload)
    main.parse_request(spec)  # raises BadJobSpec (-> the arm would measure 2, not 3)
    with pytest.raises(ValueError):
        resolve_scene(spec["scenario"]["scene"])


def test_module_entrypoint_delivers_the_code_with_hard_exit():
    """``sys.exit`` is not enough: interpreter shutdown (Kit atexit/__del__/threads)
    can still exit 0 or hang after the code is raised."""
    guard = _TREE.body[-1]
    assert isinstance(guard, ast.If) and ast.unparse(guard.test) == "__name__ == '__main__'"
    calls = [
        ast.unparse(node.func)
        for node in ast.walk(guard)
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
    ]
    assert "hard_exit" in calls and "sys.exit" not in ast.unparse(guard)
