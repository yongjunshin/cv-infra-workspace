#!/usr/bin/env python3
"""p7c1 W0 spike — HOST-side reduction of the raw evidence. THROWAWAY.

Reads only files the runs already wrote (never re-measures) and prints markdown
tables. Method is fixed by GOTCHAS:

  * G-103(a): a per-iteration VRAM slope is OLS FROM ITERATION 3 (warm-up excluded)
    with r^2 printed, and level STEPS are decomposed separately — an endpoint
    difference is never called a slope.
  * G-101: every sampled tick is scanned for compute-app PIDs that are not this
    run's Isaac process; foreign tenancy is LABELLED, never killed, never averaged
    away silently.
  * G-18: only preserved raw samples are read.

  usage: analyze.py <evidence-root> [--scan-a <json>] [--scan-b <json>]
"""

from __future__ import annotations

import argparse
import base64
import json
import math
import statistics
import struct
from pathlib import Path

STEP_MIB = 32.0  # gate ⓓ's "level step" threshold (plan 부록 B §B8)


# --------------------------------------------------------------------------- #
def read_marks(container_log: Path) -> list[dict]:
    marks = []
    if not container_log.is_file():
        return marks
    for line in container_log.read_text(errors="replace").splitlines():
        if " MARK t=" not in line:
            continue
        fields = {}
        for token in line.split(" MARK ", 1)[1].split():
            if "=" in token:
                k, v = token.split("=", 1)
                fields[k] = v
        if "t" in fields:
            fields["t"] = float(fields["t"])
            marks.append(fields)
    return marks


def read_vram(csv: Path) -> list[dict]:
    rows = []
    if not csv.is_file():
        return rows
    for line in csv.read_text(errors="replace").splitlines()[1:]:
        parts = line.split(",")
        if len(parts) != 5:
            continue
        ts, kind, pid, name, used = parts
        try:
            rows.append(
                {
                    "ts": float(ts),
                    "kind": kind,
                    "pid": pid,
                    "name": name,
                    "used": float(used) if used not in ("", "NA") else None,
                }
            )
        except ValueError:
            continue
    return rows


def ols(xs: list[float], ys: list[float]) -> tuple[float, float]:
    n = len(xs)
    if n < 2:
        return float("nan"), float("nan")
    mx, my = sum(xs) / n, sum(ys) / n
    sxx = sum((x - mx) ** 2 for x in xs)
    if sxx == 0:
        return float("nan"), float("nan")
    sxy = sum((x - mx) * (y - my) for x, y in zip(xs, ys))
    slope = sxy / sxx
    intercept = my - slope * mx
    ss_res = sum((y - (slope * x + intercept)) ** 2 for x, y in zip(xs, ys))
    ss_tot = sum((y - my) ** 2 for y in ys)
    r2 = 1.0 - ss_res / ss_tot if ss_tot else float("nan")
    return slope, r2


def vram_report(evid: Path) -> str:
    csv = evid / "vram_0.5s.csv"
    rows = read_vram(csv)
    marks = read_marks(evid / "container.log")
    if not rows:
        return f"(no VRAM samples at {csv})\n"
    apps = [r for r in rows if r["kind"] == "app" and r["used"] is not None]
    pids = {}
    for r in apps:
        pids.setdefault(r["pid"], {"name": r["name"], "n": 0, "max": 0.0})
        pids[r["pid"]]["n"] += 1
        pids[r["pid"]]["max"] = max(pids[r["pid"]]["max"], r["used"])
    if not pids:
        return f"(no compute-app rows in {csv}; GPU-wide only)\n"
    main_pid = max(pids, key=lambda p: pids[p]["max"])
    out = [f"### VRAM — {evid.name}", "", f"samples: {len(rows)} rows, tenants seen: {len(pids)}", ""]
    out.append("| pid | process | ticks | peak MiB | tenancy |")
    out.append("|---|---|---|---|---|")
    for pid, info in sorted(pids.items(), key=lambda kv: -kv[1]["max"]):
        out.append(
            f"| {pid} | {info['name']} | {info['n']} | {info['max']:.0f} | "
            f"{'THIS RUN' if pid == main_pid else '**FOREIGN (G-101)**'} |"
        )
    out.append("")

    windows = []
    begins = {}
    for m in marks:
        if m.get("phase") == "iter_begin":
            begins[m.get("iter")] = m["t"]
        elif m.get("phase") == "iter_end" and m.get("iter") in begins:
            windows.append((int(m["iter"]), begins.pop(m["iter"]), m["t"]))
    if not windows:
        peak = max(r["used"] for r in apps if r["pid"] == main_pid)
        out.append(f"no iteration markers — run peak (pid {main_pid}) = {peak:.0f} MiB\n")
        return "\n".join(out) + "\n"

    series = []
    for it, t0, t1 in sorted(windows):
        vals = [r["used"] for r in apps if r["pid"] == main_pid and t0 <= r["ts"] <= t1]
        foreign = sorted({r["pid"] for r in apps if r["pid"] != main_pid and t0 <= r["ts"] <= t1})
        if vals:
            series.append((it, max(vals), len(vals), foreign))
    out.append("| iter | per-PID peak MiB | ticks | foreign PIDs in window |")
    out.append("|---|---|---|---|")
    for it, peak, n, foreign in series:
        out.append(f"| {it} | {peak:.0f} | {n} | {', '.join(foreign) or '-'} |")
    out.append("")
    tail = [(it, peak) for it, peak, _, _ in series if it >= 3]
    if len(tail) >= 2:
        slope, r2 = ols([float(i) for i, _ in tail], [p for _, p in tail])
        out.append(f"OLS from iteration 3 (n={len(tail)}): **{slope:+.3f} MiB/iteration**, r^2={r2:.3f}")
    steps = [
        (series[i][0], series[i][1] - series[i - 1][1])
        for i in range(1, len(series))
        if abs(series[i][1] - series[i - 1][1]) >= STEP_MIB
    ]
    out.append(f"level steps >= {STEP_MIB:.0f} MiB: **{len(steps)}** {steps}")
    out.append("")
    return "\n".join(out) + "\n"


