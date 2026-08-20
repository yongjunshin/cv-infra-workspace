# 배포 평면 동기화 — 릴리즈 재태그 시 런타임 평면 동기화 절차 (G-43)

> **범위(seed)**: 이 문서는 C-2 배포 매뉴얼(`docs/deploy/`)의 **첫 시드**이며,
> **G-43 "두 배포 평면 스큐" 절차 한 건에만** 한정한다. 설치·프로비저닝·적응형
> 프로파일·트러블슈팅 전반을 담는 **전체 C-2 매뉴얼은 아직 아니다**(과설계 금지 —
> 후속 사이클에서 확장). 요구사항 원문은 재서술하지 않고 ID로만 참조한다
> (REQ-DEPLOY-001·003, NFR-DEPLOY-001~003; 정본 = deployment 그룹 명세).


> ### ⚠ 2026-08-20 — 이 문서의 워크스테이션 증적 경로는 **죽은 링크**다 (의도적 만료)
> 아래에 인용된 `~/cv-infra-p2-out/**` · `~/cv-infra-ci/**` 는 **2026-08-20 프로덕션
> 컷오버에서 전량 삭제**됐다(CEO 결정 `p5c19` D-1 — *증거 부족이 아니라 판정된 만료*).
> **무엇이 있었는가**는 남아 있다: 전체 파일 목록 + 바이트 + `sha256`(17,432 파일) +
> store 스키마·행수가 메타 저장소의 증적 매니페스트에 있다. **재현은 불가**다 —
> *"존재했음"* 과 *"재현 가능"* 을 혼동하지 마라. 그 경로를 새 증거로 인용하지 말고,
> 필요하면 **다시 측정**하라. 운영 평면의 현 경로는 `~/cv-infra-prod/{store,out,cache-warm,cache-scratch}`.
## 왜 (세 평면)

플랫폼은 릴리즈 태그가 **함께 옮기지 못하는 세 배포 평면**으로 배송된다
(GOTCHAS **G-43** + p5c8·p5c12 보강):

