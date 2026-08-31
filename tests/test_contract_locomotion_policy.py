"""``sut.locomotion_policy`` — the 2nd SUT artifact (M1, CEO decision D2 2026-08-31).

The field exists so "SUT" can be a SET of user artifacts (app image + locomotion
policy) instead of one container, and it lives under ``sut`` for one measurable
reason, pinned below: the M4 identity projection excludes the whole ``sut``
block, so swapping the policy is "the SAME request against a DIFFERENT SUT" and
the existing regression machinery (SR-20) compares the two runs unmodified. The
same field under ``scenario`` would move the identity key and make every policy
swap a brand-new, baseline-less request.

Validation happens at ADMIT (loader stage 5), mirroring the custom-oracle
ride-along it travels with: resolved against the scenario file's directory, may
not leave it, must exist, must hash to the declared digest — each violation a
friendly 8-key ``ContractError`` and exit 2 (D2: the platform holds no policy,
so it never fills one in). An undeclared policy does nothing at all (carter).

C2c added the execution-plane half: the single JOB_SPEC producer emits the
resolved path + declared digest as two flat keys, and both submission planes
(``cv-infra run`` / ``POST /envelopes``) are driven here to prove they emit the
same wire for the same document.

Stdlib + pytest (+ the contract's own pyyaml/pydantic, and fastapi's TestClient
for the REST submit plane); no Isaac, no docker — the supervisor seam is stubbed
and the M3 runner is a duck-typed recorder.
"""

from __future__ import annotations

import copy
import hashlib
import io
import json
import time
from pathlib import Path

import pytest
import yaml
from fastapi.testclient import TestClient

from cv_infra.cli.main import EXIT_CONTRACT, main
from cv_infra.contract.errors import ANNOTATION_KEYS, ContractError
from cv_infra.contract.job_batch import JobSpecBatch
from cv_infra.contract.job_spec import build_job_spec
from cv_infra.contract.loader import load_request
from cv_infra.contract.schema import EXAMPLE_POLICY_SHA256, VerificationRequest
from cv_infra.orchestrator.api import create_app
from cv_infra.orchestrator.models import Job, JobResult, JobState, Verdict
from cv_infra.orchestrator.store import Store
from cv_infra.report.regression import identity_key
from cv_infra.runner import batch as runner_batch
from cv_infra.runner import go2_wiring
from cv_infra.runner import main as runner_main
from tests.test_cli_run import RecordingSupervisor
from tests.test_cli_run import _install_supervisor as install_supervisor

CARTER_FIXTURE = Path(__file__).parent / "fixtures" / "nova_carter_warehouse_goal.yaml"

#: Stand-in policy bytes. The contract hashes BYTES — it never opens the file as
#: a model (loading TorchScript is the runner's business, C2b), so a real .pt
#: would only make this suite slower and less portable.
POLICY_BYTES = b"not a TorchScript file - the contract only ever hashes these bytes\n"
POLICY_SHA = hashlib.sha256(POLICY_BYTES).hexdigest()
#: A well-formed digest that is NOT the file's (64 hex, letters included: an
#: all-DIGIT scalar is a YAML integer, which would be a stage-3 type reject —
#: a different violation than the digest MISMATCH these cases mean to exercise).
WRONG_SHA = "dead" * 16

GO2_DOC = """\
apiVersion: cv-infra/v1
scenario:
  scene: go2_warehouse
  robot: go2
  goal: {{x: 2.0, y: 0.0, yaw: 0.0}}
  seed: 11
  timeout_s: 60
sut:
  image_ref: ghcr.io/<org>/<image>@sha256:<64-hex-digest>
{policy}acceptance_criteria:
  - oracle: reached_goal
    params:
      position_tolerance_m: 0.5
"""

POLICY_BLOCK = "  locomotion_policy:\n    file: {file}\n    sha256: {sha256}\n"


def _doc(*, file: str = "policy.pt", sha256: str = POLICY_SHA) -> str:
    return GO2_DOC.format(policy=POLICY_BLOCK.format(file=file, sha256=sha256))


def _doc_with_block(body: str) -> str:
    """The same document with a hand-written ``locomotion_policy`` body."""
    return GO2_DOC.format(policy=f"  locomotion_policy:\n    {body}\n")


