# CV-Infra 배포 매뉴얼 (C-2) — 진입점

> **누구를 위한 문서인가**: 이 플랫폼을 **처음 보는 운영자**가 GPU 호스트 한 대에
> 제어 평면을 올리는 절차. 명령을 위에서 아래로 따라가면 된다.
>
> **범위**: `docker/compose.yaml` 상단이 선언한 4단계 흐름 **①provision → ②consent →
> ③compose up → ④selftest** 전체. 세부 주제 두 개는 별도 문서로 분리돼 있다 —
> [`gpu-profiles.md`](gpu-profiles.md)(다른 GPU 카드로 이식) · [`plane-sync.md`](plane-sync.md)(릴리즈
> 태그와 실행 평면의 스큐). 호스트 프로비저닝의 *내부*(apt 핀·sudo 드롭인·드라이버
> 재정렬)는 [`scripts/workstation_setup/README.md`](../../scripts/workstation_setup/README.md).
>
> **C-2 고도**: 자동 **감지 + 매뉴얼 선택**. 완전자동 installer 는 명시적 post-MVP
> (`NFR-DEPLOY-002`)이므로 이 문서에 없고 앞으로도 만들지 않는다.
>
> 요구사항 원문은 재서술하지 않고 ID 로만 참조한다 — `REQ-DEPLOY-001~012` ·
> `NFR-DEPLOY-001~005` · `REQ-SELFTEST-001~004`(정본 = `requirements-analysis-cv-infra/`).

---

## 0. 이 배포가 약속하는 것 / 약속하지 않는 것

| | |
|---|---|
| **약속함** | **한 명령 기동 + 문서화된 동의 한 단계**(`REQ-DEPLOY-001`, M5 §7). "완전 원커맨드"가 아니라 이것이 정직한 형태다 — 동의는 자동화될 수 없기 때문이다(아래 §3-②). |
| **약속함** | 호스트에 **CUDA·Isaac Sim 을 설치하지 않는다**. 필요한 것은 드라이버 + 컨테이너 런타임뿐(`NFR-DEPLOY-005`). |
| **약속함** | 배포는 **어느 기계인지 모른다**. GPU 지식은 `profiles/**` 한 곳에만 있다(`REQ-DEPLOY-003`, [gpu-profiles.md](gpu-profiles.md)). |
| **약속함** | **④ `cv-infra selftest` 가 외부 SUT 0 의존으로 라운드트립을 돈다**(p5c15 실측: 4/4 green, 27–43 s). 단 **빌트인 stub SUT 이미지 핸들**(`CV_SELFTEST_SUT_IMAGE`)을 배포가 공급해야 하고, 없으면 **추측하지 않고 exit 3** 이다 — §3-④. |
| **약속 안 함** | **러너 이미지(15.5 GB)를 바이트 그대로 새 호스트에 옮기는 경로**. 배송 경로는 **(C) 새 호스트에서 재빌드**로 확정됐고(결정 D-6), 재빌드는 **입력 집합**(베이스 다이제스트 + `uv.lock` + apt 버전 + 소스 커밋)만 핀한다 — **결과 이미지 다이제스트는 재현되지 않는다**. §7. |
| **약속 안 함** | 인증. 제출 API 는 authn 이 없다(단일 호스트 MVP). 그래서 기본 공개 주소가 `127.0.0.1` 이다. |
| **약속 안 함** | **캐시가 저절로 데워지는 것.** 잡은 공유 캐시를 읽기 전용 원본으로만 쓰고 자기 사본을 버린다(stateless) — **운영자가 `②''` 를 `warm` 으로 돌리기 전까지 모든 잡이 콜드 비용을 낸다**(실측 141 s vs 28 s, §3-②''·§9). |

---

## 1. 사전 요구 (호스트)

| 항목 | 요구 | 확인 명령 |
|---|---|---|
| OS | 프로비저닝 스크립트가 단언하는 배포판/**코드네임 집합**/아키텍처 (핀 = `scripts/workstation_setup/common.sh`) | `. /etc/os-release; echo $ID/$VERSION_CODENAME $(dpkg --print-architecture)` |
| NVIDIA 드라이버 | **R580 브랜치**(플로어 이상 **AND** major == 580), open kernel module. 프로비저닝은 드라이버를 **절대 설치·업그레이드하지 않는다** — 단언만 한다 | `nvidia-smi --query-gpu=driver_version --format=csv,noheader` |
| Docker CE + Compose v2 | 핀된 버전(`common.sh`) | `docker version --format '{{.Server.Version}}'` · `docker compose version` |
| NVIDIA Container Toolkit | `nvidia` 런타임 등록 | `docker info --format '{{json .Runtimes}}' \| grep -o '"nvidia"'` |
| GPU 패스스루 | 컨테이너가 GPU 를 본다 (호스트에 CUDA 툴킷을 **깔 필요가 없다**는 뜻이지, 깔려 있으면 안 된다는 뜻이 아니다) | 핀은 `common.sh` 의 `CV_CUDA_TEST_IMAGE`/`_DIGEST` 다:<br>`docker run --rm --gpus all nvidia/cuda:12.8.1-base-ubuntu24.04@sha256:133c78a0…` `nvidia-smi -L` |
| 디스크 | 러너 이미지 15.5 GB + 잡 산출물(MCAP·mp4)이 자라는 여유 | `df -h /` |
| 네트워크 | `nvcr.io`(Isaac 베이스) · `download.docker.com` · `nvidia.github.io` · PyPI · **`omniverse-content-production.s3-us-west-2.amazonaws.com`**(씬 자산 — 캐시가 콜드면 여기서 받는다) 로의 egress | `for u in https://nvcr.io/v2/ https://download.docker.com/linux/ubuntu/ https://nvidia.github.io/libnvidia-container/ https://pypi.org/simple/ https://omniverse-content-production.s3-us-west-2.amazonaws.com/ ; do curl -s -o /dev/null -m 20 -w "$u %{http_code}\n" "$u"; done`<br>(`nvcr.io` 는 **401 이 정상** — 익명 토큰 교환 전이다) |

`①` 이 이 전부를 멱등하게 충족시킨다(아래).

---

## 2. 4단계 흐름 한눈에

```
⓪  git clone <이 저장소> <deploy-root>            소스 확보 (①~④ 전부가 이 체크아웃 안에서 돈다)
①  scripts/workstation_setup/provision.sh          호스트 선결(Docker CE + Toolkit + 패스스루 + Isaac 베이스 pull)
②' 설정:  docker/.env 작성  +  scripts/detect_gpu.sh >> docker/.env
②  scripts/consent/accept_eula.sh                 NVIDIA EULA — 운영자만, 1회
②'' CV_EULA_CONSENT=<동의어> scripts/measure/warm_cache.sh "$CV_ISAAC_CACHE_ROOT" provision|warm
                                                   캐시 트리 생성(+chown 1234). 없으면 모든 잡이 거부된다
③  CV_SOURCE_REVISION="$(git rev-parse HEAD)" \
     docker compose -f docker/compose.yaml up -d --build     ← 제어 평면 기동
④  CV_SELFTEST_SUT_IMAGE=<stub 이미지> cv-infra selftest   외부 SUT 0 의존 라운드트립
```

**순서가 중요하다.** `②'`(설정)를 `②`(동의) **앞에** 두는 것이 기본 순서다 —
`docker/.env.example` 의 USE 블록과 같다. 이미 동의를 마친 호스트에서 설정을 나중에
넣어야 한다면 **§3-②'의 경고**를 반드시 읽어라(파일을 다시 만들면 동의 키가 사라진다).

### 2-1. 어느 단계가 "호스트 1회"이고 어느 단계가 "OS 사용자마다"인가

> 2026-08-15 clean-host 실행(`DoD-P5-08`)이 만든 표다. 그 전까지 이 문서는 단계별 스코프를
> **구분하지 않았고**, 그래서 두 번째 운영자가 `①`을 실행하려다 막히는 것이 정상인지
> 고장인지 알 방법이 없었다. **정상이다** — `①`은 애초에 그 사람의 단계가 아니다.

| 단계 | 스코프 | 두 번째 OS 사용자가 다시 해야 하나 | 근거(실측) |
|---|---|---|---|
| `⓪` clone | **OS 사용자마다** | **예** — 자기 홈에 자기 체크아웃 | 익명 clone 1.0 s |
| `①` provision | **호스트 1회 · root 권한**(단, 이미 충족된 항목은 특권 호출 없이 skip) | **아니오 — 실행할 수 없다** | `sudo -n true` → **exit 1**. NOPASSWD 드롭인은 프로비저닝을 한 사용자에게만 부여된다. 이미 충족된 호스트에서는 §1 **확인 명령**만 돌리면 된다(또는 `①` 이 전부 skip 으로 흐른다) |
| `②'` `.env` | **OS 사용자마다** | **예** — 경로·포트·프로젝트 이름이 전부 그 사용자 것 | 아래 §2-2 격리 파라미터 |
| `②` 동의 | **OS 사용자마다** | **예** — 레코드가 `$HOME/.cv-infra/eula-consent.json` 이다 | 새 사용자에서 `check_consent.sh` **exit 3**, `③` 이 **loud 거부**. 이것은 결함이 아니라 NEG-2 가 작동한 것이다 |
| `②''` 캐시 트리 | **OS 사용자마다** | **예** — `CV_ISAAC_CACHE_ROOT` 가 그 사용자 홈이면 그 트리도 새로 만든다 | 다른 사용자의 웜 캐시는 **읽을 수도 없다**(홈 퍼미션 `drwxr-x---`) |
| `③` 기동 | **OS 사용자마다** | **예** — 자기 compose 프로젝트·포트 | 아래 §2-2 |
| `④` self-test | **배포마다** | **예** | — |

