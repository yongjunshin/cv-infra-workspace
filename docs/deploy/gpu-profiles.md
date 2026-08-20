# 적응형 GPU 프로파일 — 다른 GPU 호스트에 배포하기 (C-2 매뉴얼)

> **범위**: C-2 배포 매뉴얼(`docs/deploy/`)의 두 번째 절. **GPU 적응 한 건**만 다룬다
> (`scripts/detect_gpu.sh` + `profiles/*`). 프로비저닝 전체·트러블슈팅은 여전히
> `scripts/workstation_setup/README.md`, 평면 스큐는 `plane-sync.md`.
> 요구사항 원문은 재서술하지 않고 ID로만 참조한다(REQ-DEPLOY-003 · NFR-DEPLOY-001/002).
>
> **C-2 고도(altitude)**: 자동 **감지 + 매뉴얼 선택**. 완전자동 installer 앱은
> 명시적 post-MVP(NFR-DEPLOY-002)이므로 여기에 없고, 앞으로도 만들지 않는다.

> ### ⚠ 2026-08-20 — 이 문서의 워크스테이션 증적 경로는 **죽은 링크**다 (의도적 만료)
> 아래에 인용된 `~/cv-infra-p2-out/**` · `~/cv-infra-ci/**` 는 **2026-08-20 프로덕션
> 컷오버에서 전량 삭제**됐다(CEO 결정 `p5c19` D-1 — *증거 부족이 아니라 판정된 만료*).
> **무엇이 있었는가**는 남아 있다: 전체 파일 목록 + 바이트 + `sha256`(17,432 파일) +
> store 스키마·행수가 메타 저장소의 증적 매니페스트에 있다. **재현은 불가**다 —
> *"존재했음"* 과 *"재현 가능"* 을 혼동하지 마라. 그 경로를 새 증거로 인용하지 말고,
> 필요하면 **다시 측정**하라. 운영 평면의 현 경로는 `~/cv-infra-prod/{store,out,cache-warm,cache-scratch}`.

## 1. 왜 프로파일인가

배포가 **어느 기계에서 돌지 코드가 알면 안 된다**. 그런데 스케줄러의 k 산출식
(`cv_infra/orchestrator/scheduler.py::compute_k`)은 `vram_per_instance`라는 **하드웨어
의존 숫자**를 입력으로 요구한다. 이 둘을 화해시키는 자리가 `profiles/`다:

```
scripts/detect_gpu.sh                       ← 코드. GPU 모델을 하나도 모른다.
   │  nvidia-smi --query-gpu=name           ← 런타임 질의(호스트명 분기 아님)
   ▼  각 프로파일이 선언한 match_name_pattern에 매칭
profiles/rtx_pro_6000.yaml                  ← 데이터. GPU 지식의 유일한 자리.
profiles/a100.yaml
profiles/rtx_4080.yaml
   │
   ▼  docker/.env 조각(fragment) 출력
CV_VRAM_PER_INSTANCE_MB=<측정값>            → compose → serve.py → compute_k(NVML 2차 가드)
```

**GPU 지원 추가 = `profiles/`에 파일 하나 추가.** 코드·compose 수정 0.
`profiles/**`가 GPU 모델 지식의 **유일한 sanctioned 위치**이며(M5 D9 /
`_meta/revision-decisions.md:103`), 그 밖의 실행 평면에 호스트·모델 리터럴이 들어오면
`tests/negative/test_deployment_identity_hardcoding.py`가 red를 낸다.

## 2. 배포 흐름에서의 자리

```
① scripts/workstation_setup/provision.sh          호스트 선결(드라이버 + 툴킷)
②' scripts/detect_gpu.sh >> docker/.env           ← 이 문서
②  scripts/consent/accept_eula.sh                 NVIDIA EULA(명시적, 1회)
③  docker compose -f docker/compose.yaml up -d --build
④  cv-infra selftest                              (M7 — 구현됨; CV_SELFTEST_SUT_IMAGE 필요)
```

