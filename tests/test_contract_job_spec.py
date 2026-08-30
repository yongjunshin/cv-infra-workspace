"""STRUCTURAL pin: both submission planes assemble the JOB_SPEC in ONE place (p8c1).

``cv_infra/contract/job_spec.py::build_job_spec`` is the single definition of the
frozen M3->M2 wire shape. Until p8c1 the assembly was DUPLICATED — M8
``cli/main._job_spec_from_request`` and M3 ``orchestrator/api._job_spec_for``
each held their own dict literal, kept equal only by the behavioural parity
guard in ``tests/test_orchestrator_rest_glue.py``. That guard still runs (and
still pins what the shape IS); this file pins the thing a value comparison
cannot see: that there is no second assembly to drift (G-17, M1's single-
definition invariant).

The three checks are complementary on purpose (G-106 — a pin that only counts
calls is blind to the argument, and a pin that only compares values is blind to
a re-introduced copy that happens to agree today):

* (1) the REST plane's handle IS the contract function (identity);
* (2) the CLI plane's handle DELEGATES to it — with the caller's own arguments,
  returning its result unpost-processed (the CLI keeps a wrapper so the contract
  stays off its import surface: ``--help`` must not pull it);
* (3) neither plane's SOURCE contains a second dict assembling the frozen
  top-level key set.
"""

from __future__ import annotations

import ast
import copy
import io
import json
from pathlib import Path

import pytest
import yaml

from cv_infra.cli import main as cli_main
from cv_infra.contract import job_spec as job_spec_mod
from cv_infra.contract.job_spec import build_job_spec
from cv_infra.contract.loader import load_request
from cv_infra.orchestrator import api

# The frozen top-level key set of the wire (module docstring of job_spec.py).
_FROZEN_KEYS = {"job_id", "scenario", "sut_image_ref", "interface", "acceptance_criteria"}

_FIXTURE = Path(__file__).parent / "fixtures" / "nova_carter_warehouse_goal.yaml"
_CANONICAL_DOC = yaml.safe_load(_FIXTURE.read_text(encoding="utf-8"))


def _admit_doc(doc: dict):
    stream = io.StringIO(json.dumps(doc, indent=2, sort_keys=True))
    return load_request(stream, source_path="test-doc").request


@pytest.fixture()
def admitted():
    return load_request(_FIXTURE).request


# --------------------------------------------------------------------------- #
# (1) + (2) the two plane handles resolve to the one definition
# --------------------------------------------------------------------------- #
def test_the_rest_plane_handle_is_the_contract_function():
    """M3's ``_job_spec_for`` is the contract function itself, not a twin.

    Identity (``is``), not equality of outputs: outputs agreed BEFORE the
    unification too (that is what the parity guard measured), so only identity
    can tell "one definition" from "two that match today"."""
    assert api._job_spec_for is build_job_spec


def test_the_cli_plane_handle_delegates_to_the_contract_function(monkeypatch, admitted):
    """M8's ``_job_spec_from_request`` is a wrapper — pin that it forwards THIS
    request and THIS job_id and returns what the contract produced.

    The wrapper exists only for the lazy-import discipline (the contract must
    stay off ``cli/main``'s import surface), so its whole job is delegation: a
    body that re-assembled, re-ordered or post-processed anything would fail
    here. Patching the CONTRACT module's attribute works precisely because the
    wrapper imports it at call time — a wrapper that had inlined the assembly
    would silently ignore the patch and return a real spec instead of the
    sentinel."""
    seen: list[tuple] = []
    sentinel = {"job_id": "sentinel-not-a-real-spec"}

    def spy(request, job_id):
        seen.append((request, job_id))
        return sentinel

    monkeypatch.setattr(job_spec_mod, "build_job_spec", spy)
    out = cli_main._job_spec_from_request(admitted, "jid-7")

    assert out is sentinel
    assert seen == [(admitted, "jid-7")]


# --------------------------------------------------------------------------- #
# (3) no second assembly lives in either plane's source
# --------------------------------------------------------------------------- #
def _frozen_shape_dicts(path: Path) -> list[int]:
    """Line numbers of dict literals whose keys cover the frozen top-level set.

    Scans the real injection form (a dict literal carrying the wire's key set),
    not a word — a renamed helper or an inlined copy is caught either way
    (G-21)."""
    tree = ast.parse(path.read_text(encoding="utf-8"))
    hits = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Dict):
            continue
        keys = {
            k.value for k in node.keys if isinstance(k, ast.Constant) and isinstance(k.value, str)
        }
        if _FROZEN_KEYS <= keys:
            hits.append(node.lineno)
    return hits


def test_neither_plane_assembles_the_wire_itself():
    planes = {
        "cli/main.py": Path(cli_main.__file__),
        "orchestrator/api.py": Path(api.__file__),
    }
    offenders = {name: _frozen_shape_dicts(path) for name, path in planes.items()}
    assert not any(offenders.values()), f"a second JOB_SPEC assembly reappeared: {offenders}"
    # Positive control (비공허): the predicate DOES fire on the one definition.
    assert _frozen_shape_dicts(Path(job_spec_mod.__file__)), "the scan cannot see the real assembly"


# --------------------------------------------------------------------------- #
# Non-vacuous shape control — what the single definition actually emits
# --------------------------------------------------------------------------- #
def test_the_single_definition_emits_the_frozen_shape(admitted):
    """The contract fixture rides the frozen key set with ``sut.image_ref``
    flattened and no ``execution_settings`` (it declares none)."""
    spec = build_job_spec(admitted, "jid-1")
    assert set(spec) == _FROZEN_KEYS
    assert spec["job_id"] == "jid-1"
    assert spec["sut_image_ref"] == admitted.sut.image_ref
    assert "apiVersion" not in spec  # resolved at admit, no execution-plane consumer


def test_both_handles_agree_on_the_execution_settings_branch():
    """G-59 arming: the equality below must exercise the OPTIONAL branch, else it
    is true for reasons unrelated to the wiring. ``fixed_dt`` lands, ``repeats``
    and ``min_pass_ratio`` do not."""
    doc = copy.deepcopy(_CANONICAL_DOC)
    doc["execution_settings"] = {"repeats": 3, "fixed_dt": 0.02, "min_pass_ratio": 0.5}
    with_knob = _admit_doc(doc)
    plain_doc = copy.deepcopy(doc)
    del plain_doc["execution_settings"]
    plain = _admit_doc(plain_doc)

    for request in (with_knob, plain):
        assert cli_main._job_spec_from_request(request, "jid-1") == api._job_spec_for(
            request, "jid-1"
        )
    assert build_job_spec(with_knob, "jid-1")["execution_settings"] == {"fixed_dt": 0.02}
    assert "execution_settings" not in build_job_spec(plain, "jid-1")
