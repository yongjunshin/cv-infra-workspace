"""M1 p4c3 RequestEnvelope contract tests (envelope.py — D-2 file refs,
REQ-INTAKE-001/004/005) + the ``load_request`` ``plugin_dir`` kwarg (loader.py).

Positive: a valid envelope admits every referenced scenario through the
EXISTING 6-stage gate in file order, resolves relative paths against the
envelope file, applies the per-request ``repeats`` override on BOTH views
(``raw_doc`` wire canonical + admitted model), and anchors scenario-adjacent
custom oracles to the SCENARIO's directory (not the envelope's). Negative:
envelope-level violations carry the ENVELOPE file's line/col; a referenced
scenario's violations propagate the loader's error untouched (SCENARIO file
line/col) — the failing file is always distinguishable by ``source_path``.

The ``plugin_dir`` kwarg tests include the positive control the task pins:
WITHOUT the kwarg a stream input has no stage-5 anchor (the previously
impossible path), WITH it the same stream admits.
"""

from __future__ import annotations

import io
import sys
from pathlib import Path

import pytest

from cv_infra.contract.envelope import LoadedEnvelope, load_envelope
from cv_infra.contract.errors import ContractError
from cv_infra.contract.loader import load_request

FIXTURE = Path(__file__).parent / "fixtures" / "nova_carter_warehouse_goal.yaml"

OFFICE_SCENARIO = """\
apiVersion: cv-infra/v1
scenario:
  scene: small_office
  robot: nova_carter
  goal: {x: 2.5, y: -1.0, yaw: 0.0}
  seed: 7
  timeout_s: 90
sut:
  image_ref: carter-sut:p2
acceptance_criteria:
  - oracle: reached_goal
    params:
      position_tolerance_m: 0.5
"""

CUSTOM_ORACLE_SCENARIO = """\
apiVersion: cv-infra/v1
scenario:
  scene: loading_dock
  robot: nova_carter
  goal: {x: 0.0, y: 8.0, yaw: 3.14}
  seed: 1234
  timeout_s: 240
sut:
  image_ref: carter-sut:next
acceptance_criteria:
  - oracle: tests.oracle_plugin_fixture:CustomOracle
    params: {anything: goes, plugin: validates}
"""

VALID_ENVELOPE = """\
apiVersion: cv-infra/v1
requests:
  - scenario: scenarios/office.yaml
    repeats: 3
  - scenario: scenarios/warehouse.yaml
"""


def _oracle_src(class_name: str, oracle_name: str) -> str:
    """A consumer-authored custom oracle module (the loader-test fixture shape)."""
    return f"""\
from cv_infra.oracles.base import OracleBase


class {class_name}(OracleBase):
    name = "{oracle_name}"
    version = "0.0.1"

    def validate_params(self, criteria):
        return None

    def evaluate(self, telemetry, criteria):
        return {{"passed": True}}
"""


def _make_tree(tmp_path: Path, envelope_text: str, scenarios: dict[str, str]) -> Path:
    """tmp layout: batch.yaml (envelope) + scenarios/<name>.yaml next to it."""
    (tmp_path / "scenarios").mkdir(exist_ok=True)
    for name, text in scenarios.items():
        (tmp_path / "scenarios" / name).write_text(text, encoding="utf-8")
    envelope = tmp_path / "batch.yaml"
    envelope.write_text(envelope_text, encoding="utf-8")
    return envelope


