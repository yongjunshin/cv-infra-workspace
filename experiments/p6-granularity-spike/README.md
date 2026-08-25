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

python3 analyze.py "$E" > "$E/tables.md"
```

## 지키는 것

* 프로덕션 캐시(`~/cv-infra-prod/cache-warm`)는 **읽기 전용 복사 원본**으로만 쓴다 —
  `:ro` 로 바인드하고, 잡/프로세스는 자기 사본에만 쓴다(supervisor 의 D-B 의미 그대로).
* 이미지는 **호스트에 이미 있는 것만** 쓴다. pull·재빌드·재태그 0.
* 동의는 **운영자 입력**이다(NEG-2). 스크립트는 값을 기본값으로 갖지 않고, 호스트
  동의 **레코드**를 `scripts/consent/check_consent.sh` 로 먼저 확인한다.
* 삭제는 이 스파이크가 만든 트리만, 전체 이름 지정으로(`discard_tree`). prune 없음.