def _case(tmp_path: Path, *, file: str = "policy.pt", sha256: str = POLICY_SHA) -> Path:
    """A go2 scenario file + its ride-along policy, both under ``tmp_path``."""
    (tmp_path / "policy.pt").write_bytes(POLICY_BYTES)
    scenario = tmp_path / "go2_walk.yaml"
    scenario.write_text(_doc(file=file, sha256=sha256), encoding="utf-8")
    return scenario


# --------------------------------------------------------------------------- #
# admit — the declared artifact is resolved ONCE, at the only place that knows
# the anchor directory (REQ-INTAKE-009: what the execution plane is handed)
# --------------------------------------------------------------------------- #
def test_declared_policy_is_validated_and_its_absolute_path_rides_the_admission(tmp_path):
    admitted = load_request(_case(tmp_path))
    assert admitted.admitted is True
    assert admitted.locomotion_policy_path == str(tmp_path / "policy.pt")
    assert Path(admitted.locomotion_policy_path).is_absolute()
    # The declaration itself survives on the model (C2b puts both on the wire as
    # locomotion_policy_path / locomotion_policy_sha256).
    assert admitted.request.sut.locomotion_policy.file == "policy.pt"
    assert admitted.request.sut.locomotion_policy.sha256 == POLICY_SHA


def test_policy_in_a_subdirectory_of_the_scenario_dir_admits(tmp_path):
    """Positive control for the escape rule below: the rule is about LEAVING the
    ride-along directory, not about nesting inside it."""
    (tmp_path / "assets").mkdir()
    (tmp_path / "assets" / "policy.pt").write_bytes(POLICY_BYTES)
    scenario = tmp_path / "go2_walk.yaml"
    scenario.write_text(_doc(file="assets/policy.pt"), encoding="utf-8")
    admitted = load_request(scenario)
    assert admitted.locomotion_policy_path == str(tmp_path / "assets" / "policy.pt")


def test_stream_submission_with_the_scenario_dir_anchor_admits(tmp_path):
    """The envelope / REST plane hands the loader a STREAM plus the scenario
    directory (``plugin_dir``) — the policy resolves against that same anchor,
    exactly as a scenario-adjacent custom oracle does."""
    (tmp_path / "policy.pt").write_bytes(POLICY_BYTES)
    admitted = load_request(
        io.StringIO(_doc()), source_path="requests[0]", plugin_dir=str(tmp_path)
    )
    assert admitted.locomotion_policy_path == str(tmp_path / "policy.pt")


def test_a_request_without_a_policy_is_untouched():
    """The carter path: no declaration -> nothing resolved, nothing checked."""
    admitted = load_request(CARTER_FIXTURE)
    assert admitted.request.sut.locomotion_policy is None
    assert admitted.locomotion_policy_path is None


# --------------------------------------------------------------------------- #
# admit — the rejections (friendly, exit-2-eligible, nothing propagated)
# --------------------------------------------------------------------------- #
def _assert_friendly(err: ContractError, *, field_path: str) -> str:
    """Every rejection is the full M1 error object, not a bare message."""
    assert err.field_path == field_path
    assert set(err.to_annotation_dict()) == set(ANNOTATION_KEYS)
    assert err.example and "sha256sum" in err.example  # fixable: the command to run
    assert err.doc_link
    assert err.source_path is not None and err.source_line is not None
    assert "Traceback" not in str(err)
    return str(err)


def test_missing_policy_file_rejects_with_the_field_path_and_a_fixable_example(tmp_path):
    scenario = _case(tmp_path)
    (tmp_path / "policy.pt").unlink()
    with pytest.raises(ContractError) as excinfo:
        load_request(scenario)
    message = _assert_friendly(excinfo.value, field_path="sut.locomotion_policy.file")
    assert "never supplies the policy" in message  # D2: no substituted default
    assert str(tmp_path / "policy.pt") in message  # WHERE it looked


@pytest.mark.parametrize("relative", [True, False])
def test_path_escaping_the_scenario_directory_rejects_even_though_the_file_exists(
    tmp_path, relative
):
    """Same discipline as the custom-oracle ride-along: only the scenario
    directory reaches the runner (read-only mount), so a path out of it names a
    file that does not exist over there — reject at admit, not at boot."""
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "policy.pt").write_bytes(POLICY_BYTES)
    scenario_dir = tmp_path / "scn"
    scenario_dir.mkdir()
    scenario = scenario_dir / "go2_walk.yaml"
    declared = "../outside/policy.pt" if relative else str(outside / "policy.pt")
    scenario.write_text(_doc(file=declared), encoding="utf-8")
    assert (outside / "policy.pt").is_file()  # the reject is about ESCAPE, not absence
    with pytest.raises(ContractError) as excinfo:
        load_request(scenario)
    message = _assert_friendly(excinfo.value, field_path="sut.locomotion_policy.file")
    assert "INSIDE the scenario directory" in message