# --------------------------------------------------------------------------- #
def scan_compare(a: Path, b: Path) -> str:
    da, db = json.loads(a.read_text()), json.loads(b.read_text())
    ma, mb = da["messages"], db["messages"]
    n = min(len(ma), len(mb))
    out = [
        "### /scan 2-arm (parking invisibility)",
        "",
        f"arm A = {da['arm']} ({len(ma)} msgs, {da['steps']} steps), "
        f"arm B = {db['arm']} ({len(mb)} msgs, {db['steps']} steps); compared pairs: {n}",
        "",
    ]
    identical = 0
    worst = 0.0
    worst_idx = None
    ray_mismatch = 0
    for i in range(n):
        ba = base64.b64decode(ma[i]["ranges_b64"])
        bb = base64.b64decode(mb[i]["ranges_b64"])
        if ba == bb:
            identical += 1
            continue
        fa = struct.unpack(f"{len(ba) // 4}f", ba)
        fb = struct.unpack(f"{len(bb) // 4}f", bb)
        for x, y in zip(fa, fb):
            d = abs(x - y)
            if d > worst:
                worst, worst_idx = d, i
            if d > 0:
                ray_mismatch += 1
    out.append(f"- bitwise-identical messages: **{identical}/{n}**")
    out.append(f"- differing ray values: {ray_mismatch}")
    out.append(f"- max |Δrange|: **{worst:.6g} m** (first at message {worst_idx})")
    out.append("")
    return "\n".join(out)


# --------------------------------------------------------------------------- #
def _ranges(messages: list[dict]) -> list[float]:
    vals: list[float] = []
    for m in messages:
        raw = base64.b64decode(m["ranges_b64"])
        vals.extend(struct.unpack(f"{len(raw) // 4}f", raw))
    return vals


def scan_distribution(messages: list[dict]) -> dict:
    """Phase-INDEPENDENT summary of a /scan block.

    Message-index-aligned comparison turned out to be undecidable (measured: two
    blocks of the SAME configuration in the SAME process share 0/100 bitwise-equal
    messages, 222129 differing rays, max 25.5 m — the sweep phase and the resting
    robot's micro-pose are not reproducible). What IS stable across same-config
    repeats is the RANGE DISTRIBUTION, so the parking question is asked with that,
    together with a placed-pool positive control proving the statistic moves when
    obstacles really are visible (G-35/G-102).
    """
    vals = _ranges(messages)
    finite = [v for v in vals if math.isfinite(v) and v > 0]
    ordered = sorted(finite)
    return {
        "messages": len(messages),
        "rays": len(vals),
        "finite": len(finite),
        "min": ordered[0] if ordered else None,
        "median": statistics.median(ordered) if ordered else None,
        "mean": statistics.fmean(finite) if finite else None,
    }


