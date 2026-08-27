"""Batch CARRIER seam tests (``supervisor.run_batch``) — duck-typed fake docker client, CPU.

p6 설계 정본 §0-10/§0-12/§0-13. The carrier is "ONE container pair, n samples of ONE
request": the sample stays the logical job (one ``JobOutcome`` each) and only the CONTAINER
is shared. Proves without docker:

* container accounting — n samples still spawn EXACTLY 2 containers + 1 network (the NEG-4
  counterpart of ``test_supervisor_min``'s 1-job pin, which stays untouched);
* the wire — M1 ``JobSpecBatch`` wrapper file (:ro), ``JOB_SPEC_BATCH``/``RESULT_OUT`` env,
  and the command OVERRIDE that reaches M2's batch entry point (the image ENTRYPOINT is
  still the single-job one);
* the per-SLOT collection invariant (REQ-EXEC-013 재독해: exactly one result.json per
  ``results/<i>/``) and the 12-row failure-mode fold (P5-13) — a completed sample's verdict
  survives a carrier that dies later;
* the carrier watchdog formula (``batch_timeout_s``) and the artifact-hostify root repair.

The docker fakes are ``test_supervisor_min``'s (one fake surface for both seams — a second
one would let the two paths drift into two different definitions of a container).
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from pathlib import Path

import pytest

from cv_infra.contract.job_batch import JOB_SPEC_BATCH_ENV, JobSpecBatch
from cv_infra.orchestrator.supervisor import (
    BATCH_RESULTS_DIRNAME,
    BATCH_RUNNER_COMMAND,
    BATCH_RUNNER_ENTRYPOINT,
    DEFAULT_BATCH_BOOT_ALLOWANCE_S,
    DEFAULT_BATCH_ITER_OVERHEAD_S,
    DEFAULT_BATCH_WALL_FACTOR,
    DEFAULT_JOB_TIMEOUT_S,
    JOB_SPEC_BATCH_MOUNT,
    JOB_TIMEOUT_MARKER,
    LABEL_JOB_ID,
    LABEL_ROS_DOMAIN_ID,
    RESULT_OUT_MOUNT,
    _collect_batch_results,
    _read_result_doc,
    allocate_ros_domain_id,
    batch_timeout_s,
    network_name_for,
    run_batch,
)
from cv_infra.runner import batch as m2_batch  # source-of-truth pins only (see §constants)
from tests.test_supervisor_min import (
    RUNNER_IMAGE,
    SUT_IMAGE,
    FakeClient,
    make_two_tier_roots,
)

BATCH_ID = "env-abc123/r0"  # the carrier key IS the request id (설계 §0-10)
MISSION_TIMEOUT_S = 120.0


def make_spec(index: int, *, job_id: str | None = None, sut: str = SUT_IMAGE, **scenario) -> dict:
    scenario = {"scene": "warehouse", "timeout_s": MISSION_TIMEOUT_S, **scenario}
    return {
        "job_id": job_id if job_id is not None else f"{BATCH_ID}:{index}",
        "sut_image_ref": sut,
        "scenario": scenario,
    }


def make_specs(n: int = 3) -> list[dict]:
    return [make_spec(i) for i in range(n)]


def result_root(tmp_path, batch_id: str = BATCH_ID) -> Path:
    """The host side of the carrier's ``RESULT_OUT`` bind (``results/<i>/`` live under it)."""
    return Path(tmp_path) / network_name_for(batch_id) / "result"


def put_slot_result(
    tmp_path,
    index: int,
    *,
    batch_id: str = BATCH_ID,
    job_id: str | None = None,
    verdict: str = "pass",
    rel: str = "result.json",
    artifacts: dict | None = None,
) -> Path:
    """Pre-create sample ``index``'s result where the runner (M2) would have written it."""
    path = result_root(tmp_path, batch_id) / BATCH_RESULTS_DIRNAME / str(index) / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    doc: dict = {
        "job_id": job_id if job_id is not None else f"{batch_id}:{index}",
        "verdict": verdict,
    }
    if artifacts is not None:
        doc["artifacts"] = artifacts
    path.write_text(json.dumps(doc), encoding="utf-8")
    return path


