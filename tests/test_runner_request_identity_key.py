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