**한 호스트에 두 배포를 올릴 때 반드시 분리해야 하는 것**(§2-2):

| 노브 | 왜 |
|---|---|
| `CV_COMPOSE_PROJECT` | 컨테이너/네트워크 이름 접두 |
| `CV_PUBLISH_PORT` | 포트 충돌 |
| `CV_STATE_DIR` · `CV_OUT_DIR` · `CV_ISAAC_CACHE_ROOT` · `CV_ISAAC_CACHE_SCRATCH_ROOT` | 상태·산출물·캐시 |
| **`CV_ORCHESTRATOR_IMAGE`** | ⚠ **놓치기 쉽다.** 기본값이 `cv-infra-orchestrator:local` 이라 두 번째 배포의 `up --build` 이 **첫 배포의 이미지 태그를 가져간다**(2026-08-15 실측으로 확인된 충돌). 태그를 분리하라 |

⚠ **그리고 이것은 §3-③ 의 "다른 제어 평면이 떠 있으면 안 된다"와 긴장 관계다.** 두 평면이
같은 docker 데몬을 보면 **부팅 시 라벨 스윕이 상대의 잡 컨테이너를 지운다.** 두 배포를 정말
공존시킬 거면 **`up` 직전에 `docker ps -a --filter label=cv-infra.job_id -q | wc -l` 이 0 인지
확인**하고(= 상대가 잡을 돌리고 있지 않다), 0 이 아니면 **기다려라**. 이 조건은 순서로만 막힌다.

---

## 3. 단계별 절차

### ⓪ 소스 확보

```bash
git clone <이 저장소의 URL> <deploy-root>
cd <deploy-root>
git rev-parse HEAD          # 이 값이 이 배포의 릴리즈 ref 다 — 기록해 두어라
```

- `①`~`④` 의 스크립트·compose 파일·프로파일이 **전부 이 체크아웃 안에 있다.** §1 의 사전요구
  표가 인용하는 핀(`scripts/workstation_setup/common.sh`)도 마찬가지다 — 즉 **§1 을 제대로
  확인하려면 먼저 여기를 해야 한다.**
- 여기서 나온 SHA 가 `③` 의 `CV_SOURCE_REVISION` 이자 평면 스큐 게이트의 릴리즈 ref 다.

### ① 호스트 프로비저닝

```bash
cd <deploy-root>                     # 이 저장소의 체크아웃
bash scripts/workstation_setup/provision.sh
```

- 멱등하다. 이미 충족된 호스트에서 다시 돌려도 안전하다.
- **이 단계는 호스트 1회 · root 권한이다(§2-1).** 이미 프로비저닝된 호스트에 **새 OS 사용자로**
  들어온 것이라면 **이 단계를 실행하지 마라 — 실행할 수도 없다**(`sudo -n true` → exit 1).
  대신 **§1 표의 확인 명령만** 돌려서 호스트가 이미 충족돼 있음을 확인하라. 전부 읽기 전용이다.
- **sudo 드롭인은 "남은 특권 작업이 있을 때만" 필요하다.** 모든 특권 동작 앞에 그 결과가
  이미 참인지 읽기전용으로 확인하고, 참이면 특권 호출 없이 `SKIP (already true, checked)`
  로그만 남긴다 — 이미 충족된 호스트는 특권 호출 **0** 으로 `①` 을 통과한다. 남은 게 있으면
  드롭인 1회 설치가 필요하다(비대화 SSH 는 암호 프롬프트에 답할 수 없다). 드롭인은
  **템플릿**이고 계정 이름은 설치 시점에 치환한다. 절차 = `scripts/workstation_setup/README.md` Step A.
- 드라이버가 요구 브랜치가 아니면 **loud 하게 멈춘다**. 프로비저닝은 드라이버를 고치지
  않는다 — 유일하게 허가된 드라이버 스크립트는 `realign_driver_r580.sh` 다.
- ⚠ **`①` 이 만드는 캐시 스캐폴드는 `②'` 의 `CV_ISAAC_CACHE_ROOT` 와 같은 트리가 아닐 수 있다**
  (2026-08-19 실측). 이 단계는 자기 **기본** 루트(`$HOME/docker/isaac-sim`)에 디렉토리를 만들고,
  실제 배포가 쓰는 트리는 `②''` 가 `.env` 의 값에 대해 따로 만든다. 두 값이 다르면 `①` 이 만든
  것은 **아무도 쓰지 않는 고아 디렉토리**가 된다(잡은 정상 동작한다 — 낭비일 뿐이다).
  같은 트리를 쓰고 싶으면 `②'` 에 그 기본 경로를 적거나, `①` 을 `CV_ISAAC_CACHE_ROOT=<원하는 경로>`
  를 앞에 붙여 실행하라. **어느 쪽이든 소유권을 맞추는 것은 `②''` 이지 `①` 이 아니다**(G-15).

### ②' 설정 — `docker/.env`

`docker/compose.yaml` 은 자기 디렉토리의 `docker/.env` 를 자동으로 읽는다.
템플릿에 **모든 노브가 주석으로 자기설명** 되어 있다.

```bash
cp docker/.env.example docker/.env      # ← 동의(②) 전에만 안전하다. 아래 경고 참조
$EDITOR docker/.env                     # REQUIRED 6개를 채운다
bash scripts/detect_gpu.sh >> docker/.env   # 측정된 GPU 노브를 append (손으로 쓰지 말 것)
chmod 600 docker/.env
```

REQUIRED 6개(전부 **호스트 절대경로**):

| 키 | 무엇을 넣나 | 고르는 기준 |
|---|---|---|
| `CV_RUNNER_IMAGE` | 러너 이미지 핀 | 기본값 없음(의도적). 빌드해서 기록한 태그/다이제스트를 **그대로** 적는다 |
| `CV_MAX_CONCURRENT` | 운영자 동시성 상한 | **보수적으로 시작**(예: 2). 스케줄러는 이 값을 내리기만 하고 절대 올리지 않는다 |
| `CV_STATE_DIR` | SQLite 스토어가 사는 디렉토리 | **재배포 사이에 고정**하라. 회귀 baseline 이 이 파일에 산다 — 경로가 바뀌면 비교 기준이 조용히 사라진다 |
| `CV_OUT_DIR` | 잡 산출물 루트 | 자라는 디렉토리(MCAP·mp4) |
| `CV_ISAAC_CACHE_ROOT` | **웜 캐시 = 읽기 전용 복사 원본** | 러너 uid(1234) 소유여야 한다. **`mkdir` 만으로는 부족하다 — `②''` 가 만든다**(아래) |
| `CV_ISAAC_CACHE_SCRATCH_ROOT` | 잡별 쓰기 가능 캐시 스크래치 | `k × 웜캐시 크기` 만큼 여유 필요 (실측: 자명 잡 1건이 **930 MB** 를 잠시 쓴다) |

세 가지 함정:

1. **경로는 컨테이너 안에서 동일한 절대경로로 마운트된다.** 장식이 아니다 — 제어 평면은
   이 경로들을 **호스트 데몬**에 넘겨 러너를 스폰한다. 컨테이너 내부 경로를 적으면
   호스트의 엉뚱한 곳이 마운트되고 `result.json` 이 돌아오지 않는다.
2. **디렉토리를 `up` 전에 만들어라**(`mkdir -p`). 없으면 dockerd 가 `root:root` 로
   만들어 버리고, 비루트로 도는 러너가 나중에 조용히 깨진다.
3. **`KNOB=` (빈 값)은 "미설정"이 아니다.** 옵션 노브는 set-but-empty 이면 부팅이
   loud 하게 실패한다. 안 쓸 노브는 **줄을 주석 처리**하라.
4. **시나리오 YAML 이 사는 디렉토리도 제어 평면 컨테이너에서 보여야 한다.**
   `cv-infra submit` 은 시나리오 파일의 **부모 디렉토리를 stage-5 오라클 앵커로 항상
   함께 보낸다** — 그 시나리오가 커스텀 오라클을 **안 쓰더라도**. 제어 평면은 그 경로를
   *자기 파일시스템에서* 검증하므로(G-26), 컨테이너 안에 없으면 잡이 시작 전에 죽는다:
   ```
   infra_error: runner seam crashed: ValueError: oracle_plugin_dir <dir> does not exist
                or is not a directory (expected the scenario directory holding the custom oracle .py, D-1)
   ```
   ⇒ 시나리오를 **위 4개 루트 중 하나 아래**(가장 쉬운 것은 `CV_OUT_DIR`)에 두거나,
   그 디렉토리를 `compose.yaml` 의 주석이 보여주는 **동일-절대경로 `:ro` 바인드**로
   추가하라(CI 러너 워크스페이스가 그런 경우다). 실측 = §9.