def run_min_batch(tmp_path, client, specs=None, **kwargs):
    return run_batch(
        make_specs() if specs is None else specs,
        tmp_path,
        RUNNER_IMAGE,
        SUT_IMAGE,
        client,
        batch_id=kwargs.pop("batch_id", BATCH_ID),
        batch_timeout_s=kwargs.pop("batch_timeout_s", 1800.0),
        poll_interval_s=0.0,
        **kwargs,
    )


# --------------------------------------------------------------------------- #
# (1) container accounting: n samples = EXACTLY 2 containers + 1 network (NEG-4)
# --------------------------------------------------------------------------- #


def test_three_samples_spawn_exactly_one_runner_and_one_sut(tmp_path):
    """★ 개조의 요지 그 자체: the fan-out is 3 samples, the container count is 2.

    ``test_supervisor_min``'s 1-job pin (1 runner + 1 SUT) is UNTOUCHED and still green —
    together the two pins say "a container pair per CARRIER", which is what NEG-4 counts.
    """
    for index in range(3):
        put_slot_result(tmp_path, index)
    client = FakeClient()
    outcomes = run_min_batch(tmp_path, client)
    assert [image for image, _ in client.run_calls] == [RUNNER_IMAGE, SUT_IMAGE]  # exactly 2
    assert len([e for e in client.events if e[0] == "network-create"]) == 1
    assert len(outcomes) == 3  # ...and still one outcome PER SAMPLE
    assert all(o.infra_error is None and o.result_path is not None for o in outcomes)


def test_carrier_start_order_is_runner_then_readiness_then_sut(tmp_path):
    """G-19 supply order is the carrier's too (the sim is still the /clock source)."""
    for index in range(3):
        put_slot_result(tmp_path, index)
    client = FakeClient()

    def probe(container):
        client.events.append(("probe", container.status))
        return container.status == "running"

    run_min_batch(tmp_path, client, readiness_probe=probe)
    net_name = network_name_for(BATCH_ID)
    assert [e for e in client.events if e[0] in ("network-create", "run", "probe")] == [
        ("network-create", net_name),
        ("run", RUNNER_IMAGE),
        ("probe", "running"),
        ("run", SUT_IMAGE),
    ]


def test_carrier_shares_one_network_one_domain_and_labels_the_request(tmp_path):
    """One carrier = one bridge + one ROS_DOMAIN_ID (LOCKED §7.5), keyed on the REQUEST."""
    for index in range(3):
        put_slot_result(tmp_path, index)
    client = FakeClient()
    run_min_batch(tmp_path, client)
    (_, runner_kwargs), (_, sut_kwargs) = client.run_calls
    net_name = network_name_for(BATCH_ID)
    assert runner_kwargs["network"] == sut_kwargs["network"] == net_name
    domain = runner_kwargs["environment"]["ROS_DOMAIN_ID"]
    assert sut_kwargs["environment"]["ROS_DOMAIN_ID"] == domain
    assert int(domain) == allocate_ros_domain_id(BATCH_ID)  # carrier key = request id
    assert runner_kwargs["labels"] == {LABEL_JOB_ID: BATCH_ID, LABEL_ROS_DOMAIN_ID: domain}
    assert client.network_labels == runner_kwargs["labels"]  # R14 sweep finds the carrier


def test_explicit_domain_id_overrides_the_pure_hash_fallback(tmp_path):
    """The admission-allocated id rides in (p4c6 §7-1), same as the single-job seam."""
    for index in range(3):
        put_slot_result(tmp_path, index)
    client = FakeClient()
    run_min_batch(tmp_path, client, ros_domain_id=42)
    (_, runner_kwargs), (_, sut_kwargs) = client.run_calls
    assert runner_kwargs["environment"]["ROS_DOMAIN_ID"] == "42"
    assert sut_kwargs["environment"]["ROS_DOMAIN_ID"] == "42"


# --------------------------------------------------------------------------- #
# (2) the wire: wrapper document, env, command override
# --------------------------------------------------------------------------- #


