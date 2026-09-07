"""The v2 request surface — what a consumer the platform has never seen submits.

Everything here guards one property: **a new consumer needs its own repository
and a request document, and nothing else.** The tests are grouped by the way
that property can quietly stop being true.

§1 the two forms — a request says what world it runs EITHER by naming a scene
   the platform ships (v1) or by shipping the assets itself (v2), never both and
   never neither, because "which one wins" decided by reading order would leave
   a block the consumer wrote silently ignored.
§2 ride-alongs — profile, artifacts and input-space model all resolve against the
   request's own directory and may not leave it, and every artifact is verified
   against its declared digest. That directory is what reaches the runner; a path
   outside it does not exist over there.
§3 the wire — the embodiment reaches the runner as DATA, and a v1 document's
   JOB_SPEC does not move by a byte.
§4 cases — the covering array becomes concrete requests, each with its own
   identity, and an axis that nothing reads (or that nothing declares) is a
   rejected request rather than a silently constant one.
§5 the warm-cache key — the closure is a set over the WHOLE suite, keyed with the
   engine, so a re-verification is a hit and an engine bump is not.
§6 missions — the built-in driver stays the default, and a consumer's own driver
   loads through the same seam a custom oracle does.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest
import yaml

from cv_infra.contract import derive, pict
from cv_infra.contract.assets import asset_closure, asset_set_key
from cv_infra.contract.errors import ContractError
from cv_infra.contract.job_spec import build_job_spec
from cv_infra.contract.loader import embodiment_digest, load_request
from cv_infra.contract.profile import EmbodimentProfile
from cv_infra.contract.schema import VerificationRequest
from cv_infra.runner.mission import BUILTIN_MISSIONS, MissionDriver, load_mission
from tests.conftest import GO2_FIXTURE, GO2_PROFILE

SPACE = "start_x: -6.3, -6.0, -5.7\nstart_y: -1.3, -1.0, -0.7\n"
IMAGE = "ghcr.io/acme/robot@sha256:" + "a" * 64


def _v2_doc(**overrides) -> dict:
    doc = {
        "apiVersion": "cv-infra/v1",
        "scenario": {
            "goal": {"x": -6.0, "y": 3.2, "yaw": 1.5708},
            "initial_pose": {"x": {"param": "start_x"}, "y": {"param": "start_y"}, "yaw": 1.5708},
            "seed": 42,
            "timeout_s": 180,
        },
        "embodiment": "embodiment.yaml",
        "sut": {"image_ref": IMAGE},
        "acceptance_criteria": [{"oracle": "reached_goal"}],
        "space": {"model": "space.pict", "budget": {"wallclock_s": 3600, "repeats": 3}},
    }
    doc.update(overrides)
    return doc


@pytest.fixture
def consumer_repo(tmp_path: Path) -> Path:
    """A consumer's directory: its request, its profile, its input space."""
    (tmp_path / "embodiment.yaml").write_text(
        GO2_FIXTURE.read_text(encoding="utf-8"), encoding="utf-8"
    )
    (tmp_path / "space.pict").write_text(SPACE, encoding="utf-8")
    (tmp_path / "request.yaml").write_text(yaml.safe_dump(_v2_doc()), encoding="utf-8")
    return tmp_path


# --- §1 exactly one form -----------------------------------------------------------------


def test_a_v2_request_names_no_platform_scene(consumer_repo: Path) -> None:
    """The headline: nothing in this document refers to a platform registry."""
    admitted = load_request(consumer_repo / "request.yaml")
    assert admitted.request.scenario.scene is None
    assert admitted.request.scenario.robot is None
    assert admitted.embodiment is not None
    assert admitted.embodiment.robot.usd == "/Isaac/IsaacLab/Robots/Unitree/Go2/go2.usd"


@pytest.mark.parametrize(
    ("mutate", "expected"),
    [
        (lambda d: d.pop("embodiment"), "must say what world and robot it runs"),
        (
            lambda d: d["scenario"].update({"scene": "s", "robot": "r"}),
            "not both",
        ),
    ],
)
def test_neither_form_and_both_forms_are_rejected(tmp_path: Path, mutate, expected: str) -> None:
    doc = _v2_doc()
    mutate(doc)
    (tmp_path / "r.yaml").write_text(yaml.safe_dump(doc), encoding="utf-8")
    with pytest.raises(ContractError, match=expected):
        load_request(tmp_path / "r.yaml")


# --- §2 ride-alongs stay inside the request's directory ------------------------------------


def test_a_profile_outside_the_request_directory_is_rejected(consumer_repo: Path) -> None:
    doc = _v2_doc(embodiment="../elsewhere/embodiment.yaml")
    (consumer_repo / "request.yaml").write_text(yaml.safe_dump(doc), encoding="utf-8")
    with pytest.raises(ContractError, match="INSIDE the request directory"):
        load_request(consumer_repo / "request.yaml")


