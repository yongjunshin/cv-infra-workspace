"""CPU tests for the request-identity-key wire (DoD-P2-06 ①, cycle p5c18 T5).

T3 measured the gate's ① clause on GPU and found three of its four fields fully
observable and the fourth — ``identity_key`` — ``none``/``null`` on every plane the
job itself writes. The key existed (M4 derives it) but never reached the job, so
"all N runs agree" reduced to ``null == null``: a vacuous equality (G-35).

T4 closed the M3 half (``supervisor.REQUEST_IDENTITY_KEY_ENV`` injected into the
runner container). These tests pin the M2 half:

* the WIRE NAME — a cross-team literal, asserted against M3's own constant, so a
  rename on either side is red instead of silently absent (G-17 drift class);
* VERBATIM handling — the runner carries the value, it never parses, validates,
  normalizes or RE-DERIVES it. T4's mutation ② showed a re-derived key is
  non-null, well-formed and per-request distinct, i.e. wrong in the only way a
  shape check cannot see;
* ABSENT means absent — no substitution, no empty string, no fabricated key;
* both SINKS — the applied-settings line and ``result.json`` — and the GPU-body
  call sites that feed them (AST guards, same idiom as the sim_config tests).
"""

import ast
import json
from pathlib import Path

from cv_infra.orchestrator.supervisor import REQUEST_IDENTITY_KEY_ENV as M3_ENV
from cv_infra.runner import main
from cv_infra.runner.evaluate import build_result_dict

_KEY = "sha256:3ed4011ca34c5a1c4f1a32ded31a93812ec2a9d8664fb42a8d4cccd8da8db487"

_MAIN_TREE = ast.parse(Path(main.__file__).read_text(encoding="utf-8"))


def _func(tree: ast.AST, name: str) -> ast.FunctionDef:
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef) and node.name == name:
            return node
    raise AssertionError(f"no function {name}")


# --------------------------------------------------------------------------- #
# The wire name is a cross-team contract, not a local string.
# --------------------------------------------------------------------------- #
def test_env_key_literal_is_pinned_on_both_sides():
    assert main.REQUEST_IDENTITY_KEY_ENV == "CV_REQUEST_IDENTITY_KEY"
    assert main.REQUEST_IDENTITY_KEY_ENV == M3_ENV, (
        "the runner reads a different env name than the supervisor injects — the key "
        "would be silently absent on every job (the exact p5c18 T3 finding)"
    )


# --------------------------------------------------------------------------- #
# Reading: verbatim, and absent means absent.
# --------------------------------------------------------------------------- #
def test_reader_returns_the_injected_value_verbatim():
    assert main.read_request_identity_key({"CV_REQUEST_IDENTITY_KEY": _KEY}) == _KEY


def test_reader_does_not_validate_or_normalize_the_value():
    # Deliberately NOT a sha256: the runner is not the key's judge. A runner that
    # "fixed" a malformed key would be inventing provenance; M4 owns the format
    # and any rejection belongs there, loudly, not here silently.
    weird = "  not-a-sha256  "
    assert main.read_request_identity_key({"CV_REQUEST_IDENTITY_KEY": weird}) == weird


def test_reader_returns_none_when_the_key_was_not_injected():
    assert main.read_request_identity_key({}) is None


def test_reader_treats_an_empty_value_as_absent():
    # G-26 empty-env variant: an empty string would render as a VALUE
    # (``identity_key=`` / ``"request_identity_key": ""``) and read as "known".
    assert main.read_request_identity_key({"CV_REQUEST_IDENTITY_KEY": ""}) is None


# --------------------------------------------------------------------------- #
# Sink 1: the canonical result.json.
# --------------------------------------------------------------------------- #
def test_result_carries_the_identity_key():
    result = build_result_dict("job-0001", "pass", [], {}, request_identity_key=_KEY)
    assert result["request_identity_key"] == _KEY
    # It must survive the canonical serialization the supervisor recovers, not
    # just the in-memory model (the file IS the product).
    assert json.loads(json.dumps(result))["request_identity_key"] == _KEY


def test_result_reports_an_absent_key_as_null_not_as_a_guess():
    result = build_result_dict("job-0001", "pass", [], {})
    assert result["request_identity_key"] is None


# --------------------------------------------------------------------------- #
# The GPU call sites (pragma: no cover bodies -> AST guards).
# --------------------------------------------------------------------------- #
def test_run_reads_the_key_once_and_feeds_both_sinks():
    body = _func(_MAIN_TREE, "run")
    reads = [
        n
        for n in ast.walk(body)
        if isinstance(n, ast.Call) and ast.unparse(n.func) == "read_request_identity_key"
    ]
    assert len(reads) == 1, (
        "the key must be read exactly ONCE per job: two reads could disagree if the "
        "environment changed mid-run, and the log line and result.json would differ"
    )

    load_scenes = [
        n
        for n in ast.walk(body)
        if isinstance(n, ast.Call) and ast.unparse(n.func) == "sim.load_scene"
    ]
    assert load_scenes, "run() no longer calls sim.load_scene — this guard is stale"
    assert all(
        "identity_key" in [ast.unparse(a) for a in call.args] for call in load_scenes
    ), "load_scene() is called without the identity key -> the sim_config line says none"

    results = [
        n
        for n in ast.walk(body)
        if isinstance(n, ast.Call) and ast.unparse(n.func) == "build_result_dict"
    ]
    assert len(results) == 2, (
        "expected both the normal and the degraded result writer — a degraded job is "
        "exactly the one whose provenance a reviewer needs most"
    )
    for call in results:
        kwargs = {kw.arg: ast.unparse(kw.value) for kw in call.keywords}
        assert (
            kwargs.get("request_identity_key") == "identity_key"
        ), "a result.json is written without naming its request (p5c18 T3 실측 상태)"