> ### ⚠ 이미 동의(②)를 마친 호스트에서 설정을 고칠 때
>
> `②` 는 동의 키 2개를 **`docker/.env` 안에** 쓴다. 그 키는 **운영자만** 만들 수 있다.
> 그러므로 그 뒤로는:
>
> * `cp docker/.env.example docker/.env` · `> docker/.env` · 에디터의 "새 파일로 저장"
>   **금지** — 동의 키가 소멸하고, 복구하려면 **운영자를 다시 불러 `②`를 재실행**해야 한다.
> * **백업도 만들지 마라** — `.bak`·복사본·`docker inspect` 덤프 전부. **동의를 운영자
>   없이 복원할 수 있는 사본은 그 자체가 자동수락 경로다**(G-68 ④). 복원이 필요하면
>   `②`를 다시 돌린다 — 그게 NEG-2 의 요지다.
> * 대신 **멱등 upsert** 로 고쳐라(같은 관용구가 `scripts/consent/accept_eula.sh::env_set`
>   에 있다 — 해당 키 줄만 지우고 다시 append, 그리고 `install -m 600`). 아래는 편집본이
>   동의 키를 **잃지 않았음을 확인한 뒤에만** 원본을 갈아끼우는 형태다(백업의 안전성을
>   백업 없이 얻는 방법):
>
>   ```bash
>   tmp=$(mktemp); trap 'rm -f "$tmp"' EXIT
>   grep -v -E '^[[:space:]]*(export[[:space:]]+)?CV_MAX_CONCURRENT=' docker/.env > "$tmp"
>   printf 'CV_MAX_CONCURRENT=%s\n' "<새 값>" >> "$tmp"
>   for k in ACCEPT_EULA PRIVACY_CONSENT; do          # 키 이름만 — 값은 보지 않는다
>     grep -qE "^$k=." "$tmp" || { echo "REFUSE: consent key '$k' lost"; exit 1; }
>   done
>   install -m 600 "$tmp" docker/.env
>   ```
>   ⚠ 이 관용구는 키를 **파일 끝으로 옮긴다** — 앞에 있던 프로방넌스 주석과 분리된다
>   (p5c14 실측). 주석이 붙어 있는 키(예: `CV_RUNNER_IMAGE`)는 **제자리 치환**으로 고치고
>   주석의 Id·revision 도 같이 갱신하라(p5c15 실측 절차 = 이 문서 §9).
> * 고친 **직후 매번** 게이트를 다시 돌려라:
>   `bash scripts/consent/check_consent.sh; echo $?` → **0** 이어야 한다.
> * `scripts/detect_gpu.sh >> docker/.env` 는 순수 append 라 안전하다.
>   ⚠ **`>>` 만 써라. `2>&1` 을 붙이지 마라** — 이 스크립트는 진단 로그를 **stderr** 로 내보내고,
>   그걸 파일로 흘리면 `KEY=VALUE` 가 아닌 줄이 `.env` 에 섞인다. compose 는 그때
>   `failed to read docker/.env: line N: unexpected character …` 로 **loud 하게** 죽는다
>   (2026-08-15 실측 — 조용히 무시하지 않는다는 점은 좋은 거동이다).

### ② 동의 (NVIDIA EULA) — 운영자만

```bash
bash scripts/consent/accept_eula.sh          # 대화형: 라이선스 제시 → 명시적 입력 → 기록
bash scripts/consent/check_consent.sh; echo $?   # 게이트: 0 = 기록됨, 3 = 없음
```

- 이 저장소의 **어떤 파일에도 수락 값이 들어있지 않다**. 이미지에도 박지 않는다.
  CI 가 이 단계를 합성해서 건너뛰는 것도 금지다(`NFR-DEPLOY-004`).
- 스크립트는 (1) 무엇을 수락하는지 제시 → (2) 명시적 결정 입력 → (3) **identity +
  timestamp 기록** → (4) 그제서야 런타임 값을 `docker/.env` 에 쓴다.
- 기록은 `.env` 와 **분리된 별도 파일**이다(호스트측 감사). 런타임 부트 게이트의
  단일 진실원은 **env** 이고, env 가 없으면 러너의 부트 가드가 Isaac 기동을 거부한다(exit 3).
- 비대화 호스트(SSH 등)에서는 **운영자 신원을 함께** 줘야 한다 — 익명 자동 "yes" 는
  거부된다. 사용법은 `bash scripts/consent/accept_eula.sh --help`.

**동의가 없으면 `③`은 시작조차 못 한다.** 이것이 설계된 실패다:

```
$ docker compose -f docker/compose.yaml config
error while interpolating services.orchestrator.environment.PRIVACY_CONSENT:
required variable PRIVACY_CONSENT is missing a value: no operator consent on this host —
run scripts/consent/accept_eula.sh (NEG-2: this deployment never auto-accepts)
```

2026-08-15 clean-host 실측: 동의가 없는 새 OS 사용자에서 `③`(`up -d --build`)은 **exit 1** 로
멈췄고 **컨테이너 0 · 네트워크 0 · 빌드 0** 이었다 — 부분적으로 올라가다 마는 상태가 없다.

### ②'' 캐시 트리 — 이것을 빼면 모든 잡이 거부된다

```bash
CV_ISAAC_CACHE_ROOT=<②'에 적은 그 경로>          # 이 셸엔 .env 가 로드돼 있지 않다 (아래 ⚠)
CV_EULA_CONSENT=<②의 프롬프트에 입력한 그 단어> \
CV_MEASURE_IMAGE=<③ 이전에 빌드한 러너 이미지 태그> \
  bash scripts/measure/warm_cache.sh "$CV_ISAAC_CACHE_ROOT" provision   # 빈 트리 + chown 1234
#   ... 또는 warm  (빈 트리를 만든 뒤 씬을 한 번 부팅해 자산·셰이더까지 채운다)
ls -ln "$CV_ISAAC_CACHE_ROOT/cache/"        # kit / home / computecache 셋이 uid 1234 여야 한다
```

> ⚠ **새 호스트가 여기서 두 번 걸린다**(2026-08-19 실측, 두 번째 호스트의 첫 실행):
> * `$CV_ISAAC_CACHE_ROOT` 는 **`docker/.env` 안에만 있고 당신 셸엔 없다** — compose 가 읽는
>   파일이지 셸이 source 하는 파일이 아니다. 비운 채로 부르면 `exit 1`(cache root required).
> * 이 스크립트의 **러너 이미지 기본값은 이 프로젝트 첫 호스트의 옛 태그**다. 새 호스트엔
>   그 태그가 없으므로 `CV_MEASURE_IMAGE` 를 주지 않으면 docker 가 **레지스트리에서 찾다가**
>   `pull access denied … may require 'docker login'` 로 **exit 125** 를 낸다 — 인증 문제로
>   읽히지만 실제 원인은 *"그 태그는 여기 없다"* 이다. 그래서 **`②''` 는 러너 이미지를 빌드한
>   뒤에** 온다(§7 순서).

- **왜 필요한가**: 제어 평면은 잡마다 `cache/kit`·`cache/home`·`cache/computecache` **세 티어를
  복사**해 러너에 준다. 세 디렉토리가 없으면 잡은 시작 전에 거부된다:
  `cache base tier … does not exist … the warm cache was never provisioned`.
  **`mkdir -p` 로 때우지 마라** — 소유자가 러너 uid(1234)가 아니면 러너가 캐시에 쓰지 못하고
  캐시가 **조용히 꺼진다**(G-15/G-34). 이 스크립트는 docker 루트 헬퍼(`--user 0`)로 chown 하므로
  **호스트 sudo 가 필요 없다.**
- ⚠ **이 스크립트는 `②` 와 별개의 동의 게이트를 갖는다.** `$HOME/.cv-infra/eula-consent.json` 을
  **읽지 않고** per-run env 만 본다 — 즉 운영자는 같은 결정을 **두 번** 표명해야 한다. 값은
  **운영자만** 입력한다(NEG-2). 미설정이면 **exit 3**.
- **`provision` vs `warm`**:

| 모드 | 트리 | 첫 잡 | 언제 |
|---|---|---|---|
| `provision` | 비어 있음 | **콜드 — 실측 120 · 141 · 291 s**(§9, 3회 — 하나의 수가 아니다) | 캐시 비용을 정직하게 재고 싶을 때 |
| `warm` | 씬 클로저까지 채움 | 웜 — 실측 28 s | 실사용 배포. 워밍 자체가 Isaac 부팅 1회 값을 낸다 |