def test_a_missing_input_space_model_is_rejected_before_any_gpu(consumer_repo: Path) -> None:
    (consumer_repo / "space.pict").unlink()
    with pytest.raises(ContractError, match="existing, readable file"):
        load_request(consumer_repo / "request.yaml")


def test_an_unparseable_input_space_is_rejected_at_admit_with_a_line(
    consumer_repo: Path,
) -> None:
    """PICT gives no line number; the request surface owes one (NFR-INTAKE-002)."""
    (consumer_repo / "space.pict").write_text(
        "a: 1, 2\n\nIF [a] = 1 THEN [a] = 1;\nb: 3, 4\n", encoding="utf-8"
    )
    with pytest.raises(ContractError) as exc:
        load_request(consumer_repo / "request.yaml")
    assert exc.value.source_line == 4


def test_sut_artifacts_are_resolved_and_digest_verified(consumer_repo: Path) -> None:
    blob = b"not really a policy"
    (consumer_repo / "policy.pt").write_bytes(blob)
    digest = hashlib.sha256(blob).hexdigest()
    doc = _v2_doc()
    doc["sut"]["artifacts"] = [{"file": "policy.pt", "sha256": digest}]
    (consumer_repo / "request.yaml").write_text(yaml.safe_dump(doc), encoding="utf-8")
    admitted = load_request(consumer_repo / "request.yaml")
    assert admitted.artifact_paths["policy.pt"].endswith("policy.pt")

    doc["sut"]["artifacts"] = [{"file": "policy.pt", "sha256": "0" * 64}]
    (consumer_repo / "request.yaml").write_text(yaml.safe_dump(doc), encoding="utf-8")
    with pytest.raises(ContractError, match="hashes to"):
        load_request(consumer_repo / "request.yaml")


def test_the_embodiment_digest_splits_test_conditions_from_the_sut(
    consumer_repo: Path,
) -> None:
    """The one real identity decision: moving the CAMERA is a different test and must
    invalidate the baseline; swapping the POLICY is the same test against a different
    SUT and must not. Both halves describe one robot and live in one document, so only
    the projection separates them."""
    raw = yaml.safe_load(GO2_FIXTURE.read_text(encoding="utf-8"))
    base = embodiment_digest(EmbodimentProfile.model_validate(raw))

    swapped = json.loads(json.dumps(raw))
    swapped["robot"]["onboard"]["artifact"] = "some_other_policy.pt"
    assert embodiment_digest(EmbodimentProfile.model_validate(swapped)) == base

    moved = json.loads(json.dumps(raw))
    moved["robot"]["sensors"][0]["mount_xyz"] = [0.30, 0.0, 0.12]
    assert embodiment_digest(EmbodimentProfile.model_validate(moved)) != base


# --- §3 the wire ----------------------------------------------------------------------------


def test_the_embodiment_rides_the_job_spec_as_data(consumer_repo: Path) -> None:
    admitted = load_request(consumer_repo / "request.yaml")
    spec = build_job_spec(admitted.request, "job-1", embodiment=admitted.embodiment)
    assert spec["embodiment"]["robot"]["spawn_z"] == 0.32
    assert spec["embodiment"]["world"]["scene_usd"].endswith("warehouse_with_forklifts.usd")


def test_a_v1_documents_job_spec_does_not_move(tmp_path: Path) -> None:
    """The compatibility floor: every pre-v2 request keeps its exact wire bytes,
    which is also what keeps its request_identity_key and its baseline."""
    doc = {
        "scenario": {
            "scene": "nova_carter_warehouse",
            "robot": "nova_carter",
            "goal": {"x": -6.0, "y": 5.0, "yaw": 1.5708},
            "seed": 42,
            "timeout_s": 120,
        },
        "sut": {"image_ref": IMAGE},
        "acceptance_criteria": [{"oracle": "reached_goal"}],
    }
    request = VerificationRequest.model_validate(doc)
    spec = build_job_spec(request, "job-1")
    assert set(spec) == {"job_id", "scenario", "sut_image_ref", "interface", "acceptance_criteria"}
    assert "embodiment" not in spec and "artifact_paths" not in spec


# --- §4 cases -------------------------------------------------------------------------------


def test_the_array_becomes_one_concrete_request_per_case(consumer_repo: Path) -> None:
    admitted = load_request(consumer_repo / "request.yaml")
    array = pict.generate(SPACE, order=2)
    cases = derive.expand_cases(admitted.request, array)
    assert len(cases) == len(array)
    for case in cases:
        pose = case.scenario.initial_pose
        assert isinstance(pose.x, float) and isinstance(pose.y, float)
        assert case.scenario.derivation is not None
        assert set(case.scenario.derivation.case or {}) == {"start_x", "start_y"}