# --------------------------------------------------------------------------- #
# positive — valid envelopes admit (task tests 1 / 2 / 6)
# --------------------------------------------------------------------------- #
def test_valid_envelope_admits_in_order_with_repeats_override(tmp_path):
    envelope = _make_tree(
        tmp_path,
        VALID_ENVELOPE,
        {
            "office.yaml": OFFICE_SCENARIO,
            "warehouse.yaml": FIXTURE.read_text(encoding="utf-8"),
        },
    )
    loaded = load_envelope(envelope)
    assert isinstance(loaded, LoadedEnvelope)
    assert loaded.api_version == "cv-infra/v1"
    assert isinstance(loaded.requests, tuple)
    scenes = [ref.admitted.request.scenario.scene for ref in loaded.requests]
    assert scenes == ["small_office", "nova_carter_warehouse"]  # envelope-file order
    office, warehouse = loaded.requests

    # repeats override lands on BOTH views (raw_doc = wire canonical, admitted = model)
    assert office.admitted.request.execution_settings.repeats == 3
    assert office.raw_doc["execution_settings"]["repeats"] == 3
    assert office.raw_doc["scenario"]["scene"] == "small_office"  # otherwise verbatim parse
    # the un-overridden entry stays untouched on both views
    assert warehouse.admitted.request.execution_settings.repeats == 1  # schema default
    assert "execution_settings" not in warehouse.raw_doc

    # 6-stage evidence + resolved path fields
    assert office.admitted.oracles == ("reached_goal",)
    assert warehouse.admitted.oracles == ("reached_goal", "no_collision")
    scenarios_dir = str((tmp_path / "scenarios").resolve())
    for ref in loaded.requests:
        assert ref.admitted.admitted is True
        assert Path(ref.scenario_path).is_absolute()
        assert ref.oracle_plugin_dir == scenarios_dir
        assert ref.admitted.source_path == ref.scenario_path  # scenario-file attribution


def test_repeats_override_replaces_an_existing_execution_settings_value(tmp_path):
    scenario = OFFICE_SCENARIO + "execution_settings:\n  repeats: 2\n  fixed_dt: 0.016667\n"
    envelope = _make_tree(
        tmp_path,
        "apiVersion: cv-infra/v1\nrequests:\n  - scenario: scenarios/office.yaml\n    repeats: 5\n",
        {"office.yaml": scenario},
    )
    ref = load_envelope(envelope).requests[0]
    assert ref.admitted.request.execution_settings.repeats == 5
    assert ref.admitted.request.execution_settings.fixed_dt == 0.016667
    # sibling keys survive the override — only repeats is replaced
    assert ref.raw_doc["execution_settings"] == {"repeats": 5, "fixed_dt": 0.016667}


def test_scenario_paths_resolve_relative_to_the_envelope_file(tmp_path):
    (tmp_path / "scenarios").mkdir()
    (tmp_path / "scenarios" / "office.yaml").write_text(OFFICE_SCENARIO, encoding="utf-8")
    (tmp_path / "envelopes").mkdir()
    envelope = tmp_path / "envelopes" / "batch.yaml"
    envelope.write_text(
        "apiVersion: cv-infra/v1\nrequests:\n  - scenario: ../scenarios/office.yaml\n",
        encoding="utf-8",
    )
    ref = load_envelope(envelope).requests[0]
    assert ref.scenario_path == str((tmp_path / "scenarios" / "office.yaml").resolve())
    assert ref.oracle_plugin_dir == str((tmp_path / "scenarios").resolve())
    assert ref.admitted.request.scenario.scene == "small_office"


def test_custom_oracle_scenario_admits_via_envelope(tmp_path):
    envelope = _make_tree(
        tmp_path,
        "apiVersion: cv-infra/v1\nrequests:\n  - scenario: scenarios/dock.yaml\n",
        {"dock.yaml": CUSTOM_ORACLE_SCENARIO},
    )
    ref = load_envelope(envelope).requests[0]
    assert ref.admitted.oracles == ("custom_fixture",)  # plugin's own name, bound for real
    assert ref.admitted.request.acceptance_criteria[0].params == {
        "anything": "goes",
        "plugin": "validates",
    }