> ### ⚠ 공유 캐시는 **정상 운영으로 스스로 데워지지 않는다** (2026-08-15 실측)
> 잡은 공유 트리를 **읽기 전용 복사 원본**으로만 쓴다: 잡별 스크래치로 `cp -a` → 러너가 거기에
> 쓰고 → **잡이 끝나면 스크래치를 통째로 버린다**(stateless, `NFR-EXEC-002`). 실측에서 잡은
> 스크래치에 **930 MB** 를 만들었지만 잡이 끝난 뒤 **공유 캐시는 여전히 0 바이트 · 파일 0 개**였다.
> ⇒ **`provision`(빈 트리)으로 두면 모든 잡이 영원히 콜드 비용을 낸다.** 데우는 경로는
> `warm_cache.sh … warm` **한 가지뿐**이고, 그것은 운영자가 명시적으로 돌려야 한다.

### ③ 기동

```bash
cd <deploy-root>                    # 저장소 루트에서 (빌드 컨텍스트가 루트다)
docker compose -f docker/compose.yaml config     # 드라이런: 렌더 결과 확인 (exit 0 이어야 함)
CV_SOURCE_REVISION="$(git rev-parse HEAD)" \
  docker compose -f docker/compose.yaml up -d --build
```

- **`CV_SOURCE_REVISION` 접두는 빌드할 때만 필요하고, 빠뜨리면 빌드가 loud 실패한다.**
  이 값이 제어 평면 이미지에 `org.opencontainers.image.revision` 으로 박혀 "이 컨테이너가
  도는 코드는 어느 커밋인가"에 답한다(G-66). compose 는 명령을 실행할 수 없어 값을
  스스로 만들 수 없고, `docker compose build` 는 `--build-arg`/`--label` 을 **계승하지
  않는다** — 그래서 `compose.yaml` 의 `build.args` 가 환경에서 받아 넘긴다.
  `.git` 이 없는 배포(타르볼)면 **기록된 릴리즈 sha 를 그대로** 넣어라.
  **`docker/.env` 에는 넣지 마라** — 그 순간부터 모든 빌드가 그 옛 커밋을 주장한다
  (스큐 게이트가 잡지만, 애초에 적어 두지 않는 것이 싸다).
- 빌드 없이 재기동(`up -d`·`ps`·`down`)에는 이 접두가 **필요 없다**.
- ⚠ **`up --build` 는 "빌드했다"를 뜻하지 않는다.** compose 는 레이어 캐시가 맞으면 전부
  재사용하고 **초 단위로 끝난다**(실측 2026-08-15: **0.44 s, 전 레이어 CACHED** — 그 배포는
  이미지 바이트를 하나도 만들지 않았다). 소스가 바뀌지 않았거나 같은 데몬이 이미 그 레이어를
  갖고 있으면 이것이 정상 동작이다. **정말로 다시 구워야 할 때**(호스트 이관 검증·베이스나
  apt 인덱스가 움직였는지 확인·캐시 오염 의심)는 `up` 에 `--no-cache` 가 **없으므로** 제품
  경로의 2행 형태를 쓴다:
  ```bash
  CV_SOURCE_REVISION="$(git rev-parse HEAD)" \
    docker compose -f docker/compose.yaml build --no-cache    # 같은 compose.yaml·같은 build.args
  docker compose -f docker/compose.yaml up -d                 # 새 이미지로 교체
  ```
  실측(2026-08-18, M-1 평면): 제어 평면 `build --no-cache` **12.97 s** → `up -d` **0.61 s**.
  베이스는 다이제스트 핀이라 **재-pull 되지 않는다**(`--no-cache` 는 우리 레이어만 다시 만든다)
  — 그래서 새 이미지도 베이스 레이어 **4개는 공유**하고 **우리 레이어 3개만 새로** 생긴다.
  `docker build` 로 대체하지 마라: 그러면 검증한 것이 제품 경로가 아니게 된다(G-66).
- ⚠ **`up` 은 상주 컨테이너를 교체한다 = 컨테이너에 붙어 있던 감사·계측이 그 순간 죽는다**
  (G-73). 재기동 절차는 §4 의 `5) egress 감사 재무장`까지가 **한 세트**다 — 재무장 없이
  나간 런은 감사되지 않았고, 그 사실은 나중에 `read` 가 **exit 3** 으로만 알려준다.

- 올라오는 것은 **제어 평면 하나**(orchestrator = 제출/스케줄러 API + 운영 read model)뿐이다.
- 러너와 SUT 는 **compose 서비스가 아니다** — 잡마다 오케스트레이터가 호스트 데몬에
  직접 스폰하고, 잡 전용 브리지 네트워크에 격리한다. `compose.yaml` 에 `runner:` 를
  추가하면 자원인지 동시성 k 가 조용히 깨진다(파일 상단 주석 참조).
- **기동 전 반드시 확인**: 같은 호스트에 **다른 제어 평면이 떠 있으면 안 된다**.
  두 평면이 같은 docker 데몬을 보면 부팅 시의 라벨 스윕이 **상대의 잡 컨테이너를 지운다**.
  > **의도적으로 두 배포를 공존시키는 경우**(§2-1)는 이 금지의 예외가 아니라 **더 엄격한
  > 조건**이다: 스윕이 보는 것은 compose 프로젝트가 아니라 **라벨**이므로 프로젝트 이름을
  > 나눠도 보호되지 않는다. 아래 첫 명령이 **0** 일 때에만 `up` 하라 — 즉 **상대가 잡을
  > 돌리고 있지 않은 순간**에만. 그리고 `CV_ORCHESTRATOR_IMAGE` 를 반드시 분리하라(§2-1 표).
  ```bash
  docker ps -a --filter label=cv-infra.job_id -q | wc -l      # 0 이어야 함
  docker ps --filter publish="${CV_PUBLISH_PORT:-8000}" --format '{{.Names}}'   # 포트 점유자
  ss -ltn | grep ":${CV_PUBLISH_PORT:-8000}"
  # 포트/이름으로는 안 보이는 평면까지 쓸어보는 유일한 방법 —
  # 실측(2026-08-14): 이 호스트에 포트를 공개하지 않은 serve 가 23일째 살아 있었고
  # 위 두 명령 어디에도 나타나지 않았다.
  for c in $(docker ps --format '{{.Names}}'); do
    docker top "$c" -eo pid,cmd 2>/dev/null | grep -q orchestrator.serve && echo "control plane: $c"
  done
  ```
  발견하면 **`docker stop` 만** 한다(`rm` 금지 — 증적·앵커가 딸려 나간다). 그리고 그
  평면과 **같은 `CV_STATE_DIR` 을 쓸 거라면 반드시 stop 이 끝난 뒤에 `up`** 하라:
  같은 SQLite 에 writer 가 둘이 되는 것은 순서로만 막힌다.

### ④ self-test

```bash
# 배포에 stub SUT 이미지가 아직 없다면 먼저 굽는다 (레시피·계약 = docker/selftest_stub/README.md)
CV_SOURCE_REVISION="$(git rev-parse HEAD)" docker build \
  -f docker/selftest_stub/Dockerfile --build-arg CV_SOURCE_REVISION="$CV_SOURCE_REVISION" \
  -t cv-infra-selftest-stub:<tag> docker/selftest_stub

# 라운드트립 (CLI 는 제어 평면 이미지 자신 — §4 의 일회용 컨테이너 관용구)
docker compose -f docker/compose.yaml run --rm --no-deps \
  -e CV_SELFTEST_SUT_IMAGE=cv-infra-selftest-stub:<tag> \
  orchestrator cv-infra selftest --api http://orchestrator:8000
```

- **종료 코드가 계약이다**: `0` 라운드트립 green · `1` stub 판정 fail · `2` 계약/422 ·
  `3` 인프라(핸들 미설정·오케스트레이터 부재 등). 판정은 stub 의 대답이 아니라 **Isaac GT**
  에서 다시 계산되므로 거짓말하는 stub 은 통과가 아니라 실패한다.
- **`CV_SELFTEST_SUT_IMAGE` 를 어디에 두느냐가 함정**이다. 이 값은 봉투를 만드는
  **클라이언트 프로세스**가 읽는다(오케스트레이터 서비스가 아니다) — 위처럼 일회용 CLI
  컨테이너에 `-e` 로 준다. 미설정이면 **추측하지 않고 exit 3**(소비자 이미지로의 폴백은
  `NFR-SELFTEST-001` 위반이라 코드가 금지한다).
  > ⚠ **`docker/.env` 에 적는 것으로는 되지 않는다** — 2026-08-15 실측. 이 키는
  > `docker/.env.example` 에도 `compose.yaml` 의 `environment:` 블록에도 **없으므로**
  > compose 가 컨테이너로 넘기지 않는다. `.env` 에 적어 둔 채 `-e` 없이 부르면
  > CLI 컨테이너 안에서 그 변수는 **UNSET** 이고 self-test 는 **exit 3** 이다.
  > `.env` 의 그 줄은 *"이 배포가 쓰는 stub 은 무엇인가"* 라는 **기록**으로만 가치가 있다.
- **외부 SUT·소비자 저장소 의존 0**: 잡은 러너 + stub 두 컨테이너만 잡 전용 네트워크에
  띄우고, 러너 마운트는 캐시 스크래치 · `job_spec.json` · 결과 디렉토리뿐이다(§9 실측).
- self-test 결과는 **운영 대시보드 3면**(`/monitor`·`/monitor.json`·`cv-infra monitor`)에서
  `self_test=true` 로 식별된다(`REQ-SELFTEST-004`).
