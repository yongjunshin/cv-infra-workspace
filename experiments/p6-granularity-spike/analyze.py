#!/usr/bin/env python3
"""p6c1 spike — build the cycle-plan §5 tables from the raw evidence. THROWAWAY.

Reads ONLY files the two arms wrote; derives, never overwrites (G-18). Every number it
prints is traceable to a path it names.

TENANCY IS CHECKED, NOT ASSUMED. The measurement host also serves the live control
plane, and a CI-triggered self-test envelope landed on the GPU DURING this spike's
first Arm A pass (measured — two Isaac PIDs on the card at once). So every window is
labelled with the foreign compute PIDs that overlapped it, and a contaminated row is
never silently folded into a mean.

    usage: analyze.py <evidence-dir> [--arm-a-extra <dir> ...]
"""

from __future__ import annotations

import argparse
import collections
import csv
import json
import re
import statistics
from pathlib import Path

SAMPLE_INTERVAL_S = 0.5


def read_json(path: Path):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


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


def window(rows, t0, t1):
    return [r for r in rows if t0 <= r[0] <= t1]


def tenancy(app_rows, t0, t1):
    """-> (own_pid, own_ticks, foreign {pid: ticks}) inside [t0, t1].

    'Own' = the PID with the most samples in the window (the job/process this window
    belongs to); anything else on the card at the same time is FOREIGN and named.
    """
    counts = collections.Counter(r[1] for r in window(app_rows, t0, t1))
    if not counts:
        return None, 0, {}
    own, own_ticks = counts.most_common(1)[0]
    return own, own_ticks, {p: c for p, c in counts.items() if p != own}


def sample_interval(gpu_rows):
    if len(gpu_rows) < 3:
        return SAMPLE_INTERVAL_S
    deltas = [b[0] - a[0] for a, b in zip(gpu_rows, gpu_rows[1:], strict=False)]
    return round(statistics.median(deltas), 3)


def idle_baseline(gpu_rows, app_rows):
    busy = {round(r[0], 3) for r in app_rows}
    idle = [r[3] for r in gpu_rows if round(r[0], 3) not in busy]
    return (min(idle), statistics.median(idle), max(idle), len(idle)) if idle else (None,) * 4


def criterion(result: dict, name: str) -> dict:
    for entry in result.get("criteria_results", []):
        if entry.get("oracle") == name:
            return entry
    return {}


CLOSEST_RE = re.compile(r"closest approach ([0-9.]+) m")
REACHED_RE = re.compile(r"reached at ([0-9.]+)s")


def goal_proxy(result: dict):
    """(closest_approach_m | None, reach_time_s | None) — whichever the oracle recorded."""
    detail = criterion(result or {}, "reached_goal").get("detail", "") or ""
    closest, reached = CLOSEST_RE.search(detail), REACHED_RE.search(detail)
    return (
        float(closest.group(1)) if closest else None,
        float(reached.group(1)) if reached else None,
    )


def fmt(value, spec=".3f"):
    if value is None:
        return "—"
    return format(value, spec) if isinstance(value, float) else str(value)


def load_arm_a(base: Path):
    """Arm A rows from ``<base>/arm_a/arm_a_jobs.csv`` or ``<base>/arm_a_jobs.csv``.

    Both layouts exist on purpose: the full 8-job passes live under ``arm_a/``, the
    single-job clean re-run of sample 01 is its own base.
    """
    csv_path = base / "arm_a" / "arm_a_jobs.csv"
    if not csv_path.is_file():
        csv_path = base / "arm_a_jobs.csv"
    if not csv_path.is_file():
        return []
    raw = [ln for ln in csv_path.read_text(encoding="utf-8").splitlines() if not ln.startswith("#")]
    rows = []
    for row in csv.DictReader(raw):
        result_path = Path(row["result_json"])
        rows.append(
            {
                "index": int(row["index"]),
                "job_id": row["job_id"],
                "cache_seed_s": float(row["cache_seed_s"]),
                "wall_s": float(row["cli_wall_s"]),
                "t0": float(row["t0_epoch"]),
                "t1": float(row["t1_epoch"]),
                "exit_code": int(row["exit_code"]),
                "result": read_json(result_path),
                "result_path": result_path,
            }
        )
    return rows