def test_scenario_adjacent_oracle_anchors_to_the_scenario_dir_not_the_envelope_dir(tmp_path):
    # D-2 rationale: the file-reference form resolves scenario-adjacent custom
    # oracles naturally — the anchor is the SCENARIO's dir even though the
    # envelope lives elsewhere.
    scenarios = tmp_path / "scenarios"
    scenarios.mkdir()
    (scenarios / "p4c3_envelope_adjacent_oracle.py").write_text(
        _oracle_src("EnvelopeAdjacentOracle", "envelope_adjacent_fixture"), encoding="utf-8"
    )
    (scenarios / "dock.yaml").write_text(
        CUSTOM_ORACLE_SCENARIO.replace(
            "tests.oracle_plugin_fixture:CustomOracle",
            "p4c3_envelope_adjacent_oracle:EnvelopeAdjacentOracle",
        ),
        encoding="utf-8",
    )
    envelope = tmp_path / "batch.yaml"  # envelope dir != scenario dir
    envelope.write_text(
        "apiVersion: cv-infra/v1\nrequests:\n  - scenario: scenarios/dock.yaml\n",
        encoding="utf-8",
    )
    before = list(sys.path)
    try:
        loaded = load_envelope(envelope)
    finally:
        sys.modules.pop("p4c3_envelope_adjacent_oracle", None)  # no import residue
    ref = loaded.requests[0]
    assert ref.admitted.oracles == ("envelope_adjacent_fixture",)
    assert ref.oracle_plugin_dir == str(scenarios.resolve())
    assert sys.path == before  # stage-5-scoped anchor, restored


# --------------------------------------------------------------------------- #
# negative — envelope-level violations (envelope-file line/col; task test 3)
# --------------------------------------------------------------------------- #
def test_envelope_absent_api_version_rejects_with_add_the_key_guidance(tmp_path):
    envelope = tmp_path / "batch.yaml"
    envelope.write_text("requests:\n  - scenario: scenarios/office.yaml\n", encoding="utf-8")
    with pytest.raises(ContractError) as exc_info:
        load_envelope(envelope)
    err = exc_info.value
    assert err.field_path == "apiVersion"
    assert err.got == "(missing)"
    assert err.example == "apiVersion: cv-infra/v1"
    assert err.source_path == str(envelope)
    assert "Traceback" not in str(err)


def test_envelope_unknown_api_version_rejects_with_envelope_line_col(tmp_path):
    envelope = tmp_path / "batch.yaml"
    envelope.write_text(
        "apiVersion: cv-infra/v99\nrequests:\n  - scenario: scenarios/office.yaml\n",
        encoding="utf-8",
    )
    with pytest.raises(ContractError) as exc_info:
        load_envelope(envelope)
    err = exc_info.value
    assert err.field_path == "apiVersion"
    assert (err.source_line, err.source_col) == (1, 13)  # value node in the ENVELOPE file
    assert err.source_path == str(envelope)
    assert "cv-infra/v1" in err.example


def test_envelope_empty_requests_rejects_with_envelope_line_col(tmp_path):
    envelope = tmp_path / "batch.yaml"
    envelope.write_text("apiVersion: cv-infra/v1\nrequests: []\n", encoding="utf-8")
    with pytest.raises(ContractError) as exc_info:
        load_envelope(envelope)
    err = exc_info.value
    assert err.field_path == "requests"
    assert err.source_line == 2
    assert err.source_path == str(envelope)
    assert "Traceback" not in str(err)


def test_envelope_missing_scenario_key_rejects_with_envelope_line_col(tmp_path):
    envelope = tmp_path / "batch.yaml"
    envelope.write_text("apiVersion: cv-infra/v1\nrequests:\n  - repeats: 2\n", encoding="utf-8")
    with pytest.raises(ContractError) as exc_info:
        load_envelope(envelope)
    err = exc_info.value
    assert err.field_path == "requests[0].scenario"
    assert err.got == "(missing)"
    assert err.source_line == 3  # nearest enclosing node = the entry mapping
    assert err.source_path == str(envelope)
    assert err.example == "scenario: scenarios/nova_carter_warehouse_goal.yaml"