- **자명 시나리오**: 로봇이 목표 지점에 스폰되고 미션이 즉시 성공한다. 재는 것은 **배포가
  살아있는가**(Isaac 기동·러너 실행·SUT 경계 DDS·결과 회수)이지 항법 품질이 아니다.
  ⚠ 부작용으로 **미션이 ~0.2 s 라 녹화 경로는 사실상 검증되지 않는다** — MCAP/mp4 는
  비거나 아예 안 생긴다(§9 실측). 녹화까지 확인하려면 실제 SUT 잡을 돌려라.

---

## 4. 기동 직후 확인 (④ 가 답하지 않는 것들)

> `④` 는 *"라운드트립이 도는가"* 에 답한다. 아래는 *"어떤 구성으로 도는가"* 에 답한다 —
> 둘 다 봐라. 특히 `serve-config` 한 줄은 마운트·캐시·k·러너 이미지 핀을 한 번에 보여준다.

> **`cv-infra` CLI 는 어디서 오나**: 이 배포는 호스트에 아무것도 설치하지 않는다
> (§0). CLI 는 **제어 평면 이미지 자신**이다 — 같은 이미지를 일회용으로 띄워 쓴다.
> 그러면 CLI 는 컴포즈 네트워크 위의 형제 컨테이너가 되므로 **API 주소가
> `127.0.0.1` 이 아니라 서비스 이름 `orchestrator`** 다.
>
> ```bash
> alias cvi='docker compose -f docker/compose.yaml run --rm --no-deps orchestrator cv-infra'
> cvi --help                       # 핀된 CLI 표면
> ```
> (호스트에 별도로 설치한 CLI 가 있으면 그때는 `--api http://127.0.0.1:8000` 이다.)

```bash
# 1) 컨테이너가 살아있나
docker compose -f docker/compose.yaml ps

# 2) 부팅이 무엇을 물고 올라왔나 — 구조화된 한 줄 (이 배포의 관찰 계약)
docker logs cv-infra-orchestrator 2>&1 | grep -m1 serve-config
#    k / max_concurrent / store_path / out_dir / cache_root / cache_scratch_root /
#    runner_image / vram_per_instance_mb / 동의 키의 "이름"(값 아님) /
#    reconciliation 카운트가 전부 들어있다. 여기 찍힌 값이 곧 실제 구성이다.

# 3) API 가 응답하나 (authn 없음 — loopback 에서만)
#    ⚠ 8000 은 기본값일 뿐이다. CV_PUBLISH_PORT 를 바꿨으면 그 포트를 쓴다 —
#    한 호스트에 두 배포가 있으면 8000 을 치는 것은 '남의 평면'을 확인하는 것이다(실측).
curl -sS -o /dev/null -w '%{http_code}\n' "http://127.0.0.1:${CV_PUBLISH_PORT:-8000}/openapi.json"   # 200
#    ⚠⚠ 200 은 "내 평면에 닿았다"는 뜻이 아니다 (2026-08-19 실측). 이 데스크탑에서 8000 은
#    에디터(Cursor/VS Code 계열)의 자동 포트포워딩이 잡고 있었고, 그 뒤에 있던 것은 **다른
#    기계(원격 워크스테이션)의 제어 평면**이었다 — `docker ps --filter publish=8000` 은 비어
#    있었고 `ss -ltn` 의 소유자는 docker 가 아니라 에디터였다. 즉 커널 포트 확인만으로는
#    가짜를 못 거른다. 확실한 한 줄은 **평면에게 자기 카드를 물어보는 것**이다:
curl -sS "http://127.0.0.1:${CV_PUBLISH_PORT:-8000}/monitor.json" \
  | python3 -c 'import json,sys;r=json.load(sys.stdin)["resources"];print(r["vram_total_mib"],"MiB")'
#    -> 이 호스트의 nvidia-smi 총 VRAM 과 같아야 한다. 다르면 남의 평면을 보고 있는 것이다.

# 4) 운영 뷰
cvi monitor --api http://orchestrator:8000

# 5) 외부 egress 감사를 무장한다 — 제어 평면을 새로 띄웠으면 **반드시 다시** (G-73)
#    ⚠ 처음 하는 호스트라면 감사 사이드카 이미지를 먼저 굽는다. 이 이미지는 저장소에
#      Dockerfile 로 존재하지 않고 2줄짜리 레시피가 scripts/netns_audit.sh 헤더에 있다
#      (CV_NETNS_AUDIT_IMAGE 항목). 없이 부르면 docker 가 레지스트리를 찾다가 exit 125 다.
bash scripts/netns_audit.sh arm cv-infra-orchestrator
#    arm 은 컨테이너 정체성(id + started_at)을 레코드 파일에 남긴다:
#      ${CV_NETNS_AUDIT_RECORD_DIR:-/tmp}/cv-netns-audit.<chain>.<container>.arm  (+ .history)
#    read 는 그 레코드를 살아있는 컨테이너와 대조하고, 다르면 exit 3 으로 거부한다
#    ("0 이 아니라 VOID"). 레코드를 증적으로 남길 거면 이 두 파일을 함께 보관하라.
#    이후 아무 때나 — 단 **감사 대상 런의 시작 시각을 대야 한다**:
#      bash scripts/netns_audit.sh read cv-infra-orchestrator --since <그 런의 산출물 경로>
#    카운터는 arm 시점에 0 이 되므로 arm **보다 먼저** 시작한 런은 애초에 안 들어있다.
#    --since 는 그 창의 시작을 이름 붙이는 것이고, read 는 armed_at <= 창 시작을 단언한다
#    (아니면 exit 3 "LATE ARM"). 값은 **런이 남긴 파일 경로**를 권한다 — mtime 은 증거고
#    직접 타이핑한 타임스탬프는 주장이다. --since 없이 부르면 exit 2(usage).
#    계측이 살아있는지 의심되면 양성 대조:  … probe … → 카운터가 움직인 뒤 다시 arm
```

> **재무장은 배포 재기동 절차의 일부다.** `up`/`up --build`/`up -d` 는 상주 컨테이너를
> **교체**하고, 교체된 컨테이너는 **새 netns** 라 규칙도 카운터도 없다. 2026-08-15 에
> 이것이 실제로 일어났고 그 뒤 런 **5건이 전부 미감사**로 나갔다 — 아무 것도 실패하지
> 않았기 때문에 아무도 눈치채지 못했다. 위 `arm` 을 `up` 과 **같은 절차 안에서** 돌려라.

> `serve-config` 한 줄을 **읽지 않고 넘어가지 말 것.** 마운트나 캐시가 조용히 빠진
> 배포는 겉보기에 정상으로 돌면서 측정을 전부 콜드로 만든다. 이 줄이 그것을 눈에
> 보이게 하려고 존재한다.

**실제 SUT 로 관통**(`④` 는 외부 SUT 없이 도는 대신 자명 시나리오다 — 녹화·항법처럼
미션 길이가 필요한 것은 여기서만 검증된다):

```bash
# 시나리오는 제어 평면이 볼 수 있는 경로에 둔다 (§3-②' 함정 4)
mkdir -p "$CV_OUT_DIR/_smoke" && cp <scenario>.yaml "$CV_OUT_DIR/_smoke/"
cvi submit "$CV_OUT_DIR/_smoke/<scenario>.yaml" --api http://orchestrator:8000 --wait
# 도는 동안 다른 셸에서: 잡 컨테이너 2개(runner + sut)가 잡 전용 네트워크에 뜬다
docker ps --filter label=cv-infra.job_id \
  --format '{{.Names}}\t{{.Image}}\t{{.Networks}}'
# 끝나면: 산출물은 호스트의 CV_OUT_DIR 아래 잡 디렉토리로 돌아온다
ls "$CV_OUT_DIR"/cvj-*/result/result.json
```

`submit --wait` 의 종료 코드가 곧 계약이다 — 0 PASS / 1 FAIL(SUT 판정) /
3 INFRA. **3 이면 SUT 가 아니라 배포를 의심하라**(§8).

---

## 5. 일상 운영

