"""CPU tests for the applied-settings line (DoD-P2-06 ①, cycle p5c15).

The gate's ① clause needs the settings a run ACTUALLY applied to be observable;
until now ``sim_runtime`` applied ``physics_dt``/``rendering_dt``/seed and reported
none of them, so N runs could not be compared at all. These tests pin:

* the WIRE — prefix + field order + field names, verbatim (QA greps it), with the
  values rendered at full round-trip precision;
* the SOURCE of the values — the line must carry what the World holds, not what
  the config asked for, and must say so loudly when the two differ or when the
  read-back is unavailable;
* the CALL SITE — ``load_scene`` is a ``# pragma: no cover - GPU path`` body, so an
  AST assert is the only available guard that the line is actually emitted, and
  emitted AFTER the World that owns the applied values exists.

Nothing here is evidence of determinism. It is evidence that a run says what it
ran with — the prerequisite for measuring anything on GPU.
"""

import ast
from pathlib import Path

from cv_infra.runner import sim_runtime
from cv_infra.runner.sim_runtime import SimConfig, SimRuntime

_SRC = Path(sim_runtime.__file__).read_text(encoding="utf-8")
_TREE = ast.parse(_SRC)


class _FakeWorld:
    """A World that reports back the dt it was constructed with (Isaac's shape)."""

    def __init__(self, physics_dt: float, rendering_dt: float) -> None:
        self._physics_dt = physics_dt
        self._rendering_dt = rendering_dt

    def get_physics_dt(self) -> float:
        return self._physics_dt

    def get_rendering_dt(self) -> float:
        return self._rendering_dt


class _MuteWorld:
    """A World with no dt read-back at all (the vendor API is not assumed)."""


def _runtime(world, **config) -> SimRuntime:
    runtime = SimRuntime(SimConfig(scene_ref="s.usd", robot_usd_ref="r.usd", **config))
    runtime.world = world
    return runtime


# --------------------------------------------------------------------------- #
# The wire (PM verbatim pin — shape only; every value below is this test's input).
# --------------------------------------------------------------------------- #
def test_sim_config_line_is_the_pinned_shape():
    line = sim_runtime.sim_config_log_line(0.02, 0.04, 42, "sha256:abc")
    assert line == (
        "[cv-runner] sim_config physics_dt=0.02 rendering_dt=0.04 "
        "seed=42 identity_key=sha256:abc"
    )


def test_sim_config_line_renders_absent_seed_and_key_as_none():
    line = sim_runtime.sim_config_log_line(0.02, 0.02, None, None)
    assert line.endswith("seed=none identity_key=none")


def test_sim_config_line_keeps_full_float_precision():
    # 1/60 is the runner default: a fixed-decimal format would print 0.016667 for
    # BOTH 1/60 and a subtly different dt, i.e. hide the divergence the line exists
    # to expose. repr round-trips.
    line = sim_runtime.sim_config_log_line(1.0 / 60.0, 1.0 / 60.0, 0, None)
    assert "physics_dt=0.016666666666666666 " in line
    assert float(line.split("physics_dt=")[1].split(" ")[0]) == 1.0 / 60.0


# --------------------------------------------------------------------------- #
# The values: APPLIED, not requested.
# --------------------------------------------------------------------------- #
def test_emit_reports_the_world_values_when_they_match_the_request():
    runtime = _runtime(_FakeWorld(0.02, 0.05), physics_dt=0.02, rendering_dt=0.05, seed=7)
    assert runtime.emit_sim_config() == (
        "[cv-runner] sim_config physics_dt=0.02 rendering_dt=0.05 seed=7 identity_key=none"
    )


def test_emit_prints_exactly_one_line_when_nothing_diverges(capsys):
    runtime = _runtime(_FakeWorld(0.02, 0.02), physics_dt=0.02, rendering_dt=0.02, seed=1)
    line = runtime.emit_sim_config()
    out = capsys.readouterr().out.splitlines()
    assert out == [line]