def test_wrapper_document_is_the_m1_shape_mounted_read_only(tmp_path):
    specs = make_specs(3)
    for index in range(3):
        put_slot_result(tmp_path, index)
    client = FakeClient()
    run_min_batch(tmp_path, client, specs=specs)
    _, runner_kwargs = client.run_calls[0]
    batch_path = Path(tmp_path) / network_name_for(BATCH_ID) / "job_spec_batch.json"
    assert runner_kwargs["volumes"][str(batch_path)] == {"bind": JOB_SPEC_BATCH_MOUNT, "mode": "ro"}
    assert runner_kwargs["environment"][JOB_SPEC_BATCH_ENV] == JOB_SPEC_BATCH_MOUNT
    # RESULT_OUT is the same mount as the single-job seam, but for a carrier it is the ROOT.
    assert runner_kwargs["environment"]["RESULT_OUT"] == RESULT_OUT_MOUNT
    assert runner_kwargs["volumes"][str(result_root(tmp_path))] == {
        "bind": RESULT_OUT_MOUNT,
        "mode": "rw",
    }
    assert result_root(tmp_path).is_dir()  # G-15: the supervisor pre-creates the bind source
    # The file IS the M1 document (specs verbatim, in array order, + carrier identity).
    written = json.loads(batch_path.read_text(encoding="utf-8"))
    assert written == JobSpecBatch(specs=specs, request_id=BATCH_ID).model_dump(mode="json")
    assert [s["job_id"] for s in written["specs"]] == [f"{BATCH_ID}:{i}" for i in range(3)]
    # The single-job seam key is ABSENT — an image that read JOB_SPEC would run the wrong thing.
    assert "JOB_SPEC" not in runner_kwargs["environment"]


def test_runner_is_started_through_the_command_override(tmp_path):
    """설계 §0-7: the image ENTRYPOINT is the SINGLE-job entry point (M5's Dockerfile is
    unchanged by p6), so the carrier is reached by overriding it. A pre-p6 image then fails
    loudly with "No module named cv_infra.runner.batch" instead of half-running the request."""
    for index in range(3):
        put_slot_result(tmp_path, index)
    client = FakeClient()
    run_min_batch(tmp_path, client)
    (_, runner_kwargs), (_, sut_kwargs) = client.run_calls
    assert runner_kwargs["entrypoint"] == BATCH_RUNNER_ENTRYPOINT
    assert runner_kwargs["command"] == list(BATCH_RUNNER_COMMAND)
    # DoD-P2-03 불변: the SUT is still an UNMODIFIED blackbox — no override of any kind.
    assert "entrypoint" not in sut_kwargs and "command" not in sut_kwargs
    assert set(sut_kwargs["environment"]) == {"ROS_DOMAIN_ID"}


def test_runner_only_seam_env_rides_the_carrier(tmp_path):
    """Operator env passes through; the runner-only keys stay runner-only (blackbox no-leak)."""
    for index in range(3):
        put_slot_result(tmp_path, index)
    client = FakeClient()
    specs = [
        dict(s, interface={"adapter_config": {"ros_distro": "jazzy", "rmw": "rmw_x"}})
        for s in make_specs(3)
    ]
    run_min_batch(
        tmp_path,
        client,
        specs=specs,
        runner_env={"ACCEPT_EULA": "operator-token"},
        request_identity_key="sha256:abc",
    )
    (_, runner_kwargs), (_, sut_kwargs) = client.run_calls
    env = runner_kwargs["environment"]
    assert env["ACCEPT_EULA"] == "operator-token"  # LOCKED §7.8 — verbatim, never a literal here
    assert env["CV_REQUEST_IDENTITY_KEY"] == "sha256:abc"
    assert (env["ROS_DISTRO"], env["RMW_IMPLEMENTATION"]) == ("jazzy", "rmw_x")
    assert "ACCEPT_EULA" not in sut_kwargs["environment"]
    assert "CV_REQUEST_IDENTITY_KEY" not in sut_kwargs["environment"]


# --------------------------------------------------------------------------- #
# (3) source-of-truth pins (G-25): the copied literals must equal their sources
# --------------------------------------------------------------------------- #


