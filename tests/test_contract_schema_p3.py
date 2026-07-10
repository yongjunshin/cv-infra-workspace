"""M1 P3 schema unit tests — pydantic v2 contract models (schema.py).

Covers: extra='forbid' at EVERY nesting level (loud unknown-key rejection),
the REQ-INTAKE-006 required triad, known-key oracle params (legacy
``goal_tolerance_m`` clear rejection), optional ``sut.image_id`` sha256 pin,
envelope/budget/settings shapes, and the mechanical field-name guards binding
the new sub-models to the Phase-2 dataclasses they formalize (G-25).

Also pins the container-safety constraints (D-C/R20 as amended by D-4'
2026-07-10): the wheel still installs --no-deps, pydantic is BUNDLE-SUPPLIED
(runner consumes contract.schema on the kit-prebundled pydantic; skew guarded
in docker/runner/Dockerfile), while host-only control-plane deps (yaml/docker)
must never reach the runner import surface — asserted in subprocesses.
"""

from __future__ import annotations

import subprocess
import sys
from dataclasses import fields as dc_fields

import pytest
from pydantic import ValidationError

from cv_infra.contract import models as old_models
from cv_infra.contract.apiversion import API_VERSION
from cv_infra.contract.schema import (
    CustomCriterion,
    DebugObstacle,
    ExecutionSettings,
    Goal,
    NoCollisionCriterion,
    ReachedGoalCriterion,
    RequestEnvelope,
    ResourceBudget,
    Scenario,
    SutRef,
    VerificationRequest,
)

VALID_DOC = {
    "scenario": {
        "scene": "nova_carter_warehouse",
        "robot": "nova_carter",
        "goal": {"x": -6.0, "y": 5.0, "yaw": 1.5708, "frame": "map"},
        "seed": 42,
        "timeout_s": 120,
    },
    "sut": {"image_ref": "carter-sut:p2"},
    "interface": {"type": "ros2", "adapter_config": {}},
    "acceptance_criteria": [
        {"oracle": "reached_goal", "params": {"position_tolerance_m": 0.75}},
        {
            "oracle": "no_collision",
            "params": {"chassis_path": "/World/Nova_Carter_ROS/chassis_link"},
        },
    ],
}

SHA256_ID = "sha256:" + "0" * 64


def _doc(**overrides) -> dict:
    return {**VALID_DOC, **overrides}


def test_valid_document_validates_and_defaults_materialize():
    req = VerificationRequest.model_validate(VALID_DOC)
    assert req.api_version == API_VERSION  # absent -> current (canonical fixture has none)
    assert req.sut.image_id is None
    assert req.execution_settings.repeats == 1
    assert isinstance(req.acceptance_criteria[0], ReachedGoalCriterion)
    assert isinstance(req.acceptance_criteria[1], NoCollisionCriterion)


def test_api_version_populates_from_the_wire_alias():
    req = VerificationRequest.model_validate(_doc(apiVersion=API_VERSION))
    assert req.api_version == API_VERSION


@pytest.mark.parametrize(
    "corrupt",
    [
        lambda d: d.update(bogus_top=1),
        lambda d: d["scenario"].update(bogus=1),
        lambda d: d["scenario"]["goal"].update(z=0.0),
        lambda d: d["sut"].update(entrypoint="/bin/sh"),
        lambda d: d["interface"]["adapter_config"].update(bogus=1),
        lambda d: d["acceptance_criteria"][0].update(weight=2),
        lambda d: d["acceptance_criteria"][0]["params"].update(bogus=1),
        lambda d: d["acceptance_criteria"][1]["params"].update(bogus=1),
    ],
)
def test_unknown_keys_reject_loudly_at_every_nesting_level(corrupt):
    import copy

    doc = copy.deepcopy(VALID_DOC)
    corrupt(doc)
    with pytest.raises(ValidationError):
        VerificationRequest.model_validate(doc)


@pytest.mark.parametrize("missing", ["scenario", "sut", "acceptance_criteria"])
def test_required_triad_missing_rejects(missing):
    doc = {k: v for k, v in VALID_DOC.items() if k != missing}
    with pytest.raises(ValidationError):
        VerificationRequest.model_validate(doc)


def test_empty_acceptance_criteria_rejects():
    with pytest.raises(ValidationError):
        VerificationRequest.model_validate(_doc(acceptance_criteria=[]))