def test_emit_prefers_the_applied_value_and_says_the_request_diverged(capsys):
    # The runtime asked for 1/60 and the World is running something else: the LINE
    # carries what is running, and the divergence itself is loud (never silent).
    runtime = _runtime(_FakeWorld(0.02, 0.02), physics_dt=1.0 / 60.0, rendering_dt=1.0 / 60.0)
    line = runtime.emit_sim_config()
    assert "physics_dt=0.02 rendering_dt=0.02" in line
    warning = capsys.readouterr().out.splitlines()[1]
    assert warning.startswith("[cv-runner] WARNING: sim_config ")
    assert "physics_dt requested=0.016666666666666666 applied=0.02" in warning
    assert "rendering_dt requested=0.016666666666666666 applied=0.02" in warning


def test_emit_falls_back_to_the_request_and_marks_it_unobserved(capsys):
    runtime = _runtime(_MuteWorld(), physics_dt=0.02, rendering_dt=0.04, seed=3)
    line = runtime.emit_sim_config()
    assert "physics_dt=0.02 rendering_dt=0.04 seed=3" in line
    warning = capsys.readouterr().out.splitlines()[1]
    assert "physics_dt not readable from the World (line carries the REQUEST)" in warning
    assert "rendering_dt not readable from the World (line carries the REQUEST)" in warning


def test_emit_survives_a_read_back_that_raises(capsys):
    class _AngryWorld:
        def get_physics_dt(self):
            raise RuntimeError("vendor read-back exploded")

        def get_rendering_dt(self):
            return 0.02

    runtime = _runtime(_AngryWorld(), physics_dt=0.02, rendering_dt=0.02)
    assert "physics_dt=0.02" in runtime.emit_sim_config()
    assert "physics_dt not readable" in capsys.readouterr().out


def test_emit_carries_an_identity_key_when_one_is_supplied():
    # M4 derives the key and M3 injects it (p5c18 T4/T5 — see the method
    # docstring); this pins that the field is plumbed, not hardcoded to none.
    # The END-TO-END wire lives in tests/test_runner_request_identity_key.py.
    runtime = _runtime(_FakeWorld(0.02, 0.02), physics_dt=0.02, rendering_dt=0.02)
    assert runtime.emit_sim_config("sha256:deadbeef").endswith("identity_key=sha256:deadbeef")


# --------------------------------------------------------------------------- #
# The call site (GPU body -> AST guard, same idiom as test_runner_exit_contract).
# --------------------------------------------------------------------------- #
def _method(cls_name: str, name: str) -> ast.FunctionDef:
    for node in ast.walk(_TREE):
        if isinstance(node, ast.ClassDef) and node.name == cls_name:
            for child in node.body:
                if isinstance(child, ast.FunctionDef) and child.name == name:
                    return child
    raise AssertionError(f"sim_runtime.py has no {cls_name}.{name}")


def test_load_scene_emits_the_line_after_the_world_exists():
    """Deleting the emission (or moving it above the World) must fail here.

    ``load_scene`` cannot run on CPU, so the ordering invariant — settings applied
    THEN reported, before anything downstream can fail — is asserted on the source.
    """
    body = _method("SimRuntime", "load_scene")
    emits = [
        node.lineno
        for node in ast.walk(body)
        if isinstance(node, ast.Call) and ast.unparse(node.func) == "self.emit_sim_config"
    ]
    worlds = [
        node.lineno
        for node in ast.walk(body)
        if isinstance(node, ast.Call) and ast.unparse(node.func) == "World"
    ]
    assert emits, "load_scene() never emits the applied-settings line (DoD-P2-06 ①)"
    assert worlds, "load_scene() no longer constructs the World — this guard is stale"
    assert min(emits) > max(worlds), (
        "the applied-settings line must be emitted AFTER the World that holds the "
        "applied dt, or it reports a request instead of an application"
    )