def test_a_symlink_out_of_the_scenario_directory_rejects_too(tmp_path):
    """The check runs on the RESOLVED path, so a link that merely LOOKS local is
    rejected as well — and correctly so: the runner mounts the directory, not the
    link's target, where that name would dangle."""
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "policy.pt").write_bytes(POLICY_BYTES)
    scenario_dir = tmp_path / "scn"
    scenario_dir.mkdir()
    (scenario_dir / "policy.pt").symlink_to(outside / "policy.pt")
    scenario = scenario_dir / "go2_walk.yaml"
    scenario.write_text(_doc(), encoding="utf-8")
    with pytest.raises(ContractError) as excinfo:
        load_request(scenario)
    assert "INSIDE the scenario directory" in _assert_friendly(
        excinfo.value, field_path="sut.locomotion_policy.file"
    )


def test_digest_mismatch_rejects_naming_both_digests(tmp_path):
    """The pin is the point: same file name, different bytes = a different SUT,
    and the request said which one it meant."""
    declared = WRONG_SHA
    with pytest.raises(ContractError) as excinfo:
        load_request(_case(tmp_path, sha256=declared))
    message = _assert_friendly(excinfo.value, field_path="sut.locomotion_policy.sha256")
    assert POLICY_SHA in message  # what the file actually hashes to
    assert declared in message  # what the document claimed


def test_anchor_less_submission_rejects_a_declared_policy():
    """A stream with no anchor has nowhere to resolve the ride-along file — say so
    instead of guessing a directory (the CWD would make admission depend on where
    the process happened to be started)."""
    with pytest.raises(ContractError) as excinfo:
        load_request(io.StringIO(_doc()), source_path="s.yaml")
    message = _assert_friendly(excinfo.value, field_path="sut.locomotion_policy.file")
    assert "anchor" in message


def test_rejected_policy_never_produces_an_admitted_request(tmp_path, monkeypatch):
    """NFR-INTAKE-003 for this field: stage 6 is not reached."""
    scenario = _case(tmp_path)
    (tmp_path / "policy.pt").unlink()
    monkeypatch.setattr(
        "cv_infra.contract.loader.AdmittedRequest",
        lambda **kwargs: pytest.fail("stage 6 reached on a rejected policy"),
    )
    with pytest.raises(ContractError):
        load_request(scenario)


# --------------------------------------------------------------------------- #
# schema shape (stage 3) — the block is closed and a digest is a digest
# --------------------------------------------------------------------------- #
def test_malformed_digest_is_a_schema_reject_carrying_the_shape_example(tmp_path):
    with pytest.raises(ContractError) as excinfo:
        load_request(_case(tmp_path, sha256="deadbeef"))
    err = excinfo.value
    assert err.field_path == "sut.locomotion_policy.sha256"
    assert EXAMPLE_POLICY_SHA256 in err.example  # the 64-hex shape sample
    assert "Traceback" not in str(err)


@pytest.mark.parametrize(
    ("body", "expected_path"),
    [
        (
            f"file: policy.pt\n    sha256: {POLICY_SHA}\n    url: http://elsewhere/p.pt",
            "sut.locomotion_policy.url",
        ),
        (f"sha256: {POLICY_SHA}", "sut.locomotion_policy.file"),
        ("file: policy.pt", "sut.locomotion_policy.sha256"),
        (f"file: ''\n    sha256: {POLICY_SHA}", "sut.locomotion_policy.file"),
    ],
)
def test_the_policy_block_is_closed_and_both_keys_are_required(tmp_path, body, expected_path):
    """``extra="forbid"`` at this level too (nothing silently dropped, G-25), and
    half a pin is not a pin: a file with no digest pins nothing."""
    (tmp_path / "policy.pt").write_bytes(POLICY_BYTES)
    scenario = tmp_path / "go2_walk.yaml"
    scenario.write_text(_doc_with_block(body), encoding="utf-8")
    with pytest.raises(ContractError) as excinfo:
        load_request(scenario)
    assert excinfo.value.field_path == expected_path