def test_load_scene_forwards_its_parameter_to_the_settings_line():
    import cv_infra.runner.sim_runtime as sim_runtime

    tree = ast.parse(Path(sim_runtime.__file__).read_text(encoding="utf-8"))
    body = _func(tree, "load_scene")
    assert "identity_key" in [
        a.arg for a in body.args.args
    ], "load_scene lost its identity_key parameter"
    emits = [
        n
        for n in ast.walk(body)
        if isinstance(n, ast.Call) and ast.unparse(n.func) == "self.emit_sim_config"
    ]
    assert emits, "load_scene() no longer emits the applied-settings line"
    assert all(
        [ast.unparse(a) for a in call.args] == ["identity_key"] for call in emits
    ), "emit_sim_config() is not fed load_scene's parameter -> the line reports none"


# --------------------------------------------------------------------------- #
# The anti-derivation guard — T4's mutation ②, asserted on OUR side.
# --------------------------------------------------------------------------- #
def test_the_runner_never_derives_an_identity_key_of_its_own():
    """A key the runner computes would be non-null, well-formed and per-request
    distinct — and WRONG. The only correct value is the one M3 injected, so the
    runner plane must contain no derivation at all.

    ★ THIS IS A DEFENCE LINE, NOT A STYLE CHECK. QA measured (p5c18) that a
    mutation which re-derives the key turns exactly ONE test in the whole suite
    red. Weaken this assert and a wrong key passes 1082 green tests, because
    every OTHER check it faces is a shape check and the wrong key has the right
    shape. If you must relax it, relax it toward MORE specific, never toward
    deletion.

    It is STRUCTURAL (no derivation code exists). Its mechanism-independent
    partner is ``test_the_key_reaches_both_sinks_unchanged_and_absence_is_not
    _filled_in`` below, which would catch a derivation this AST walk cannot
    name (a hand-rolled digest, an inlined fallback). Keep BOTH: one asks
    "is there a second source in the tree", the other asks "did a second source
    reach the output".
    """
    offenders = []
    for path in sorted(Path(main.__file__).parent.rglob("*.py")):
        src = path.read_text(encoding="utf-8")
        tree = ast.parse(src)
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom) and node.module == "cv_infra.report.regression":
                offenders.append(f"{path.name}: imports {node.module}")
            if isinstance(node, ast.Call) and ast.unparse(node.func).endswith("identity_key"):
                if ast.unparse(node.func) != "read_request_identity_key":
                    offenders.append(f"{path.name}:{node.lineno}: calls {ast.unparse(node.func)}")
    assert (
        offenders == []
    ), f"the runner derives an identity key instead of carrying M3's: {offenders}"


# --------------------------------------------------------------------------- #
# The behavioural layer — the composition run.run() performs, both arms (G-35).
# --------------------------------------------------------------------------- #
def _both_sinks(env: dict) -> tuple[str, object]:
    """Reproduce the exact chain ``run()`` performs: read once, feed both sinks.

    ``run()`` itself is a ``# pragma: no cover`` GPU body, so the AST guards above
    prove the WIRING and this proves the VALUE that travels through it.
    """
    from cv_infra.runner.sim_runtime import sim_config_log_line

    key = main.read_request_identity_key(env)
    line = sim_config_log_line(1.0 / 60.0, 1.0 / 60.0, 42, key)
    result = build_result_dict("job-0001", "pass", [], {}, request_identity_key=key)
    return line, result["request_identity_key"]


def test_the_key_reaches_both_sinks_unchanged_and_absence_is_not_filled_in():
    """Presence and ABSENCE, asserted together — the pair is the point.

    Cross-plane equality (job plane key == report plane key) is proven on M3's
    side, where both planes exist (T4: one wire dump, two consumers). The runner
    half of that chain is this: whatever key arrived, BOTH runner outputs carry
    it byte-identically, and when none arrived neither output invents one. If
    either half fails, the two planes can disagree no matter how careful M3 was.

    The sentinel is SHAPED like a real key but is not derivable from any runner
    input, so it catches both failure modes at once: a substituted value cannot
    coincide with it, AND a transform that keys off the ``sha256:`` shape (strip,
    normalize, re-case) makes the two sinks disagree instead of sailing past a
    shapeless marker. That distinction is measured, not assumed — a shapeless
    sentinel let a prefix-stripping mutation through this assert.

    The absence arm catches a fabrication mechanism the structural guard cannot
    see: an inlined ``or <derive>`` fallback is invisible to an AST walk looking
    for M4's name, but it CANNOT keep this arm green.
    """
    sentinel = "sha256:" + "de1ec7ab1e" + "5" * 54  # key-shaped, non-derivable
    line, field = _both_sinks({"CV_REQUEST_IDENTITY_KEY": sentinel})
    assert line.endswith(f"identity_key={sentinel}")
    assert field == sentinel, "the two sinks disagree — one of them transformed the key"

    line, field = _both_sinks({})
    assert line.endswith("identity_key=none"), (
        "no key was injected yet the settings line reports one — the runner "
        "fabricated provenance (the failure mode T4 변이 ② demonstrated)"
    )
    assert field is None, "no key was injected yet result.json claims a request"