def scan_blocks_report(path: Path) -> str:
    data = json.loads(path.read_text())
    out = [
        "### Gate d — /scan blocks (ONE process: parked / parked / placed / parked)",
        "",
        f"pool members: {len(data['pool_members'])}, render products enabled: "
        f"{data['render_products_enabled']}, obstacle-namespace contacts: "
        f"{data['pool_contact_events']}",
        "",
        "| block | msgs | steps | finite rays | min m | median m | mean m |",
        "|---|---|---|---|---|---|---|",
    ]
    stats = {}
    for b in data["blocks"]:
        d = scan_distribution(b["messages"])
        stats[b["label"]] = d
        out.append(
            f"| {b['label']} | {d['messages']} | {b['steps']} | {d['finite']} | "
            f"{d['min']:.4f} | {d['median']:.4f} | {d['mean']:.4f} |"
        )
    parked = [v["mean"] for k, v in stats.items() if "parked" in k]
    placed = [v["mean"] for k, v in stats.items() if "placed" in k]
    out.append("")
    if parked and placed:
        spread = max(parked) - min(parked)
        effect = min(parked) - placed[0]
        out.append(f"- parked-block mean spread (same config = noise floor): **{spread:.4f} m**")
        ratio = f" = {effect / spread:.1f}x the floor" if spread else ""
        out.append(f"- placed-vs-parked mean shift (positive control): **{effect:.4f} m**{ratio}")
    out.append("")
    return "\n".join(out)


# --------------------------------------------------------------------------- #
def arm_log_counts(container_log: Path) -> dict[str, dict[str, int]]:
    """[Warning]/[Error] line counts BETWEEN each arm's ARM_BEGIN/ARM_END markers.

    Attribution by log ORDER, not by timestamp arithmetic: the spike's markers and
    Kit's own diagnostics share one stream. The ``(none)|RAM_CONTROL`` arm is the
    control — warnings that also appear there are not obstacle-attributable
    (G-103(b)).
    """
    counts: dict[str, dict[str, int]] = {}
    current = None
    if not container_log.is_file():
        return counts
    for line in container_log.read_text(errors="replace").splitlines():
        if "ARM_BEGIN " in line:
            current = line.split("ARM_BEGIN ", 1)[1].strip()
            counts.setdefault(current, {"warning": 0, "error": 0, "physx_material": 0})
            continue
        if "ARM_END " in line:
            current = None
            continue
        if current is None:
            continue
        low = line.lower()
        if "[warning]" in low:
            counts[current]["warning"] += 1
        if "[error]" in low:
            counts[current]["error"] += 1
        if "getmaterialfrominternalfaceindex" in low:
            counts[current]["physx_material"] += 1
    return counts


def collider_table(path: Path) -> str:
    data = json.loads(path.read_text())
    logs = arm_log_counts(path.parent.parent / "container.log")
    out = [
        "### Gate ⓑ — collider candidate grid (3 criteria AND)",
        "",
        "| asset | cand | api | apply s | collision APIs after | contacts on obstacle | Δpos m | Δyaw rad | ①contact | ②static | ③warn/err in arm |",
        "|---|---|---|---|---|---|---|---|---|---|---|",
    ]
    for arm in data["arms"]:
        c = arm.get("collider", {})
        r = arm.get("result", {})
        tag_counts = logs.get(f"{arm['asset']}|{arm['candidate']}", {})
        out.append(
            f"| {arm['asset']} | {arm['candidate']} | "
            f"{(c.get('error') and 'ERROR: ' + c['error'][:60]) or c.get('api', '')} | "
            f"{c.get('apply_wall_s')} | {c.get('api_census_after', {}).get('collision')} | "
            f"{r.get('contacts_on_obstacle')} | {r.get('delta_pos_m', float('nan')):.2e} | "
            f"{r.get('delta_yaw_rad', float('nan')):.2e} | "
            f"{'PASS' if r.get('criterion1_contact_ge1') else 'FAIL'} | "
            f"{'PASS' if r.get('criterion2_static') else 'FAIL'} | "
            f"{tag_counts.get('warning', '-')}/{tag_counts.get('error', '-')} |"
        )
    out.append("")
    return "\n".join(out)