# --------------------------------------------------------------------------- #
# identity — the reason the field is a SUT axis (D2 / SR-20, REQ-REPORT-002)
# --------------------------------------------------------------------------- #
def _key(doc: str) -> str:
    """``request_identity_key`` of a document, through the REAL M1 model dump."""
    request = VerificationRequest.model_validate(yaml.safe_load(doc))
    return identity_key(request.model_dump(mode="json", by_alias=True))


def test_swapping_the_policy_keeps_the_request_identity_key():
    """ "Same request, different SUT" — exactly what the regression machinery
    already compares (SR-20), so a policy upgrade lands as a regression candidate
    against the SAME baseline row instead of as a brand-new request."""
    base = _key(_doc())
    assert _key(_doc(sha256="a" * 64)) == base  # different policy bytes
    assert _key(_doc(file="policy_v2.pt", sha256="b" * 64)) == base  # different artifact
    assert _key(GO2_DOC.format(policy="")) == base  # not declaring one at all


def test_changing_the_mission_still_changes_the_key():
    """Positive control: the key is not blind to everything."""
    assert _key(_doc().replace("y: 0.0", "y: 4.0")) != _key(_doc())


# --------------------------------------------------------------------------- #
# execution plane — the producer (C2c: the assertion C2a left for the wiring)
# --------------------------------------------------------------------------- #
def test_the_job_spec_carries_the_resolved_policy_pin(tmp_path):
    """C2a declared the two key names and left the wire byte-identical; C2c wires
    the single producer, so this is the flipped assertion.

    What rides is the pair a consumer can act on with no re-derivation: the
    ABSOLUTE path admit resolved (valid inside the container — the scenario dir
    is ro-mounted at the same absolute path) and the DECLARED digest. The keys
    are asserted through the CONSUMER's constants, which is what ties the
    producer's two literals to the reader (the contract may not import a
    sibling, so the tie is a test, G-25), and the round trip through
    ``policy_pin`` closes it the way G-17 asks: measured, not prose.
    """
    admitted = load_request(_case(tmp_path))
    spec = build_job_spec(
        admitted.request, "req-go2:0", locomotion_policy_path=admitted.locomotion_policy_path
    )
    assert [key for key in spec if "locomotion" in key] == [
        go2_wiring.POLICY_PATH_KEY,
        go2_wiring.POLICY_SHA_KEY,
    ]
    assert spec[go2_wiring.POLICY_PATH_KEY] == str(tmp_path / "policy.pt")
    assert spec[go2_wiring.POLICY_SHA_KEY] == POLICY_SHA
    assert spec["sut_image_ref"] == admitted.request.sut.image_ref  # 1st artifact unmoved
    pin = go2_wiring.policy_pin(spec)  # the runner reads back exactly what admit resolved
    assert pin.path == admitted.locomotion_policy_path and pin.sha256 == POLICY_SHA
    # G-74 in the ADD direction: the runner re-validates the whole document with
    # ``extra="forbid"``, so a new top-level wire key is safe only because that
    # side peels it off (``runner/main.parse_request``) — measured, not assumed.
    restored, _adapter = runner_main.parse_request(spec)
    assert restored.sut.image_ref == admitted.request.sut.image_ref
    assert restored.scenario.scene == "go2_warehouse"


def test_an_undeclared_policy_leaves_the_wire_byte_identical(tmp_path):
    """The carter plane, pinned against the branch itself: even when a caller
    hands a path in, a request that declares no policy emits the same bytes it
    emitted before C2c. The DOCUMENT decides what is pinned (a path with no
    declared digest would be half a pin), so the declaration is the gate."""
    request = load_request(CARTER_FIXTURE).request
    plain = build_job_spec(request, "jid-1")
    with_a_stray_path = build_job_spec(
        request, "jid-1", locomotion_policy_path=str(tmp_path / "policy.pt")
    )
    assert json.dumps(plain, sort_keys=True) == json.dumps(with_a_stray_path, sort_keys=True)
    assert not [key for key in plain if "locomotion" in key]