| 평면 | 무엇 | 무엇으로 갱신되나 |
|---|---|---|
| **① YAML 평면** | reusable workflow / composite action (`.github/workflows/verify.yml` · `actions/verify`) | 릴리즈 태그 `@vN` 이동으로 **자동** 갱신(소비자 `uses: …@vN` 핀) |
| **② 런타임 평면** | GPU 잡이 **실제 실행하는 코드** = 러너 venv의 editable install + **사전 설치된 serve/CLI 컨테이너** | **체크아웃 + 재설치 + 컨테이너 재기동으로만** 갱신 |
| **②' 제어 평면 이미지** | p5c14부터 제어 평면은 **컨테이너**다 — 그 안의 `cv_infra`는 `docker/orchestrator/Dockerfile`이 **구운 wheel** | **리빌드로만**(`up -d --build`). **p5c15부터 스큐 게이트가 이 평면도 본다**(`--orchestrator-image` 필수) |
| **③ 러너 이미지 평면** | 잡 컨테이너 **안에서** 실행되는 wheel(`cv-infra-runner:<tag>`에 박힘) | **이미지 리빌드로만** 갱신 — ①②를 아무리 옮겨도 움직이지 않는다([아래 절](#-러너-이미지-평면--잡-컨테이너-안의-wheel)). **p5c13부터 스큐 게이트가 이 평면도 본다**(`--image` 필수) |

GPU 잡은 설계상(R10) `actions/checkout`을 **하지 않는다** → 소비자가 실행하는 코드는
러너에 **사전 설치된 패키지**이지 태그가 가리키는 코드가 아니다. 따라서 `@vN`을 새
커밋으로 **재태그하면 ①만 움직이고 ②③은 옛 코드에 머문다** → 평면들이 **조용히 스큐**
되고, 라이브 leg가 stale 코드를 실행한다. G-43은 이 갭이 **이미 재발했음**을 실측했다.

**측정 앵커(2026-07-24, SSH 읽기전용 단일 채널)**:

| 대상 | 커밋 | 증적 |
|---|---|---|
| 런타임 평면(워크스테이션 체크아웃 HEAD) | `0e9ec21` (clean) | `ssh cv-infra-ws 'git -C ~/cv-infra-p2-src/cv-infra-workspace rev-parse HEAD'` |
| `main`(다음 라이브 leg의 릴리즈 대상) | `75123e5` | `git rev-parse HEAD` (본 저장소) |
| 태그 `v1` peel | `0e9ec21` | `git rev-parse 'v1^{commit}'` (본 저장소·WS 체크아웃 둘 다 동일) |

즉 **런타임 평면은 main보다 2 커밋 뒤(`0e9ec21`)**, 그리고 **태그 `v1`도 아직
`0e9ec21`에 머물러 있다** — 태그와 런타임이 *우연히 일치*하지만 **둘 다 main보다
stale**하다. 이 "우연한 일치"가 아래 **stale-local-tag 함정**의 핵심이다.

**첫 실집행 기록(2026-08-03, p5c8 T1, SSH 단일 채널)** — 위 절차를 처음으로 실제로
돌렸다. 게이트를 두 형태로 돌린 결과가 이 문서의 나머지를 확정한다:

| 시점 | 호출 | 결과 | 증적(워크스테이션) |
|---|---|---|---|
| 재동기화 **전** | 기본값(`--tag v1`) | **exit 0 (IN SYNC)** — 그러나 런타임·태그가 **둘 다 main보다 9 커밋 stale** | `~/cv-infra-p2-out/p5c8/plane/02-fetch-and-pre-gate.log` |
| 재동기화 **전** | `--tag <릴리즈 대상 SHA>` | **exit 3**, `runtime is 9 commit(s) behind` | 같은 파일 |
| 재동기화 **후** | `--tag <릴리즈 대상 SHA>` | **exit 0 (IN SYNC)** | `~/cv-infra-p2-out/p5c8/plane/03-resync-and-post-gate.log` |
| 재동기화 **후** | 기본값(`--tag v1`) | **exit 3**, `9 commit(s) ahead` — 태그가 아직 안 옮겨졌기 때문 | 같은 파일 |

첫 줄이 이 게이트의 **가장 중요한 실전 교훈**이다: 기본 호출은 "런타임 == 태그"만
보증하며, **태그 자신이 릴리즈 대상보다 뒤면 조용히 통과한다**(아래 트러블슈팅 ★★).

**두 번째 실집행(2026-08-06, p5c12, SSH 단일 채널)** — `a6fe344` → `ae3b477`(21 커밋).
1회차와 다른 것만 적는다:

| 관측 | 결과 |
|---|---|
| 재동기화 **전** 게이트 | **두 형태 모두 exit 3** — 기본(`--tag v1`)은 `0 behind / 16 ahead`, 명시(`--tag ae3b477`)는 `21 behind / 0 ahead`. 1회차의 거짓 통과가 재현되지 **않은** 이유는 이번엔 런타임이 태그보다 **앞서** 있었기 때문이다(우연) — 함정이 사라진 게 아니다. |
| 재설치 필요 판정 | `git diff --name-only a6fe344 ae3b477 -- pyproject.toml uv.lock` **공집합** → 재설치 0회. 두 venv 모두 `git checkout`만으로 새 심볼 전파(`contract.schema.InitialPose`·`report.regression._without_nulls` import 성공, `cv-infra --help` exit 0). |
| serve 재기동(3-3) | **운영자에게 이월** — consent env는 운영자 소유 값이라 자동화가 채울 수 없다(NEG-2). 체크아웃 평면은 동기, **상주 serve 프로세스는 여전히 옛 코드**라는 상태를 명시적으로 남긴다(G-43 보강의 "세 번째 평면"). |
| 태그 이동 | **하지 않음.** 아래 ★★ 예외 레시피대로 내용 동일성을 증거로 남겼다: `git diff --stat v1 ae3b477 -- .github/workflows/verify.yml actions/` **공집합** → 소비자가 `@v1`로 실행하는 YAML 평면은 릴리즈 대상과 바이트 동일. |
| 재동기화 **후** 게이트 | `--tag ae3b477` **exit 0**. 기본 `--tag v1`은 exit 3(`37 ahead`) — 태그 미이동의 정상 귀결. |

증적: 워크스테이션 `~/cv-infra-p2-out/p5c12/plane/01~05*.log`.

## ③ 러너 이미지 평면 — 잡 컨테이너 안의 wheel

①②는 **호스트에서 실행되는 코드**만 옮긴다. 잡은 `cv-infra-runner:<tag>` 안에서 돌고,
그 안의 `cv_infra`는 **빌드 시점에 박힌 wheel**이다 — 체크아웃·재설치·serve 재기동
어느 것도 이 평면을 건드리지 않는다. 실측된 비용(2026-08-06, p5c12): 러너 이미지가
`p4c5`에 머물러 있어 `scenario.initial_pose`(p5c11 T4 랜딩)를 선언한 잡이 표준 경로에서
`Extra inputs are not permitted`로 **거절**됐다 — 제어 평면은 신선했는데 잡이 죽었다.
즉 **러너 코드 변경은 리빌드 전까지 라이브로 검증 불가**다.

**리빌드는 명시적 결정이 있을 때만** 한다(FU-10 image-as-artifact,
`decisions/2026-07-07-fu10-image-as-artifact.md`). 결정되면 GPU 호스트에서:

```bash
# ① 빌드 컨텍스트를 대상 커밋 X 그대로 내보낸다(런타임 평면 체크아웃과 무관 — 경합 0).
#    아직 push되지 않은 커밋도 이 경로로 빌드할 수 있다.
git archive --format=tar X | ssh <gpu-host> 'mkdir -p <build>/src && tar -x -C <build>/src'

# ② 빌드(태그는 사이클 슬러그. latest 금지 — LOCKED §2)
#    CV_SOURCE_REVISION = X의 **full sha**. git archive 컨텍스트에는 .git이 없으므로
#    이미지가 자기 커밋을 알 수 있는 유일한 경로다(없으면 빌드가 loud 실패한다).
ssh <gpu-host> 'cd <build>/src && docker build --progress=plain \
  --build-arg CV_SOURCE_REVISION=<X-full-sha> \
  -f docker/runner/Dockerfile -t cv-infra-runner:<tag> . > <build>/build.log 2>&1'

# ③ 핀 기록(로컬 태그는 RepoDigest가 없다 → Image Id가 핀이다) + 평면 ③ 스탬프 확인
docker image inspect cv-infra-runner:<tag> \
  --format 'Id={{.Id}} Created={{.Created}} rev={{index .Config.Labels "org.opencontainers.image.revision"}}'
```

**이전 태그는 지우지 않는다** — 과거 게이트/앵커가 그 Id를 인용하고 있다(FU-10 핀 대장).
**단, 2026-08-20 프로덕션 컷오버에서 이 규칙이 한 번 명시적으로 뒤집혔다**: 개발기 러너
태그 15종(`p2`~`p5c17`)과 제어평면 3종이 CEO 결정으로 **삭제**됐다. 규칙이 폐기된 것이
아니라 *"MVP 개발기 핀 대장은 릴리즈 시점에 만료된다"* 는 판정이 내려진 것이다 — 삭제
전에 **repo:tag + 전체 sha256 Id + 크기 + 생성일을 전수 기록**했고(그 기록이 이제 핀 대장
이다), 아래 리빌드 표의 Id 들은 **그 이미지가 더 이상 이 호스트에 없다는 뜻**이다.
릴리즈 이후 태그는 다시 이 규칙(지우지 않는다)의 적용을 받는다.

**apt 버전 핀(p5c15, D-6 선행)**: 이 Dockerfile이 호명하는 apt 패키지 8개는 이제
`=<정확한 버전>`으로 핀돼 있다(값의 출처·전량 핀을 포기한 근거·재핀 절차는 Dockerfile의
`apt VERSION PINS` 블록). ROS/우분투 저장소는 **대체된 버전을 인덱스에서 내리므로**,
언젠가 리빌드가 `Version '…' for '…' was not found`로 **loud 실패**한다 — 그것이 의도된
동작이다(조용한 드리프트보다 낫다). 그때는 **마지막으로 성공한 이미지에서 실측해 재핀**한다:
```bash
docker run --rm --entrypoint dpkg-query <last-good-runner-image> -W \
  -f='${binary:Package}=${Version}\n' <the 8 package names>
```
핀되지 않는 것: **transitive 262개**(빌드 로그의 `rosbag2 apt layer manifest`가 전부
찍는다) · **결과 이미지 다이제스트**(D-6: 핀되는 것은 입력 집합이지 출력이 아니다).

**provenance 확인(이미지 ↔ 커밋 결속)**: wheel에는 커밋 스탬프가 없다(`__version__`은
`0.0.0` 고정). 그래서 결속은 **바이트 대조**로 만든다 — 이미지 안 site-packages의
`cv_infra/**.py` 매니페스트가 소스 트리의 그것과 같아야 한다:

```bash
docker run --rm --entrypoint /bin/bash cv-infra-runner:<tag> -c \
  'cd /isaac-sim/kit/python/lib/python3.11/site-packages && find cv_infra -name "*.py" | sort | xargs sha256sum'
# vs.  (cd <build>/src && find cv_infra -name "*.py" | sort | xargs sha256sum)   → diff 공집합
```

**리빌드 기록**:

| 태그 | Image Id | 소스 커밋 | 확인 |
|---|---|---|---|
| `cv-infra-runner:p5c12` | `sha256:d3e945d9546ec9ce8e06512920cd5cea82478c66cd04c54055d3fc781e0dcb8b` | `d4ac0a0` (workspace main, 미push 상태에서 `git archive`로 빌드) | wheel 49파일 ↔ 소스 diff 공집합 · D-4' pydantic 2.11.7 가드 통과 · `scenario.initial_pose` 수용(p4c5는 거절) · `recording.bag_topics(include_sensors=True)`가 선언 센서 3토픽 append |
| **`cv-infra-runner:p5c14`** (**첫 스탬프 이미지**) | `sha256:39482af4c6f67090e51c0cc21c9de243291e0df2647c34b7415aca1f7c4e0308` | `1207dd40ab03a4a4626f81dfc4f4d64e4abf15c0` (`git archive` 컨텍스트 + `--build-arg CV_SOURCE_REVISION`) | wheel 49파일 ↔ 소스 diff 공집합 · D-4' pydantic 2.11.7 가드 통과 · `revision` 라벨 == 소스 커밋 → **게이트 exit 0(③ 최초)** · exit 계약 0/1/2/3 실측 · `ros-jazzy-sensor-msgs=5.3.8-1noble.20260615.112429`(p5c13과 동일 — apt 레이어 캐시 히트, RootFS 19/23 레이어 공유) · 증적 `~/cv-infra-p2-out/p5c13/rebuild/` |
| **`cv-infra-runner:p5c16`** (**첫 apt-핀 빌드**) | `sha256:1e9750b3015f1b03e968be6f76804afda141930f13663bca49320a4c50d0a960` | `ac442eeb03c383bf62a0c779646b7d08c53914ea` (`git archive` 컨텍스트 + `--build-arg`) | **2m10s**, apt 핀 8/8 해석(레이어 실행됨 — 캐시 히트 아님) · 레이어 매니페스트 **252** 패키지 · wheel **50파일 ↔ 소스 diff 공집합** · D-4' pydantic 2.11.7 가드 통과 · 게이트 **exit 0**(③) · **라이브 self-test 3회 pass** · 증적 `~/cv-infra-p2-out/p5c15/t7/` |
| `cv-infra-runner:p5c16-rebuild2` (**D-6 ① 동등성 대조군**) | `sha256:ef66de2401480b9f59f9f4f8e6a95aaa0e6f552b25d52c38e2d039a3939a931e` | 같은 커밋 `ac442ee`, **독립 `git archive` 컨텍스트(diff 공집합) + `--no-cache`**, **1m6s** | **Image Id 는 다르다**(D-6 예고대로) — 나머지는 동일: apt 매니페스트 252/252 · 전체 dpkg **413/413** · wheel 50파일 sha256 · import 스모크 · **같은 self-test 잡의 verdict·지표**. 총 Size 도 바이트 동일(15,461,682,451 B) |
| `cv-infra-orchestrator:local` (평면 ②') | `sha256:0cbc0b0d284dd1c37fb76d8367f9394c0527aa1d9d6b4a10ead5623441e448f1` | `ac442ee` (`CV_SOURCE_REVISION=… docker compose … up -d --build`, **7.9 s**) | **compose 경로가 처음으로 revision 라벨을 남긴 이미지**(G-66 수리 확인). 리빌드 전 `:local`은 `rev=<unstamped>` → 게이트 exit 3, 후 exit 0 |
| `cv-infra-selftest-stub:p5c16` (stub SUT 평면) | `sha256:b8a8b4c310d860a2ee84959fdbfa07140b3cd9c29414ef5269c8b62cb7ba861f` | `ac442ee` (`docker/selftest_stub/README.md` §5) | 931 MB · 라이브 self-test 4회에서 readiness 배리어 **0.36 s** 통과. ⚠ 이 빌드는 스탬프 레이어만 새로 굽고 **apt 레이어는 캐시 히트**였다 — 이 이미지의 핀 가용성은 오늘 재확인되지 **않았다** |
| **`cv-infra-runner:prod-5ae46d5`** (**첫 릴리즈 각인 — 프로덕션 컷오버**) | `sha256:bb04b8b5fe5794ecfed22bc4f3ea5a0808f3c561cb032bed713fb2a95a169f17` | `5ae46d59acb5a296aefb966b58d76f60a30b62e9` (`git archive` 컨텍스트 + `--build-arg`) | **7 s** — ⚠ `docker/runner/Dockerfile` 무변경이라 apt 레이어 **캐시 히트**, 이 빌드도 apt 핀 가용성을 재확인하지 **않았다**(p5c16 의 2m10s 가 마지막 실검증) · wheel **50파일 ↔ 소스 diff 공집합** · 이미지 안 `./python.sh -c "import isaacsim, cv_infra"` OK(numpy 1.26.0 · pydantic 2.11.7) · 게이트 **exit 0**(3평면) · 라이브 self-test **2회 pass**(31 s / 32 s) · 태그가 **사이클 슬러그가 아니라 릴리즈 각인**인 첫 이미지 · 증적 = 메타 저장소 `evidence-manifests/2026-08-20-p5c19-intentional-expiry/cutover-records/` |
| **`cv-infra-orchestrator:prod-5ae46d5`** (평면 ②' 릴리즈 각인) | `sha256:7fa08a866761752236ca9ba8edf47bf0c6eecc96daa66a0f4b28658a8177b77d` | 같은 커밋 (`CV_SOURCE_REVISION=… docker compose … up -d --build`, **14 s**) | G-88 그대로 — 러너 내용이 바뀌었든 아니든 **라벨을 옮기려면 이 이미지도 구워야** 게이트가 초록이 된다. `CV_ORCHESTRATOR_IMAGE` 를 `docker/.env` 에 명시해 compose 가 이 태그로 굽도록 고정했다(기본 `:local` 은 릴리즈 각인이 아니다) |
| **`cv-infra-runner:p5c17`** (**G-74 스큐 창 폐쇄**) | `sha256:15f140d5b860bcd7d8fb6caa29b296b5887f64727b7707ab1acda1799162a786` | `8016803e4e5f7e4430d5c652a914af0fe7960f72` (`git archive` 컨텍스트 + `--build-arg`) | **5 s** — ⚠ apt 레이어 **캐시 히트**라 이 빌드는 apt 핀 가용성을 재확인하지 **않았다**(p5c16 의 2m10s 와 대비) · wheel **50파일 ↔ 소스 diff 공집합** · 이미지 안 `./python.sh -c "import isaacsim, cv_infra"` OK(pydantic 2.11.7 · numpy 1.26.0) · 게이트 **exit 0**(3평면) · `cv-infra run` 단독 경로 **exit 3 → exit 0**(같은 잡이 재빌드 전에는 `verdict=pass` 인데 exit 3, G-74) · 라이브 self-test pass · `docker/.env` 핀 갱신 + `up -d` · 증적 `~/cv-infra-p2-out/p5c17/t2/` |

### 게이트가 ③을 본다 (p5c13, Q1-B) — 그리고 **옛 이미지는 전부 미스탬프**다

`scripts/check_plane_skew.sh`는 이제 `--image <ref>`를 **필수**로 받아 이미지의
`org.opencontainers.image.revision` 라벨을 릴리즈 ref와 대조한다(위 provenance
바이트 대조는 여전히 유효한 정밀 수단이고, 게이트는 그 값싼 상시판이다).
라벨은 `docker/runner/Dockerfile`이 `--build-arg CV_SOURCE_REVISION`으로 박는다.

**마이그레이션 상태(2026-08-10 실측)**: 기존 핀 `p4c5`·`p5c12`·`p5c13`은 전부
**라벨이 없다**(`docker image inspect --format '{{json .Config.Labels}}'` →
베이스 상속 `ref.name/version` 2개뿐). 그래서 스탬프가 들어간 이미지로 **리빌드하기
전까지 게이트는 exit 3(UNSTAMPED)로 fail-closed**한다 — 이것이 정상 동작이며,
"이 평면은 아직 검증 불가"를 조용히 통과시키지 않겠다는 뜻이다.

**마이그레이션 완료(2026-08-10, 같은 날 리빌드)**: `cv-infra-runner:p5c14`가 **첫
스탬프 이미지**다(`org.opencontainers.image.revision=1207dd4…`). 실 러너 이미지를
상대로 한 게이트 **exit 0의 최초 실측**:

```
[cv-infra][plane-skew] runtime plane : …/cv-infra-workspace @ 1207dd4… -> 1207dd40ab03…
[cv-infra][plane-skew] release tag   : …/cv-infra-workspace @ 1207dd4… -> 1207dd40ab03…
[cv-infra][plane-skew] runner image  : cv-infra-runner:p5c14 @ org.opencontainers.image.revision -> 1207dd40ab03…
[cv-infra][plane-skew] IN SYNC — runtime plane AND runner image both match the release ref.
GATE exit=0
```

옛 핀들은 **여전히 미스탬프이고 삭제하지 않는다**(FU-10) — 그 태그로 게이트를 부르면
계속 exit 3이 나오는 것이 정상이다. 라이브 leg는 **스탬프된 이미지**를 써야 한다.
증적: `~/cv-infra-p2-out/p5c13/rebuild/03-verify-p5c14.log`.

### ②' 제어 평면 이미지 — **제품 경로가 임시 명령보다 프로방넌스를 잃고 있었다** (G-66)

러너 이미지는 PM이 `docker build --build-arg CV_SOURCE_REVISION=<sha>`로 만들어 라벨이
박혔는데, **`docker compose up --build`로 만든 제어 평면 이미지는 `rev=[]`** 였다(실측
2026-08-14). `docker compose build`는 명령줄의 `--build-arg`/`--label`을 **계승하지
않는다** — compose 파일이 `build.args`로 명시하지 않으면 아무것도 전달되지 않는다.
그리고 스큐 게이트는 **러너 이미지만** 봤으므로 이 구멍은 게이트를 깨지 않았다:
**조용했다**. 이것이 G-43이 이름 붙인 실패 모드 그 자체다 — *게이트가 보지 않는 평면*.

수리(p5c15, 세 조각):

1. `docker/compose.yaml`의 `build.args`가 `CV_SOURCE_REVISION`을 환경에서 받아 넘긴다.
   compose는 명령을 실행할 수 없으므로 값은 **빌드 명령의 환경**에서 온다:
   ```bash
   CV_SOURCE_REVISION="$(git rev-parse HEAD)" \
     docker compose -f docker/compose.yaml up -d --build
   ```
   `.git`이 없는 배포(타르볼)면 **기록된 릴리즈 sha**를 그대로 넣는다.
   **`docker/.env`에 적지 마라** — 이후 모든 빌드가 그 옛 커밋을 주장하게 된다.
2. `docker/orchestrator/Dockerfile`이 러너와 **같은 규율**로 비어 있음을 거부한다
   (`ARG` → `test -n` → `LABEL org.opencontainers.image.revision`). 스탬프 없는 이미지는
   빌드 시점에 죽는다 — 게이트에서 죽는 것보다 크고 싸다.
3. 게이트가 `--orchestrator-image`를 **필수**로 받아 이 평면도 대조한다. 미스탬프는
   러너와 똑같이 **exit 3 fail-closed**(마이그레이션 상태를 통과로 읽지 않는다).

**옛 제어 평면 이미지는 전부 미스탬프다**(`cv-infra-orchestrator:local`·`:p5c14` —
2026-08-14 실측 `rev=[]`). 즉 **첫 리빌드 전까지 게이트는 exit 3**이고, 그것이 정상
동작이다. 이 수리는 CPU에서 저작·검증됐고(`tests/test_deploy_image_provenance.py`가
스텁 `docker`로 게이트를 실제로 돌려 fail-closed를 실증), **실 compose 빌드로 스탬프가
박히는 것은 아직 미실측**이다 — 첫 리빌드에서 확인할 것:
```bash
docker image inspect <control-plane-image> \
  --format 'rev={{index .Config.Labels "org.opencontainers.image.revision"}}'
```

### 리빌드 후 검증 — exit 계약 4값 (p5c13 Q2 수리의 발효 확인)

같은 리빌드에서 러너 이미지의 **exit 계약 붕괴 수리**도 발효된다(베이스 `python.sh`의
`|| error_exit`가 러너의 2/3을 1로 뭉개던 결함 — 아래 [실측](#실측-근거-2026-08-10)).
리빌드 직후 GPU 없이 돌릴 수 있는 확인:

```bash
# (a) 래퍼가 4값을 전부 통과시키는가
for n in 0 1 2 3; do
  docker run --rm --entrypoint ./python.sh cv-infra-runner:<tag> -c "import sys; sys.exit($n)"
  echo "N=$n -> $?"      # 기대: 0 1 2 3   (수리 전: 0 1 1 1)
done
# (b) 실제 ENTRYPOINT로 계약 2 (JOB_SPEC 부재 = BadJobSpec)
docker run --rm cv-infra-runner:<tag>; echo "no-JOB_SPEC -> $?"        # 기대 2 (수리 전 1)
# (c) 실제 ENTRYPOINT로 계약 3 (EULA 부트 가드 — consent 값을 주지 않는 것이 요점)
docker run --rm -v <spec>:/tmp/jobspec.json:ro \
  -e JOB_SPEC=/tmp/jobspec.json -e RESULT_OUT=/tmp/out cv-infra-runner:<tag>; echo "$?"  # 기대 3
```

`0`/`1`은 잡이 실제로 pass/fail해야 나오므로 (a)의 래퍼 수준에서만 CPU로 확인된다
— 실 잡 수준 0/1은 다음 GPU 라이브 leg에서 자연히 관측된다.

#### 실측 근거 (2026-08-10)

| 관측 | 값 |
|---|---|
| 베이스 `python.sh` 기전 | `$python_exe "${filtered_args[@]}" $args \|\| error_exit` + `error_exit(){ … exit 1; }` → **모든 비-0을 1로 붕괴** |
| 수리 전(`cv-infra-runner:p5c13`, Id `sha256:7d4ac8f3…`) | `-c sys.exit(N)` → 0/1/**1**/**1** · 실 ENTRYPOINT `no JOB_SPEC` → **1** · EULA 거부 → **1** |
| 수리 후(같은 이미지, throwaway 컨테이너 writable layer에 sed만 적용) | 0/1/**2**/**3** · `no JOB_SPEC` → **2** · EULA 거부 → **3** |
| 이미지 불변 확인 | 프로브 전후 Image Id 동일(`sha256:7d4ac8f3…`) — 기존 핀 무손상 |
| **발효 확인(리빌드 후 `cv-infra-runner:p5c14`, Id `sha256:39482af4…`)** | `-c sys.exit(N)` → **0/1/2/3** · 실 ENTRYPOINT `no JOB_SPEC` → **2**(`[cv-runner] bad job spec: RESULT_OUT is required`) · 유효 spec + consent 없음 → **3**(`… EULA not accepted for this run — boot refused (NEG-2)`). **수리가 이미지에 들어갔다** |

증적: 워크스테이션 `~/cv-infra-p2-out/p5c13/exit/01~03*.log`(수리 전/후 비교) ·
`~/cv-infra-p2-out/p5c13/rebuild/03-verify-p5c14.log`(리빌드 이미지 발효 확인).

## 불변식 + 게이트를 언제 돌리나

**불변식**: *어떤 라이브 leg를 시작하기 전에도* 런타임 평면(②)·**제어 평면
이미지(②')**·**러너 이미지 평면(③)** 은 라이브 leg가 실행할 릴리즈 커밋과 **바이트
동일**해야 한다.

**게이트**: `scripts/check_plane_skew.sh` — 런타임 평면 체크아웃 커밋 **및 두 이미지의
revision 스탬프**를 릴리즈 태그 peel과 대조하고, 어긋나면 loud fail(exit 3,
fail-closed). **읽기 대조만** 하며 워크스테이션·체크아웃·git ref·이미지를 **일절
변경하지 않는다**(`docker image inspect`도 읽기다). 라이브 leg 착수의 **선행 게이트**로
돌린다.

## 릴리즈(재태그) 절차 — 런타임 평면 동기화는 **필수 단계**

> `git push`(태그 이동 포함)는 **CEO 승인 필수**(CLAUDE.md §2-2). 아래 push 단계는
> 승인 후에만 실행한다.

1. **릴리즈 커밋 X 확정** — 라이브 leg로 검증할 커밋(대개 `main` tip). 예: `75123e5`.
2. **YAML 평면 이동(태그 재태그)** — `git tag -f vN X` → (CEO 승인 후) `git push -f origin vN`.
   이때 ①만 움직인다. G-44: **태그 push ≠ 브랜치 push** — 태그만 옮겼다고 런타임이
   따라오지 않는다.
3. **런타임 평면 동기화(② — MANDATORY, 이 단계가 G-43의 핵심)** — GPU 호스트에서:
   1. 체크아웃 전진: `git -C <src> fetch --tags --force && git -C <src> checkout X`
      ⚠ **`--force` 는 선택이 아니다** — 아래 STALE-LOCAL-TAG 항 참조.
      (`<src>` = 런타임 평면 체크아웃, 기본 `~/cv-infra-p2-src/cv-infra-workspace`).
   2. editable 패키지 **전파 확인** — 대개 **재설치는 필요 없다**(2026-08-03 실측으로
      확정). 런타임 평면의 두 venv는 모두 editable 설치이고
      `_editable_impl_cv_infra.pth`의 내용이 **체크아웃 경로 한 줄**이라, `git checkout`
      만으로 새 코드가 그대로 전파된다:
      * **러너 CLI 평면** = `~/cv-infra-host-venv` — 소비자 러너의 `.path` 첫 항목이
        이 venv의 `bin`이라 `cv-infra`/`python -m cv_infra.cli.*`가 여기서 해석된다.
      * **serve 평면** = `~/cv-infra-ci/venv` — **컨테이너 안에서 만든 venv**라 호스트에서
        `bin/python`은 dangling symlink다. 반드시 `docker exec <serve>` 로 프로브할 것
        (호스트에서 실행하면 `No such file or directory`가 나오는데, 이는 설치가 깨진
        게 아니라 평면을 잘못 고른 것이다).
      ```
      cat <venv>/lib/python3.11/site-packages/_editable_impl_cv_infra.pth   # == <src>
      <venv>/bin/python -c "import cv_infra; print(cv_infra.__file__)"      # == <src>/cv_infra/__init__.py
      ```
      **재설치가 필요한 유일한 경우 = 패키지 메타(의존성·엔트리포인트) 변화**이고, 그
      판정 자체가 한 줄 게이트다:
      ```
      git -C <src> diff --name-only <old> <new> -- pyproject.toml uv.lock   # 비면 재설치 불필요
      ```
      비어 있지 않으면 해당 venv에 `uv pip install -e .`(또는 그 venv의 설치 방식대로)
      재설치하고 **콘솔 스크립트를 재확인**(`cv-infra --help`)한다. 전파 확인은 새
      커밋에만 있는 심볼을 임포트해 실증한다(2026-08-03: `DEFAULT_OUTER_WALLCLOCK_S` —
      재설치 0회로 성공, `cv-infra --help` exit 0).
   3. serve/CLI 컨테이너 재기동 — **자동 전파되지 않는 유일한 지점**이다. 실측 배선
      (2026-08-03): serve는 상주 컨테이너 안에서
      `<ci venv>/bin/python -m cv_infra.orchestrator.serve`로 돌고, 새 코드는 **디스크엔
      이미 있으나** 프로세스는 부팅 때 임포트한 모듈을 계속 쓴다 → 재기동해야 반영된다.
      재기동 시 지켜야 할 3가지(전부 실측 근거):
      * **NVML이 살아 있는 컨테이너에서 띄울 것.** `build_app`은 부팅 때 **한 번**
        `compute_k`를 계산하고 `CV_VRAM_PER_INSTANCE_MB`가 설정돼 있으면
        `PynvmlVramGauge`가 붙는다 → NVML이 죽은 컨테이너에서 재기동하면 **부팅 자체가
        loud 실패**한다. 장기 상주 컨테이너는 NVML을 잃으므로(G-36 — 2026-08-03
        `cv-p5c6-ci`에서 `NVMLError_Unknown` 실측, 호스트 `nvidia-smi`는 정상)
        **fresh sibling 컨테이너**로 띄운다. 뒤집어 말하면, 재기동하지 않는 한 이미
        계산된 k로 잡은 계속 돈다 — `gpu_reachable=false`는 M6 텔레메트리 저하일 뿐
        잡 수용을 막지 않는다(재기동 타이밍 판단의 근거).
      * **store 경로를 그대로 유지할 것.** baseline이 그 SQLite에 산다(C-1) — 경로가
        바뀌면 회귀 판정의 기준선이 조용히 사라진다.
      * **consent env는 운영자가 다시 공급한다.** `ACCEPT_EULA`/`PRIVACY_CONSENT`는
        운영자 소유 값이고 이미지·저장소·CI 어디에도 박지 않는다(NEG-2). 재기동은
        *운영자가 값을 다시 넣는 시점*이지, 자동화가 기존 프로세스 env에서 값을 긁어올
        시점이 **아니다**. 부팅 로그 `serve-config`의 `consent_env_present`에 두 키가
        보이는지로만 확인한다(값은 로그되지 않는다 — G-21).
      재기동 직후 확인 3종: ① 부팅 로그 `serve-config` 한 줄의 `store_path`·`out_dir`·
      `runner_image`·`max_concurrent`·`cache_root`·`consent_env_present`가 재기동 전과
      동일 ② `GET /monitor.json` 200 + `gpu_reachable` 관찰값 기록 ③ 같은 응답의
      `requests[]`에 이전 봉투가 그대로 보이는지(= store 연속성).
      **확정 명령줄**(2026-08-03 운영자 실집행·PM 확인 — 이전 `[VERIFY]` 해소). 값은
      전부 재기동 **전** `serve-config`에서 그대로 옮긴 것이고, `<consent>` 2곳만
      운영자가 실행 시점에 채운다(Isaac 엔트리포인트 `license.sh`가 보는 값 — 저장소에
      리터럴로 박지 않는다, NEG-2):

      ```bash
      docker stop -t 30 <old-serve-container>          # 비파괴: stop만, rm 금지
      docker run -d --name <new-serve-container> --gpus all \
        -e NVIDIA_DRIVER_CAPABILITIES=utility -p 127.0.0.1:8000:8000 \
        -v /var/run/docker.sock:/var/run/docker.sock \
        -v <src-root>:<src-root> -v <out-root>:<out-root> -v <ci-root>:<ci-root> \
        -v <cache-root>:<cache-root>:ro \
        -v <runner-work>:<runner-work>:ro -v <runner-user-work>:<runner-user-work>:ro \
        python:3.11-slim sleep infinity
      docker exec <new-serve-container> <ci-venv>/bin/python \
        -c "import pynvml; pynvml.nvmlInit(); print('nvml-ok')"   # ← 실패하면 여기서 멈춘다
      docker exec -d \
        -e CV_STORE_PATH=<store> -e CV_OUT_DIR=<out> -e CV_RUNNER_IMAGE=<runner-image> \
        -e CV_MAX_CONCURRENT=<k> -e CV_VRAM_PER_INSTANCE_MB=<mb> \
        -e CV_ISAAC_CACHE_ROOT=<cache> -e CV_ISAAC_CACHE_SCRATCH_ROOT=<scratch> \
        -e CV_BIND_HOST=0.0.0.0 -e CV_BIND_PORT=8000 \
        -e ACCEPT_EULA=<consent> -e PRIVACY_CONSENT=<consent> \
        <new-serve-container> sh -c '<ci-venv>/bin/python -m cv_infra.orchestrator.serve \
          >> <ci-root>/logs/<serve-log> 2>&1'
      ```

      **NVML 스모크(3번째 명령)를 건너뛰지 마라** — 이게 "부팅이 loud 실패할지"를
      *serve를 띄우기 전에* 가르는 유일한 지점이다.

      실집행 관측(2026-08-03, `cv-p5c6-ci` → `cv-p5c8-ci`): `nvml-ok` → serve-config에
      `store_path` 동일·`consent_env_present=["ACCEPT_EULA","PRIVACY_CONSENT"]`(값 미로깅)·
      **`outer_wallclock_s=13726.2`**(= 새 코드로 떴다는 증거)·`job_timeout_s=1800.0`
      (strict `outer > inner` coherence 통과)·`k=8`·reconciliation 전부 0 →
      `/monitor.json`에서 **`gpu_reachable` false→true 회복**(G-36 해소)·
      `vram_total_mib=97887`·`requests[]`에 2026-07-22 봉투 잔존(**store 연속성 실증**).
      ★ **재기동 후 netns 감사 하네스는 반드시 재무장**한다(`netns_audit.sh arm <새 컨테이너>`)
      — 컨테이너가 바뀌면 이전 무장은 무효고, 빠뜨리면 그 사이클은 감사되지 않은 런이 된다.
      **순서가 곧 증거다**: `arm` → 라이브 leg → `read <컨테이너> --since <그 leg 의 산출물>`.
      런보다 늦은 `arm` 은 `read` 가 **exit 3 `LATE ARM`** 으로 거부한다(카운터는 arm 에서 0 이
      되므로 그 런은 애초에 안 들어있다 — 0 이 아니라 VOID).
3-bis. **러너 이미지 평면 동기화(③)** — 러너가 실행하는 wheel이 X와 다르면 잡이
   컨테이너 안에서 죽는다(p5c12 실측). X가 `cv_infra/**`를 건드렸다면
   [위 ③ 절](#-러너-이미지-평면--잡-컨테이너-안의-wheel)의 리빌드가 **선행 조건**이다
   (`--build-arg CV_SOURCE_REVISION=<X-full-sha>` 필수).
3-ter. **제어 평면 이미지 동기화(②')** — 제어 평면은 컨테이너이므로 체크아웃을 옮겨도
   **상주 컨테이너 안의 wheel은 그대로다**. X가 `cv_infra/**`를 건드렸다면 리빌드가
   선행 조건이다: `CV_SOURCE_REVISION="$(git rev-parse X)" docker compose -f
   docker/compose.yaml up -d --build` (스탬프 없이는 빌드가 거부된다 — G-66).
4. **스큐 게이트 통과 확인** — `scripts/check_plane_skew.sh --tag X --image cv-infra-runner:<tag>
   --orchestrator-image <control-plane-image>`
   → **exit 0**(IN SYNC)이어야 한다. exit 3이면 3/3-bis/3-ter 단계 미완 → 라이브 leg 착수 금지.
5. **회귀 기준선 통제** — 아래 [fail-baseline 통제](#재동기화-이후-첫-라이브-leg--fail-baseline-통제)
   절. 코드가 앞으로 가면 **기준선이 조용히 리셋될 수 있고**, 리셋 직후 첫 런의 verdict가
   무엇이든 그대로 기준선이 된다.
6. **그때서야 라이브 leg 착수.**

## 재동기화 이후 첫 라이브 leg — fail-baseline 통제

**언제 이 절이 발동하나**: 재동기화가 `request_identity_key`의 **정규화 범위**를 바꾸는
커밋을 넘어갈 때. 넘어갔는지는 추측하지 말고 **재계산해서** 판정한다(아래 ①).

**왜 위험한가** — 두 사실의 곱이다.

1. **리셋**: 정규화가 바뀌면 기존 `request_baselines` 행의 키는 **한 번 빗나간다**
   (`cv_infra/report/regression.py` 헤더 `ONE-OFF RESET`). 리셋된 요청은 기준선 **부재**
   상태가 된다.
2. **부재 시엔 `fail`도 기준선이 된다**: `cv_infra/report/baseline.py::update_baseline`은
   `existing is not None and verdict != PASS`일 때만 no-op이다 → **기준선이 없으면 첫
   definite verdict가 pass든 fail이든 그대로 수립**된다. 그리고 그 수립은 게시 CLI가
   아니라 **오케스트레이터 완료 경로**(`orchestrator/api.py::_persist_terminal` ③)에서
   자동으로 일어난다 — "리포트 명령을 안 부르면 안전"은 **거짓**이다(G-53).

여기에 flakiness가 곱해진다. `fail` 기준선이 굳으면 판정이 뒤집힌다(2026-08-06 실측,
격리 store):

| 기준선 | 이후 런 | `judge_regression` | 읽히는 의미 |
|---|---|---|---|
| `fail`(오염) | `pass` | `improved` | 원래 정상인 것이 "고쳐졌다"로 |
| `fail`(오염) | `fail` | **`unchanged`** | **진짜 회귀가 보이지 않는다** |
| `pass`(정상) | `fail` | `regressed` | 정상 |

### 절차 (GPU 호스트, 운영자)

**① 리셋 여부를 재계산으로 판정한다** — 새 코드로 캐노니컬 요청의 키를 뽑아 store의
기준선 키 집합에 있는지 본다. 프로덕션 store는 **사본으로만 읽는다**(G-53 ④ — 원본
열기·체크포인트 0):

```bash
cp -p <store>.sqlite3 <store>.sqlite3-wal <evidence>/   # -shm은 복사 후 지운다
<venv>/bin/python - <<'PY'
import sqlite3, sys
from cv_infra.contract.loader import load_request
from cv_infra.report.regression import identity_key
req = load_request("<src>/tests/fixtures/nova_carter_warehouse_goal.yaml").request
key = identity_key(req.model_dump(mode="json", by_alias=True))   # api.py:563과 같은 dump
db = sqlite3.connect("<evidence>/<store>.sqlite3")
stored = {r[0] for r in db.execute("SELECT request_identity_key FROM request_baselines")}
print(key, "MATCHES" if key in stored else "ABSENT -> 리셋. 아래 ②/③ 적용")
PY
```

**② 통제 A(권장) — 기준선을 pass 확인 런에서만 수립한다.** 리셋 이후 **첫 런의 verdict가
곧 기준선**이므로, 첫 런은 *기준선을 세우는 런*으로 취급한다. 리포트가 `pass`로 끝나면
그대로 두고, `fail`이면 아래 ③로 되돌린다. (플랫폼이 자동으로 세우므로 "안 세우기"라는
선택지는 없다 — 세워진 것을 **검사**하는 것이 통제다.)

**③ 통제 B — 이미 fail 행이 수립됐으면 제거 후 재수립.** 그 키의 non-pass 행만 지우고,
같은 요청을 pass로 한 번 더 돌려 기준선을 다시 세운다:

```bash
sqlite3 <store>.sqlite3 \
  "DELETE FROM request_baselines WHERE request_identity_key='<key>' AND verdict!='pass';"
```

지운 뒤 다시 `find_baseline` → `None`이 되고, 다음 pass 런이 정상 기준선을 세운다.

**④ 증거는 행 덤프로 남긴다.** WAL 모드 db의 **파일 sha256은 논리적 불변성의 척도가
아니다**(M6 헬스 샘플러가 5초마다 `-wal`/`-shm`을 움직인다, G-53 ③). 오염 0의 증명은
`request_baselines` **전 행 덤프 대조**다.

**⑤ 측정·실험은 격리에서.** 위 표를 다시 얻고 싶거나 통제를 리허설하려면 프로덕션 store가
아니라 **사본**에서 하라. 2026-08-06 실집행이 그 형태다
(`~/cv-infra-p2-out/p5c12/baseline/{fail_baseline_lab.py,04-fail-baseline-lab.log}` —
사본 3개 위에서 위험·통제 A·통제 B를 전부 실증하고 프로덕션 2행은 불변 확인).

## 게이트 사용법 — `scripts/check_plane_skew.sh`

입력(전부 arg/env; 호스트명·GPU 리터럴 **하드코딩 0** — DoD-P5-09 정신).
⚠ **단 한 곳 예외가 2026-08-15 clean-host 실행에서 드러났다**: `--src` 의 **기본값**은 이
프로젝트 워크스테이션의 **디렉토리 레이아웃**이다. 호스트 정체성은 아니지만 *한 호스트의
배치*라서, 체크아웃이 다른 곳에 있는 배포에서는 게이트가 항상 `exit 3` 이다(실측).
**배포 문맥에서는 `--src <deploy-root>` 를 항상 명시하라.**

| 인자 | env | 의미 | 기본값 |
|---|---|---|---|
| `--src PATH` | `CV_PLANE_SRC` | 런타임 평면 체크아웃 디렉토리 | `$HOME/cv-infra-p2-src/cv-infra-workspace` ⚠ 위 경고 |
| `--src-rev REV` | `CV_PLANE_SRC_REV` | 런타임 평면 커밋으로 읽을 rev | `HEAD` (라이브 체크아웃) |
| `--tag REF` | `CV_PLANE_TAG` | YAML 평면의 릴리즈 태그/ref (`REF^{commit}`로 peel) | `v1` |
| `--tag-repo PATH` | `CV_PLANE_TAG_REPO` | 태그를 peel할 저장소 | `= --src` |
| `--image REF` | `CV_PLANE_IMAGE` | **러너 이미지 평면 ③** — 라이브 leg가 스폰할 이미지(= 그 leg의 `CV_RUNNER_IMAGE`) | **없음 — 필수** |
| `--orchestrator-image REF` | `CV_PLANE_ORCH_IMAGE` | **제어 평면 이미지 ②'** — 지금 도는 제어 평면 이미지(= compose의 `CV_ORCHESTRATOR_IMAGE`) | **없음 — 필수** |

두 이미지 인자에 기본값을 두지 않는 이유는 같다: ① 평면을 안 보는 게이트는 거짓 초록을
낸다(G-43·G-66) ② 여기에 기본 이미지 ref를 박으면 호스트측 리터럴이 스크립트에 들어온다
(`DoD-P5-09`). 빠뜨리면 **exit 2**로 즉시 멈춘다.

**exit 코드**: `0` = 전 평면 IN SYNC(라이브 leg 안전) · `2` = 사용법 오류(**필수 이미지
인자 누락 포함**) · `3` = 어느 평면이든 스큐 탐지, **또는** rev/저장소/이미지 해석
실패, **또는** 이미지에 revision 스탬프 부재(fail-closed, 인프라/구성 오류류 — consent
게이트·D-2 pull-timeout `infra_error`와 동급).

예:
```
# 프로덕션(워크스테이션) — 라이브 leg 직전. 릴리즈 대상 SHA + 그 leg가 쓸 두 이미지:
scripts/check_plane_skew.sh --tag <release-sha> \
  --image cv-infra-runner:<tag> --orchestrator-image <control-plane-image>
# = CV_PLANE_SRC=~/cv-infra-p2-src/cv-infra-workspace, HEAD/이미지 라벨 vs 릴리즈 SHA
```

### 게이트 자체의 비공허 실증 (positive control — G-35)

게이트를 고칠 때마다 **양방향**으로 다시 증명한다. 라벨만 든 1줄 이미지면 충분하다
(`FROM scratch`라 바이트 ~0, GPU·베이스 pull 불요):

```bash
T=$(git rev-parse <release-sha>); O=$(git rev-parse <older-commit>)
printf 'FROM scratch\nLABEL org.opencontainers.image.revision=%s\n' "$T" | docker build -q -t cv-plane-selftest:insync -
printf 'FROM scratch\nLABEL org.opencontainers.image.revision=%s\n' "$O" | docker build -q -t cv-plane-selftest:stale  -
printf 'FROM scratch\nENV X=1\n'                                         | docker build -q -t cv-plane-selftest:unstamped -

G=(scripts/check_plane_skew.sh --src <src> --tag "$T")
"${G[@]}" --image cv-plane-selftest:insync    --orchestrator-image cv-plane-selftest:insync     # exit 0
"${G[@]}" --image cv-plane-selftest:stale     --orchestrator-image cv-plane-selftest:insync     # exit 3 (③ N behind)
"${G[@]}" --image cv-plane-selftest:unstamped --orchestrator-image cv-plane-selftest:insync     # exit 3 (③ UNSTAMPED)
"${G[@]}" --image cv-plane-selftest:insync    --orchestrator-image cv-plane-selftest:stale      # exit 3 (②' N behind)
"${G[@]}" --image cv-plane-selftest:insync    --orchestrator-image cv-plane-selftest:unstamped  # exit 3 (②' UNSTAMPED)
"${G[@]}" --image cv-plane-selftest:insync                                                      # exit 2 (필수 인자)
```

CPU 등가는 이미 스위트에 있다 — `tests/test_deploy_image_provenance.py`가 **스텁
`docker`를 PATH에 놓고 같은 6개 형태를 실행**한다(임시 git 저장소 + 라벨만 답하는
스텁). 위 라이브 형태는 그 등가가 실 docker에서도 성립함을 확인하는 자리다.

착지(0)와 탐지(3)를 **쌍으로** 확인하지 않으면, 라벨을 아예 안 읽어도 초록이 나오는
상태와 구분되지 않는다.

## 트러블슈팅

- **`PLANE SKEW DETECTED` (exit 3)** — 런타임 평면이 릴리즈 태그와 다르다. 위 절차
  **3단계(런타임 동기화)**를 실행하고 게이트를 다시 돌린다. 출력의
  `N commit(s) behind / M ahead`가 어느 방향으로 얼마나 어긋났는지 알려준다.

- **★ stale-local-tag 함정(false pass)** — 게이트는 태그를 `--tag-repo`의 **로컬
  ref**에서 peel한다. 그 저장소가 옮겨진 릴리즈 태그를 아직 fetch하지 않았다면 peel이
  **stale**해 게이트가 **거짓 통과**할 수 있다. 실측(2026-07-24): 워크스테이션 체크아웃의
  로컬 `v1`은 여전히 stale `0e9ec21`로 peel됐다 → 만약 `--tag-repo`를 그 체크아웃으로
  두면 런타임(0e9ec21)==태그(0e9ec21)로 **통과**하지만 둘 다 main보다 뒤다. 방어:
  - peel 전 `--tag-repo` 쪽에서 태그를 authoritative하게: **`git -C <tag-repo> fetch --tags --force`**
    ⚠ **`--force` 없이는 안 된다.** 평범한 `git fetch --tags` 는 **옮겨진 태그를 조용히 거부**하고
    (`! [rejected] v1 -> v1 (would clobber existing tag)`) **exit 0** 으로 끝나서, 로컬 ref 가 **옛
    릴리즈를 계속 가리킨다**. 실측 2026-08-21(p5c19 F-6): 배포 호스트의 `v1` 이 평범한 fetch 를 거쳐도
    **4주 전**에 머물렀고, 그래서 **기본 게이트 호출**(`--tag v1`)이 평면을 **7월 커밋과** 대조했다.
    오늘은 평면이 stale 태그보다 **앞서 있어서 false FAIL** 로 드러났을 뿐 — **false PASS 방향은 같은
    결함이고 그쪽은 조용하다.**
    (본인 쪽에서 실행), **또는**
  - 검증한 릴리즈 대상 커밋을 **명시 전달**: `--tag <sha>`, **또는**
  - fresh clone에서 peel, **또는**
  - 로컬 ref를 건드리지 않고 push된 태그를 읽기: `git ls-remote --tags <remote> vN`
    (그리고 `^{}` deref 줄의 커밋을 `--tag`로 전달).
  게이트는 read-only 원칙상 **스스로 fetch하지 않는다** — authoritative 태그 확보는
  운영자 책임이다.

- **★★ 태그 자신이 릴리즈 대상보다 뒤일 때(실측된 false pass)** — 위 ★의 사촌이지만
  **로컬/원격 태그가 일치해도 발생**한다. 2026-08-03 실측: 로컬 `v1` peel == 원격
  `v1^{}` peel == `0e9ec21`인데 릴리즈 대상은 `main`(9 커밋 앞) — 런타임도 `0e9ec21`이라
  기본 호출이 **exit 0으로 통과**했다(`~/cv-infra-p2-out/p5c8/plane/02-fetch-and-pre-gate.log`).
  게이트는 설계상 "런타임 == 태그"만 본다. **"태그 == 릴리즈 대상"은 운영자가 따로
  확인해야 한다.** 대응:
  - 라이브 leg 직전에는 **항상 릴리즈 대상 SHA를 명시**해 돌린다: `--tag <sha>`.
    기본 호출은 태그를 이미 옮긴 뒤의 **재확인용**으로만 쓴다.
  - 릴리즈 대상이 **아직 태그되지 않았고 태그를 옮길 수 없을 때**(예: push 승인 미확보)
    는 두 평면의 *내용* 동일성을 증거로 남기고 진행 여부를 판단한다:
    ```
    git -C <src> diff --stat <tag> <target> -- .github/workflows/verify.yml actions/
    ```
    **출력이 비면** 소비자가 `@vN`으로 실행하는 YAML 평면은 릴리즈 대상과 **바이트
    동일**이므로, 남은 커밋 차이는 *내용상 무해*하다(런타임 평면만 앞선 상태 —
    사이클 노트에 증적과 함께 기록하고 진행). **비어 있지 않으면** 태그를 먼저 옮기기
    전까지 라이브 leg 착수 금지. 이 예외는 **기록될 때만** 유효하다 — 조용히 넘어가면
    그게 G-43 재발이다.

- **게이트 스크립트가 런타임 평면에 아예 없다(stale 평면의 닭-달걀)** — 게이트는
  `scripts/`에 있으므로 **그 게이트가 머지된 커밋보다 오래된 런타임 평면에는 존재하지
  않는다**(2026-08-03 실측: `0e9ec21` 체크아웃엔 `check_plane_skew.sh` 없음). 첫
  재동기화 때는 **릴리즈 소스에서 꺼내 실행**한다:
  ```
  git -C <src> fetch origin --tags                       # refs만 갱신(워킹트리 불변)
  git -C <src> show <target>:scripts/check_plane_skew.sh > <evidence>/gate.sh
  bash <evidence>/gate.sh --src <src> --tag <target> \
       --image cv-infra-runner:<tag> \
       --orchestrator-image <control-plane-image>        # 재동기화 전 positive control
  ```
  아직 머지되지 않은 게이트(브랜치/worktree)를 돌릴 때도 같은 형태다 — 그 브랜치의
  스크립트를 증적 디렉토리로 복사해 실행한다(런타임 평면은 건드리지 않는다).

- **`not a git repo` / `cannot resolve … rev` (exit 3)** — 경로/rev 오타는 조용히
  통과시키지 않고 **fail-closed**로 막는다(G-26). `--src`/`--tag`를 확인한다.

## 관련

- GOTCHAS **G-43**(두 평면·합의된 대응 4항) · **G-44**(태그≠브랜치 push) ·
  **G-35**(게이트 비공허 — 변이로 실증) · **G-36**(장기 상주 serve NVML 소실).
- C-2 경계: 기존 기술(Docker/Compose) + **문서화된 매뉴얼**로 이식성 확보, 전용
  installer 앱 = post-MVP(01-architecture-and-scope §7.1, NFR-DEPLOY-002).