`docker/.env`를 만든 **뒤**, `up` **전**에 돌린다. 출력은 그냥 `.env` 조각이라
`>> docker/.env`로 덧붙이거나, 눈으로 보고 손으로 옮겨도 된다(둘 다 지원되는 경로 —
매뉴얼 선택이 C-2 고도다).

## 3. 사용법

```bash
./scripts/detect_gpu.sh                 # 감지 → 조각을 stdout에, 진단 로그는 stderr에
./scripts/detect_gpu.sh >> docker/.env  # 실제 배포에서 쓰는 형태
./scripts/detect_gpu.sh --profile a100  # 감지를 건너뛰고 운영자가 직접 선택
./scripts/detect_gpu.sh --help
```

종료 코드: `0` 선택 성공 · `2` 사용법 오류 · `3` **선택 실패**(미지 GPU, 모델이 섞인
멀티 GPU, `nvidia-smi` 부재, 프로파일 손상/모호). **3은 fail-closed다** — 모르는 카드에
추측 예산을 내주는 것이 바로 이 게이트가 막으려는 조용한 오설정이라, 스크립트는 침묵하는
대신 거부하고 무엇을 하라고 말한다.

## 4. 미지 GPU를 만났을 때 (= 프로파일 추가)

1. 이름을 확인한다: `nvidia-smi --query-gpu=name --format=csv,noheader`
2. `profiles/<id>.yaml`을 만든다(기존 파일 복사). `id` + `match_name_pattern`만 채우고
   **`vram_per_instance_mb`는 비워 둔다.**
3. 그대로 배포한다. 값이 비면 NVML 2차 가드가 **꺼진 채로** 나가고
   `k = min(CV_MAX_CONCURRENT, render_cap)`가 된다 — 운영자 상한만으로 도는,
   `docker/.env.example`에 이미 문서화된 기본 계약이다.
4. **그 다음에 측정한다.** 잡 1개를 돌리며 per-PID VRAM을 표집:
   `nvidia-smi --query-compute-apps=pid,used_memory --format=csv`, 전체 프로세스 표(G+C)와
   교차확인, idle baseline 차감(DoD-P2-09 / DoD-P4-10 레시피).
5. 측정값을 `vram_per_instance_mb`에, **재현 경로를 `vram_per_instance_source`에 같이**
   적는다(G-24). source 없는 숫자는 스크립트가 **거부한다**(exit 3) — 앵커 없는 정량값이
   배포에 도달하지 못하게 하는 기계적 장치다.

**절대 하지 말 것**: 다른 카드의 숫자를 베껴 넣기. 낮으면 조용한 과다기동 → OOM,
높으면 카드 낭비다. 비워 두는 쪽이 언제나 정직하고 안전하다(CLAUDE §2-4).

## 5. 프로파일 문법 (지킬 것)

평평한 `key: value` 한 줄씩. 중첩·값 뒤 인라인 주석·값 안의 `: ` 금지.
**파서가 둘**이기 때문이다 — `scripts/detect_gpu.sh`(sed/awk. 갓 프로비저닝한 호스트에
python이 없을 수 있다)와 테스트(pyyaml). `tests/test_deploy_gpu_profiles.py`가 두 파서가
**같은 값을 읽는지** 단정한다. 중첩을 쓰면 셸 쪽만 조용히 깨진다.

| 키 | 소비자 | 비고 |
|---|---|---|
| `id` | 선택 결과·파일명 | `profiles/<id>.yaml`과 일치해야 함 |
| `match_name_pattern` | `detect_gpu.sh`의 매칭(ERE, 대소문자 무시) | 토큰 기반으로 느슨하게 — SKU 표기 변형을 다 받는다 |
| `vram_per_instance_mb` | `CV_VRAM_PER_INSTANCE_MB` → `compute_k` | **실측만**. 없으면 빈 값 |
| `vram_per_instance_source` | 출력 조각의 앵커 주석 | 값이 있으면 **필수**(G-24) |

