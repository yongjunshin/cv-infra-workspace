# 배포 평면 동기화 — 릴리즈 재태그 시 런타임 평면 동기화 절차 (G-43)

> **범위(seed)**: 이 문서는 C-2 배포 매뉴얼(`docs/deploy/`)의 **첫 시드**이며,
> **G-43 "두 배포 평면 스큐" 절차 한 건에만** 한정한다. 설치·프로비저닝·적응형
> 프로파일·트러블슈팅 전반을 담는 **전체 C-2 매뉴얼은 아직 아니다**(과설계 금지 —
> 후속 사이클에서 확장). 요구사항 원문은 재서술하지 않고 ID로만 참조한다
> (REQ-DEPLOY-001·003, NFR-DEPLOY-001~003; 정본 = deployment 그룹 명세).

## 왜 (두 평면)

플랫폼은 릴리즈 태그가 **함께 옮기지 못하는 두 배포 평면**으로 배송된다
(GOTCHAS **G-43**):

| 평면 | 무엇 | 무엇으로 갱신되나 |
|---|---|---|
| **① YAML 평면** | reusable workflow / composite action (`.github/workflows/verify.yml` · `actions/verify`) | 릴리즈 태그 `@vN` 이동으로 **자동** 갱신(소비자 `uses: …@vN` 핀) |
| **② 런타임 평면** | GPU 잡이 **실제 실행하는 코드** = 러너 venv의 editable install + **사전 설치된 serve/CLI 컨테이너** | **체크아웃 + 재설치 + 컨테이너 재기동으로만** 갱신 |

GPU 잡은 설계상(R10) `actions/checkout`을 **하지 않는다** → 소비자가 실행하는 코드는
러너에 **사전 설치된 패키지**이지 태그가 가리키는 코드가 아니다. 따라서 `@vN`을 새
커밋으로 **재태그하면 ①만 움직이고 ②는 옛 코드에 머문다** → 두 평면이 **조용히 스큐**
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

> **재동기화는 이 사이클에서 하지 않는다.** 이번 사이클의 몫은 **게이트로 탐지만**
> 하는 것이다. 실제 런타임 평면 재동기화는 이 사이클 코드가 전부 머지되고 **다음
> 라이브 사이클 직전**이 올바른 시점이다(계획 리스크 1). 아래 절차는 그때 실행할
> **문서상 절차**로만 남긴다.

## 불변식 + 게이트를 언제 돌리나

**불변식**: *어떤 라이브 leg를 시작하기 전에도* 런타임 평면(②)은 라이브 leg가
실행할 릴리즈 커밋과 **바이트 동일**해야 한다.

**게이트**: `scripts/check_plane_skew.sh` — 런타임 평면 체크아웃 커밋 vs 릴리즈 태그
peel을 대조하고, 어긋나면 loud fail(exit 3, fail-closed). **읽기 대조만** 하며
워크스테이션·체크아웃·git ref를 **일절 변경하지 않는다**. 라이브 leg 착수의 **선행
게이트**로 돌린다.

## 릴리즈(재태그) 절차 — 런타임 평면 동기화는 **필수 단계**

> `git push`(태그 이동 포함)는 **CEO 승인 필수**(CLAUDE.md §2-2). 아래 push 단계는
> 승인 후에만 실행한다.

1. **릴리즈 커밋 X 확정** — 라이브 leg로 검증할 커밋(대개 `main` tip). 예: `75123e5`.
2. **YAML 평면 이동(태그 재태그)** — `git tag -f vN X` → (CEO 승인 후) `git push -f origin vN`.
   이때 ①만 움직인다. G-44: **태그 push ≠ 브랜치 push** — 태그만 옮겼다고 런타임이
   따라오지 않는다.
3. **런타임 평면 동기화(② — MANDATORY, 이 단계가 G-43의 핵심)** — GPU 호스트에서:
   1. 체크아웃 전진: `git -C <src> fetch --tags && git -C <src> checkout X`
      (`<src>` = 런타임 평면 체크아웃, 기본 `~/cv-infra-p2-src/cv-infra-workspace`).
   2. editable 패키지 재설치: 러너/serve가 쓰는 venv에 `cv_infra`를 재설치
      (`<src>/.venv` 관측됨). **[VERIFY]** 정확한 재설치 명령(예: `uv pip install -e .`
      또는 상응)은 워크스테이션 런타임 평면의 실제 설치 방식으로 확정 — 본 시드는
      *형태*만 규정하고, 미검증 명령을 발명하지 않는다(G-24). 2026-07-24 `pip show`
      프로브는 결론이 나지 않았음.
   3. serve/CLI 컨테이너 재기동: 사전 설치된 서비스 컨테이너가 새 코드를 집도록
      **재기동**한다(관측: `cv-p5c6-ci` 등 장기 상주 컨테이너). **[VERIFY]** 정확한
      재기동/재빌드 절차는 실제 serve 배선으로 확정. (부수: 장기 상주 serve는 NVML을
      잃을 수 있음 — G-36; GPU leg 직전 `gpu_reachable` 재확인은 별도 관례.)