# --------------------------------------------------------------------------- #
# negative — referenced-scenario violations (scenario-file line/col; tasks 4/5)
# --------------------------------------------------------------------------- #
def test_referenced_scenario_violation_carries_scenario_file_line_col(tmp_path):
    broken = OFFICE_SCENARIO.replace("timeout_s: 90", "timeout_s: banana")
    envelope = _make_tree(
        tmp_path,
        "apiVersion: cv-infra/v1\nrequests:\n  - scenario: scenarios/office.yaml\n",
        {"office.yaml": broken},
    )
    with pytest.raises(ContractError) as exc_info:
        load_envelope(envelope)
    err = exc_info.value
    scenario_path = str((tmp_path / "scenarios" / "office.yaml").resolve())
    assert err.source_path == scenario_path  # the SCENARIO file, not the envelope
    assert err.source_path != str(envelope)  # -> the failing file is distinguishable
    assert err.field_path == "scenario.timeout_s"
    timeout_line = next(
        i for i, ln in enumerate(broken.splitlines(), 1) if ln.lstrip().startswith("timeout_s:")
    )
    assert err.source_line == timeout_line  # SCENARIO-file line, verbatim text
    assert err.example == "timeout_s: 120"


def test_missing_scenario_file_rejects_with_envelope_location(tmp_path):
    envelope = tmp_path / "batch.yaml"
    envelope.write_text(
        "apiVersion: cv-infra/v1\nrequests:\n  - scenario: scenarios/ghost.yaml\n",
        encoding="utf-8",
    )
    with pytest.raises(ContractError) as exc_info:
        load_envelope(envelope)
    err = exc_info.value
    assert err.field_path == "requests[0].scenario"
    assert err.source_path == str(envelope)
    assert err.source_line == 3  # the reference's own line in the envelope
    assert "ghost.yaml" in err.got  # names the resolved path the user gave
    assert "Traceback" not in str(err)


# --------------------------------------------------------------------------- #
# load_request plugin_dir kwarg (task test 7 — stream anchor, new capability)
# --------------------------------------------------------------------------- #
def test_plugin_dir_kwarg_anchors_a_stream_input(tmp_path):
    (tmp_path / "p4c3_stream_oracle.py").write_text(
        _oracle_src("StreamOracle", "stream_fixture"), encoding="utf-8"
    )
    text = CUSTOM_ORACLE_SCENARIO.replace(
        "tests.oracle_plugin_fixture:CustomOracle", "p4c3_stream_oracle:StreamOracle"
    )

    # positive control: WITHOUT the kwarg a stream has no stage-5 anchor —
    # the exact path that was impossible before p4c3.
    with pytest.raises(ContractError) as exc_info:
        load_request(io.StringIO(text), source_path="dock.yaml")
    assert exc_info.value.field_path == "acceptance_criteria[0].oracle"

    before = list(sys.path)
    try:
        admitted = load_request(
            io.StringIO(text), source_path="dock.yaml", plugin_dir=str(tmp_path)
        )
    finally:
        sys.modules.pop("p4c3_stream_oracle", None)  # no import residue
    assert admitted.oracles == ("stream_fixture",)
    assert sys.path == before  # kwarg anchor is stage-5-scoped and restored
    assert str(tmp_path.resolve()) not in sys.path


def test_plugin_dir_kwarg_wins_over_the_file_parent_anchor(tmp_path):
    plugins = tmp_path / "plugins"
    plugins.mkdir()
    (plugins / "p4c3_elsewhere_oracle.py").write_text(
        _oracle_src("ElsewhereOracle", "elsewhere_fixture"), encoding="utf-8"
    )
    scenarios = tmp_path / "scenarios"
    scenarios.mkdir()
    scenario = scenarios / "dock.yaml"
    scenario.write_text(
        CUSTOM_ORACLE_SCENARIO.replace(
            "tests.oracle_plugin_fixture:CustomOracle", "p4c3_elsewhere_oracle:ElsewhereOracle"
        ),
        encoding="utf-8",
    )

    # control: the parent-dir auto-anchor cannot see plugins/
    with pytest.raises(ContractError):
        load_request(scenario)

    try:
        admitted = load_request(scenario, plugin_dir=str(plugins))
    finally:
        sys.modules.pop("p4c3_elsewhere_oracle", None)
    assert admitted.oracles == ("elsewhere_fixture",)  # explicit anchor won