def pool_table(path: Path) -> str:
    data = json.loads(path.read_text())
    its = data["iters"]
    base = its[0]["census"] if its else {}
    out = [
        "### Gate ⓓ — pool + parking cycle",
        "",
        f"pool_total = {data['pool_total']}",
        "",
        "| iter | placed | parked | total prims | Looks | PhysMat | pool children | contacts placed | contacts parked | sim_t | monotonic | reset s | steps s |",
        "|---|---|---|---|---|---|---|---|---|---|---|---|---|",
    ]
    for r in its:
        c = r["census"]
        out.append(
            f"| {r['iter']} | {r['placed']} | {r['parked']} | {c['total_prims']} | {c['looks']} | "
            f"{c['physics_materials']} | {c['pool_children']} | {r['contacts_on_placed']} | "
            f"{r['contacts_on_parked']} | {r['sim_time_s']:.2f} | {r['sim_time_monotonic']} | "
            f"{r['reset_wall_s']} | {r['steps_wall_s']} |"
        )
    last = its[-1]["census"] if its else {}
    out.append("")
    out.append(f"census iter 1 = {base}")
    out.append(f"census iter {len(its)} = {last}")
    out.append(f"identical: **{base == last}**")
    out.append("")
    return "\n".join(out)


def assets_table(path: Path) -> str:
    data = json.loads(path.read_text())
    out = [
        "### Gate ⓐ/ⓔ — probed asset candidates",
        "",
        f"assets root = `{data['root']}`",
        "",
        "| category | name | usd_path | size B | bbox_min_z | z_offset | size xyz | meshes | collision | rigid | artic | load s | ΔVRAM(gpu-wide) MiB | rx bytes |",
        "|---|---|---|---|---|---|---|---|---|---|---|---|---|---|",
    ]
    for c in data["candidates"]:
        counts = c.get("counts", {})
        bb = c.get("bbox_min", [None, None, None])
        out.append(
            f"| {c.get('category')} | {c.get('name')} | `{c.get('usd_path')}` | {c.get('size')} | "
            f"{bb[2]} | {c.get('z_offset')} | {c.get('size_xyz')} | {counts.get('meshes')} | "
            f"{counts.get('collision')} | {counts.get('rigid_body')} | {counts.get('articulation')} | "
            f"{c.get('load_wall_s')} | {c.get('vram_gpu_wide_delta_mib')} | {c.get('rx_bytes')} |"
        )
    out.append("")
    return "\n".join(out)


def yaw_table(path: Path) -> str:
    rows = json.loads(path.read_text())
    out = [
        "### Gate ⓒ — yaw read-back",
        "",
        "| asset | declared yaw | read-back yaw | |Δyaw| | roll | pitch |",
        "|---|---|---|---|---|---|",
    ]
    for r in rows:
        out.append(
            f"| {r['asset']} | {r['declared_yaw']:.6f} | {r['readback_yaw']:.6f} | "
            f"{r['yaw_abs_diff']:.3e} | {r['roll']:.3e} | {r['pitch']:.3e} |"
        )
    out.append("")
    return "\n".join(out)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("root")
    ap.add_argument("--scan-a")
    ap.add_argument("--scan-b")
    args = ap.parse_args()
    root = Path(args.root)

    print(f"# p7c1 W0 evidence — {root}\n")
    for run in sorted(p for p in root.iterdir() if p.is_dir()):
        out_dir = run / "out"
        if (out_dir / "assets.json").is_file():
            print(assets_table(out_dir / "assets.json"))
        for cost in sorted(out_dir.glob("cost_*.json")):
            data = json.loads(cost.read_text())
            print(f"### Gate ⓔ — cost ({data['cache_label']} cache) — {run.name}\n")
            print("| name | load s | rx bytes | ΔVRAM(gpu-wide) MiB |")
            print("|---|---|---|---|")
            for r in data["rows"]:
                print(
                    f"| {r.get('name')} | {r.get('load_wall_s')} | {r.get('rx_bytes')} | "
                    f"{r.get('vram_gpu_wide_delta_mib')} |"
                )
            print()
        if (out_dir / "collider_grid.json").is_file():
            print(collider_table(out_dir / "collider_grid.json"))
        if (out_dir / "yaw.json").is_file():
            print(yaw_table(out_dir / "yaw.json"))
        if (out_dir / "pool_cycle.json").is_file():
            print(pool_table(out_dir / "pool_cycle.json"))
        if (out_dir / "scan_blocks.json").is_file():
            print(scan_blocks_report(out_dir / "scan_blocks.json"))
        for scan in sorted(out_dir.glob("scan_*_pool.json")):
            d = scan_distribution(json.loads(scan.read_text())["messages"])
            print(f"### /scan single arm — {run.name} / {scan.name}\n")
            print(f"{d}\n")
        if (run / "vram_0.5s.csv").is_file():
            print(vram_report(run))
    if args.scan_a and args.scan_b:
        print(scan_compare(Path(args.scan_a), Path(args.scan_b)))


if __name__ == "__main__":
    main()