4. **스큐 게이트 통과 확인** — `scripts/check_plane_skew.sh` → **exit 0**(IN SYNC)이어야
   한다. exit 3이면 3단계 미완 → 라이브 leg 착수 금지.
5. **그때서야 라이브 leg 착수.**

## 게이트 사용법 — `scripts/check_plane_skew.sh`

입력(전부 arg/env; 호스트명·GPU 리터럴 **하드코딩 0** — DoD-P5-09 정신):

| 인자 | env | 의미 | 기본값 |
|---|---|---|---|
| `--src PATH` | `CV_PLANE_SRC` | 런타임 평면 체크아웃 디렉토리 | `$HOME/cv-infra-p2-src/cv-infra-workspace` |
| `--src-rev REV` | `CV_PLANE_SRC_REV` | 런타임 평면 커밋으로 읽을 rev | `HEAD` (라이브 체크아웃) |
| `--tag REF` | `CV_PLANE_TAG` | YAML 평면의 릴리즈 태그/ref (`REF^{commit}`로 peel) | `v1` |
| `--tag-repo PATH` | `CV_PLANE_TAG_REPO` | 태그를 peel할 저장소 | `= --src` |

**exit 코드**: `0` = IN SYNC(라이브 leg 안전) · `2` = 사용법 오류 · `3` = 스큐 탐지
**또는** rev/저장소 해석 실패(fail-closed, 인프라/구성 오류류 — consent 게이트·D-2
pull-timeout `infra_error`와 동급).

예:
```
# 프로덕션(워크스테이션) — 라이브 leg 직전. 기본값만으로:
scripts/check_plane_skew.sh
# = CV_PLANE_SRC=~/cv-infra-p2-src/cv-infra-workspace, HEAD vs v1 peel

# 릴리즈 대상을 명시(태그 대신 커밋 SHA로):
scripts/check_plane_skew.sh --tag 75123e5
```

## 트러블슈팅

- **`PLANE SKEW DETECTED` (exit 3)** — 런타임 평면이 릴리즈 태그와 다르다. 위 절차
  **3단계(런타임 동기화)**를 실행하고 게이트를 다시 돌린다. 출력의
  `N commit(s) behind / M ahead`가 어느 방향으로 얼마나 어긋났는지 알려준다.

- **★ stale-local-tag 함정(false pass)** — 게이트는 태그를 `--tag-repo`의 **로컬
  ref**에서 peel한다. 그 저장소가 옮겨진 릴리즈 태그를 아직 fetch하지 않았다면 peel이
  **stale**해 게이트가 **거짓 통과**할 수 있다. 실측(2026-07-24): 워크스테이션 체크아웃의
  로컬 `v1`은 여전히 stale `0e9ec21`로 peel됐다 → 만약 `--tag-repo`를 그 체크아웃으로
  두면 런타임(0e9ec21)==태그(0e9ec21)로 **통과**하지만 둘 다 main보다 뒤다. 방어:
  - peel 전 `--tag-repo` 쪽에서 태그를 authoritative하게: `git -C <tag-repo> fetch --tags`
    (본인 쪽에서 실행), **또는**
  - 검증한 릴리즈 대상 커밋을 **명시 전달**: `--tag <sha>`, **또는**
  - fresh clone에서 peel, **또는**
  - 로컬 ref를 건드리지 않고 push된 태그를 읽기: `git ls-remote --tags <remote> vN`
    (그리고 `^{}` deref 줄의 커밋을 `--tag`로 전달).
  게이트는 read-only 원칙상 **스스로 fetch하지 않는다** — authoritative 태그 확보는
  운영자 책임이다.

- **`not a git repo` / `cannot resolve … rev` (exit 3)** — 경로/rev 오타는 조용히
  통과시키지 않고 **fail-closed**로 막는다(G-26). `--src`/`--tag`를 확인한다.

## 관련

- GOTCHAS **G-43**(두 평면·합의된 대응 4항) · **G-44**(태그≠브랜치 push) ·
  **G-35**(게이트 비공허 — 변이로 실증) · **G-36**(장기 상주 serve NVML 소실).
- C-2 경계: 기존 기술(Docker/Compose) + **문서화된 매뉴얼**로 이식성 확보, 전용
  installer 앱 = post-MVP(01-architecture-and-scope §7.1, NFR-DEPLOY-002).
