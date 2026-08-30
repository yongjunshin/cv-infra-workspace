# docs/ — 문서 지도

두 층이다. **처음이면 [사용자 문서]의 위 두 개**를 순서대로 읽으면 된다(제품 소개·빠른 시작은
저장소 루트 [README.md](../README.md)).

## [사용자 문서] — 플랫폼을 설치하고 쓰는 사람

| 문서 | 무엇 |
|---|---|
| [installation.md](installation.md) | **설치 매뉴얼** — GPU 호스트 한 대에 올리는 최소 경로(요구사항 → 명시 동의 → 캐시 워밍 → 기동 → `cv-infra selftest` 검증 → 업그레이드 → 최초 설치 트러블슈팅) |
| [user-guide.md](user-guide.md) | **사용 매뉴얼** — 시나리오 계약·랜덤화·CLI·CI 배선·exit 0/1/2/3·리포트/회귀 읽기 |
| [releases.md](releases.md) | 릴리즈 대장 — *어떤 태그를 쓰고 어떤 태그를 쓰면 안 되는지*의 정본(릴리즈별 노트·정정 이력) |
| [version-compatibility.md](version-compatibility.md) | 버전 호환 매트릭스 — Action 태그 · CLI/패키지 · 계약 `apiVersion` **3축(서로 독립)** |

## [운영·내부] — 배포를 운영하거나 플랫폼을 손보는 사람

| 문서 | 무엇 |
|---|---|
| [deploy/README.md](deploy/README.md) | **C-2 배포 매뉴얼(정본, 793줄)** — 4단계 흐름 전체·단계별 스코프·일상 운영·다른 호스트로 재배포·트러블슈팅 13행·프로방넌스(실측 조건). `installation.md` 는 여기서 최초 설치 경로만 추출한 것이다 |
| [deploy/gpu-profiles.md](deploy/gpu-profiles.md) | 다른 GPU 카드로 이식 — `scripts/detect_gpu.sh` + `profiles/*`(파일 하나 추가 = 지원 추가, 코드 수정 0) |
| [deploy/plane-sync.md](deploy/plane-sync.md) | 배포 평면 스큐 — 태그를 옮겨도 이미지 안의 코드는 따라오지 않는다. 재빌드·스큐 게이트 절차 |
| [evidence-anchors.md](evidence-anchors.md) | 증적 만료 대장 — 코드 주석·테스트가 인용하는 측정 경로 중 **무엇이 만료됐는지**(*"측정 사실은 유효 · 재현 불가"*) |

## 코드 옆에 사는 README (제자리 유지)

| 경로 | 무엇 |
|---|---|
| [scripts/workstation_setup/](../scripts/workstation_setup/README.md) | 호스트 프로비저닝 내부 — apt·이미지 핀, sudo 드롭인, 드라이버 재정렬, self-hosted 러너 등록 |
| [scripts/measure/](../scripts/measure/README.md) | 측정 하네스 — 캐시 워밍·베이스라인·VRAM 표집(측정값을 스크립트에 굽지 않는다) |
| [scripts/isaac_smoke/](../scripts/isaac_smoke/README.md) | Isaac headless 스모크 + 컨테이너 경계 DDS 핸드셰이크 |
| [docker/selftest_stub/](../docker/selftest_stub/README.md) | 빌트인 stub SUT — `cv-infra selftest` 의 상대역(외부 SUT 0 의존) |

> 버전·수치·태그의 **정본은 언제나 코드·태그·프로파일**이고 문서는 사본이다 — 어긋나면 정본이 이긴다.
