"""Case identity and expansion — a covering array becomes the list of runs.

The covering array says WHICH combinations to try; this module turns each row into a
run that is addressable across commits. Three derivations, each with a reason:

* **case_id** = ``sha256:`` over the axis assignment, canonicalised by SORTING the
  items. Sorting is what makes a baseline survive a model edit that only reorders or
  renames-around the axes: the id is a property of the ASSIGNMENT, not of the file's
  column order. A baseline keyed on a case id that no longer exists is an absent
  baseline, and an absent baseline SKIPS — i.e. an unstable id silences the gate
  instead of failing it.
* **argv** = ``[sim_script, "--<axis>=<value>", ...]`` in the MODEL's declaration order,
  because a human reading the container command should see the file's order back.
  ``--name=value`` (one token, not two) so a value that starts with ``-`` cannot be
  read as the next flag.
* **seed** = the first 4 bytes of ``sha256(case_id:repeat)``. Derived, not random: the
  same case+repeat gets the same ``CV_SEED`` on a re-run, so a flake is reproducible by
  re-running the same commit, and repeats of one case still differ from each other.

Expansion is CASE-MAJOR (all repeats of case i, then case i+1) because the budget cuts
a PREFIX of this list: a truncated run must lose whole cases, never leave a case with
half its repeats (a half-sampled pass ratio is a fabricated regression).
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from dataclasses import dataclass

from cv_infra.contract.pict import CoveringArray


@dataclass(frozen=True)
class CaseRun:
    """One case+repeat = one container. ``case_index`` is the case's position in the
    array (shared by its repeats), so a run directory listing stays in plan order."""

    case_index: int
    case_id: str
    axes: Mapping[str, str]
    repeat: int
    seed: int
    argv: tuple[str, ...]


def case_id_for(axes: Mapping[str, str]) -> str:
    """Stable identity of an axis assignment (see the module doc — sorted, not ordered)."""
    canonical = json.dumps(sorted(axes.items()), separators=(",", ":"))
    return "sha256:" + hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def seed_for(case_id: str, repeat: int) -> int:
    """``CV_SEED`` for one run — deterministic in (case, repeat), distinct across both."""
    digest = hashlib.sha256(f"{case_id}:{repeat}".encode()).digest()
    return int.from_bytes(digest[:4], "big")


def argv_for(sim_script: str, axes: Mapping[str, str]) -> tuple[str, ...]:
    """The container command's argv: the script, then one ``--axis=value`` per axis."""
    return (sim_script, *(f"--{name}={value}" for name, value in axes.items()))


def expand(array: CoveringArray, *, sim_script: str, repeats: int) -> list[CaseRun]:
    """The array's rows -> the run list, case-major (see the module doc).

    ``repeats`` is taken as declared (>= 1 is enforced at admit — ``contract.inputs``);
    the historical repeats FLOOR is gone, so a consumer that asks for 1 gets 1 and the
    report labels its ratios ``single_sample`` instead of the plan quietly overruling it.
    """
    runs: list[CaseRun] = []
    for case_index, axes in enumerate(array.as_dicts()):
        case_id = case_id_for(axes)
        argv = argv_for(sim_script, axes)
        runs.extend(
            CaseRun(
                case_index=case_index,
                case_id=case_id,
                axes=axes,
                repeat=repeat,
                seed=seed_for(case_id, repeat),
                argv=argv,
            )
            for repeat in range(repeats)
        )
    return runs
