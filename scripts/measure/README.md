# `scripts/measure/` — M5 measurement harness (Phase 2)

Host-side glue that **measures and records** the Phase 2 GPU numbers. It never bakes a
measured value into a script (CLAUDE §2-4): the scripts sample, preserve raw evidence,
and derive summaries. The actual numbers land in `implementation-plan/nfr-measurement-notes.md`
(PM), never here.

Everything here is **CPU-authored**; it is **executed on the workstation (GPU)** in
Wave 2. Nothing here boots Isaac at author time — the `bash -n` / lint gates are
structure-only.

## Ground rules baked into every script

- **EULA (NEG-2 / LOCKED §8)** — any script that *boots Isaac* (`warm_cache.sh`,
  `isaac_baseline_run.sh`) refuses without per-run operator consent (`CV_EULA_CONSENT=yes`)
  and **exits 3**; it synthesises the acceptance env at run time. `observe_cv_infra_run.sh`
  boots nothing (it watches a `cv-infra run` that carries the operator's own consent), so
  it has **no** gate. No EULA acceptance literal is committed anywhere.
- **No `sudo`** — T0 measured plain `docker` is enough (`etri` is in the docker group).
  File permissions for the uid-1234 (`isaac-sim`) app go through a **docker root helper**
  (`--user 0`), never host sudo (G-15).
- **Host-absolute cache root** — the cache root is a host absolute path (sibling-container
  safety, D-O) and is what the supervisor reads as `CV_ISAAC_CACHE_ROOT` (D-1). The
  supervisor does **not** create/chown it (loud `ValueError` on a missing root); that is
  `warm_cache.sh`'s job.
- **Steady-state, preserved (G-18)** — VRAM is sampled continuously to a CSV; the pmon
  burst + full `nvidia-smi` process table are captured **inside the steady window** and
  preserved raw. Summaries (peak/steady/median) are derived from the raw, which is never
  deleted. `CV_STEADY_K` / `CV_STEADY_EPS` are the *judgement window* that decides *when*
  to sample — they are **not** measurement targets.

## Files

| file | role |
|---|---|
| `common.sh` | shared pins + EULA gate + G-15 tree provisioning + sampling helpers. Sources `../workstation_setup/common.sh` (reuses `log`/`die`/`require_cmd` + image/cache pins — no duplication) and adds only the measure-specific values. |
| `warm_cache.sh` | cache-tree lifecycle: `provision` / `warm` / `strip-gpu` (idempotent). Boots `warm_scene.py` to fill the closure. |
| `warm_scene.py` | minimal in-container scene loader (boot → `open_stage` → pump until net-idle). Committed port of the T0 probe's proven `openstage` warming path. **Not** the M2 runner runtime — no physics/oracle/ROS/telemetry. |
| `isaac_baseline_run.sh` | **FU-12** single-instance baseline (cold/warm boot wall + steady-state per-PID VRAM), with the QA cycle-2 Issue-1 fix. |
| `observe_cv_infra_run.sh` | **DoD-P2-09** per-job observer: attaches to a running `cvj-*-runner`, collects per-PID VRAM / wall / received bytes. |

## The D-1 canonical cache mounts (verbatim — do not edit)

```
{CV_ISAAC_CACHE_ROOT}/cache/kit          -> /isaac-sim/kit/cache              rw
{CV_ISAAC_CACHE_ROOT}/cache/home         -> /isaac-sim/.cache                 rw
{CV_ISAAC_CACHE_ROOT}/cache/computecache -> /isaac-sim/.nv/ComputeCache       rw
{CV_ISAAC_CACHE_ROOT}/logs               -> /isaac-sim/.nvidia-omniverse/logs rw
{CV_ISAAC_CACHE_ROOT}/data               -> /isaac-sim/.local/share/ov/data   rw
{CV_ISAAC_CACHE_ROOT}/documents          -> /isaac-sim/Documents              rw
```

### Two kinds of cache — warmed/shared differently

- **Portable asset cache** — the downloaded scene dependency closure lives under
  `cache/home` (`omni.client`'s `.cache/ov/client`). It is host/GPU independent: warm it
  once, mount it, and every job serves the scene from disk instead of re-hitting S3
  (D-1 opt B; T0 proved the second load is essentially free — see
  `reports/deployment-2026-07-09-fu16-probe.md`, do not restate the numbers here).
- **GPU-derived caches** — the Kit RTX/shader cache (`cache/kit`), `ComputeCache`
  (`cache/computecache`), and the GL shader cache (`.cache/nvidia`) are **regenerated per
  GPU**. They are what `strip-gpu` deletes to isolate the shader/compute-compile term.

## P2-09 cold/warm attribution — procedure

Goal: split the **cold penalty** of a carter job into **(a) asset download** and
**(b) shader/compute compile**. cycle-2's "cold penalty is dominated by shader compile"
was a *smoke-scene* observation (D-1 side-correction); the dominant term for the carter
scene must be **re-measured**, not assumed. Do **not** pre-write the conclusion.

Prepare three cache trees, then run the same carter job against each while observing:

| condition | tree prep | expectation to test |
|---|---|---|
| **cold-fresh** | `warm_cache.sh <root_A> provision` (empty) | high RX (download) **and** shader compile |
| **cold-assets-warm-shaders** | warm `<root_B>` fully, then `warm_cache.sh <root_B> strip-gpu` | low RX (assets from disk), shaders/compute recompile |
| **warm-all** | `warm_cache.sh <root_C> warm` | low RX, no recompile |

For each condition (terminal A launches the job with `CV_ISAAC_CACHE_ROOT` pointing at
that tree; terminal B observes):

```bash
# terminal A — operator launches the real job (supervisor mounts the D-1 caches)
CV_ISAAC_CACHE_ROOT=<root_X> CV_EULA_CONSENT=yes cv-infra run <scenario> --out <out>
# terminal B — attach + record
bash scripts/measure/observe_cv_infra_run.sh <run-name> <condition-label>
```

Then read the three `SUMMARY.txt` files. With
`RX` = container received bytes and `wall` = runner wall:

- **asset-download term** ≈ `RX(cold-fresh) − RX(warm-all)`, and its wall share ≈
  `wall(cold-fresh) − wall(cold-assets-warm-shaders)` (the leg where only downloading differs).
- **shader/compute-compile term** ≈ `wall(cold-assets-warm-shaders) − wall(warm-all)` (RX
  is ~equal on both legs, so the wall delta is compile-bound).

The two terms should roughly reconstruct `wall(cold-fresh) − wall(warm-all)`; any residual
is boot/settle overhead. Record all three raw sets — **do not** delete raw or hardcode the
resulting split into any script.

## Evidence layout

```
$CV_MEASURE_OUT_ROOT/<run-name>/
  host/ (baseline) or ./ (observe)
    SUMMARY.txt                       derived: exit, wall, per-PID VRAM peak/steady/median, RX
    vram_compute_apps_samples.csv     RAW per-PID VRAM time series (preserved)
    docker_stats_netio.csv            RAW container NetIO/mem time series (preserved)
    pmon_steady.txt                   RAW nvidia-smi pmon burst, captured IN the steady window
    nvidia_smi_full_steady.txt        RAW full G+C process table, captured IN the steady window
    container.log / runner.log,sut.log
    gpu_mem_idle_before.txt           (baseline only) idle VRAM before boot
```

## Reproduction (Wave 2, on the workstation)

```bash
export CV_ISAAC_CACHE_ROOT=/home/etri/cv-infra-cache/base   # host absolute (D-1)
export CV_MEASURE_IMAGE=cv-infra-runner:<image-5호-tag>     # Wave-2 built runner image

# 1) warm the shared tree once (fills the portable asset closure + GPU caches)
CV_EULA_CONSENT=yes bash scripts/measure/warm_cache.sh "$CV_ISAAC_CACHE_ROOT" warm

# 2) FU-12 single-instance baseline (steady-state VRAM, preserved evidence)
CV_EULA_CONSENT=yes bash scripts/measure/isaac_baseline_run.sh baseline-01 60

# 3) P2-09 cold/warm attribution — three trees, three observed runs (see procedure above)
```

## Config knobs (env-overridable; defaults are structure, not targets)

`CV_MEASURE_IMAGE` (runner image · Wave-2 override), `CV_MEASURE_SCENE_REL` (closure to
warm — match the scenario under test), `CV_ISAAC_CACHE_ROOT` (host-absolute cache root),
`CV_MEASURE_OUT_ROOT` (evidence root), `CV_MEASURE_NET`, `CV_MEASURE_SHM_SIZE`,
`CV_MEASURE_TIMEOUT_S`, `CV_OBSERVE_WAIT_S`, `CV_STEADY_K` / `CV_STEADY_EPS` /
`CV_SAMPLE_INTERVAL_S` (steady-state judgement window — **not** a measurement target).
