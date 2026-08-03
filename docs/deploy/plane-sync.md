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
      **[VERIFY]** 정확한 재기동 **명령줄**은 여전히 미확정이다 — 2026-08-03 T1은 위
      3제약과 부팅 env 계약(`serve-config` 한 줄에 값이 전부 있다)까지 실측했으나,
      consent 값이 운영자 소유라 **실집행하지 못했다**. 다음 운영자 실행 때 그 명령을
      여기에 확정 기입한다(발명 금지 — G-24).
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
  bash <evidence>/gate.sh --src <src> --tag <target>     # 재동기화 전 positive control
  ```

- **`not a git repo` / `cannot resolve … rev` (exit 3)** — 경로/rev 오타는 조용히
  통과시키지 않고 **fail-closed**로 막는다(G-26). `--src`/`--tag`를 확인한다.

## 관련

- GOTCHAS **G-43**(두 평면·합의된 대응 4항) · **G-44**(태그≠브랜치 push) ·
  **G-35**(게이트 비공허 — 변이로 실증) · **G-36**(장기 상주 serve NVML 소실).
- C-2 경계: 기존 기술(Docker/Compose) + **문서화된 매뉴얼**로 이식성 확보, 전용
  installer 앱 = post-MVP(01-architecture-and-scope §7.1, NFR-DEPLOY-002).
