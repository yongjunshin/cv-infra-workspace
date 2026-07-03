# Isaac Smoke + DDS Handshake (M2 / Phase 1)

Host-side wrappers + one in-container script that close **DoD-P1-04** (headless
`SimulationApp` smoke with a non-black off-screen frame) and **DoD-P1-05** (container-
boundary DDS handshake with a separate `ros:jazzy` container). No custom image is
built; everything runs on the pinned `isaac-sim:5.1.0` base (`common.sh` pins).

| File | Role |
|---|---|
| `headless_smoke.py` | runs INSIDE the container via `/isaac-sim/python.sh` (`--mode smoke\|handshake`) |
| `run_smoke.sh` | DoD-P1-04 wrapper: boot, GPU-PID evidence, frame + non-black assertion |
| `run_dds_handshake.sh` | DoD-P1-05 wrapper: `/clock` forward, `/cmd_vel` reverse, endpoints, SHM-off |
| `fastdds_udp_profile.xml` | Fast DDS UDPv4-only profile (SHM transport disabled, R8) |

## Run (on the workstation)

```bash
rsync -a scripts/ etri6000:~/cv-infra-p1-smoke/scripts/     # from the repo checkout
ssh etri6000
CV_EULA_CONSENT=yes bash ~/cv-infra-p1-smoke/scripts/isaac_smoke/run_smoke.sh
CV_EULA_CONSENT=yes bash ~/cv-infra-p1-smoke/scripts/isaac_smoke/run_dds_handshake.sh
```

Serial contract: the handshake refuses to run until the smoke has passed
(`out/last_smoke_pass`). Evidence lands under `~/cv-infra-p1-smoke/out/<run-id>/`
(`container.log`, `nvidia_smi_evidence.txt`, `frame_0001.*`, `clock_echo.txt`,
`cmd_vel_info.txt`, `*_dev_shm.txt`, ...).

## EULA (NEG-2, decision 2026-07-03-p1-eula-runtime-consent)

No committed file contains the acceptance literal (repo grep = 0). The wrappers
**refuse to boot** without the per-run operator input `CV_EULA_CONSENT=yes`; the
runtime env is synthesized from that input and injected with `-e` for that run only.
`headless_smoke.py` re-checks the env before instantiating `SimulationApp` (exit 3).

## Measured layout notes (2026-07-03, etri6000, isaac-sim:5.1.0 @ locked digest)

* The 5.1.0 image runs as **uid 1234 `isaac-sim`, HOME=`/isaac-sim`** (NOT root /
  `/root/.cache` as the pre-measurement R2 note guessed). Cache mounts follow the
  measured home; host scaffold dirs are chown-ed to 1234 via the pinned image itself.
* Entrypoint is `runheadless.sh` -> `license.sh` (checks `$ACCEPT_EULA`) -> streaming.
  The wrappers bypass it with `--entrypoint`, so **livestream is off** by construction
  and the EULA gate is enforced by our wrapper + in-script guard instead.
* `NVIDIA_DRIVER_CAPABILITIES=all` is baked into the image env; the wrapper still
  passes it explicitly (R19) and records the effective container env as evidence.

## [VERIFY] results

* **internal Jazzy `LD_LIBRARY_PATH`**: the official `/isaac-sim/setup_ros_env.sh`
  sets `ROS_DISTRO=jazzy` + appends `exts/isaacsim.ros2.bridge/jazzy/lib` — but only
  when `ROS_DISTRO` is *unset* (so we never pass `-e ROS_DISTRO`). Default mode
  (`preset`) sources it before `python.sh` (LOCKED: env before interpreter boot)
  -> handshake PASS. **Measured (2026-07-03): manual pre-set is REQUIRED** —
  `CV_ROS_ENV_MODE=auto` (helper skipped) has `enable_extension` return True but
  the bridge is non-functional: OmniGraph registers zero ros2 node types
  (`Could not create node ... 'isaacsim.ros2.bridge.ROS2PublishClock'`).
* **`--shm-size`** (`CV_SMOKE_SHM_SIZE=1g`): measured in-run usage 28K/1.0G (1%)
  for the smoke scene — 1g is ample; DDS runs over UDPv4 (SHM transport disabled)
  so /dev/shm stays near-empty. Kept at 1g for Kit headroom (docker default 64m).
* **Multicast discovery on the bridge net: WORKS** (measured) — `/clock` echoed by
  the ros:jazzy peer via default multicast discovery (~437 msg/s observed); the
  `initialPeers` unicast fallback in `run_dds_handshake.sh` was NOT needed.
* **Frame capture**: plain `world.step(render=True)` alone never flushed annotator
  data (measured: empty after 80 steps); `rep.orchestrator.step()` is required to
  trigger the replicator capture (headless_smoke.py does both).
