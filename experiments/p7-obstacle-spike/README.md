# p7c1 — W0 장애물 스파이크 (자산 열거·콜라이더·yaw·풀+파킹·비용)

> **THROWAWAY.** `experiments/**` 는 제품 코드가 아니다 — `cv_infra/**` 는 이 실험을 위해
> **한 줄도 수정되지 않았고, 한 줄도 import 되지 않는다**(태스크 §1: 스파이크 독립).
> 정본 프로토콜 = `implementation-plan/p7-obstacles-plan.md` 부록 B **§B8**(사전등록 게이트),
> 결과 = `agent-comms/reports/runner-2026-08-28-p7c1-t1-obstacle-spike-w0.md`.

프로덕션에 이미 있는 레시피는 **미러 + 포인터**로만 재현한다(재구현·import 둘 다 안 함):
SimulationApp-first + EULA 가드(`sim_runtime`), 텍스처 예산 캡(`simulation_app_launch_config`),
섀시 한정 `PhysxContactReportAPI`(`telemetry.PhysicsTelemetrySampler`), FU-17 렌더 프로덕트
활성 walk(`enable_sensor_render_products`), 번들 jazzy rclpy 사이트(`ros_bridge`).

## 파일

| 파일 | 하는 일 |
|---|---|
| `spike.py` | 컨테이너 안 단일 진입점. 게이트 = `enumerate` ⓐ / `cost` ⓔ / `collider` ⓑ / `yaw` ⓒ / `pool` ⓓ / `scan` ⓓ(2-arm 중 1 arm) |
| `run_gate.sh` | 게이트 배치 1개를 러너 이미지에서 실행(동의 게이트 · 배타 창 · 캐시 루트 · 샘플러 · 증적 디렉토리) |
| `common.sh` | 노브 · 동의 · 배타 GPU 창 · 캐시 6바인드(supervisor verbatim) · warm/cold 캐시 루트 · 이미지 LD 경로 |
| `vram_sampler.sh` | p6c1에서 **verbatim 복사**(profiles 레시피): per-PID 0.5 s + GPU-wide 동시 표집 |
| `analyze.py` | 호스트측 리덕션 — VRAM OLS(반복 3부터)+계단 분해, `/scan` 2-arm 비교, 게이트별 표 |

## 실행 (워크스테이션, 배타 GPU 창)

```bash
cd ~/cv-infra-exp-p7spike/experiments/p7-obstacle-spike
E=~/cv-infra-p7c1-evidence/w0
export ACCEPT_EULA=Y PRIVACY_CONSENT=Y          # 운영자 입력(NEG-2) — 스크립트 기본값 없음

# ⓐ + ⓔ(웜): 자산 열거 → 후보 probe → assets.json
CV_SPIKE_SUBDIRS=/Isaac/Props CV_SPIKE_MAX_DIRS=400 bash run_gate.sh enumerate enumerate

# ⓔ(콜드): 빈 캐시 루트로 같은 자산 재측정
CV_SPIKE_CACHE=cold CV_SPIKE_CACHE_LABEL=cold \
  CV_SPIKE_ASSETS_SRC=$E/enumerate/out/assets.json bash run_gate.sh cost-cold cost

# ⓑ + ⓒ: 콜라이더 격자 + yaw (한 부팅)
CV_SPIKE_ASSETS_SRC=$E/enumerate/out/assets.json bash run_gate.sh collider collider yaw

# ⓓ: 풀 n=12 순환 (샘플러 창 = 이 컨테이너 수명)
CV_SPIKE_ASSETS_SRC=$E/enumerate/out/assets.json CV_SPIKE_N=12 \
  CV_SPIKE_MULTIPLICITY=2,3,1 bash run_gate.sh pool pool

# ⓓ 파킹 비가시 2-arm (/scan) — arm 마다 별도 프로세스
CV_SPIKE_ASSETS_SRC=$E/enumerate/out/assets.json CV_SPIKE_ARM=with_pool bash run_gate.sh scan-with scan
CV_SPIKE_ARM=no_pool bash run_gate.sh scan-without scan

python3 analyze.py "$E" \
  --scan-a "$E/scan-with/out/scan_with_pool.json" \
  --scan-b "$E/scan-without/out/scan_no_pool.json" > "$E/tables.md"
```

⚠ **측정 중 push 금지(G-101).** 이 저장소 `ci.yml` 은 `on: push` 이고 tier-2 self-test 가
**같은 GPU 호스트의 self-hosted 러너**에서 돈다. 이 스파이크는 워크스테이션에 **rsync** 로만
운송되므로 창은 구성적으로 깨끗하지만, `analyze.py` 가 창마다 외부 테넌트 PID 를 라벨한다
(발견해도 **kill 금지** — 라벨하고 재측정).

## 지키는 것

* 프로덕션 캐시(`~/cv-infra-prod/cache-warm`)는 `:ro` 복사 원본으로만. 콜드 측정은 **신규 빈**
  캐시 루트(`$E/<run>/cache`)에서만.
* 이미지는 호스트에 **이미 있는 것만**. pull·재빌드·재태그·prune 0.
* 동의는 운영자 입력(NEG-2). 스크립트는 값을 기본으로 갖지 않고, 호스트 동의 **레코드**를
  `scripts/consent/check_consent.sh` 로 먼저 확인한다.
* 자산 경로는 **열거 결과에서만** 온다(G-28) — `spike.py` 의 카테고리 키워드는 열거 결과를
  *고르는* 필터일 뿐, 경로를 만들지 않는다.