BOOT_SUMMARY_RE = re.compile(r"boot_summary (.+)")


def boot_summary(log: Path) -> dict:
    """Parse the runner's own ``boot_summary`` fold out of a preserved container log.

    That line is the ONLY place the in-container split between fixed overhead
    (``boot_to_mission_s``) and the mission itself (``mission_s``) is recorded, and it
    is what makes Arm A's per-job wall comparable to an Arm B iteration (which pays no
    boot at all). Absent log -> {} (the row simply says so).
    """
    if not log.is_file():
        return {}
    for line in reversed(log.read_text(encoding="utf-8", errors="replace").splitlines()):
        match = BOOT_SUMMARY_RE.search(line)
        if match:
            out = {}
            for token in match.group(1).split():
                if "=" in token:
                    key, value = token.split("=", 1)
                    try:
                        out[key] = float(value)
                    except ValueError:
                        out[key] = value
            return out
    return {}


def arm_a_log(base: Path, job_id: str) -> Path:
    ctlogs = base / "arm_a" / "ctlogs"
    if not ctlogs.is_dir():
        ctlogs = base / "ctlogs"
    matches = sorted(ctlogs.glob(f"*{job_id}*-runner.log")) if ctlogs.is_dir() else []
    return matches[0] if matches else Path("/nonexistent")