def test_carrier_literals_match_their_sources_of_truth():
    """The control plane does NOT import the data plane's module tree, so these three
    literals are COPIES — and a copy without a mechanical guard silently drifts (G-25).

    양성 대조 for this guard = change any constant in supervisor.py and this test goes red.
    """
    assert BATCH_RESULTS_DIRNAME == m2_batch.BATCH_RESULTS_DIRNAME
    assert list(BATCH_RUNNER_COMMAND) == ["-m", m2_batch.BATCH_MODULE]
    # ...and the ENTRYPOINT we override is really the image's (M5 owns the Dockerfile).
    dockerfile = Path(__file__).resolve().parents[1] / "docker" / "runner" / "Dockerfile"
    entrypoints = re.findall(r"^ENTRYPOINT (\[.*\])$", dockerfile.read_text(encoding="utf-8"), re.M)
    assert len(entrypoints) == 1, entrypoints
    declared = json.loads(entrypoints[0])
    assert declared[0] == BATCH_RUNNER_ENTRYPOINT
    assert declared[1:] == ["-m", "cv_infra.runner.main"]  # ...still the SINGLE-job entry point


# --------------------------------------------------------------------------- #
# (4) pre-resource guards: loud BEFORE any docker resource exists
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    ("case", "specs", "batch_id"),
    [
        ("fewer-than-two", [make_spec(0)], BATCH_ID),
        ("empty", [], BATCH_ID),
        ("missing-job-id", [{"sut_image_ref": SUT_IMAGE}, make_spec(1)], BATCH_ID),
        ("duplicate-job-id", [make_spec(0), make_spec(1, job_id=f"{BATCH_ID}:0")], BATCH_ID),
        ("sut-image-disagreement", [make_spec(0), make_spec(1, sut="other:tag")], BATCH_ID),
        ("empty-batch-id", make_specs(2), ""),
    ],
)
def test_pre_resource_guards_raise_before_any_docker_resource(tmp_path, case, specs, batch_id):
    client = FakeClient()
    with pytest.raises(ValueError):
        run_min_batch(tmp_path, client, specs=specs, batch_id=batch_id)
    assert client.events == [], case  # no network, no container, nothing to clean up


# --------------------------------------------------------------------------- #
# (5) teardown + scratch: every path, including exceptions (REQ-EXEC-015 결)
# --------------------------------------------------------------------------- #


def test_carrier_tears_down_both_containers_and_the_network(tmp_path):
    for index in range(3):
        put_slot_result(tmp_path, index)
    client = FakeClient()
    run_min_batch(tmp_path, client)
    assert (client.runner.stop_calls, client.runner.remove_calls) == (1, 1)
    assert (client.sut.stop_calls, client.sut.remove_calls) == (1, 1)
    assert client.network.remove_calls == 1


def test_carrier_tears_down_after_a_docker_exception_and_still_returns_n_outcomes(tmp_path):
    client = FakeClient(raise_on_sut_run=RuntimeError("docker refused the SUT"))
    outcomes = run_min_batch(tmp_path, client)
    assert len(outcomes) == 3  # never raises, never returns short
    assert client.runner.remove_calls == 1
    assert client.network.remove_calls == 1


def test_carrier_discards_its_per_job_scratch(tmp_path, monkeypatch):
    """D-B stateless: the carrier's seeded cache tree dies with the carrier, and its slug is
    the CARRIER's (one tree per container pair, not per sample)."""
    monkeypatch.delenv("CV_ISAAC_CACHE_ROOT", raising=False)
    monkeypatch.delenv("CV_ISAAC_CACHE_SCRATCH_ROOT", raising=False)
    for index in range(3):
        put_slot_result(tmp_path, index)
    base, scratch_root = make_two_tier_roots(tmp_path)
    client = FakeClient()
    seen: dict = {}

    def probe(container):
        seen["exists"] = (scratch_root / network_name_for(BATCH_ID)).is_dir()
        return container.status == "running"

    run_min_batch(
        tmp_path, client, cache_root=base, cache_scratch_root=scratch_root, readiness_probe=probe
    )
    assert seen["exists"] is True  # seeded for the carrier's life...
    assert not (scratch_root.resolve() / network_name_for(BATCH_ID)).exists()  # ...and discarded
    assert len(list(scratch_root.iterdir())) == 0  # exactly ONE tree existed, and it is gone


# --------------------------------------------------------------------------- #
# (6) per-SLOT collection (REQ-EXEC-013 재독해)
# --------------------------------------------------------------------------- #