def test_a_declared_policy_without_a_resolved_path_emits_nothing_and_the_runner_says_so(tmp_path):
    """The remaining arm: a caller that holds the MODEL but not the admission
    (no path) emits neither key — and that is not a silent no-op, because the
    consumer refuses the boot naming the plane that dropped it (G-26/G-74). This
    is the state every go2 request was in until C2c."""
    admitted = load_request(_case(tmp_path))
    spec = build_job_spec(admitted.request, "req-go2:0")  # path NOT forwarded
    assert not [key for key in spec if "locomotion" in key]
    with pytest.raises(go2_wiring.PolicyContractError) as excinfo:
        go2_wiring.check_firmware_slot(admitted.request, go2_wiring.policy_pin(spec))
    assert go2_wiring.POLICY_PATH_KEY in str(excinfo.value)


# --------------------------------------------------------------------------- #
# execution plane — the two SUBMISSION planes agree (the reason the producer is
# single: C2b's question §3 option A). Both real entrypoints are driven.
# --------------------------------------------------------------------------- #
class _SpecRecordingRunner:
    """M3 runner seam that records the JOB_SPEC that rode the job (rest-glue idiom)."""

    def __init__(self) -> None:
        self.specs: list[dict] = []

    def run(self, job: Job) -> JobResult:
        self.specs.append(copy.deepcopy(job.job_spec))
        return JobResult(job=job, state=JobState.COMPLETED, verdict=Verdict.PASS)


def _rest_plane_spec(tmp_path: Path, scenario: Path) -> dict:
    """The spec the REAL REST submit path puts on a job (``POST /envelopes``)."""
    runner = _SpecRecordingRunner()
    with Store(tmp_path / "cv.sqlite3") as store:
        with TestClient(create_app(store, runner, k=1)) as client:
            body = {
                "requests": [yaml.safe_load(scenario.read_text(encoding="utf-8"))],
                "oracle_plugin_dirs": [str(scenario.parent)],  # the ride-along anchor
            }
            response = client.post("/envelopes", json=body)
            assert response.status_code == 202, response.text
            envelope_id = response.json()["envelope_id"]
            deadline = time.monotonic() + 10.0
            status = client.get(f"/envelopes/{envelope_id}").json()
            while status["status"] != "completed":
                assert time.monotonic() < deadline, "envelope did not complete in time"
                time.sleep(0.02)
                status = client.get(f"/envelopes/{envelope_id}").json()
    assert len(runner.specs) == 1
    return runner.specs[0]


def _cli_plane_spec(monkeypatch, tmp_path: Path, scenario: Path) -> dict:
    """The spec the REAL ``cv-infra run`` path hands the supervisor seam."""
    stub = RecordingSupervisor()
    install_supervisor(monkeypatch, stub)
    out_dir = tmp_path / "out"
    code = main(
        [
            "run",
            str(scenario),
            "--runner-image",
            "cv-infra-runner:x",
            "--out-dir",
            str(out_dir),
            "--job-id",
            "req-go2:0",
        ]
    )
    assert code == 0, stub.calls
    return stub.calls[0]["job_spec"]


def _without_job_id(spec: dict) -> str:
    """Canonical bytes of a spec minus the one key the two planes name differently."""
    return json.dumps({k: v for k, v in spec.items() if k != "job_id"}, sort_keys=True)


def test_both_submission_planes_put_the_same_policy_pin_on_the_wire(monkeypatch, tmp_path):
    """``cv-infra run`` and ``POST /envelopes`` emit the SAME spec for the same
    go2 document — the property option A was chosen for (one producer, both call
    sites forwarding their own admission).

    Armed per G-59: the input DECLARES a policy, so the equality exercises the
    optional branch instead of agreeing about a wire neither plane filled. Both
    entrypoints are driven for real (the fan-out plane through ``TestClient``,
    the CLI through ``main``) — comparing the two thin aliases would prove
    nothing, since they are the same function object; what can drift is the
    CALL SITE forgetting to forward (G-106 ③).
    """
    scenario_dir = tmp_path / "scn"
    scenario_dir.mkdir()
    scenario = _case(scenario_dir)
    cli_spec = _cli_plane_spec(monkeypatch, tmp_path, scenario)
    rest_spec = _rest_plane_spec(tmp_path, scenario)

    assert cli_spec[go2_wiring.POLICY_PATH_KEY] == str(scenario_dir / "policy.pt")
    assert cli_spec[go2_wiring.POLICY_SHA_KEY] == POLICY_SHA
    # job_id is the one honest difference: each plane names its own job.
    assert cli_spec["job_id"] == "req-go2:0" and rest_spec["job_id"].endswith(":0")
    assert _without_job_id(cli_spec) == _without_job_id(rest_spec)


