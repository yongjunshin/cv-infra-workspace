#!/usr/bin/env python3
"""examples/selftest/oracle.py — judge the dropped cube; print one flat JSON dict.

The oracle runs after the sim, in the SAME image with the SAME argv but without a GPU,
and its whole output contract is one line of flat JSON on stdout where the TYPES carry
the meaning:

    {"fell": true, "z_final": 0.25, "note": "121 samples, seed 7"}
     ^ bool = a CHECK (the case passes when every bool is true)
                    ^ number = a METRIC (compared to the baseline, never gates)
                                  ^ string = a NOTE (shown in the report)

Stdlib only, on purpose: the oracle must be runnable and debuggable anywhere the sim's
output file can be copied to, including a laptop with no Isaac Sim.

    python3 examples/selftest/oracle.py --drop_height=1.5 --cube_scale=0.5
"""

import argparse
import json
import sys
from pathlib import Path

#: The path sim.py wrote, on the same checkout-root-relative terms (the platform mounts
#: the case's output directory back over it, read-only).
TRAJECTORY = Path("examples/selftest/out/trajectory.json")

#: A drop counts as "fell" once the cube is this far below where it started. Slack for
#: the cube's own half-height and for a bounce that has not settled yet.
FELL_MARGIN_M = 0.2


def main() -> int:
    parser = argparse.ArgumentParser(description="cv-infra selftest oracle")
    parser.add_argument("--drop_height", type=float, default=1.5)
    parser.add_argument("--cube_scale", type=float, default=0.5)
    # parse_known_args, not parse_args: the platform replays the SIM's argv verbatim, so
    # an oracle must tolerate axes it does not read rather than exit 2 on them.
    args, _ = parser.parse_known_args()

    try:
        record = json.loads(TRAJECTORY.read_text(encoding="utf-8"))
        z_samples = [float(value) for value in record["z"]]
    except (OSError, ValueError, KeyError, TypeError) as exc:
        # No verdict is possible, so say nothing on stdout: a non-zero exit puts this
        # run in the ERROR lane, which is honest, instead of failing the check.
        print(f"oracle: cannot read {TRAJECTORY}: {exc!r}", file=sys.stderr)
        return 1
    if not z_samples:
        print(f"oracle: {TRAJECTORY} holds no samples", file=sys.stderr)
        return 1

    z_final = z_samples[-1]
    print(
        json.dumps(
            {
                "fell": z_final < args.drop_height - FELL_MARGIN_M,
                "z_final": round(z_final, 4),
                "note": f"{len(z_samples)} samples, seed {record.get('seed')},"
                f" dropped from {args.drop_height} m",
            }
        )
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