def test_collect_batch_results_requires_exactly_one_result_per_slot(tmp_path):
    root = result_root(tmp_path)
    put_slot_result(tmp_path, 0)  # 1 = the invariant
    put_slot_result(tmp_path, 2)  # slot 1 missing entirely
    put_slot_result(tmp_path, 2, rel="extra/result.json")  # ...and slot 2 has TWO
    slots = _collect_batch_results(root, 3)
    assert [(s.index, s.found) for s in slots] == [(0, 1), (1, 0), (2, 2)]
    assert slots[0].path is not None and slots[0].error is None
    assert slots[1].path is None and "no result.json" in slots[1].error
    assert "REQ-EXEC-013" in slots[1].error and "슬롯당 정확히 1개" in slots[1].error
    assert slots[2].path is None and "found 2" in slots[2].error
    assert "REQ-EXEC-013" in slots[2].error
    for slot in slots:  # every message names its SLOT (traceable to one sample, not "the batch")
        if slot.error is not None:
            assert slot.error.startswith(f"slot {slot.index}:")


def test_a_slot_whose_result_names_another_sample_is_refused_not_re_mapped(tmp_path):
    """침묵 재매핑 금지: the array position IS the sample index (specs[i] <-> results/<i>).
    A result.json declaring someone else's job_id fails THAT slot — nothing downstream
    could detect a silent swap."""
    put_slot_result(tmp_path, 0)
    put_slot_result(tmp_path, 1, job_id=f"{BATCH_ID}:7")  # wrong sample's id in slot 1
    put_slot_result(tmp_path, 2)
    client = FakeClient()
    outcomes = run_min_batch(tmp_path, client)
    assert outcomes[0].result_path is not None and outcomes[2].result_path is not None
    assert outcomes[1].result_path is None
    assert "declares job_id" in outcomes[1].infra_error
    assert outcomes[1].job_id == f"{BATCH_ID}:1"  # the OUTCOME still names the real sample


# --------------------------------------------------------------------------- #
# (7) THE 12-ROW FAILURE-MODE TABLE (P5-13) — rows 1..9 (carrier-level).
#     Rows 10~12 (outer watchdog / seam crash / restart reconcile) live one layer
#     up, in tests/test_orchestrator_batch.py.
# --------------------------------------------------------------------------- #


@dataclass
class _Row:
    """One row of the failure-mode table: how the carrier died, what landed, what M3 folds."""

    client: dict  # FakeClient kwargs
    seed: tuple[int, ...] = ()  # slots that have their result.json
    extra: tuple[tuple[int, str], ...] = ()  # (slot, relative path) — extra result.json files
    kwargs: dict = field(default_factory=dict)  # run_batch overrides
    exit_code: int | None = None  # expected runner_exit_code on EVERY row
    reasons: tuple[str | None, ...] = ()  # per-slot substring (None = collected, no error)


_RUNNING = ("running",)
_EXITED = ("exited",)

