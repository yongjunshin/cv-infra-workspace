#!/usr/bin/env python3
"""p6c2 — per-iteration VRAM slope per variant. THROWAWAY (experiments/**).

Consumes what ``arm_b2.sh`` leaves behind for each variant:

    <evidence>/p6c2/<variant>/vram_0.5s.csv     the 0.5 s per-PID + GPU-wide samples
    <evidence>/p6c2/<variant>/out/timings.json  the loop's own per-iteration record,
                                                including the p6c2 ``epoch`` marks

and answers one question per variant: **how many MiB does the Isaac process keep,
per additional iteration** — reported BOTH per iteration and per sim-second, because
p6c1 left open whether the pass-to-pass slope difference was mission length (report
§11 unresolved ①).

Two deliberate choices, both from p6c1's lessons:

* iterations 1 and 2 are EXCLUDED from the fit by default (``--from 3``). Iteration 1
  is a fresh scene (no re-stage at all) and the 1->2 step is the recording render
  product + adapter/telemetry steady state arriving — p6c1 measured +600-700 MiB
  there, which is a level change, not a slope. Fitting through it would report a
  "leak" that is really a one-off.
* every window is TENANCY-CHECKED (G-101): if a foreign PID held the card during an
  iteration, that iteration is named and dropped from the fit rather than averaged in.

Usage:
    python3 vram_slope.py <evidence-root> [variant ...] [--from N] [--csv out.csv]
"""

from __future__ import annotations

import argparse
import collections
import csv
import json
import statistics
import sys
from pathlib import Path

DEFAULT_FIT_FROM = 3


def load_vram(path: Path):
    """-> (gpu_rows, app_rows); rows are (ts, pid, name, mib)."""
    gpu, app = [], []
    if not path.is_file():
        return gpu, app
    with path.open(encoding="utf-8") as handle:
        for row in csv.DictReader(handle):
            try:
                ts, mib = float(row["ts_epoch"]), float(row["used_mib"])
            except (TypeError, ValueError):
                continue
            (gpu if row["kind"] == "gpu" else app).append(
                (ts, row["pid"], row["process_name"], mib)
            )
    return gpu, app


def own_pid(app_rows) -> str | None:
    counts = collections.Counter(r[1] for r in app_rows)
    return counts.most_common(1)[0][0] if counts else None


def ols(xs, ys):
    """(slope, intercept, r2) by least squares; (None, None, None) if degenerate."""
    n = len(xs)
    if n < 3 or len(set(xs)) < 2:
        return None, None, None
    mx, my = statistics.fmean(xs), statistics.fmean(ys)
    sxx = sum((x - mx) ** 2 for x in xs)
    sxy = sum((x - mx) * (y - my) for x, y in zip(xs, ys, strict=True))
    slope = sxy / sxx
    intercept = my - slope * mx
    sst = sum((y - my) ** 2 for y in ys)
    ssr = sum((y - (slope * x + intercept)) ** 2 for x, y in zip(xs, ys, strict=True))
    r2 = None if sst == 0 else 1.0 - ssr / sst
    return slope, intercept, r2


def iteration_rows(variant_dir: Path):
    """-> (meta, rows) where each row carries the iteration's VRAM window facts."""
    timings = json.loads((variant_dir / "out" / "timings.json").read_text(encoding="utf-8"))
    _gpu, app = load_vram(variant_dir / "vram_0.5s.csv")
    pid = own_pid(app)
    by_pid = [r for r in app if r[1] == pid]
    rows = []
    for record in timings.get("iterations", []):
        epoch = record.get("epoch") or {}
        t0, t1 = epoch.get("iteration_begin"), epoch.get("iteration_end")
        if t0 is None or t1 is None:
            continue
        window = [r for r in by_pid if t0 <= r[0] <= t1]
        foreign = sorted({r[1] for r in app if t0 <= r[0] <= t1} - {pid})
        sim_s = None
        after = record.get("sim_time_after_reset_s")
        end = record.get("sim_time_at_end_s")
        if after is not None and end is not None and end >= after:
            sim_s = end - after
        rows.append(
            {
                "index": record["index"],
                "verdict": record.get("verdict"),
                "peak": max((r[3] for r in window), default=None),
                "last": window[-1][3] if window else None,
                "samples": len(window),
                "foreign": foreign,
                "sim_s": sim_s,
                "wall_s": (record.get("timings_s") or {}).get("iteration"),
                "materials": record.get("material_prims"),
                "cleanup": record.get("cleanup") or {},
                "frames": record.get("video_frames"),
            }
        )
    meta = {
        "n": timings.get("n"),
        "ablate": timings.get("ablate") or [],
        "cleanup": timings.get("cleanup") or [],
        "boot_total_s": (timings.get("boot") or {}).get("total_s"),
        "wall_total_s": timings.get("wall_total_s"),
        "verdicts": timings.get("verdicts") or [r.get("verdict") for r in rows],
        "pid": pid,
    }
    return meta, rows