def test_legacy_goal_tolerance_m_is_rejected_with_migration_prose():
    doc = _doc(
        acceptance_criteria=[{"oracle": "reached_goal", "params": {"goal_tolerance_m": 0.5}}]
    )
    with pytest.raises(ValidationError) as exc_info:
        VerificationRequest.model_validate(doc)
    assert "position_tolerance_m" in str(exc_info.value)  # clear migration, not a bare reject


def test_no_collision_requires_chassis_path_at_contract_time():
    # P2-13 precondition: absent chassis_path used to surface as a MID-MISSION
    # bind() raise — the P3 contract rejects it before the execution plane.
    doc = _doc(acceptance_criteria=[{"oracle": "no_collision", "params": {}}])
    with pytest.raises(ValidationError):
        VerificationRequest.model_validate(doc)


def test_custom_oracle_criterion_keeps_free_params():
    doc = _doc(acceptance_criteria=[{"oracle": "my_pkg.mod:MyOracle", "params": {"k": 1}}])
    req = VerificationRequest.model_validate(doc)
    assert isinstance(req.acceptance_criteria[0], CustomCriterion)
    assert req.acceptance_criteria[0].params == {"k": 1}


def test_sut_image_id_optional_but_sha256_when_given():
    assert SutRef(image_ref="carter-sut:p2").image_id is None
    assert SutRef(image_ref="carter-sut:p2", image_id=SHA256_ID).image_id == SHA256_ID
    for bad in ["47aff", "sha256:xyz", "sha256:" + "0" * 63]:
        with pytest.raises(ValidationError):
            SutRef(image_ref="carter-sut:p2", image_id=bad)


def test_scenario_timeout_must_be_positive_sim_time_budget():
    with pytest.raises(ValidationError):
        Scenario.model_validate({**VALID_DOC["scenario"], "timeout_s": -5})


# --- scenario.debug_obstacle (D-2' 2026-07-10: world state, not a criterion) -- #
def test_scenario_debug_obstacle_accepts_known_keys():
    minimal = Scenario.model_validate(
        {**VALID_DOC["scenario"], "debug_obstacle": {"x": -6.0, "y": 2.0}}
    )
    assert (minimal.debug_obstacle.x, minimal.debug_obstacle.y) == (-6.0, 2.0)
    # dimensions default to None = "runner default applies" (values stay M2-owned)
    assert minimal.debug_obstacle.height is None
    full = {"x": -6.0, "y": 2.0, "height": 0.15, "width": 1.2, "depth": 0.4}
    scenario = Scenario.model_validate({**VALID_DOC["scenario"], "debug_obstacle": full})
    assert scenario.debug_obstacle.model_dump() == full


def test_scenario_debug_obstacle_is_optional_and_rejects_unknown_keys():
    assert Scenario.model_validate(VALID_DOC["scenario"]).debug_obstacle is None  # optional
    with pytest.raises(ValidationError):  # known-key: no free-form ride-alongs (D-2')
        Scenario.model_validate(
            {**VALID_DOC["scenario"], "debug_obstacle": {"x": 0.0, "y": 0.0, "radius": 1.0}}
        )
    with pytest.raises(ValidationError):  # x/y are the runner's unconditional reads
        Scenario.model_validate({**VALID_DOC["scenario"], "debug_obstacle": {"y": 0.0}})
    with pytest.raises(ValidationError):  # physical dimensions must be positive
        Scenario.model_validate(
            {**VALID_DOC["scenario"], "debug_obstacle": {"x": 0.0, "y": 0.0, "height": 0}}
        )


def test_debug_obstacle_keys_match_the_runner_read_set():
    # G-25-style mechanical bind: the contract's known keys == the keys
    # ``SimRuntime.spawn_debug_obstacle`` actually reads (``spec["k"]`` /
    # ``spec.get("k", ...)`` call sites) — never a hand-kept list.
    import inspect
    import re

    from cv_infra.runner.sim_runtime import SimRuntime

    src = inspect.getsource(SimRuntime.spawn_debug_obstacle)
    reads = set(re.findall(r"""spec(?:\.get\(|\[)\s*["'](\w+)["']""", src))
    assert reads, "read-set extraction went empty (positive control, G-07)"
    assert set(DebugObstacle.model_fields) == reads