_TABLE: dict[str, _Row] = {
    # 1. pre-boot spec rejection: the carrier exits 2 before anything runs. exit 2 is what
    #    makes the operational view read *contract* rather than *infra* — on EVERY row.
    "1-spec-invalid-exit-2": _Row(
        client={"runner_statuses": _EXITED, "runner_exit_code": 2},
        exit_code=2,
        reasons=("never produced a result (carrier exited 2)",) * 3,
    ),
    # 2a. platform/boot failure: exit 3.
    "2a-boot-failure-exit-3": _Row(
        client={"runner_statuses": _EXITED, "runner_exit_code": 3},
        exit_code=3,
        reasons=("never produced a result (carrier exited 3)",) * 3,
    ),
    # 2b. the carrier was signal-killed (137 OOM-kill) — the code still rides every row.
    "2b-hard-kill-137": _Row(
        client={"runner_statuses": _EXITED, "runner_exit_code": 137},
        exit_code=137,
        reasons=("never produced a result (carrier exited 137)",) * 3,
    ),
    # 3. readiness gate never opened: no SUT was started, no sample ran, exit unknowable.
    "3-readiness-timeout": _Row(
        client={"runner_statuses": ("created",)},
        kwargs={"readiness_timeout_s": 0.0},
        exit_code=None,
        reasons=("readiness gate timed out",) * 3,
    ),
    # 4. death mid-batch (the p6c3 W2 실측 3분기): finished samples KEEP their verdicts,
    #    the sample in flight has M2's degraded result, the rest never ran.
    "4-death-mid-batch-split": _Row(
        client={"runner_statuses": _EXITED, "runner_exit_code": 3},
        seed=(0, 1),
        exit_code=3,
        reasons=(None, None, "never produced a result (carrier exited 3)"),
    ),
    # 5. inner watchdog fired: the marker must reach the UNPRODUCED slots (-> TIMEOUT),
    #    while slot 0's completed verdict is NOT retracted.
    "5-inner-timeout": _Row(
        client={"runner_statuses": _RUNNING},
        seed=(0,),
        kwargs={"batch_timeout_s": 0.0},
        exit_code=None,
        reasons=(None, f"{JOB_TIMEOUT_MARKER} vehicle timeout", f"{JOB_TIMEOUT_MARKER} vehicle"),
    ),
    # 6. the SUT died more times than the restart contract allows (G-19 bringup).
    "6-sut-restarts-exhausted": _Row(
        client={"runner_statuses": _RUNNING, "sut_statuses": ("exited",)},
        seed=(0,),
        kwargs={"sut_restart_limit": 0},
        exit_code=None,
        reasons=(None, "restart limit (0) exhausted", "restart limit (0) exhausted"),
    ),
    # 7. a docker/OS fault at the infra boundary — the results that DID land are salvaged.
    "7-docker-exception-salvage": _Row(
        client={"raise_on_sut_run": RuntimeError("docker refused the SUT")},
        seed=(0,),
        exit_code=None,
        reasons=(None, "RuntimeError: docker refused the SUT", "RuntimeError: docker refused"),
    ),
    # 8. one DAMAGED slot in an otherwise healthy carrier (2+ results): only that sample fails,
    #    and it says the collection violation — not "never ran", which would be a lie.
    "8-damaged-slot-alone": _Row(
        client={},
        seed=(0, 1, 2),
        extra=((1, "extra/result.json"),),
        exit_code=0,
        reasons=(None, "found 2", None),
    ),
    # 9. both collection outcomes in ONE otherwise-clean carrier: a slot with 0 results
    #    (charged the carrier's context) next to a slot with 2+ (charged the violation
    #    itself). The collector's own 0/2+ prose is pinned in section (6).
    "9-zero-and-two-plus-slots": _Row(
        client={},
        seed=(0, 2),
        extra=((2, "extra/result.json"),),
        exit_code=0,
        reasons=(None, "slot 1: iteration never produced a result (carrier exited 0)", "found 2"),
    ),
}


@pytest.mark.parametrize("row_id", list(_TABLE))
def test_failure_mode_table_folds_each_slot_separately(tmp_path, row_id):
    row = _TABLE[row_id]
    for index in row.seed:
        put_slot_result(tmp_path, index)
    for index, rel in row.extra:
        put_slot_result(tmp_path, index, rel=rel)
    outcomes = run_min_batch(tmp_path, FakeClient(**row.client), **row.kwargs)

    assert len(outcomes) == 3, "a carrier ALWAYS answers for every sample it carried"
    assert [o.job_id for o in outcomes] == [f"{BATCH_ID}:{i}" for i in range(3)]
    assert [o.runner_exit_code for o in outcomes] == [row.exit_code] * 3
    for index, (outcome, expected) in enumerate(zip(outcomes, row.reasons, strict=True)):
        if expected is None:
            assert outcome.infra_error is None, f"slot {index} should have been collected"
            assert outcome.result_path is not None
            assert outcome.result_path.parent.name == str(index)  # results/<i> — the wire
        else:
            assert outcome.result_path is None, f"slot {index} should NOT carry a result"
            assert expected in outcome.infra_error, outcome.infra_error


def test_timeout_marker_rides_only_the_unproduced_slots(tmp_path):
    """비공허 대조 for row 5: the marker is what classifies TIMEOUT downstream, so it must
    NOT be smeared over the sample that actually finished."""
    put_slot_result(tmp_path, 0)
    outcomes = run_min_batch(tmp_path, FakeClient(runner_statuses=_RUNNING), batch_timeout_s=0.0)
    assert outcomes[0].infra_error is None
    assert all(o.infra_error.startswith(JOB_TIMEOUT_MARKER) for o in outcomes[1:])
    assert "before slot 1 produced a result" in outcomes[1].infra_error
    assert "before slot 2 produced a result" in outcomes[2].infra_error


# --------------------------------------------------------------------------- #
# (8) the carrier watchdog formula (설계 §0-12)
# --------------------------------------------------------------------------- #