def load_arm_b(base: Path):
    out = base / "arm_b" / "out"
    timings = read_json(out / "timings.json") or {}
    rows = []
    for record in timings.get("iterations", []):
        index = record["index"]
        result_file = out / "results" / str(index) / "result.json"
        rows.append(
            {
                **record,
                "result": read_json(result_file),
                # The file's mtime is the exact wall instant iteration i ENDED —
                # a measurement, unlike a cumulative sum of durations.
                "end_ts": result_file.stat().st_mtime if result_file.is_file() else None,
                "bag": (out / "results" / str(index) / "bag"),
                "mp4": (out / "results" / str(index) / "recording.mp4"),
            }
        )
    summary = {}
    summary_file = base / "arm_b" / "arm_b_summary.txt"
    if summary_file.is_file():
        for line in summary_file.read_text(encoding="utf-8").splitlines():
            if "=" in line and not line.startswith("#"):
                key, value = line.split("=", 1)
                summary[key] = value
    return timings, rows, summary


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("evidence")
    ap.add_argument(
        "--run",
        action="append",
        default=[],
        metavar="LABEL=DIR:ARM",
        help="another measured run to put in the verdict matrix, e.g. A2=/path/pass2:a",
    )
    args = ap.parse_args()
    evid = Path(args.evidence)

    a_rows = load_arm_a(evid)
    runs: dict[str, dict] = {
        "A1": {r["index"]: r["result"] for r in a_rows},
    }
    b_timings, b_rows, b_summary = load_arm_b(evid)
    a_gpu, a_app = load_vram(evid / "arm_a" / "vram_0.5s.csv")
    b_gpu, b_app = load_vram(evid / "arm_b" / "vram_0.5s.csv")
    a_int, b_int = sample_interval(a_gpu), sample_interval(b_gpu)
    b_by_early = {r["index"]: r for r in b_rows}
    if b_rows:
        runs["B1"] = {r["index"]: r["result"] for r in b_rows}
    for spec in args.run:
        label, rest = spec.split("=", 1)
        directory, _, arm = rest.rpartition(":")
        path = Path(directory)
        if arm == "a":
            runs[label] = {r["index"]: r["result"] for r in load_arm_a(path)}
        else:
            _t, rows, _s = load_arm_b(path)
            runs[label] = {r["index"]: r["result"] for r in rows}

    # ---------------------------------------------------------------- Table 1
    print("## Table 1 — 비용\n")
    print("### 1a. Arm A (C-1) — 잡당\n")
    print(
        "| # | job_id | exit | verdict | cache seed s | CLI wall s | GPU-초(own PID) | "
        "own peak MiB | 외부 테넌트 |"
    )
    print("|---|---|---|---|---|---|---|---|---|")
    a_clean_walls, a_gpu_secs, a_peaks = [], [], []
    for row in a_rows:
        own, own_ticks, foreign = tenancy(a_app, row["t0"], row["t1"])
        gsec = own_ticks * a_int
        peaks = [r[3] for r in window(a_app, row["t0"], row["t1"]) if r[1] == own]
        peak = max(peaks) if peaks else None
        a_gpu_secs.append(gsec)
        if peak is not None:
            a_peaks.append(peak)
        if not foreign:
            a_clean_walls.append(row["wall_s"])
        verdict = (row["result"] or {}).get("verdict")
        note = "clean" if not foreign else "; ".join(f"pid {p} x{c}" for p, c in foreign.items())
        print(
            f"| {row['index']} | {row['job_id']} | {row['exit_code']} | {verdict} | "
            f"{fmt(row['cache_seed_s'])} | {fmt(row['wall_s'])} | {fmt(gsec, '.1f')} | "
            f"{fmt(peak, '.0f')} | {note} |"
        )
    if a_clean_walls:
        print(
            f"| — | **mean (clean only, n={len(a_clean_walls)})** | | | | "
            f"**{fmt(statistics.mean(a_clean_walls))}** | "
            f"**{fmt(statistics.mean(a_gpu_secs), '.1f')}** | "
            f"**{fmt(max(a_peaks) if a_peaks else None, '.0f')}** (max) | |"
        )
        print(
            f"| — | **sum (all 8)** | | | | **{fmt(sum(r['wall_s'] for r in a_rows))}** | "
            f"**{fmt(sum(a_gpu_secs), '.1f')}** | | |"
        )

    print("\n### 1b. Arm B (C-2) — 부팅 1회 + 반복당\n")
    boot = b_timings.get("boot", {})
    if boot:
        print("| boot phase | s |")
        print("|---|---|")
        for key in sorted(boot):
            print(f"| {key} | {fmt(boot[key])} |")
    print(
        "\n| # | verdict | restage s | SUT realign s | readiness s | mission s | "
        "record s | evaluate s | **iteration s** |"
    )
    print("|---|---|---|---|---|---|---|---|---|")
    b_iters = []
    for row in b_rows:
        t = row.get("timings_s", {})
        rec = (t.get("record_start") or 0.0) + (t.get("record_stop") or 0.0)
        if t.get("iteration") is not None:
            b_iters.append(t["iteration"])
        print(
            f"| {row['index']} | {row.get('verdict')} | {fmt(t.get('restage'))} | "
            f"{fmt(t.get('sut_realign'))} | {fmt(t.get('readiness'))} | {fmt(t.get('mission'))} | "
            f"{fmt(rec)} | {fmt(t.get('evaluate'))} | **{fmt(t.get('iteration'))}** |"
        )
    if b_iters:
        print(f"| — | **mean** | | | | | | | **{fmt(statistics.mean(b_iters))}** |")
        print(f"| — | **sum** | | | | | | | **{fmt(sum(b_iters))}** |")

    print("\n### 1c. 손익분기 (n\\*)\n")
    t_boot_b = boot.get("total_s")
    t_job = statistics.mean(a_clean_walls) if a_clean_walls else None
    t_iter = statistics.mean(b_iters) if b_iters else None
    b_t0 = float(b_summary.get("t0_epoch", 0) or 0)
    b_t1 = float(b_summary.get("t1_epoch", 0) or 0)
    b_own, b_own_ticks, b_foreign = tenancy(b_app, b_t0, b_t1)
    b_gpu_total = b_own_ticks * b_int
    print(
        f"- Arm A: 잡 8개 wall 합 = **{fmt(sum(r['wall_s'] for r in a_rows))} s**, "
        f"GPU-초 합 = **{fmt(sum(a_gpu_secs), '.1f')} s**"
    )
    print(
        f"- Arm B: 컨테이너 wall = **{b_summary.get('container_wall_s')} s**, "
        f"GPU-초 = **{fmt(b_gpu_total, '.1f')} s**, "
        f"exit code = **{b_summary.get('runner_exit_code')}**"
    )
    print(f"- t_job (Arm A 오염 없는 잡 평균 wall) = **{fmt(t_job)} s** (n={len(a_clean_walls)})")
    print(f"- t_iter (Arm B 반복 평균) = **{fmt(t_iter)} s**")
    print(f"- t_boot_B (Arm B 1회 부팅) = **{fmt(t_boot_b)} s**")
    if t_job is not None and t_iter is not None:
        delta = t_job - t_iter
        print(f"- t_job − t_iter = **{fmt(delta)} s**")
        if t_boot_b and delta > 0:
            print(f"- **n\\* = t_boot_B / (t_job − t_iter) = {fmt(t_boot_b / delta, '.2f')}**")
        else:
            print("- **n\\* 정의 불가** — t_iter ≥ t_job (상각할 이득이 없다)")

    print("\n### 1d. 표본당 **고정 오버헤드** (미션을 뺀 값 — 이게 입자가 실제로 바꾸는 것)\n")
    print(
        "Arm A 의 잡당 wall 은 미션 길이에 지배되고 두 arm 의 verdict 구성이 다르므로 "
        "평균 wall 끼리의 비교는 사과와 오렌지다. 러너 자신의 `boot_summary` 가 "
        "컨테이너 안의 고정비(`boot_to_mission_s`)와 미션(`mission_s`)을 갈라 준다.\n"
    )
    print(
        "| # | A CLI wall s | A boot_to_mission s | A mission s | A 컨테이너 밖 s | "
        "B 반복 고정비 s | B mission s |"
    )
    print("|---|---|---|---|---|---|---|")
    a_fixed_in, a_fixed_out, b_fixed = [], [], []
    for row in a_rows:
        summary = boot_summary(arm_a_log(evid, row["job_id"]))
        b2m = summary.get("boot_to_mission_s")
        mis = summary.get("mission_s")
        total = summary.get("total_s")
        outside = (row["wall_s"] - total) if total is not None else None
        if b2m is not None:
            a_fixed_in.append(b2m)
        if outside is not None:
            a_fixed_out.append(outside)
        brow = b_by_early.get(row["index"], {})
        bt = brow.get("timings_s", {})
        bfix = None
        if bt:
            bfix = sum(
                bt.get(k) or 0.0
                for k in (
                    "restage",
                    "sut_realign",
                    "readiness",
                    "record_start",
                    "record_stop",
                    "evaluate",
                )
            )
            b_fixed.append(bfix)
        print(
            f"| {row['index']} | {fmt(row['wall_s'])} | {fmt(b2m)} | {fmt(mis)} | "
            f"{fmt(outside)} | {fmt(bfix)} | {fmt(bt.get('mission'))} |"
        )
    if a_fixed_in and b_fixed:
        a_total_fixed = statistics.mean(a_fixed_in) + statistics.mean(a_fixed_out or [0.0])
        b_mean_fixed = statistics.mean(b_fixed)
        print(
            f"| — | **mean** | **{fmt(statistics.mean(a_fixed_in))}** | | "
            f"**{fmt(statistics.mean(a_fixed_out) if a_fixed_out else None)}** | "
            f"**{fmt(b_mean_fixed)}** | |"
        )
        print(
            f"\n- 표본당 고정 오버헤드: Arm A **{fmt(a_total_fixed)} s** "
            f"(컨테이너 안 {fmt(statistics.mean(a_fixed_in))} + 밖 "
            f"{fmt(statistics.mean(a_fixed_out) if a_fixed_out else None)}) vs "
            f"Arm B **{fmt(b_mean_fixed)} s**"
        )
        delta_fixed = a_total_fixed - b_mean_fixed
        print(f"- 차이 (A − B) = **{fmt(delta_fixed)} s / 표본**")
        if t_boot_b and delta_fixed > 0:
            print(
                f"- 고정비 기준 손익분기 **n\\* = t_boot_B / Δ고정비 = "
                f"{fmt(t_boot_b / delta_fixed, '.2f')}**"
            )

    # ---------------------------------------------------------------- Table 2
    print("\n## Table 2 — 독립성 (표본별 A vs B)\n")
    print(
        "| # | A verdict | B verdict | 일치 | A reach s | B reach s | A closest m | "
        "B closest m | A path_len | B path_len | A coll | B coll | B artifacts |"
    )
    print("|---|---|---|---|---|---|---|---|---|---|---|---|---|")
    a_by = {r["index"]: r for r in a_rows}
    b_by = {r["index"]: r for r in b_rows}
    agree = 0
    for index in sorted(set(a_by) | set(b_by)):
        ar = (a_by.get(index) or {}).get("result") or {}
        br = (b_by.get(index) or {}).get("result") or {}
        av, bv = ar.get("verdict"), br.get("verdict")
        same = "=" if av is not None and av == bv else "≠"
        agree += int(av is not None and av == bv)
        a_close, a_reach = goal_proxy(ar)
        b_close, b_reach = goal_proxy(br)
        am, bm = ar.get("metrics", {}), br.get("metrics", {})
        brow = b_by.get(index) or {}
        mcaps = sorted(brow["bag"].glob("*.mcap")) if brow.get("bag") else []
        mp4_ok = (
            brow.get("mp4") is not None and brow["mp4"].is_file() and brow["mp4"].stat().st_size > 0
        )
        arts = f"mcap={'Y' if mcaps else 'N'} mp4={'Y' if mp4_ok else 'N'}"
        print(
            f"| {index} | {av} | {bv} | {same} | {fmt(a_reach)} | {fmt(b_reach)} | "
            f"{fmt(a_close)} | {fmt(b_close)} | {fmt(am.get('path_len_m'))} | "
            f"{fmt(bm.get('path_len_m'))} | {am.get('collision_count')} | "
            f"{bm.get('collision_count')} | {arts} |"
        )
    print(f"\n- verdict 일치: **{agree}/{len(set(a_by) | set(b_by))}**")

    if runs:
        print("\n### 2b. 대조군 — 같은 표본을 여러 번 (입자 간 차이 vs 런간 차이)\n")
        labels = list(runs)
        print("| # | " + " | ".join(labels) + " |")
        print("|---" * (1 + len(labels)) + "|")
        for index in sorted({i for r in runs.values() for i in r}):
            cells = [str((runs[name].get(index) or {}).get("verdict")) for name in labels]
            print(f"| {index} | " + " | ".join(cells) + " |")
        print("\n**쌍별 verdict 일치 (8 표본 중)**\n")
        print("| | " + " | ".join(labels) + " |")
        print("|---" * (1 + len(labels)) + "|")
        for a_name in labels:
            cells = []
            for b_name in labels:
                if a_name == b_name:
                    cells.append("—")
                    continue
                shared = set(runs[a_name]) & set(runs[b_name])
                hit = sum(
                    1
                    for i in shared
                    if (runs[a_name][i] or {}).get("verdict")
                    == (runs[b_name][i] or {}).get("verdict")
                )
                cells.append(f"{hit}/{len(shared)}")
            print(f"| **{a_name}** | " + " | ".join(cells) + " |")

    # ---------------------------------------------------------------- Table 3
    print("\n## Table 3 — VRAM 시계열\n")
    for label, gpu_rows, app_rows, interval in (
        ("Arm A", a_gpu, a_app, a_int),
        ("Arm B", b_gpu, b_app, b_int),
    ):
        lo, med, hi, count = idle_baseline(gpu_rows, app_rows)
        # Method (A) must be taken on SINGLE-TENANT ticks only: a GPU-wide peak sampled
        # while a foreign job shared the card measures two instances, not one.
        per_tick = collections.Counter(round(r[0], 3) for r in app_rows)
        solo = {t for t, n in per_tick.items() if n == 1}
        peak_app = max((r[3] for r in app_rows if round(r[0], 3) in solo), default=None)
        peak_gpu = max((r[3] for r in gpu_rows if round(r[0], 3) in solo), default=None)
        shared_ticks = sum(1 for t, n in per_tick.items() if n > 1)
        net = (peak_gpu - med) if (peak_gpu is not None and med is not None) else None
        print(
            f"- **{label}** (interval {interval}s; {len(gpu_rows)} gpu / {len(app_rows)} app "
            f"samples; {shared_ticks} ticks had a FOREIGN tenant and are excluded from the "
            f"peaks below): idle GPU-wide min/med/max = {fmt(lo,'.0f')}/{fmt(med,'.0f')}/"
            f"{fmt(hi,'.0f')} MiB over {count} idle ticks · **(B) per-PID peak "
            f"{fmt(peak_app,'.0f')} MiB** · (A) GPU-wide peak {fmt(peak_gpu,'.0f')} − idle med "
            f"= {fmt(net,'.0f')} MiB · 채택(larger of A/B) = "
            f"**{fmt(max(v for v in (peak_app, net) if v is not None), '.0f')} MiB**"
        )
    print("\n### Arm B — 반복별 per-PID peak (Q3 누수 판정; 창은 result.json mtime 기준)\n")
    print(
        "| # | 창 길이 s | per-PID peak MiB | per-PID 마지막 MiB | "
        "GPU-wide peak MiB | 외부 테넌트 |"
    )
    print("|---|---|---|---|---|---|")
    prev = b_t0
    for row in b_rows:
        end = row.get("end_ts")
        if end is None:
            continue
        own, _ticks, foreign = tenancy(b_app, prev, end)
        peaks = [r[3] for r in window(b_app, prev, end) if r[1] == own]
        gpeaks = [r[3] for r in window(b_gpu, prev, end)]
        note = "clean" if not foreign else "; ".join(f"pid {p} x{c}" for p, c in foreign.items())
        print(
            f"| {row['index']} | {end - prev:.1f} | {fmt(max(peaks) if peaks else None, '.0f')} | "
            f"{fmt(peaks[-1] if peaks else None, '.0f')} | "
            f"{fmt(max(gpeaks) if gpeaks else None, '.0f')} | {note} |"
        )
        prev = end

    # ---------------------------------------------------------------- Table 4
    print("\n## Table 4 — 결과 형태 (Q4)\n")
    print(
        f"- Arm A: `result.json` **{sum(1 for r in a_rows if r['result'])}/{len(a_rows)}** — "
        "잡 디렉토리 1개당 정확히 1개, 컨테이너 exit code 1개가 그 판정을 나른다."
    )
    print(
        f"- Arm B: `results/<i>/result.json` **{sum(1 for r in b_rows if r['result'])}/"
        f"{len(b_rows)}** — 컨테이너 **하나**가 N개를 낸다. verdicts = {b_timings.get('verdicts')} "
        f"→ 컨테이너 exit code는 **{b_summary.get('runner_exit_code')}** 하나뿐."
    )
    for row in b_rows:
        realign = row.get("sut_realign")
        if realign:
            print(
                f"  - iter {row['index']} realign: subs={realign.get('initialpose_subscribers')} "
                f"cleared={len(realign.get('costmaps_cleared', []))} "
                f"missing={realign.get('missing')} · "
                f"sim_time {fmt(row.get('sim_time_before_reset_s'))} → "
                f"{fmt(row.get('sim_time_after_reset_s'))} s · frames={row.get('video_frames')}"
            )
    if b_timings.get("error"):
        print(f"- Arm B 종료 오류: `{b_timings['error']}`")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
