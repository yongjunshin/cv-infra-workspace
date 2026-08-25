#!/usr/bin/env python3
"""p6c1 spike — build the cycle-plan §5 tables from the raw evidence. THROWAWAY.

Reads ONLY files the two arms wrote; derives, never overwrites (G-18). Every number it
prints is traceable to a path it names.

    usage: analyze.py <evidence-dir>
"""

from __future__ import annotations

import csv
import json
import re
import statistics
import sys
from pathlib import Path

SAMPLE_INTERVAL_S = 0.5  # vram_sampler.sh default; also derived below from the CSV


def read_json(path: Path):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def load_vram(path: Path):
    """-> (gpu_rows, app_rows) where each row is (ts, pid, name, mib)."""
    gpu, app = [], []
    if not path.is_file():
        return gpu, app
    with path.open(encoding="utf-8") as handle:
        for row in csv.DictReader(handle):
            try:
                ts = float(row["ts_epoch"])
                mib = float(row["used_mib"])
            except (TypeError, ValueError):
                continue
            (gpu if row["kind"] == "gpu" else app).append(
                (ts, row["pid"], row["process_name"], mib)
            )
    return gpu, app


def window(rows, t0, t1):
    return [r for r in rows if t0 <= r[0] <= t1]


def gpu_seconds(app_rows, t0, t1, interval):
    """GPU-seconds = ticks in [t0,t1] that had >=1 compute app x the sample interval."""
    ticks = {round(r[0], 3) for r in window(app_rows, t0, t1)}
    return len(ticks) * interval


def idle_baseline(gpu_rows, app_rows):
    """GPU-wide memory.used on ticks with NO compute app = this host's idle floor."""
    busy = {round(r[0], 3) for r in app_rows}
    idle = [r[3] for r in gpu_rows if round(r[0], 3) not in busy]
    return (min(idle), statistics.median(idle), max(idle), len(idle)) if idle else (None,) * 4


def sample_interval(gpu_rows):
    if len(gpu_rows) < 3:
        return SAMPLE_INTERVAL_S
    deltas = [b[0] - a[0] for a, b in zip(gpu_rows, gpu_rows[1:], strict=False)]
    return round(statistics.median(deltas), 3)


def criterion(result: dict, name: str) -> dict:
    for entry in result.get("criteria_results", []):
        if entry.get("oracle") == name or entry.get("name") == name:
            return entry
    return {}


CLOSEST_RE = re.compile(r"closest approach ([0-9.]+) m")
REACHED_RE = re.compile(r"reached at ([0-9.]+)s")


def goal_distance_proxy(result: dict):
    """A CONTINUOUS per-sample comparable that survives in result.json alone.

    ``reached_goal``'s detail carries either the closest GT approach (fail/timeout) or
    the sim-time it was reached (pass) — the two are not interchangeable, so both are
    reported and the caller compares like with like.
    """
    detail = criterion(result, "reached_goal").get("detail", "") or ""
    closest = CLOSEST_RE.search(detail)
    reached = REACHED_RE.search(detail)
    return (
        float(closest.group(1)) if closest else None,
        float(reached.group(1)) if reached else None,
        detail,
    )


def fmt(value, spec=".3f"):
    if value is None:
        return "—"
    if isinstance(value, float):
        return format(value, spec)
    return str(value)


def arm_a_rows(evid: Path):
    csv_path = evid / "arm_a" / "arm_a_jobs.csv"
    rows = []
    if not csv_path.is_file():
        return rows
    raw = csv_path.read_text(encoding="utf-8").splitlines()
    lines = [ln for ln in raw if not ln.startswith("#")]
    for row in csv.DictReader(lines):
        result_path = Path(row["result_json"])
        result = read_json(result_path) if row["result_json"] != "MISSING" else None
        job_dir = result_path.parent.parent if result else None
        rows.append(
            {
                "index": int(row["index"]),
                "job_id": row["job_id"],
                "cache_seed_s": float(row["cache_seed_s"]),
                "wall_s": float(row["cli_wall_s"]),
                "t0": float(row["t0_epoch"]),
                "t1": float(row["t1_epoch"]),
                "exit_code": int(row["exit_code"]),
                "result": result,
                "job_dir": job_dir,
            }
        )
    return rows


def arm_b_rows(evid: Path):
    timings = read_json(evid / "arm_b" / "out" / "timings.json") or {}
    rows = []
    for record in timings.get("iterations", []):
        index = record["index"]
        result = read_json(evid / "arm_b" / "out" / "results" / str(index) / "result.json")
        rows.append({**record, "result": result})
    return timings, rows


def boot_lines(log: Path) -> dict:
    """Pull the runner's own boot-summary fold out of a container log, if preserved."""
    if not log.is_file():
        return {}
    for line in log.read_text(encoding="utf-8", errors="replace").splitlines():
        if "boot summary" in line or "boot_summary" in line:
            return {"line": line.strip()}
    return {}


