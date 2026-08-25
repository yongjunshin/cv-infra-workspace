#!/usr/bin/env python3
"""p6c1 spike — scenario YAMLs -> the canonical JOB_SPEC array Arm B consumes.

THROWAWAY (experiments/**). It REUSES the production admit gate and the production
JOB_SPEC wire builder (``cv_infra.contract.loader.load_request`` +
``cv_infra.cli.main._job_spec_from_request``, the frozen Phase-2 seam) so that Arm B
receives literally the same dicts ``cv-infra run`` writes to ``job_spec.json`` in Arm A.
Re-typing that shape here would make granularity AND input differ between the arms —
the one thing the spike must not allow (and G-25's exact failure mode).

``_job_spec_from_request`` is private; importing it is deliberate. The alternative is a
second definition of a frozen wire shape, which is worse.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from cv_infra.cli.main import _job_spec_from_request
from cv_infra.contract.loader import load_request


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("scenarios", nargs="+", help="scenario YAML paths, in iteration order")
    ap.add_argument("--out", required=True, help="output specs.json path")
    ap.add_argument("--job-id-prefix", default="p6b")
    args = ap.parse_args(argv)

    specs = []
    for index, path in enumerate(args.scenarios, start=1):
        admitted = load_request(Path(path))
        for warning in admitted.warnings:
            print(f"[make_specs] WARNING {path}: {warning}")
        specs.append(_job_spec_from_request(admitted.request, f"{args.job_id_prefix}-{index:02d}"))

    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(specs, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    print(f"[make_specs] wrote {len(specs)} JOB_SPEC(s) -> {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