def test_execution_settings_bounds():
    assert ExecutionSettings().repeats == 1 and ExecutionSettings().fixed_dt is None
    assert ExecutionSettings(repeats=3, fixed_dt=1 / 60).repeats == 3
    with pytest.raises(ValidationError):
        ExecutionSettings(repeats=0)
    with pytest.raises(ValidationError):
        ExecutionSettings(fixed_dt=0)


def test_envelope_requires_trigger_source_and_one_request():
    env = RequestEnvelope.model_validate({"trigger_source": "ci-cd", "requests": [VALID_DOC]})
    assert env.api_version == API_VERSION and env.is_self_test is False
    with pytest.raises(ValidationError):
        RequestEnvelope.model_validate({"trigger_source": "cron", "requests": [VALID_DOC]})
    with pytest.raises(ValidationError):
        RequestEnvelope.model_validate({"trigger_source": "ci-cd", "requests": []})
    with pytest.raises(ValidationError):
        RequestEnvelope.model_validate({"requests": [VALID_DOC]})  # never silently defaulted


def test_resource_budget_shape_and_bounds():
    budget = ResourceBudget.model_validate({"vram_per_instance_gb": 8.0, "max_concurrent": 2})
    assert budget.scheduling_policy == "fifo"
    with pytest.raises(ValidationError):
        ResourceBudget.model_validate({"vram_per_instance_gb": 0, "max_concurrent": 2})
    with pytest.raises(ValidationError):
        ResourceBudget.model_validate({"vram_per_instance_gb": 8.0, "max_concurrent": 0})
    with pytest.raises(ValidationError):
        ResourceBudget.model_validate(
            {"vram_per_instance_gb": 8.0, "max_concurrent": 2, "bogus": 1}
        )


# --------------------------------------------------------------------------- #
# Mechanical drift guards: new sub-models <-> the Phase-2 dataclasses (G-25).
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize(
    ("new_model", "old_dataclass", "p3_additions"),
    [
        (Goal, old_models.Goal, set()),
        # debug_obstacle: D-2' addition (2026-07-10) — intentionally absent from
        # the retiring Phase-2 dataclass (models.py retires this cycle, D-4').
        (Scenario, old_models.Scenario, {"debug_obstacle"}),
    ],
)
def test_field_names_track_the_phase2_dataclasses(new_model, old_dataclass, p3_additions):
    expected = {f.name for f in dc_fields(old_dataclass)} | p3_additions
    assert set(new_model.model_fields) == expected


def test_criterion_members_keep_the_canonical_two_keys():
    for member in (ReachedGoalCriterion, NoCollisionCriterion, CustomCriterion):
        assert set(member.model_fields) == {
            f.name for f in dc_fields(old_models.AcceptanceCriterion)
        }


def test_runner_import_surface_pulls_no_host_only_deps():
    # D-4' (supersedes the D-C/R20 "no pydantic in the runner" premise): the
    # runner consumes contract.schema on the BUNDLE-SUPPLIED pydantic, so
    # pydantic on its import surface is sanctioned (version skew is a loud
    # BUILD failure — docker/runner/Dockerfile assert). Host-only control-plane
    # deps must still never be pulled (wheel installs --no-deps).
    code = (
        "import sys\n"
        "import cv_infra.contract.models, cv_infra.oracles.no_collision, cv_infra.runner.main\n"
        "assert 'yaml' not in sys.modules, 'runner import surface pulled pyyaml'\n"
        "assert 'docker' not in sys.modules, 'runner import surface pulled docker'\n"
    )
    subprocess.run([sys.executable, "-c", code], check=True)


def test_contract_package_import_stays_stdlib_only():
    # The PEP 562 lazy-export invariant survives D-4': ``import
    # cv_infra.contract`` alone (no schema attribute touched) stays stdlib-only
    # — third-party pulls happen only at the consumer's explicit submodule
    # import (runner: schema on the bundled pydantic; CLI: + yaml).
    code = (
        "import sys\n"
        "import cv_infra.contract\n"
        "assert 'pydantic' not in sys.modules, 'contract package import pulled pydantic'\n"
        "assert 'yaml' not in sys.modules, 'contract package import pulled pyyaml'\n"
    )
    subprocess.run([sys.executable, "-c", code], check=True)
