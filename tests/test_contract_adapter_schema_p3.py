"""M1 P3 canonical adapter schema tests (contract/adapter_schema.py —
REQ-EXEC-004/005/006, FU-3 F2).

The pydantic canon must stay 1:1 with the Phase-2 dataclass canon
(cv_infra/adapter/adapter_schema.py — unmodified this cycle, consumers still
import it): bound MECHANICALLY (G-25) by field-name-set equality per model
pair AND by default-tree dump equality (dataclass ``to_dict()`` == pydantic
``model_dump()``), with the canonical fixture's measured adapter_config driven
through BOTH stacks. Plus the REQ-EXEC-005 blackbox negative: the schema has
NO field that reaches inside the SUT container.
"""

from __future__ import annotations

from dataclasses import fields as dc_fields
from pathlib import Path

import pytest
import yaml
from pydantic import ValidationError

from cv_infra.adapter import adapter_schema as dc_canon
from cv_infra.contract import adapter_schema as pyd_canon

FIXTURE = Path(__file__).parent / "fixtures" / "nova_carter_warehouse_goal.yaml"

# Every (pydantic model, phase-2 dataclass) canonical pair.
PAIRS = [
    (pyd_canon.GoalInterface, dc_canon.GoalInterface),
    (pyd_canon.CmdVel, dc_canon.CmdVel),
    (pyd_canon.SensorInput, dc_canon.SensorInput),
    (pyd_canon.Frames, dc_canon.Frames),
    (pyd_canon.Readiness, dc_canon.Readiness),
    (pyd_canon.Ros2AdapterConfig, dc_canon.Ros2AdapterConfig),
]


def _fixture_adapter_config() -> dict:
    doc = yaml.safe_load(FIXTURE.read_text(encoding="utf-8"))
    return doc["interface"]["adapter_config"]


# --------------------------------------------------------------------------- #
# mechanical 1:1 guards against the phase-2 dataclass canon (G-25)
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize(("model", "dataclass"), PAIRS)
def test_field_name_sets_match_the_dataclass_canon(model, dataclass):
    assert set(model.model_fields) == {f.name for f in dc_fields(dataclass)}


def test_default_trees_are_identical_across_both_canons():
    assert pyd_canon.Ros2AdapterConfig().model_dump() == dc_canon.Ros2AdapterConfig().to_dict()


def test_canonical_fixture_config_round_trips_identically_through_both():
    cfg = _fixture_adapter_config()
    via_dataclass = dc_canon.Ros2AdapterConfig.from_dict(cfg).to_dict()
    via_pydantic = pyd_canon.Ros2AdapterConfig.model_validate(cfg).model_dump()
    assert via_pydantic == via_dataclass


# --------------------------------------------------------------------------- #
# loud-reject + interface discriminator
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize(
    "corrupt",
    [
        lambda d: d.update(bogus=1),
        lambda d: d["goal_interface"].update(bogus=1),
        lambda d: d["sensors"][0].update(bogus=1),
        lambda d: d["frames"].update(bogus=1),
        lambda d: d["readiness"].update(bogus=1),
    ],
)
def test_unknown_keys_reject_loudly_at_every_nesting_level(corrupt):
    cfg = _fixture_adapter_config()
    corrupt(cfg)
    with pytest.raises(ValidationError):
        pyd_canon.Ros2AdapterConfig.model_validate(cfg)


def test_sensor_topic_and_type_required():
    with pytest.raises(ValidationError):
        pyd_canon.SensorInput.model_validate({"topic": "/scan"})


def test_interface_accepts_ros2_only():
    iface = pyd_canon.Interface.model_validate(
        {"type": "ros2", "adapter_config": _fixture_adapter_config()}
    )
    assert iface.type == "ros2"
    with pytest.raises(ValidationError):
        pyd_canon.Interface.model_validate({"type": "grpc", "adapter_config": {}})


def test_interface_defaults_are_the_locked_pins():
    iface = pyd_canon.Interface()
    assert iface.adapter_config.ros_distro == "jazzy"  # LOCKED §7-1
    assert iface.adapter_config.rmw == "rmw_fastrtps_cpp"
    assert iface.adapter_config.odom_topics == []  # R7: no SUT-specific default


# --------------------------------------------------------------------------- #
# REQ-EXEC-005 blackbox negative — the ABSENCE is the contract
# --------------------------------------------------------------------------- #
def test_schema_has_no_sut_internal_mutation_field():
    names = set(pyd_canon.Ros2AdapterConfig.model_fields) | set(pyd_canon.Interface.model_fields)
    forbidden = {
        "sut_command",
        "sut_entrypoint",
        "sut_env",
        "sut_args",
        "command_override",
        "entrypoint_override",
        "patch",
        "inject",
    }
    assert names.isdisjoint(forbidden)
    # exact top-level field set: adding ANY field is a conscious contract change
    assert set(pyd_canon.Ros2AdapterConfig.model_fields) == {
        "ros_distro",
        "rmw",
        "use_sim_time",
        "goal_interface",
        "cmd_vel",
        "clock_topic",
        "odom_topics",
        "sensors",
        "frames",
        "readiness",
    }
