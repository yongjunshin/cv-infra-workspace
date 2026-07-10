"""M1 P3 loader 6-stage pipeline tests (loader.py — REQ-INTAKE-004/006/009,
NFR-INTAKE-003).

Positive: the canonical consumer fixture + 2 further scene/goal/criteria
variants load, validate, bind oracles and come back ADMITTED with zero
consumer/runner modification (DoD-P3-01 unit material). Negative: every
failing stage raises a friendly ``ContractError`` BEFORE an ``AdmittedRequest``
exists — and a stage-3 schema violation provably never reaches the stage-5
oracle binder (no propagation past the gate, NFR-INTAKE-003).
"""

from __future__ import annotations

import io
from pathlib import Path

import pytest

import cv_infra.contract.loader as loader_mod
import cv_infra.contract.version as version_mod
from cv_infra.contract.errors import ContractError
from cv_infra.contract.loader import AdmittedRequest, load_request
from cv_infra.contract.version import DeprecatedVersion

FIXTURE = Path(__file__).parent / "fixtures" / "nova_carter_warehouse_goal.yaml"

# Variant material (scene/goal/criteria all differ from the canonical fixture).
OFFICE_VARIANT = """\
scenario:
  scene: small_office
  robot: nova_carter
  goal: {x: 2.5, y: -1.0, yaw: 0.0}
  seed: 7
  timeout_s: 90
sut:
  image_ref: carter-sut:p2
  image_id: sha256:47aff5c993dac05b1664482e44af9401073336f142cb6d4919d81b47f8f9d48a
acceptance_criteria:
  - oracle: reached_goal
    params:
      position_tolerance_m: 0.5
"""