def test_batch_timeout_is_boot_plus_per_sample_wall_and_overhead():
    specs = make_specs(6)
    expected = DEFAULT_BATCH_BOOT_ALLOWANCE_S + 6 * (
        MISSION_TIMEOUT_S * DEFAULT_BATCH_WALL_FACTOR + DEFAULT_BATCH_ITER_OVERHEAD_S
    )
    assert expected > DEFAULT_JOB_TIMEOUT_S  # the floor is not what is being measured here
    assert batch_timeout_s(specs) == expected
    # ...and it GROWS with n — the whole reason a fixed outer cap had to be scaled.
    assert batch_timeout_s(make_specs(7)) > batch_timeout_s(specs)


def test_batch_timeout_never_dips_below_the_single_job_watchdog():
    """A 2-sample carrier must not be judged more harshly than one job would be."""
    assert batch_timeout_s(make_specs(2)) == DEFAULT_JOB_TIMEOUT_S


def test_batch_timeout_coefficients_are_injectable():
    specs = [make_spec(0, timeout_s=10), make_spec(1, timeout_s=20)]
    assert batch_timeout_s(
        specs, boot_allowance_s=2000.0, wall_factor=3.0, iter_overhead_s=5.0
    ) == 2000.0 + (10 * 3 + 5) + (20 * 3 + 5)


def test_batch_timeout_refuses_to_invent_a_mission_budget():
    """CLAUDE §2-4: a spec with no ``scenario.timeout_s`` gets a LOUD error, never a guess."""
    with pytest.raises(ValueError, match="scenario.timeout_s"):
        batch_timeout_s([make_spec(0), {"job_id": "x", "scenario": {"scene": "s"}}])


# --------------------------------------------------------------------------- #
# (9) 설계 §0-13: the artifact hostify root repair (regression)
# --------------------------------------------------------------------------- #


_CONTAINER_MCAP = f"{RESULT_OUT_MOUNT}/{BATCH_RESULTS_DIRNAME}/1/bag/bag_1.mcap"
_CONTAINER_MP4 = f"{RESULT_OUT_MOUNT}/{BATCH_RESULTS_DIRNAME}/1/recording.mp4"


def test_batch_artifact_paths_hostify_against_the_result_out_root(tmp_path):
    """The BUG (설계 §0-13): the runner declares ``/cv/out/results/1/x.mcap`` and the doc lives
    at ``<root>/results/1/result.json``, so mapping against the file's PARENT nested the
    relative part twice (``…/results/1/results/1/…``). The root is the host side of the
    RESULT_OUT bind — one level up from the slot."""
    path = put_slot_result(tmp_path, 1, artifacts={"mcap": _CONTAINER_MCAP, "mp4": _CONTAINER_MP4})
    root = result_root(tmp_path)
    doc = _read_result_doc(path, root)
    assert doc["artifacts"]["mcap"] == str(
        root / BATCH_RESULTS_DIRNAME / "1" / "bag" / "bag_1.mcap"
    )
    assert doc["artifacts"]["mp4"] == str(root / BATCH_RESULTS_DIRNAME / "1" / "recording.mp4")
    assert f"{BATCH_RESULTS_DIRNAME}/1/{BATCH_RESULTS_DIRNAME}/1" not in doc["artifacts"]["mcap"]


def test_single_job_hostify_is_byte_identical_without_the_root(tmp_path):
    """회귀 0: the default (None) keeps the frozen "parent of result.json" rule, which is
    exactly right for the single-job seam (result.json sits DIRECTLY under RESULT_OUT)."""
    result_dir = tmp_path / "job" / "result"
    result_dir.mkdir(parents=True)
    path = result_dir / "result.json"
    path.write_text(
        json.dumps(
            {
                "job_id": "j",
                "verdict": "pass",
                "artifacts": {"mcap": f"{RESULT_OUT_MOUNT}/bag/bag_0.mcap", "mp4": None},
            }
        ),
        encoding="utf-8",
    )
    doc = _read_result_doc(path)
    assert doc["artifacts"]["mcap"] == str(result_dir / "bag" / "bag_0.mcap")
    assert doc["artifacts"]["mp4"] is None  # 정직한 부재
    assert doc == _read_result_doc(path, None)  # explicit None == default