필드를 늘리고 싶으면 **소비자를 먼저 만들어라.** 아무도 읽지 않는 필드는 다음 사람이
사실로 믿는 거짓말이 된다. 지금 프로파일에 **없는 것**과 그 이유:

- `max_concurrent` — 운영자 권한 상한(LOCKED §7.4)이지 하드웨어 속성이 아니다.
  실측된 처리량-최적 k(이 HW·이 워크로드에서 ≈4, `nfr-measurement-notes.md` P4c5)는
  **관측이지 목표치가 아니다** — 프로파일이 운영자 정책을 덮어쓰면 안 된다.
- `texture_streaming_budget_cap` — 소비자가 `cv_infra/runner/sim_runtime.py`
  (`TEXTURE_BUDGET_FRACTION`, M2 소유)라 배선하려면 M2 변경이 필요하다. M5가 여기에
  값을 심어도 아무도 읽지 않는다.

## 6. 이 문서가 **주장하지 않는** 것 (정직 표기)

- ~~**라이브 GPU에서의 적응 선택은 아직 관찰되지 않았다.**~~ **해소(2026-08-06, p5c12 —
  SSH 단일 채널·워크스테이션 실 카드 1회).** 그 전까지 `detect_gpu.sh`는 CPU에서 stub
  `nvidia-smi`로만 실증됐고(`tests/test_deploy_gpu_profiles.py`), 실제 카드가 내놓는
  **정확한 문자열**을 이 저장소가 캡처한 적이 없어 패턴을 토큰 기반으로 느슨하게 두었다.
  이제 캡처됐다:

  | 항목 | 실측값 |
  |---|---|
  | `nvidia-smi --query-gpu=name` (1 디바이스) | `NVIDIA RTX PRO 6000 Blackwell Workstation Edition` |
  | 드라이버 / `memory.total` | `580.159.03` / `97887 MiB` |
  | 선택된 프로파일 | `rtx_pro_6000` (매치 1건 — 모호성 0, `a100.yaml`은 비발화) |
  | 방출 조각 | `CV_VRAM_PER_INSTANCE_MB=6000` + `# measured` 앵커 주석 · exit **0** |

  즉 느슨한 토큰 패턴 `RTX[ _-]?PRO[ _-]?6000`이 **접두 `NVIDIA `와 접미 `Blackwell
  Workstation Edition`을 모두 통과**했다 — 정확한 제품 문자열을 몰라도 SKU 표기 변형을
  받는다는 설계 의도가 실 카드에서 확인된 것이다. 방출된 `6000`은 상주 serve가 NVML
  가드로 이미 쓰고 있는 값과 같다(`serve-config`의 `vram_per_instance_mb: 6000.0`) —
  적응 경로가 라이브에서 검증된 구성과 **같은 숫자**로 수렴한다.
  증적: 워크스테이션 `~/cv-infra-p2-out/p5c12/detect-gpu/`(`01-nvidia-smi.txt` ·
  `02-fragment.env` · `03-stderr.log` · `04-exit.txt`).
- **A100은 우리가 가진 적이 없다.** `profiles/a100.yaml`은 구조만 있고 숫자는
  `TBD(미실측)`이다. 렌더 경로(D-A)·MIG(LOCKED §18 미사용)도 그 카드에서 미검증이다.
- **RTX 4080(두 번째 배포 호스트)**: `profiles/rtx_4080.yaml`이 p5c17 T1에 추가됐다.
  이름 문자열은 **실측 캡처**(`NVIDIA GeForce RTX 4080` · 16376 MiB · sm 8.9 · CPU 전용
  질의)이지만 `vram_per_instance_mb`는 **TBD(미실측)** — 잡을 돌려야 나오는 값이라
  같은 사이클 T4 몫이다. 그때까지 이 호스트에서 NVML 2차 가드는 **OFF**로 흐르고
  k = min(`CV_MAX_CONCURRENT`, render_cap)이다(스크립트가 stderr로 크게 알린다).