| 하고 싶은 것 | 명령 |
|---|---|
| 상태 | `docker compose -f docker/compose.yaml ps` |
| 로그 | `docker logs -f cv-infra-orchestrator` (json-file, 회전 상한은 `.env` 노브) |
| 중지 | `docker compose -f docker/compose.yaml stop` |
| 내리기 | `docker compose -f docker/compose.yaml down` (네트워크까지 제거) |
| 설정 변경 반영 | `.env` 를 **upsert 로** 고친 뒤(§3-②' 경고) `up -d` 재실행 → **감사 재무장**(§4-5) |
| 제어 평면 코드 갱신 | 체크아웃 갱신 → `CV_SOURCE_REVISION="$(git rev-parse HEAD)" docker compose -f docker/compose.yaml up -d --build` → **감사 재무장**(§4-5) |
| 캐시를 무시하고 **정말로 다시 굽기** | `CV_SOURCE_REVISION=… docker compose … build --no-cache` → `up -d` → **감사 재무장**(§3-③ ⚠ 블록) |
| **러너 이미지 핀 교체** | 새 이미지 빌드 → `.env` 의 `CV_RUNNER_IMAGE` 한 줄을 upsert → `up -d` → **감사 재무장**. 스큐 확인: `bash scripts/check_plane_skew.sh --src <deploy-root> --tag <release-sha> --image <ref> --orchestrator-image <ref>` |
| CLI 한 번 쓰기 | `docker compose -f docker/compose.yaml run --rm --no-deps orchestrator cv-infra <cmd> --api http://orchestrator:8000` (§4) |

> ⚠ **`up -d --build` 를 `CV_SOURCE_REVISION` 없이 돌리면 빌드가 실패한다** — 그것이
> 의도다. 2026-08-14 까지는 반대였다: compose 가 만든 제어 평면 이미지는 리비전 라벨이
> **비어 있었고**(`compose build` 는 `--build-arg`/`--label` 을 계승하지 않는다),
> 그래서 **문서화된 제품 경로가 임시 `docker build` 보다 프로방넌스가 낮았다**(G-66).
> 지금은 `compose.yaml` 의 `build.args` 가 환경의 값을 넘기고, 비면 Dockerfile 이
> 빌드를 멈춘다. 이미 태깅된 이미지를 쓰고 싶으면 `CV_ORCHESTRATOR_IMAGE` 로
> 가리켜라 — 그때 `up` 은 빌드하지 않으므로 접두도 필요 없다.

**동의를 남긴 채 스택을 내려라.** compose 파일이 동의 키를 요구하므로 `down`·`ps` 같은
서브커맨드도 키가 있어야 돈다. 호스트에서 동의를 지우기 **전에** 스택을 먼저 내려라.

**배포 평면**(YAML / 런타임 / **제어 평면 이미지** / 러너 이미지)이 따로 논다는 사실과
그 동기화 절차는 [`plane-sync.md`](plane-sync.md) 에 있다. 릴리즈 태그를 옮겼다고 두
이미지 안의 코드가 따라오지 않는다 — 이미지는 **리빌드로만** 움직인다.

---

## 6. 다른 GPU 카드로

`scripts/detect_gpu.sh` 가 살아있는 GPU 에 물어보고 맞는 `profiles/*.yaml` 을 고른다.
지원 추가 = **`profiles/` 에 파일 하나 추가**(코드·compose 수정 0). 미측정 카드는
숫자를 비워 둔 채 프로파일만 두고, VRAM 가드는 **꺼진 채로** 둔다 — 추측한 예산으로
도는 것보다 낫다. 전체 절차 = [`gpu-profiles.md`](gpu-profiles.md).

---

## 7. 다른 호스트로 옮기기 — 배송 경로 = **(C) 재빌드** (결정 D-6, 2026-08-14)

제어 평면 이미지는 어디서나 소스에서 재빌드하면 된다(아래 실측: 161 MB / 8.3 s).
러너 이미지(15.5 GB)도 **같은 방식으로 옮긴다 — 즉 옮기지 않고 새 호스트에서 다시 굽는다.**

**왜 C 인가**: A·B 는 **NVIDIA Isaac Sim 베이스 레이어를 제3자에게 재배포**하는 형태이고
그것이 EULA 상 허용되는지 **확인된 바 없다**. C 는 각 호스트가 베이스를 `nvcr.io` 에서
**직접 pull** 하므로 재배포가 **발생하지 않는다**.

> ### ⚠ C 가 핀하는 것 / 핀하지 않는 것 (D-6 문면 그대로)
> **핀된다 = 입력 집합**: 베이스 다이제스트(`isaac-sim:5.1.0@sha256:…`) + 빌더 베이스
> 다이제스트 + `uv.lock` + **apt 버전 8개**(`docker/runner/Dockerfile` 의 `apt VERSION PINS`)
> + 소스 커밋(`org.opencontainers.image.revision`).
> **핀되지 않는다 = 결과 이미지 다이제스트.** 같은 입력으로 두 번 구우면 Image Id 가 다르다
> (실측 §9). ⇒ ***"당신이 돌리는 이미지는 우리가 테스트한 바로 그 바이트다" 라고 쓰지 마라 —
> C 에서 그것은 거짓이다.***
> 실측된 것은 **동작 동등성**이다(§9: 두 빌드의 apt 매니페스트 252/252 · 전체 dpkg 413/413 ·
> wheel 50 파일 · 같은 잡의 verdict/지표 동일). **핀되지 않는 것도 있다** — 이 레이어의
> transitive 262 패키지는 비핀이고, ROS/우분투 아카이브는 대체된 버전을 인덱스에서 내리므로
> 언젠가 재빌드가 **loud 하게 실패한다**(그때의 재핀 절차 = Dockerfile 의 같은 블록).

**재빌드 레시피**(러너 = [`plane-sync.md`](plane-sync.md) ③ · 제어 평면 = 위 `③` · stub SUT =
[`docker/selftest_stub/README.md`](../../docker/selftest_stub/README.md) §5). 새 호스트에서는
`①`→`②'`→`②`→(러너·stub 두 이미지 빌드)→`②''`→`③`(제어 평면은 여기서 빌드된다)→`④` 순서다. **`②''` 를 이 목록에서 빠뜨리면 모든 잡이 거부된다** — 그리고 그 단계는 러너 이미지를 필요로 하므로 빌드 **뒤**에 와야 한다(§3-②'' ⚠).

**미채택 경로와 그 실측 비용** (D-6 이 A 를 영구 폐기하지는 않았다 — 재론 트리거는
① EULA 재배포 조항이 허용으로 확인되거나 ② 소비자가 재빌드 시간을 수용 불가로 요구할 때):

| 경로 | 전송/시간 | 인증·전제 | 재현성 | 상태 |
|---|---|---|---|---|
| **A. 레지스트리 push/pull**(GHCR 등) | 압축 전송량: Isaac 베이스만으로 **7.62 GB**(실측 — 레지스트리 매니페스트의 압축 blob 합, 17 레이어, 최대 단일 레이어 7.50 GB). 우리가 얹는 레이어는 **비압축 661 MB**(실측, `docker history`) → 압축 후 크기 **미실측**. 소요: **미실측**. 참고 앵커 = 이 호스트의 GHCR 콜드 pull 실측 **0.70 MB/s**(p5c9, 다른 이미지) — 두 실측을 산술 투영하면 **≈3 시간**이나 이는 **투영이지 실측이 아니다** | push 권한 필요(write:packages 등). **NVIDIA 베이스 레이어를 제3자 레지스트리에 재배포하는 것이 EULA 상 허용되는지 미확인** — 이 경로를 고르기 전에 반드시 확인할 것 | pull 은 **다이제스트 고정**이라 바이트 동일 보장. 가장 강한 재현성 | 저장소에 push 경로 **0** |
| **B. `docker save` / `load`** | tar **15,550,096,384 B(15.55 GB)**, `save` 파이프 **30.4 s**(실측, 디스크 미기록). `load` 소요·전달 매체 소요 **미실측** | 없음(파일 복사). 15.5 GB 를 옮길 매체·대역폭 필요 | Image Id 보존 = 바이트 동일 | 즉시 가능. 스크립트 없음 |
| **C. 새 호스트에서 재빌드** ← **채택(D-6)** | 베이스 pull **7.62 GB 압축**(실측) + apt/pip 수신(**미실측**) + **빌드 2m10s(캐시 웜 베이스, 레이어 캐시 없음) / 1m6s(`--no-cache`)**(실측 §9, 베이스가 이미 로컬인 호스트) | `nvcr.io` egress. 익명 pull 가능 | **바이트 동일하지 않다**(위 ⚠ 블록). 동작 동등성은 실측(§9) | `docker/runner/Dockerfile` 로 가능 — **오늘의 정본 경로** |

> 셋 다 **문서화된 수동 절차**다. 자동화(릴리즈 파이프라인)는 D-6 이 정한 C 위에서
> 만들 일이다.

---

## 8. 잘 안 될 때

| 증상 | 원인 | 조치 |
|---|---|---|
| `config`/`up` 이 `required variable ACCEPT_EULA/PRIVACY_CONSENT is missing` | 동의 미기록(또는 `.env` 를 덮어써서 소멸) | `②` 실행. 이건 버그가 아니라 게이트다 |
| `up` 이 `port is already allocated` | 다른 프로세스/컨테이너가 그 포트를 점유 | `docker ps --filter publish=8000` 로 범인 확인 → 내리거나 `CV_PUBLISH_PORT` 변경 |
| `up` 이 `could not select device driver "nvidia"` | 컨테이너 툴킷 미설치/미등록 | `①` 재실행. 이 배포는 GPU 호스트를 전제한다(`NFR-DEPLOY-005`) |
| 잡이 시작되자마자 exit **3** | 인프라/플랫폼 문제 — 동의 env 부재, 오케스트레이터 다운 등. **SUT FAIL(1)과 구분되는 코드다** | `docker logs cv-infra-orchestrator` + `curl .../envelopes/<id>` 의 `infra_error` |
| `infra_error: … oracle_plugin_dir <dir> does not exist …` (잡 컨테이너가 아예 안 뜬다) | 시나리오 디렉토리가 **제어 평면 컨테이너 안에 없다**. `submit` 은 커스텀 오라클을 안 써도 그 경로를 함께 보내고, 검사는 컨테이너 안에서 돈다(G-26) | 시나리오를 `CV_OUT_DIR` 등 이미 마운트된 루트 아래로 옮기거나 동일-절대경로 `:ro` 바인드 추가 — §3-②' 함정 4 |
| NEG-2 점검("수락 리터럴이 박혀 있지 않다"는 `grep`)이 **배포 루트에서 매치를 낸다** | 배포된 호스트에서는 당연하다 — 동의 값이 사는 곳이 바로 `docker/.env`(git-ignored)다. 그 점검은 **커밋된 파일**에 대한 것이고, 그대로 돌리면 운영자의 동의 값이 터미널·로그에 찍힌다 | **`git grep`** 으로 돌려라(추적 파일만 본다). 굳이 `grep -r` 을 쓸 거면 `--exclude=.env`. 저장소 안의 정본 점검은 `tests/negative/test_eula_gate.py` 이며 그 테스트가 **문서에도 리터럴을 못 쓰게** 막는다(이 표에 리터럴이 없는 이유) |
| 컨테이너 안 앱이 캐시/디렉토리에 못 쓴다 | 마운트 부모를 dockerd 가 `root:root` 로 생성 | 해당 호스트 디렉토리를 **미리** 만들고 러너 uid 소유로 맞춘 뒤 재기동 |
| 캐시가 웜인데 잡마다 느리다 | 캐시를 **`:ro` 로 러너에 물리면 캐시가 꺼진다**(읽기 폴백이 아니라 비활성화) | 러너에는 **쓰기 가능한 잡별 사본**을 준다. 공유 트리는 복사 *원본*으로만 |
| 옵션 노브를 넣었는데 부팅이 죽는다 | `KNOB=` 빈 값 | 줄을 **주석 처리**(미설정 = 문서화된 기본값) |
| 실행 코드가 옛날 것 같다 | 평면 스큐 | [`plane-sync.md`](plane-sync.md) + `scripts/check_plane_skew.sh --src <deploy-root> --tag <sha> --image <ref> --orchestrator-image <ref>` |
| `cv-infra run` 이 `result.json … is unreadable or non-canonical: … extra_forbidden` 로 **exit 3**(잡이 `verdict=pass` 였는데도) | **평면 스큐의 결과 방향**(G-74) — 러너 이미지에 구워진 코드가 지금 체크아웃의 계약과 다르다(대개 이미지가 더 오래됐고, 삭제된 필드를 계속 내보낸다). `extra="forbid"` 재검증이 그 문서를 통째로 거부하므로 **이미 얻은 판정이 버려진다** | stderr 둘째 줄이 시키는 대로: 이미지 스탬프(`org.opencontainers.image.revision`)를 체크아웃과 대조 → **러너 이미지를 다시 빌드/pull** 하고 잡을 다시 돌려라. 판정을 소급 복구할 방법은 없다 |
| 스큐 게이트가 `runtime-plane path is not a git repo: '…/cv-infra-p2-src/cv-infra-workspace'` 로 **exit 3** | `--src` 의 **기본값이 이 프로젝트 워크스테이션의 디렉토리 레이아웃**(`$HOME/cv-infra-p2-src/cv-infra-workspace`)이다. 체크아웃이 다른 곳에 있는 배포에서는 항상 틀린다 | **`--src <deploy-root>` 를 명시하라.** 2026-08-15 clean-host 실측에서 처음 드러났다(그 전까지는 기본값이 우연히 맞는 호스트에서만 돌았다) |
| `netns_audit.sh read` 가 `audit chain … is ABSENT` / `no arm record` / `container was REPLACED or RESTARTED since arm` / **`LATE ARM`** 으로 **exit 3** | 넷 다 같은 사실을 말한다 — **그 런은 감사되지 않았다**. 앞 셋은 컨테이너를 교체하는 모든 명령(`up`·`up -d`·`up --build`)이 netns 를 새로 만들기 때문(G-73), **`LATE ARM` 은 컨테이너가 그대로여도** 무장이 런보다 **뒤**였기 때문이다(카운터는 arm 에서 0 이 된다) | 다시 `arm` 하고, **감사가 필요한 런을 다시 돌려라**. 이미 나간 런의 감사 결과를 소급해 만들 방법은 없다(0 이 아니라 VOID) |
| `netns_audit.sh read` 가 `--since / CV_NETNS_AUDIT_SINCE is REQUIRED` 로 **exit 2** | 카운터만 보고는 **어느 런을 감사한 것인지** 말할 수 없다. 무장 이전의 런은 카운터에 없는데도 0 으로 읽혔다(QA p5c16 D-3) | 감사 대상 런의 시작을 대라 — 그 런의 산출물 경로(`--since <…/result.json>`, mtime 사용)가 가장 좋고, 타임스탬프 문자열도 받는다 |
| `up --build` 이 `CV_SOURCE_REVISION=<source commit sha> is required` 로 죽는다 | 스탬프 없는 이미지를 만들지 않겠다는 게이트(G-66) | 접두를 붙여 다시: `CV_SOURCE_REVISION="$(git rev-parse HEAD)" docker compose … up -d --build`. `.git` 이 없으면 기록된 릴리즈 sha 를 넣어라 |

---

## 9. 부록 — 이 매뉴얼이 검증된 조건 (프로방넌스, 2026-08-14)

> 아래는 **증거이지 설정값이 아니다**. 이 문서의 어떤 절차도 특정 호스트·특정 카드에
> 분기하지 않는다(`DoD-P5-09`). 값은 전부 위 명령들의 실행 결과다.

- 채널 = SSH. 증적 = 워크스테이션 `~/cv-infra-p2-out/p5c14/{t2,t4}/*.log`.
- `①` 은 이미 충족된 호스트에서 확인만 함(드라이버 R580 브랜치·Docker 28.3.3·
  Compose v2.39.2·`nvidia` 런타임 등록·`--gpus all` 스모크 exit 0).
- `②` = 운영자가 직접 실행(2026-08-14). 게이트 exit 0, 기록에 identity + timestamp 존재.
- `②'` = REQUIRED 6 + `detect_gpu.sh` append. `detect_gpu` 가 살아있는 카드를 질의해
  프로파일을 선택하고 **측정 출처 주석과 함께** VRAM 노브를 써 넣는 것을 관측.
- 동의 키만 제거한 동일 구성에서 `config` 는 **exit 1 로 loud 실패**(§3-② 인용문).
- 제어 평면 이미지 빌드: **160,840,981 B / 8.3 s**(ad-hoc `docker build`),
  **7.5 s**(`compose up --build`, 캐시 웜) — 둘 다 빌드 중 임포트 가드 통과.

**`③`·§4 는 2026-08-14 에 실제로 실행됐다**(이 프로젝트 최초의 `docker compose up`,
증적 `~/cv-infra-p2-out/p5c14/t4/`):

- `up -d --build` **exit 0 / wall 7.5 s** — 네트워크 `cv-infra_default` 생성,
  컨테이너 `cv-infra-orchestrator` 기동, `127.0.0.1:8000` 공개.
- `serve-config` 실측: `k=2` `max_concurrent=2` `runner_image=cv-infra-runner:p5c15`
  `vram_per_instance_mb=6000` `consent_env_present=[ACCEPT_EULA, PRIVACY_CONSENT]`
  (**이름만**, 값 아님) `reconciliation` 전 카운트 **0**. 5개 바인드 전부 host==container
  동일 절대경로.
- **관통 1건**: `submit --wait` → 잡 전용 네트워크 위에 **runner + SUT 두 컨테이너**
  스폰(라벨 `cv-infra.job_id`·`cv-infra.ros_domain_id=1`) → `result.json` + MCAP + mp4 가
  호스트 `CV_OUT_DIR` 아래로 회수 → `report_outcome=pass`, **exit 0**. 제출~종료 **60 s**
  (웜 캐시 시딩 1.84 GB / **0.31 s**).
- 이 관통에서 **제어 평면·클라이언트 어느 쪽도 호스트 venv 가 아니었다** — 둘 다
  `cv-infra-orchestrator:local` 컨테이너다.
- 첫 시도는 **실패했고 그 실패가 §3-②' 함정 4·§8 두 줄을 낳았다**: 컨테이너 밖 경로에
  둔 시나리오는 `oracle_plugin_dir … does not exist` 로 잡이 뜨기도 전에 죽는다.

**`④`·재빌드는 2026-08-15 에 실제로 실행됐다**(증적 `~/cv-infra-p2-out/p5c15/t7/`, 채널 SSH,
호스트 etri6000, 소스 커밋 `ac442ee`):

- **재빌드 3종**: 러너 `cv-infra-runner:p5c16`(`sha256:1e9750b3…`, **2m10s**) · 같은 입력
  `--no-cache` 2회차 `:p5c16-rebuild2`(`sha256:ef66de24…`, **1m6s**) · 제어 평면
  `cv-infra-orchestrator:local`(`sha256:0cbc0b0d…`, `up -d --build` **7.9 s**) · stub SUT
  `cv-infra-selftest-stub:p5c16`(931 MB). **apt 핀 8개가 실제로 해석됐다**(첫 실빌드).
- **재빌드 동등성**(D-6 ①): Image Id 는 **다르고**(예고된 대로) 나머지는 같다 —
  apt 레이어 매니페스트 **252/252 동일** · 이미지 전체 dpkg **413/413 동일**(비핀
  transitive 포함) · 이미지 안 `cv_infra/**.py` **50 파일 sha256 동일**(그리고 소스 트리와도
  동일) · `import isaacsim; import cv_infra` + pydantic 2.11.7 / numpy 1.26.0 동일 ·
  **같은 self-test 잡의 verdict·지표 동일**(`path_len_m` 마지막 자리까지).
- **3평면 스큐 게이트**: 리빌드 **전 exit 3**(②' 제어 평면 미스탬프 + ③ 러너 17 커밋 뒤짐),
  리빌드 **후 exit 0**(세 평면 전부 `ac442ee`). 옛 호출형(`--orchestrator-image` 없음)은
  **exit 2**. G-66 수리가 **제품 경로(`compose up --build`)에서** 확인됐다.
- **`④` 라운드트립 4/4 green**: `report_outcome=pass exit=0`, **27.7 / 42.7 / 27.7 / 27.7 s**
  (제출~CLI 종료). 잡마다 컨테이너 **2개**(러너 + stub SUT)가 잡 전용 브리지 네트워크에
  뜬다. 러너 `/dev/shm` **1 GiB**(`CV_RUNNER_SHM_SIZE`), stub 은 docker 기본 64 m.
  미설정 배포에서는 **exit 3**(핸들을 추측하지 않는다).
- **`/dev/shm` 실측**(2 s 폴링, 미션 포함): 러너 피크 **7,405,568 B / 1 GiB = 0.69 %**
  (SUT 배리어 통과 시점에 49 KB → 7.4 MB 로 점프 = Fast DDS data-sharing 세그먼트),
  stub 피크 **675,840 B / 64 MiB = 1.0 %**. ⇒ 1 g 는 헤드룸이다(단 이 미션은 0.2 s 로
  짧다 — 긴 잡의 피크는 미실측).
- **부트 프로파일**(실측 4회 중 대표): `simulation_app_init` 13.1 s · `scene_load` 7.1–7.8 s ·
  `robot_spawn` 1.0 s · `sut_readiness_wait` **0.36 s** · `first_render_frame` 0.28 s ·
  `mission` 0.20 s.
- **`.env` 취급**: 파일을 다시 만들지도, 백업하지도 않았다(G-68 ④). `CV_RUNNER_IMAGE` 는
  **제자리 치환 + 프로방넌스 주석(Id/revision) 동시 갱신**, 편집 전후 `check_consent.sh`
  **exit 0**, 줄 수·모드(600) 불변.

**캐시-없는 재빌드는 2026-08-18 에 M-1 평면에서 실행됐다**(증적 `~cvm1/cv-infra-m1-evidence/p5c16/`,
채널 SSH, 소스 커밋 `4a257fb`) — 이 문서의 §3-③ ⚠ 블록이 그 실행에서 나왔다:

- `compose build --no-cache` **12.97 s** → `up -d` **0.61 s**(대조: 같은 평면의 `up --build`
  2026-08-15 = **0.44 s / 전 레이어 CACHED**). 새 이미지는 이전 이미지와 **베이스 레이어 4개를
  공유하고 우리 레이어 3개는 고유**하다(`docker inspect … .RootFS.Layers` 집합 연산) —
  즉 `--no-cache` 는 다이제스트-핀된 베이스를 다시 받지 않고 **우리 레이어만** 다시 만든다.
- **러너 이미지도 같은 평면에서 캐시 없이 구웠다**: `docker build --no-cache`(제품 경로 =
  `plane-sync.md` ②) **60.78 s**, 15.46 GB, 이전 러너 이미지와 레이어 **19 공유 / 4 고유**
  (공유분 = 다이제스트-핀된 Isaac 베이스). **apt 버전 핀 8개가 다시 해석됐다** — D-6 (C)
  입력 집합이 두 번째로 실측됐다. 그 뒤 3평면 스큐 게이트 **exit 0**(셋 다 `4a257fb`).
- 재빌드 후 `④` self-test **exit 0**(`report_outcome=pass`) **2회**: 제어 평면만 재빌드한
  상태에서 CLI wall **291.13 s**, 러너까지 재빌드한 뒤 **120.11 s** — 둘 다 **콜드 캐시**다.
  지배항은 매번 `robot_spawn`(**243.67 s** / **74.92 s**; 2026-08-15 콜드는 97.87 s).
  ⇒ **콜드 비용은 하나의 수가 아니다** — 같은 호스트·같은 잡·같은 빈 캐시에서 **120~291 s**
  로 흔들린다(측정 중 같은 GPU 에 다른 텐넌트가 있었다). 판정·지표는 그 사이에도 불변
  (`verdict=pass`, `path_len_m=1.9078142372702626e-05` 마지막 자리까지 동일).
- 잡 종료 후 공유 캐시 **0 B / 파일 0 개** — §3-②'' ⚠ 블록이 두 번 더 재현됐다.

**⓪~④ 전체를 "처음부터" 완주한 것은 2026-08-15 이 처음이다** — 같은 호스트의 **새 OS 사용자**
(`cvm1`, uid 2001, 새 홈·새 체크아웃·새 동의·**콜드 캐시**)로 수행. 증적
`~cvm1/cv-infra-m1-evidence/` (채널 SSH, 소스 커밋 `ac442ee`). **이 문서의 §2-1 스코프 표와
§3-⓪·§3-②'' 는 그 실행이 만든 것이다.**

- `①` **미실행 — 실행 불가**(`sudo -n true` exit 1). §1 확인 명령 전항만 통과: 드라이버
  **580.159.03**(open KMD) · Docker **28.3.3** · Compose **v2.39.2** · `nvidia` 런타임 등록 ·
  `--gpus all` 스모크 **exit 0 / 0.33 s** · 여유 **1.6 T**.
- **NEG-2 양성 실증**: 동의 **전** `③` → **exit 1**, 컨테이너/네트워크/빌드 **전부 0**.
  동의 **후** 같은 명령 → **exit 0**. 레코드는 `consent_channel=interactive`(사람이 입력).
- `③` `up -d --build` **exit 0 / 0.44 s** — 단, **모든 레이어가 공유 데몬의 BuildKit 캐시
  히트**였다(이 호스트에서의 실제 제어평면 빌드 비용은 위 T7 블록의 7.9 s).
- **`④` 콜드 라운드트립 `exit 0` / CLI wall 141.05 s** (웜 대조군 27.7 s = **5.09×**).
  부트 단계별 콜드 vs 웜: `simulation_app_init` 14.84/13.16 · `scene_load` 20.32/7.76 ·
  **`robot_spawn` 97.87/1.00 ← 콜드 비용의 지배항** · `first_render_frame` 3.25/0.27 ·
  `sut_readiness_wait` 3.41/0.36 · `mission` 0.20/0.20 · 부트 합계 **138.26/39.30**.
  ⇒ **콜드 페널티는 씬 다운로드가 아니라 robot_spawn 에 몰려 있다.**
- **판정·지표는 콜드/웜이 동일**: `verdict=pass`, `path_len_m=1.9078142372702626e-05`
  (T7 웜 실행과 **마지막 자리까지 동일**), `collision_count=0`.
- **캐시는 채워지지 않았다**: 잡 중 스크래치 **930,416,284 B** → 잡 종료 후 공유 캐시
  **0 B / 파일 0 개**(§3-②'' ⚠ 블록의 근거).
- **격리 substrate**: 잡 전용 브리지 `cvj-…`, 러너 shm **1 GiB** / stub **64 MiB**,
  `ros_domain_id=42`(상주 배포의 55 와 다름), 잡 종료 후 컨테이너·네트워크 잔존 **0**.
- **외부 SUT 0 의존**: 잡 컨테이너 2개(`cv-infra-runner:p5c16` + `cv-infra-selftest-stub:p5c16`),
  소비자 이미지 참여 **0**, `-e` 없이 부르면 **exit 3**. egress 감사 `external_packets=0`.
- **3평면 스큐 게이트 `exit 0`**(`--src` 명시 필요 — §8 마지막 행). 음성 대조 2종
  (옛 러너 이미지 · 미스탬프 제어평면) 각각 **exit 3**.
- **격리 실측**: 상주 배포(포트 8000)와 포트(8021)·프로젝트(`cv-infra-m1`)·상태/산출물/캐시
  경로·**제어평면 이미지 태그**(`:m1`)를 전부 분리. 상주 평면의 `:local` 태그 Id **무이동**,
  상주 컨테이너 8개 **무정지**, 삭제 **0건**.
- ⚠ **이 실행이 증명하지 않는 것**: docker 데몬·이미지 레이어·BuildKit 캐시·드라이버·커널·
  호스트 프로비저닝이 **전부 공유**된다. **"완전히 새 기계"의 증거가 아니다.**
