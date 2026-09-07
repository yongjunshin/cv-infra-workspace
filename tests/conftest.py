"""Shared test scaffolding.

``go2_world`` is the one piece worth explaining. The platform used to SHIP a
``go2_warehouse`` row in ``sim_runtime.SCENE_ASSETS``, holding a consumer's robot
USD, its measured drop height, its trained stance and its training render
interval. That row is gone: a v2 request carries an embodiment profile and the
runner builds the row from it at admit.

Tests that exercise a COMPOSED world (a robot-free scene plus a referenced robot)
still need such a row, so this fixture installs the one the go2 consumer's own
document produces — i.e. it performs, explicitly, the step admit performs for a
v2 request. It is deliberately NOT autouse: a test that gets a composed world
should say so, and ``test_contract_profile`` asserts that the shipped registry
holds nothing but the scene a v1 document still names.
"""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from cv_infra.contract.profile import EmbodimentProfile

GO2_FIXTURE = Path(__file__).parent / "fixtures" / "go2_embodiment.yaml"

#: The go2 consumer's embodiment document, loaded once.
GO2_PROFILE = EmbodimentProfile.model_validate(
    yaml.safe_load(GO2_FIXTURE.read_text(encoding="utf-8"))
)


@pytest.fixture
def go2_world(monkeypatch: pytest.MonkeyPatch) -> EmbodimentProfile:
    """Register the go2 consumer's profile under the name its v1 documents use."""
    from cv_infra.runner.sim_runtime import SCENE_ASSETS, SceneAsset

    monkeypatch.setitem(SCENE_ASSETS, "go2_warehouse", SceneAsset.from_profile(GO2_PROFILE))
    return GO2_PROFILE
