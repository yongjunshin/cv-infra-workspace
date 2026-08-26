# p6c1 — 잡 입자 스파이크 (n job vs 1 job n repeat)

> **THROWAWAY.** `experiments/**` 는 제품 코드가 아니다 — `cv_infra/**` 는 이 실험을
> 위해 **한 줄도 수정되지 않았다**. 계획 = `agent-comms/cycle-plans/2026-08-25-p6c1-granularity-spike.md`,
> 결과 = `agent-comms/reports/runner-2026-08-25-p6c1-granularity-spike.md`.

같은 8개 **구체화 시나리오**를 두 가지 실행 입자로 돌려 비용·독립성·VRAM·결과형태를 잰다.

| | Arm A (C-1) | Arm B (C-2) |
|---|---|---|
| 입자 | 잡 8개 (직렬, k=1) | 잡 1개 안에서 8회 직렬 반복 |
| 경로 | 기존 `cv-infra run` **그대로** (신규 플랫폼 코드 0) | `loop_runner.py` (= `runner/main.py::run()` 복사·개조) |
| Isaac 부팅 | 8회 | 1회 |
| SUT | 잡마다 새 컨테이너 (M3) | 컨테이너 1개, 반복마다 nav 상태만 ROS 로 재정렬 |
| 산출 | `result.json` × 8 (잡 디렉토리별) | `results/<i>/result.json` × 8 (한 컨테이너 안) |

## 파일

| 파일 | 하는 일 |
|---|---|
| `derive.py` | 고정 seed(`DERIVE_SEED`) → fixture 기반 구체화 YAML 8개. `--self-check` = 2회 생성 diff 0 + 8개 전부 contract 로더 통과 |
| `scenarios/sample_0N.yaml` | 그 산출물(커밋됨). 두 arm 의 **동일 입력** |
| `make_specs.py` | YAML 8 → 정본 JOB_SPEC 배열 (**생산 코드 재사용**: `load_request` + `cli.main._job_spec_from_request`) |
| `loop_runner.py` | Arm B 루프 러너. 컨테이너에 bind-mount + `PYTHONPATH` 로 주입 (이미지 재빌드 0) |
| `common.sh` | 공통 노브 · 동의 게이트 · 배타 GPU 창 확인 · 캐시 시딩(root 컨테이너) |
| `arm_a.sh` / `arm_b.sh` | 두 arm 드라이버 |
| `vram_sampler.sh` | per-PID 0.5 s + GPU-wide 동시 표집 (`profiles/` 레시피 그대로) |

## 실행 (워크스테이션, 배타 GPU 창)

```bash
cd ~/cv-infra-exp-p6spike/experiments/p6-granularity-spike
E=~/cv-infra-p6c1-evidence

bash vram_sampler.sh "$E/arm_a/vram_0.5s.csv" "$E/arm_a/crosscheck" 0.5 &  SAMPLER=$!
ACCEPT_EULA=Y PRIVACY_CONSENT=Y bash arm_a.sh "$E"
kill $SAMPLER

bash vram_sampler.sh "$E/arm_b/vram_0.5s.csv" "$E/arm_b/crosscheck" 0.5 &  SAMPLER=$!
ACCEPT_EULA=Y PRIVACY_CONSENT=Y bash arm_b.sh "$E"
kill $SAMPLER

# 표 4개 (같은 표본을 여러 번 잰 런들을 --run 으로 매트릭스에 넣는다)
python3 analyze.py "$E" --run A2="$E/pass2":a --run B2="$E/pass2":b > "$E/tables.md"
```

⚠ **측정 중에는 push 하지 마라.** 이 저장소의 `ci.yml` 은 `on: push` 이고 tier-2
(`selftest`)가 **같은 GPU 호스트의 self-hosted 러너**에서 돈다. p6c1 실측에서 exp 브랜치
push 3회가 각각 self-test 잡을 이 카드 위에 올렸고, 그중 하나가 Arm A 1번 잡의 창을
침범했다(측정: 같은 순간 Isaac PID 2개). `analyze.py` 는 창마다 외부 테넌트를 이름으로
찍어 주며, 오염된 행은 평균에 넣지 않는다.

## 지키는 것

* 프로덕션 캐시(`~/cv-infra-prod/cache-warm`)는 **읽기 전용 복사 원본**으로만 쓴다 —
  `:ro` 로 바인드하고, 잡/프로세스는 자기 사본에만 쓴다(supervisor 의 D-B 의미 그대로).
* 이미지는 **호스트에 이미 있는 것만** 쓴다. pull·재빌드·재태그 0.
* 동의는 **운영자 입력**이다(NEG-2). 스크립트는 값을 기본값으로 갖지 않고, 호스트
  동의 **레코드**를 `scripts/consent/check_consent.sh` 로 먼저 확인한다.
* 삭제는 이 스파이크가 만든 트리만, 전체 이름 지정으로(`discard_tree`). prune 없음.

---

## p6c2 — VRAM 미리셋 원인 규명 (같은 하네스의 2차 사용)

p6c1 이 남긴 미해결 ①(반복당 per-PID VRAM 단조 증가, n=8 내 미포화)을 **성분 분해**로
좁히고, 설치된 Isaac Sim 5.1.0 의 **기존** 리셋·청소 API 로 해소한다. 결정(C-2 채택)은
이미 났으므로 이것은 채택 심사가 아니라 **채택된 구조의 비효율 해소**다.

### 추가 파일

| 파일 | 하는 일 |
|---|---|
| `arm_b2.sh` | 변형 1개 실행 = 샘플 순환 n개 + 토글 + **자기 소유 VRAM 샘플러** |
| `vram_slope.py` | 변형별 반복당 VRAM 기울기(반복수·**sim-초** 양쪽 정규화) + 반복별 상세 |

### 토글 (전부 기본 OFF = p6c1 동작 그대로)

```
CV_SPIKE_ABLATE   obstacle_move | no_obstacle | no_sensors | no_video | no_bag
                  no_mission | no_realign | no_restage        (Phase 1 절제)
CV_SPIKE_CLEANUP  orphan_materials | gc | mesh_cache          (Phase 2 표적 청소)
```

`no_*` 중 다수는 **진단용**이다(그 변형은 측정 대상 자체를 바꾼다) — 프로덕션 후보는
`obstacle_move` 와 `CV_SPIKE_CLEANUP` 쪽뿐이다.

### 실행

```bash
cd ~/cv-infra-exp-p6spike/experiments/p6-granularity-spike
E=~/cv-infra-p6c1-evidence            # p6c2/ 하위에 변형별로 쌓인다

CV_SPIKE_N=24 ACCEPT_EULA=Y PRIVACY_CONSENT=Y bash arm_b2.sh base
CV_SPIKE_N=24 CV_SPIKE_ABLATE=obstacle_move ACCEPT_EULA=Y PRIVACY_CONSENT=Y \
  bash arm_b2.sh obstacle_move
…
python3 vram_slope.py "$E" --csv "$E/p6c2/rows.csv" > "$E/p6c2/slopes.md"
```

⚠ 위의 "측정 중 push 금지"(G-101)는 p6c2 에도 그대로 적용된다 — 코드는 **측정 창을 열기
전에** 전부 push 하고, 창이 닫힐 때까지 push 하지 않는다.