CUSTOM_ORACLE_VARIANT = """\
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


def _fixture_text() -> str:
    return FIXTURE.read_text(encoding="utf-8")


def _load_mutated(replace: str, with_: str) -> AdmittedRequest:
    return load_request(io.StringIO(_fixture_text().replace(replace, with_)), source_path="s.yaml")


# --------------------------------------------------------------------------- #
# positive — admit (stages 1-6)
# --------------------------------------------------------------------------- #
def test_canonical_fixture_admits_with_bound_oracles():
    admitted = load_request(FIXTURE)
    assert admitted.admitted is True
    assert admitted.oracles == ("reached_goal", "no_collision")  # stage-5 binding proof
    assert admitted.warnings == ()
    assert admitted.source_path == str(FIXTURE)
    assert admitted.request.sut.image_ref == "carter-sut:p2"
    assert admitted.request.scenario.timeout_s == 120


def test_office_variant_admits_from_a_stream():
    admitted = load_request(io.StringIO(OFFICE_VARIANT), source_path="office.yaml")
    assert admitted.request.scenario.scene == "small_office"
    assert admitted.request.sut.image_id.startswith("sha256:")
    assert admitted.oracles == ("reached_goal",)


def test_custom_oracle_variant_admits_via_explicit_path(tmp_path):
    scenario = tmp_path / "dock.yaml"
    scenario.write_text(CUSTOM_ORACLE_VARIANT, encoding="utf-8")
    admitted = load_request(scenario)
    assert admitted.oracles == ("custom_fixture",)  # plugin's own name, bound for real
    assert admitted.request.acceptance_criteria[0].params == {
        "anything": "goes",
        "plugin": "validates",
    }


def test_deprecated_api_version_warns_but_admits(monkeypatch):
    monkeypatch.setattr(
        version_mod,
        "DEPRECATED",
        {"cv-infra/v0": DeprecatedVersion(sunset="2 releases", migration_link="changelog")},
    )
    admitted = load_request(
        io.StringIO("apiVersion: cv-infra/v0\n" + _fixture_text()), source_path="s.yaml"
    )
    assert admitted.admitted is True  # accept + WARNING, execution continues
    assert len(admitted.warnings) == 1 and "DEPRECATED" in admitted.warnings[0]


# --------------------------------------------------------------------------- #
# negative — each stage rejects, nothing admitted (NFR-INTAKE-003)
# --------------------------------------------------------------------------- #
def test_stage1_parse_error_is_friendly_with_line():
    with pytest.raises(ContractError) as exc_info:
        load_request(io.StringIO("scenario: [unclosed\n  nope"), source_path="s.yaml")
    err = exc_info.value
    assert err.source_line is not None
    assert "Traceback" not in str(err)


def test_stage1_non_mapping_document_rejects():
    with pytest.raises(ContractError):
        load_request(io.StringIO("- just\n- a\n- list\n"), source_path="s.yaml")


def test_missing_file_rejects_as_contract_error(tmp_path):
    with pytest.raises(ContractError):
        load_request(tmp_path / "does_not_exist.yaml")


def test_stage2_unknown_api_version_rejects_with_location():
    with pytest.raises(ContractError) as exc_info:
        load_request(
            io.StringIO("apiVersion: cv-infra/v99\n" + _fixture_text()), source_path="s.yaml"
        )
    err = exc_info.value
    assert err.field_path == "apiVersion"
    assert (err.source_line, err.source_col) == (1, 13)  # value node of line 1
    assert "cv-infra/v1" in err.example


def test_stage3_schema_violation_never_reaches_the_oracle_binder(monkeypatch):
    # NFR-INTAKE-003 propagation negative: a request rejected at stage 3 must
    # not touch stage 5 (oracle load/bind = the execution-plane doorstep).
    calls: list[str] = []
    monkeypatch.setattr(loader_mod, "load_oracle", lambda name: calls.append(name))
    with pytest.raises(ContractError) as exc_info:
        _load_mutated("timeout_s: 120", "timeout_s: banana")
    assert calls == []  # gate held: nothing propagated past the failing stage
    err = exc_info.value
    assert err.field_path == "scenario.timeout_s"
    assert err.example == "timeout_s: 120"
    assert err.source_line is not None  # YAML-located for the M8 annotation


def test_stage3_missing_sut_rejects():
    text = "\n".join(
        line
        for line in _fixture_text().splitlines()
        if not line.startswith(("sut:", "  image_ref:"))
    )
    with pytest.raises(ContractError) as exc_info:
        load_request(io.StringIO(text), source_path="s.yaml")
    assert exc_info.value.field_path == "sut"
    assert exc_info.value.got == "(missing)"


def test_stage3_legacy_goal_tolerance_key_rejects_with_migration():
    with pytest.raises(ContractError) as exc_info:
        _load_mutated("position_tolerance_m: 0.75", "goal_tolerance_m: 0.5")
    assert "position_tolerance_m" in str(exc_info.value)


def test_stage3_bad_image_id_rejects_with_sha256_example():
    with pytest.raises(ContractError) as exc_info:
        load_request(
            io.StringIO(
                OFFICE_VARIANT.replace(
                    "sha256:" + "47aff5c993dac05b1664482e44af9401073336f142cb6d4919d81b47f8f9d48a",
                    "not-a-digest",
                )
            ),
            source_path="office.yaml",
        )
    err = exc_info.value
    assert err.field_path == "sut.image_id"
    assert "sha256:" in err.example


def test_stage5_unknown_oracle_rejects_with_criterion_path():
    with pytest.raises(ContractError) as exc_info:
        _load_mutated("oracle: no_collision", "oracle: does_not_exist")
    err = exc_info.value
    assert err.field_path == "acceptance_criteria[1].oracle"
    assert "does_not_exist" in err.got
    assert err.source_line is not None


def test_rejection_paths_never_construct_an_admitted_request(monkeypatch):
    # AdmittedRequest is the ONLY execution-plane handoff object; count its
    # constructions across every negative path above (belt over the raises).
    built: list[object] = []
    original = AdmittedRequest

    def spying(*args, **kwargs):
        obj = original(*args, **kwargs)
        built.append(obj)
        return obj

    monkeypatch.setattr(loader_mod, "AdmittedRequest", spying)
    for bad in (
        "- a list\n",
        "apiVersion: cv-infra/v99\n" + _fixture_text(),
        _fixture_text().replace("timeout_s: 120", "timeout_s: banana"),
        _fixture_text().replace("oracle: no_collision", "oracle: does_not_exist"),
    ):
        with pytest.raises(ContractError):
            load_request(io.StringIO(bad), source_path="s.yaml")
    assert built == []