def test_neither_plane_puts_a_policy_key_on_an_undeclared_request(monkeypatch, tmp_path):
    """Regression control for the carter plane through both REAL entrypoints:
    the byte-identical wire above is not an artifact of calling the producer
    directly."""
    scenario = tmp_path / "scn" / "carter.yaml"
    scenario.parent.mkdir()
    scenario.write_text(CARTER_FIXTURE.read_text(encoding="utf-8"), encoding="utf-8")
    cli_spec = _cli_plane_spec(monkeypatch, tmp_path, scenario)
    rest_spec = _rest_plane_spec(tmp_path, scenario)
    assert not [key for key in cli_spec if "locomotion" in key]
    assert not [key for key in rest_spec if "locomotion" in key]
    assert _without_job_id(cli_spec) == _without_job_id(rest_spec)
    # ...and it is the very spec the producer builds with no path at all.
    assert build_job_spec(load_request(scenario).request, "req-go2:0") == cli_spec


# --------------------------------------------------------------------------- #
# batch uniformity (runner/batch.py ``_UNIFORM_FIELDS`` — mirror of sut.image_ref)
# --------------------------------------------------------------------------- #
def _batch_spec(index: int, policy: dict | None) -> dict:
    """One JOB_SPEC with the FLAT policy pin.

    C2b chose the wire form M1's C2a report §6 left open (that report's "the row
    only fires on a NESTED sut block" note): the runner is handed the RESOLVED
    path + digest as two envelope keys, because the flattened ``sut_image_ref``
    has no room for a second artifact and a relative ``file`` would make the
    runner re-derive an anchor admit already resolved. ``runner/batch.py``'s
    uniformity row reads that pin.
    """
    spec = {
        "job_id": f"req-go2:{index}",
        "scenario": {
            "scene": "go2_warehouse",
            "robot": "go2",
            "goal": {"x": 2.0, "y": 0.0, "yaw": 0.0},
            "seed": 11,
            "timeout_s": 60.0,
        },
        "sut_image_ref": "go2-sut:c2a",
        "interface": {"type": "ros2", "adapter_config": {}},
        "acceptance_criteria": [
            {"oracle": "reached_goal", "params": {"position_tolerance_m": 0.5}}
        ],
    }
    if policy is not None:
        spec[go2_wiring.POLICY_PATH_KEY] = policy["path"]
        spec[go2_wiring.POLICY_SHA_KEY] = policy["sha256"]
    return spec


def test_a_carrier_rejects_samples_that_disagree_on_the_policy():
    """One carrier = one SUT: a sample judged against a different policy than
    sample 0 would run sample 0's world and wear its own verdict (pre-boot, 0 GPU s)."""
    doc = {
        "specs": [
            _batch_spec(0, {"path": "/scn/policy.pt", "sha256": POLICY_SHA}),
            _batch_spec(1, {"path": "/scn/policy.pt", "sha256": "c" * 64}),
        ]
    }
    with pytest.raises(runner_batch.BadJobSpec) as excinfo:
        runner_batch.admit_specs(JobSpecBatch.model_validate(doc))
    assert "batch spec 1:" in str(excinfo.value)
    assert "sut.locomotion_policy" in str(excinfo.value)


def test_a_carrier_accepts_samples_that_agree_on_the_policy():
    """Positive control (a row that rejects everything would pass the test above)."""
    policy = {"path": "/scn/policy.pt", "sha256": POLICY_SHA}
    doc = {"specs": [_batch_spec(i, policy) for i in range(3)]}
    parsed = runner_batch.admit_specs(JobSpecBatch.model_validate(doc))
    assert [p.policy.sha256 for p in parsed] == [POLICY_SHA] * 3


# --------------------------------------------------------------------------- #
# exit code — the rejection reaches the user as 2 (exit-code contract LOCKED §7-9)
# --------------------------------------------------------------------------- #
def test_cli_run_exits_2_with_the_friendly_error_and_no_traceback(tmp_path, capsys):
    scenario = _case(tmp_path, sha256=WRONG_SHA)
    code = main(
        ["run", str(scenario), "--runner-image", "cv-infra-runner:x", "--out-dir", str(tmp_path)]
    )
    assert code == EXIT_CONTRACT
    err = capsys.readouterr().err
    assert "sut.locomotion_policy.sha256" in err and POLICY_SHA in err
    assert "sha256sum" in err  # the fixable example
    assert "Traceback" not in err