def test_every_case_gets_its_own_stable_identity(consumer_repo: Path) -> None:
    """Case-level regression rests on this: distinct cases must not collide, and the
    same case must hash the same on the next commit."""
    from cv_infra.report.regression import identity_key

    admitted = load_request(consumer_repo / "request.yaml")
    array = pict.generate(SPACE, order=2)
    first = [
        identity_key(c.model_dump(by_alias=True))
        for c in derive.expand_cases(admitted.request, array)
    ]
    second = [
        identity_key(c.model_dump(by_alias=True))
        for c in derive.expand_cases(admitted.request, array)
    ]
    assert first == second
    assert len(set(first)) == len(first)


def test_an_axis_nothing_reads_and_an_axis_nothing_declares_are_both_rejected(
    consumer_repo: Path,
) -> None:
    """G-25 in its input-space form: a declared axis that no field binds does not
    vary anything, and a bound name the model never declares cannot be filled.
    Both are the request being wrong, so both are loud."""
    admitted = load_request(consumer_repo / "request.yaml")
    with pytest.raises(ValueError, match="no field reads"):
        derive.expand_cases(admitted.request, pict.generate(SPACE + "ghost: a, b\n", order=2))
    with pytest.raises(ValueError, match="does not declare"):
        derive.expand_cases(
            admitted.request, pict.generate("start_x: -6.3, -6.0\nz: 1, 2\n", order=2)
        )


# --- §5 the warm-cache key -------------------------------------------------------------------


def test_the_closure_is_every_asset_the_request_can_open() -> None:
    refs = asset_closure(GO2_PROFILE)
    assert refs == (
        "/Isaac/Environments/Simple_Warehouse/Stage/warehouse_extras.usd",
        "/Isaac/Environments/Simple_Warehouse/warehouse_with_forklifts.usd",
        "/Isaac/IsaacLab/Robots/Unitree/Go2/go2.usd",
    )


def test_the_closure_is_the_union_over_the_whole_suite() -> None:
    """Warming per case would have case 1 pull one asset cold and case 2 another;
    the array is enumerated at admit, so the union is knowable up front."""
    case_a = {"props": [{"usd": "/Isaac/People/person.usd"}]}
    case_b = {"props": [{"usd": "/Isaac/Props/chair.usd"}]}
    union = asset_closure(GO2_PROFILE, case_a, case_b)
    assert set(union) == set(asset_closure(GO2_PROFILE)) | {
        "/Isaac/People/person.usd",
        "/Isaac/Props/chair.usd",
    }
    assert list(union) == sorted(union)  # a set in a stable order, or it is not a key


def test_the_key_ignores_order_but_not_the_engine() -> None:
    refs = asset_closure(GO2_PROFILE)
    key = asset_set_key(refs, engine_version="isaac-sim:5.1.0")
    assert asset_set_key(reversed(refs), engine_version="isaac-sim:5.1.0") == key
    # A warmed tier holds GPU-derived caches a different runtime cannot reuse;
    # serving it anyway would look like a rendering bug, not a cache bug.
    assert asset_set_key(refs, engine_version="isaac-sim:6.0.0") != key


def test_a_reverification_of_the_same_document_is_a_hit(tmp_path: Path) -> None:
    from cv_infra.orchestrator.store import Store

    store = Store(tmp_path / "cv.sqlite3")
    refs = asset_closure(GO2_PROFILE)
    key = asset_set_key(refs, engine_version="isaac-sim:5.1.0")
    assert store.touch_asset_set(key, refs, "isaac-sim:5.1.0").is_warm is False
    store.mark_asset_set_warmed(key, size_bytes=4_200_000_000)
    again = store.touch_asset_set(key, refs, "isaac-sim:5.1.0")
    assert again.is_warm and again.bytes == 4_200_000_000
    assert [r.asset_set_key for r in store.asset_sets_by_least_recently_used()] == [key]


# --- §6 missions -----------------------------------------------------------------------------


def test_the_builtin_driver_is_the_default_and_the_only_shipped_one() -> None:
    assert BUILTIN_MISSIONS == ("goal_pose",)
    assert VerificationRequest.model_fields["mission"].default is None
    assert type(load_mission("goal_pose")).__name__ == "GoalPoseMission"


def test_a_consumer_driver_loads_through_the_same_seam_a_custom_oracle_does(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    (tmp_path / "patrol.py").write_text(
        "from cv_infra.runner.mission import MissionDriver\n"
        "class PatrolMission(MissionDriver):\n"
        "    def start(self, handle): handle.append('started')\n",
        encoding="utf-8",
    )
    monkeypatch.syspath_prepend(str(tmp_path))
    driver = load_mission("patrol:PatrolMission", {"laps": 3})
    assert isinstance(driver, MissionDriver)
    assert driver.params == {"laps": 3}
    seen: list[str] = []
    driver.start(seen)
    assert seen == ["started"]


def test_a_driver_that_is_not_one_is_refused(tmp_path: Path, monkeypatch) -> None:
    (tmp_path / "notadriver.py").write_text("class Nope:\n    pass\n", encoding="utf-8")
    monkeypatch.syspath_prepend(str(tmp_path))
    with pytest.raises(ContractError, match="subclass of"):
        load_mission("notadriver:Nope")