def fit(rows, fit_from: int):
    """Slope over iterations >= fit_from, on clean windows only."""
    clean = [
        r for r in rows if r["index"] >= fit_from and r["last"] is not None and not r["foreign"]
    ]
    if len(clean) < 3:
        return None
    idx = [float(r["index"]) for r in clean]
    last = [r["last"] for r in clean]
    slope_i, _b, r2_i = ols(idx, last)
    # Cumulative sim-seconds THROUGH the end of each iteration, accumulated over
    # EVERY iteration in the run (not just the fitted ones): a window dropped for
    # foreign tenancy still consumed its sim-seconds, and skipping them would put a
    # hole in the x-axis and inflate the per-sim-second slope.
    running, cum_all = 0.0, {}
    for r in rows:
        running += r["sim_s"] or 0.0
        cum_all[r["index"]] = running
    base = cum_all[clean[0]["index"]]
    cum = [cum_all[r["index"]] - base for r in clean]
    total = cum[-1]
    slope_s, _b2, r2_s = ols(cum, last) if total > 0 else (None, None, None)
    return {
        "count": len(clean),
        "first_index": clean[0]["index"],
        "last_index": clean[-1]["index"],
        "mib_first": clean[0]["last"],
        "mib_last": clean[-1]["last"],
        "delta_mib": clean[-1]["last"] - clean[0]["last"],
        "slope_per_iter": slope_i,
        "r2_per_iter": r2_i,
        "sim_s_total": total,
        "slope_per_sim_s": slope_s,
        "r2_per_sim_s": r2_s,
        "peak_max": max(r["peak"] for r in clean if r["peak"] is not None),
        "wall_mean_s": statistics.fmean([r["wall_s"] for r in clean if r["wall_s"]]),
        "dropped_foreign": sorted(r["index"] for r in rows if r["foreign"]),
    }


def f(value, spec=".2f"):
    if value is None:
        return "—"
    return format(value, spec) if isinstance(value, float) else str(value)


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("evidence", help="evidence root (contains p6c2/<variant>/)")
    ap.add_argument("variants", nargs="*", help="variant names (default: all found)")
    ap.add_argument("--from", dest="fit_from", type=int, default=DEFAULT_FIT_FROM)
    ap.add_argument("--csv", help="also dump the per-iteration rows here")
    args = ap.parse_args(argv)

    root = Path(args.evidence) / "p6c2"
    names = args.variants or sorted(
        p.name for p in root.iterdir() if (p / "out" / "timings.json").is_file()
    )

    dump = []
    print("# p6c2 — VRAM slope by variant\n")
    print(
        "| variant | ablate/cleanup | n fit | MiB @first→last | Δ MiB | "
        "**MiB/iter** | r² | sim-s | **MiB/sim-s** | peak MiB | t_iter s | foreign |"
    )
    print("|---|---|---|---|---|---|---|---|---|---|---|---|")
    details = []
    for name in names:
        variant_dir = root / name
        try:
            meta, rows = iteration_rows(variant_dir)
        except (OSError, ValueError, KeyError) as exc:
            print(f"| {name} | (unreadable: {exc}) | | | | | | | | | | |")
            continue
        stats = fit(rows, args.fit_from)
        arms = ",".join(meta["ablate"] + [f"+{c}" for c in meta["cleanup"]]) or "base"
        if stats is None:
            print(f"| {name} | {arms} | (too few clean iterations) | | | | | | | | | |")
        else:
            print(
                f"| {name} | {arms} | {stats['count']} "
                f"({stats['first_index']}→{stats['last_index']}) | "
                f"{f(stats['mib_first'],'.0f')}→{f(stats['mib_last'],'.0f')} | "
                f"{f(stats['delta_mib'],'+.0f')} | **{f(stats['slope_per_iter'],'+.2f')}** | "
                f"{f(stats['r2_per_iter'],'.3f')} | {f(stats['sim_s_total'],'.0f')} | "
                f"**{f(stats['slope_per_sim_s'],'+.4f')}** | {f(stats['peak_max'],'.0f')} | "
                f"{f(stats['wall_mean_s'],'.1f')} | "
                f"{stats['dropped_foreign'] or '-'} |"
            )
        details.append((name, meta, rows, stats))
        for r in rows:
            dump.append({"variant": name, **{k: v for k, v in r.items() if k != "cleanup"}})

    print("\n## per-iteration detail\n")
    for name, meta, rows, _stats in details:
        print(f"### {name} — pid={meta['pid']} boot={f(meta['boot_total_s'],'.1f')}s")
        print(
            "\n| # | verdict | peak MiB | last MiB | Δ last | sim-s | wall-s | "
            "frames | material prims | cleanup | foreign |"
        )
        print("|---|---|---|---|---|---|---|---|---|---|---|")
        prev = None
        for r in rows:
            delta = "—" if prev is None or r["last"] is None else f"{r['last'] - prev:+.0f}"
            prev = r["last"] if r["last"] is not None else prev
            print(
                f"| {r['index']} | {r['verdict']} | {f(r['peak'],'.0f')} | "
                f"{f(r['last'],'.0f')} | {delta} | {f(r['sim_s'],'.1f')} | "
                f"{f(r['wall_s'],'.1f')} | {r['frames']} | {r['materials']} | "
                f"{r['cleanup'] or '-'} | {r['foreign'] or '-'} |"
            )
        print()

    if args.csv and dump:
        out = Path(args.csv)
        out.parent.mkdir(parents=True, exist_ok=True)
        with out.open("w", encoding="utf-8", newline="") as handle:
            writer = csv.DictWriter(handle, fieldnames=list(dump[0]))
            writer.writeheader()
            for row in dump:
                writer.writerow(row)
        print(f"<!-- per-iteration rows -> {out} -->")
    return 0


if __name__ == "__main__":
    sys.exit(main())