def main(argv: list[str]) -> int:
    if len(argv) != 2:
        print(__doc__)
        return 2
    evid = Path(argv[1])

    a_rows = arm_a_rows(evid)
    b_timings, b_rows = arm_b_rows(evid)
    a_gpu, a_app = load_vram(evid / "arm_a" / "vram_0.5s.csv")
    b_gpu, b_app = load_vram(evid / "arm_b" / "vram_0.5s.csv")
    a_interval = sample_interval(a_gpu)
    b_interval = sample_interval(b_gpu)

    print("## Table 1 — 비용 (per-sample marginal cost)\n")
    print("### 1a. Arm A — 잡당 (wall = `cv-infra run` 전체; GPU-초 = compute-app 존재 구간)\n")
    print("| # | job_id | exit | cache seed s | CLI wall s | GPU-초 | peak per-PID MiB |")
    print("|---|---|---|---|---|---|---|")
    a_walls, a_gpus, a_peaks = [], [], []
    for row in a_rows:
        gsec = gpu_seconds(a_app, row["t0"], row["t1"], a_interval)
        peaks = [r[3] for r in window(a_app, row["t0"], row["t1"])]
        peak = max(peaks) if peaks else None
        a_walls.append(row["wall_s"])
        a_gpus.append(gsec)
        if peak is not None:
            a_peaks.append(peak)
        print(
            f"| {row['index']} | {row['job_id']} | {row['exit_code']} | "
            f"{fmt(row['cache_seed_s'])} | {fmt(row['wall_s'])} | {fmt(gsec, '.1f')} | "
            f"{fmt(peak, '.0f')} |"
        )
    if a_walls:
        print(
            f"| — | **mean** | | | **{fmt(statistics.mean(a_walls))}** | "
            f"**{fmt(statistics.mean(a_gpus), '.1f')}** | "
            f"**{fmt(max(a_peaks) if a_peaks else None, '.0f')}** (max) |"
        )
        print(f"| — | **sum** | | | **{fmt(sum(a_walls))}** | **{fmt(sum(a_gpus), '.1f')}** | |")

    print("\n### 1b. Arm B — 부팅 1회 + 반복당\n")
    boot = b_timings.get("boot", {})
    if boot:
        print("| boot phase | s |")
        print("|---|---|")
        for key in sorted(boot):
            print(f"| {key} | {fmt(boot[key])} |")
    print(
        "\n| # | verdict | restage s | SUT realign s | readiness s | mission s | "
        "record s | evaluate s | iteration s |"
    )
    print("|---|---|---|---|---|---|---|---|---|")
    b_iters = []
    for row in b_rows:
        t = row.get("timings_s", {})
        rec = (t.get("record_start", 0.0) or 0.0) + (t.get("record_stop", 0.0) or 0.0)
        b_iters.append(t.get("iteration"))
        print(
            f"| {row['index']} | {row.get('verdict')} | {fmt(t.get('restage'))} | "
            f"{fmt(t.get('sut_realign'))} | {fmt(t.get('readiness'))} | {fmt(t.get('mission'))} | "
            f"{fmt(rec)} | {fmt(t.get('evaluate'))} | {fmt(t.get('iteration'))} |"
        )
    b_iters_ok = [v for v in b_iters if v is not None]
    if b_iters_ok:
        print(f"| — | **mean** | | | | | | | **{fmt(statistics.mean(b_iters_ok))}** |")
        print(f"| — | **sum** | | | | | | | **{fmt(sum(b_iters_ok))}** |")

    print("\n### 1c. 손익분기\n")
    t_boot_b = boot.get("total_s")
    t_job = statistics.mean(a_walls) if a_walls else None
    t_iter = statistics.mean(b_iters_ok) if b_iters_ok else None
    b_gpu_total = None
    summary = evid / "arm_b" / "arm_b_summary.txt"
    b_t0 = b_t1 = None
    if summary.is_file():
        kv = dict(
            line.split("=", 1)
            for line in summary.read_text(encoding="utf-8").splitlines()
            if "=" in line and not line.startswith("#")
        )
        b_t0, b_t1 = float(kv.get("t0_epoch", 0)), float(kv.get("t1_epoch", 0))
        b_gpu_total = gpu_seconds(b_app, b_t0, b_t1, b_interval)
        wall = kv.get("container_wall_s")
        print(f"- Arm B container wall (docker run -> docker wait): **{wall} s**")
        print(f"- Arm B runner exit code: **{kv.get('runner_exit_code')}**")
    print(f"- t_job (Arm A mean CLI wall) = **{fmt(t_job)} s**")
    print(f"- t_iter (Arm B mean iteration) = **{fmt(t_iter)} s**")
    print(f"- t_boot_B (Arm B one-off boot) = **{fmt(t_boot_b)} s**")
    if t_job and t_iter is not None:
        delta = t_job - t_iter
        print(f"- t_job - t_iter = **{fmt(delta)} s**")
        if t_boot_b and delta > 0:
            print(f"- **n\\* = t_boot_B / (t_job - t_iter) = {fmt(t_boot_b / delta, '.2f')}**")
        else:
            print(
                "- **n\\* undefined** — t_iter >= t_job "
                "(Arm B has no marginal advantage to amortize)"
            )
    print(
        f"- GPU-초 총합: Arm A **{fmt(sum(a_gpus), '.1f')} s** "
        f"vs Arm B **{fmt(b_gpu_total, '.1f')} s**"
    )

    print("\n## Table 2 — 독립성 (표본별 A vs B)\n")
    print(
        "| # | A verdict | B verdict | 일치 | A time_to_goal | B time_to_goal | "
        "A closest | B closest | A path_len | B path_len | A coll | B coll | B artifacts |"
    )
    print("|---|---|---|---|---|---|---|---|---|---|---|---|---|")
    a_by_index = {r["index"]: r for r in a_rows}
    b_by_index = {r["index"]: r for r in b_rows}
    agree = 0
    for index in sorted(set(a_by_index) | set(b_by_index)):
        ar = (a_by_index.get(index) or {}).get("result") or {}
        br = (b_by_index.get(index) or {}).get("result") or {}
        av, bv = ar.get("verdict"), br.get("verdict")
        same = "=" if av is not None and av == bv else "≠"
        agree += av is not None and av == bv
        a_closest, _, _ = goal_distance_proxy(ar) if ar else (None, None, "")
        b_closest, _, _ = goal_distance_proxy(br) if br else (None, None, "")
        am, bm = ar.get("metrics", {}), br.get("metrics", {})
        bart = br.get("artifacts", {}) or {}
        arts = "mcap+mp4" if bart.get("mcap") and bart.get("mp4") else str(sorted(bart))
        print(
            f"| {index} | {av} | {bv} | {same} | {fmt(am.get('time_to_goal_s'))} | "
            f"{fmt(bm.get('time_to_goal_s'))} | {fmt(a_closest)} | {fmt(b_closest)} | "
            f"{fmt(am.get('path_len_m'))} | {fmt(bm.get('path_len_m'))} | "
            f"{am.get('collision_count')} | {bm.get('collision_count')} | {arts} |"
        )
    total = len(set(a_by_index) | set(b_by_index))
    print(f"\n- verdict 일치: **{agree}/{total}**")

    print("\n## Table 3 — VRAM 시계열\n")
    for label, gpu_rows, app_rows, interval in (
        ("Arm A", a_gpu, a_app, a_interval),
        ("Arm B", b_gpu, b_app, b_interval),
    ):
        lo, med, hi, count = idle_baseline(gpu_rows, app_rows)
        peak_app = max((r[3] for r in app_rows), default=None)
        peak_gpu = max((r[3] for r in gpu_rows), default=None)
        net_peak = (peak_gpu - med) if (peak_gpu is not None and med is not None) else None
        print(
            f"- **{label}** (interval {interval}s, {len(gpu_rows)} gpu / {len(app_rows)} app "
            f"samples): idle GPU-wide min/med/max = {fmt(lo,'.0f')}/{fmt(med,'.0f')}/"
            f"{fmt(hi,'.0f')} MiB over {count} idle ticks; **(B) per-PID peak "
            f"{fmt(peak_app,'.0f')} MiB**; (A) GPU-wide peak {fmt(peak_gpu,'.0f')} MiB "
            f"minus idle median = {fmt(net_peak, '.0f')} MiB"
        )
    print("\n### Arm B — 반복별 per-PID peak (누수 판정)\n")
    print("| # | window start | window end | per-PID peak MiB | GPU-wide peak MiB |")
    print("|---|---|---|---|---|")
    prev_end = b_t0
    for row in b_rows:
        # iteration windows are derived from the cumulative iteration walls
        t = row.get("timings_s", {})
        if prev_end is None or t.get("iteration") is None:
            continue
        start, end = prev_end, prev_end + t["iteration"]
        peaks = [r[3] for r in window(b_app, start, end)]
        gpeaks = [r[3] for r in window(b_gpu, start, end)]
        print(
            f"| {row['index']} | {start:.1f} | {end:.1f} | "
            f"{fmt(max(peaks) if peaks else None, '.0f')} | "
            f"{fmt(max(gpeaks) if gpeaks else None, '.0f')} |"
        )
        prev_end = end

    print("\n## Table 4 — 결과 형태 (Q4) 관찰\n")
    print(
        f"- Arm A: `result.json` **{sum(1 for r in a_rows if r['result'])}/{len(a_rows)}** "
        "— 잡 디렉토리 하나당 정확히 1개, 컨테이너 exit code 하나가 그 판정을 나른다."
    )
    print(
        f"- Arm B: `results/<i>/result.json` **{sum(1 for r in b_rows if r['result'])}/"
        f"{len(b_rows)}** — 컨테이너 **하나**가 N개를 낸다. exit code는 1개뿐이다: "
        f"{b_timings.get('verdicts')}"
    )
    for row in b_rows:
        if row.get("sut_realign"):
            print(f"  - iter {row['index']} realign: {row['sut_realign']}")
    if b_timings.get("error"):
        print(f"- Arm B terminated with an error: `{b_timings['error']}`")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
