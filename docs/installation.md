# 설치 매뉴얼 — GPU 호스트 한 대에 cv-infra 올리기

> **누구를 위한 문서인가**: 이 플랫폼을 **처음 설치하는 운영자**. 위에서 아래로 한 번
> 따라가면 `cv-infra selftest` 가 초록으로 도는 배포가 남는다.
>
> **범위 = 최초 설치의 최소 경로뿐이다.** 심화(두 배포 공존·평면 스큐·다른 GPU 카드·
> 재배포·전체 트러블슈팅)는 이미 [C-2 배포 매뉴얼](deploy/README.md)에 있고, 이 문서는
> 그 793줄에서 *처음 한 번*에 필요한 것만 뽑아 순서대로 놓은 것이다. 갈림길마다 C-2 의
> 해당 절을 가리킨다 — **막히면 거기로 가라.**
>
> **이 문서의 수치는 전부 인용이다.** 값 옆의 대괄호가 원천이며, 측정된 호스트·잡이
> 함께 적혀 있다. 당신의 호스트에서 같은 수가 나온다는 뜻이 아니다(CLAUDE §2-4) —
> **자기 호스트의 값은 자기가 잰다.** 원천 표기: `[C-2 §x]` = [deploy/README.md](deploy/README.md),
> `[프로파일]` = [`profiles/*.yaml`](../profiles), `[프로비저닝]` =
> [scripts/workstation_setup/README.md](../scripts/workstation_setup/README.md).
>
> 요구사항 원문은 재서술하지 않고 ID 로만 참조한다 — `REQ-DEPLOY-001~012` ·
> `NFR-DEPLOY-001~005` · `REQ-SELFTEST-001~004`.

---

## 0. 이 설치가 약속하는 것

- **한 명령 기동 + 문서화된 동의 한 단계**가 정직한 형태다. 동의는 자동화될 수 없다(§2-②).
- 호스트에 **CUDA·Isaac Sim 을 설치하지 않는다.** 필요한 것은 드라이버 + 컨테이너 런타임뿐
  (`NFR-DEPLOY-005`). `cv-infra` CLI 조차 호스트에 설치하지 않는다 — 제어 평면 이미지를
  일회용으로 띄워 쓴다(§3).
- 배포는 **어느 기계인지 모른다.** GPU 지식은 `profiles/**` 한 곳에만 있다(`REQ-DEPLOY-003`).
- **약속하지 않는 것**: 인증(제출 API 에 authn 이 없다 — 그래서 기본 공개 주소가
  `127.0.0.1` 이다) · 캐시가 저절로 데워지는 것(§2-④) · 이미지 바이트 동일성(§2-③). [C-2 §0]

---

## 1. 요구사항

| 항목 | 요구 | 확인 명령 |
|---|---|---|
| OS | Ubuntu `noble` / `jammy` 중 하나 · amd64 [프로비저닝 Pins] | `. /etc/os-release; echo $ID/$VERSION_CODENAME $(dpkg --print-architecture)` |
| NVIDIA 드라이버 | **R580 브랜치** — `580.65.06` 이상 **AND** major == 580, open kernel module. 설치는 **당신 몫**이다(프로비저닝은 드라이버를 절대 건드리지 않고 단언만 한다) [프로비저닝 Pins] | `nvidia-smi --query-gpu=driver_version --format=csv,noheader` |
| GPU / VRAM | NVIDIA GPU 1장. **잡 1개가 무는 VRAM 은 카드마다 다르다** — 기측정 예: RTX PRO 6000 Blackwell **6000 MiB** · GeForce RTX 4080 **4659 MiB** · A100 **미실측(빈 값)** [프로파일]. 당신 카드가 미실측이면 그대로 배포하고 나중에 재면 된다([gpu-profiles.md](deploy/gpu-profiles.md) §4) | `nvidia-smi --query-gpu=name,memory.total,driver_version,compute_cap --format=csv,noheader` [프로파일] |
| Docker CE + Compose v2 | 핀된 버전(`28.3.3` / `v2.39.2`) [프로비저닝 Pins] — `①` 이 설치한다 | `docker version --format '{{.Server.Version}}'` · `docker compose version` |
| NVIDIA Container Toolkit | `1.17.8-1`, `nvidia` 런타임 등록 [프로비저닝 Pins] — `①` 이 설치한다 | `docker info --format '{{json .Runtimes}}' \| grep -o '"nvidia"'` |
| GPU 패스스루 | 컨테이너가 GPU 를 본다 | `docker run --rm --gpus all <common.sh 의 CV_CUDA_TEST_IMAGE@다이제스트> nvidia-smi -L` [C-2 §1] |
| 디스크 | 러너 이미지 **15.5 GB** + 제어 평면 이미지 **161 MB** + stub SUT **931 MB** + 웜 캐시 트리 **1,760,066,608 B** + 잡별 캐시 스크래치(자명 잡 1건이 **930 MB** 를 잠시 쓴다) + 산출물(MCAP·mp4)이 자라는 여유 [C-2 §0·§9] | `df -h /` |
| 네트워크 egress | `nvcr.io`(Isaac 베이스 — **401 이 정상**, 익명 토큰 교환 전이다) · `download.docker.com` · `nvidia.github.io` · PyPI · `omniverse-content-production.s3-us-west-2.amazonaws.com`(씬 자산) [C-2 §1] | C-2 §1 의 `curl` 루프 |
| (선택) CI 연동 | GitHub self-hosted runner — 레이블 `[self-hosted, cv-infra-gpu]`, systemd 유닛 `cv-infra-gh-runner`, 등록 스크립트 `register_gh_runner.sh` [프로비저닝 §Self-hosted] | `systemctl is-active cv-infra-gh-runner` |

> **CI 러너를 같은 호스트에서 돌린다면 `.env` 에 일곱 번째 키(`CV_CI_WORKSPACE_ROOT`)가
> 필요하다** — 러너가 체크아웃한 시나리오 디렉토리는 아래 REQUIRED 6 루트 어디에도 속하지
> 않아서, 비워 두면 제어 평면이 그 경로를 *"does not exist"* 로 거절한다. [C-2 §3-②']

---

## 2. 설치

전체 흐름은 **⓪ 소스 → ① 호스트 → ②' 설정 → ② 동의 → ③ 이미지 → ④ 캐시 → ⑤ 기동**
이고, 그 다음이 §3 검증이다. C-2 의 4단계 번호(`①provision → ②consent → ③compose up → ④selftest`)와
같은 절차이며, 이 문서는 새 호스트에서 실제로 필요한 순서로 펼쳐 놓았다. [C-2 §7]

### ⓪ 소스 확보

```bash
git clone <이 저장소의 URL> <deploy-root>
cd <deploy-root>
git fetch --tags --force            # 옮겨진 태그(@v1)를 받으려면 --force 가 필요하다
git checkout v1.2.1                 # 현행 권장 태그. 무엇을 고를지는 아래 표(정본 = releases.md)
git rev-parse HEAD                  # 이 값이 이 배포의 릴리즈 ref 다 — 기록해 두어라
```

**어떤 태그를 쓰나** — 정본은 [releases.md](releases.md)다. 읽는 법만 옮기면:

| 쓰고 싶은 것 | 고를 것 |
|---|---|
| 재현 가능한 배포(권장) | **불변 태그** `v1.2.1` — 커밋이 고정된다 |
| 최신 v1.x 를 계속 따라감 | 별칭 `v1`(GitHub Actions 관례). ⚠ 태그가 움직이므로 배포 재현이 아니다 |
| 절대 쓰면 안 되는 것 | `v1.0.0`(⛔ 결함 — 대장에 명시돼 있다) |

**커밋 칸은 사본이고 정본은 태그 자신**이다: 의심되면 `git rev-parse '<태그>^{commit}'`.
소비자 CI 가 쓰는 `uses: …@v1` 은 **YAML 평면**이고 지금 체크아웃하는 커밋은 **런타임
평면**이다 — 두 축은 독립이며(3축 = [version-compatibility.md](version-compatibility.md)),
같은 릴리즈를 가리키게 맞추는 절차가 §4다.

### ① 호스트 프로비저닝 (호스트 1회 · root 권한)

```bash
bash scripts/workstation_setup/provision.sh
```

멱등하다 — OS·드라이버 단언(읽기전용) → Docker CE → Container Toolkit →
`--gpus all` 패스스루 스모크 → Isaac 베이스 이미지 pull 순으로 돌고, **이미 충족된 항목은
특권 호출 없이 skip** 한다. [프로비저닝 Step B]

- **드라이버는 이 단계가 고치지 않는다.** 요구 브랜치가 아니면 loud 하게 멈춘다.
- 비대화 SSH 로 돌릴 때 남은 특권 작업이 있으면 **sudo 드롭인 1회 설치**가 선행돼야 한다
  (절차 = [프로비저닝 Step A]).
- **이미 프로비저닝된 호스트에 새 OS 사용자로 들어왔다면 이 단계를 실행하지 마라 — 실행할
  수도 없다**(`sudo -n true` → exit 1). §1 확인 명령만 돌리면 된다. [C-2 §2-1]

### ②' 설정 — `docker/.env`

```bash
cp docker/.env.example docker/.env          # ← 동의(②) 전에만 안전하다
$EDITOR docker/.env                         # REQUIRED 6개를 채운다
bash scripts/detect_gpu.sh >> docker/.env   # 측정된 GPU 노브를 append (손으로 쓰지 말 것)
chmod 600 docker/.env
```

REQUIRED 6개(경로 노브는 전부 **호스트 절대경로**): `CV_RUNNER_IMAGE`(§③ 에서 만드는 태그) ·
`CV_MAX_CONCURRENT`(보수적으로 시작 — 예: 2) · `CV_STATE_DIR`(**재배포 사이에 고정**하라,
회귀 baseline 이 이 파일에 산다) · `CV_OUT_DIR` · `CV_ISAAC_CACHE_ROOT` ·
`CV_ISAAC_CACHE_SCRATCH_ROOT`. 각 노브의 자기설명은 [`docker/.env.example`](../docker/.env.example)에
전부 주석으로 있다.

세 가지 함정 [C-2 §3-②']:
1. **경로는 컨테이너 안에서 같은 절대경로로 마운트된다** — 컨테이너 내부 경로를 적으면
   결과가 돌아오지 않는다.
2. **디렉토리를 기동 전에 만들어라**(`mkdir -p`). 없으면 dockerd 가 `root:root` 로 만들고,
   비루트로 도는 러너가 나중에 조용히 깨진다.
3. **`KNOB=`(빈 값)은 "미설정"이 아니다** — 옵션 노브는 set-but-empty 면 부팅이 loud 하게
   실패한다. 안 쓸 노브는 **줄을 주석 처리**하라.

`detect_gpu.sh` 는 살아있는 카드에 물어보고 맞는 프로파일을 고른다. 미지 카드면 **추측하지
않고 exit 3** 이고, 지원되지만 미실측인 카드면 VRAM 줄을 **주석 처리해서** 방출한다(2차
가드 OFF). 카드를 추가하는 절차 = [gpu-profiles.md](deploy/gpu-profiles.md).

> ⚠ **`>>` 만 써라. `2>&1` 을 붙이지 마라** — 진단 로그가 stderr 로 나오고, 그것이 `.env`
> 에 섞이면 compose 가 `unexpected character` 로 죽는다. [C-2 §3-②']

### ② 동의 (NVIDIA EULA) — 운영자만, 1회

```bash
bash scripts/consent/accept_eula.sh              # 대화형: 라이선스 제시 → 명시적 입력 → 기록
bash scripts/consent/check_consent.sh; echo $?   # 게이트: 0 = 기록됨, 3 = 없음
```

**이 단계는 자동화되지 않는다. 그것이 제품 계약이다**(`REQ-DEPLOY-008~011` ·
`NFR-DEPLOY-004`):

- 이 저장소의 **어떤 커밋된 파일에도 수락 값이 없다.** 이미지에도 굽지 않는다. CI 가 이
  단계를 합성해 건너뛰는 것도 **금지**다.
- 스크립트는 ① 무엇을 수락하는지 제시 → ② 명시적 결정 입력 → ③ **identity + timestamp
  기록**(`$HOME/.cv-infra/eula-consent.json`) → ④ 그제서야 런타임 값을 `docker/.env` 에
  쓴다. `.env` 는 git-ignored 이며 이 호스트를 떠나지 않는다.
- 비대화 호스트(SSH 등)에서는 **운영자 신원을 함께** 줘야 한다 — 익명 자동 "yes" 는
  거부된다(`bash scripts/consent/accept_eula.sh --help`).
- **동의가 없으면 기동이 시작조차 못 한다**: compose 가 필수 변수 부재로 loud 하게 거부하고,
  마지막 방어선으로 러너의 부트 가드가 Isaac 기동을 exit 3 으로 막는다. 2026-08-15 clean-host
  실측 — 동의 전 기동은 **exit 1 · 컨테이너 0 · 네트워크 0 · 빌드 0**, 동의 후 같은 명령은
  exit 0. [C-2 §3-②]

> ⚠ **이 시점부터 `docker/.env` 는 운영자만 만들 수 있는 것을 담는다.** 노브를 고치려고
> **파일을 다시 만들지 마라**(`cp .env.example .env` · `> docker/.env` · 에디터의 "새 파일로
> 저장" 전부 금지) — 동의가 소멸하고 복구는 운영자 재실행뿐이다. **백업도 만들지 마라**:
> 운영자 없이 동의를 복원할 수 있는 사본은 그 자체가 자동수락 경로다. 고치는 방법은
> **한 줄 upsert + 직후 `check_consent.sh` 재확인**이다(관용구 = [C-2 §3-②'] 경고 블록).

### ③ 이미지 빌드 — 러너 + stub SUT

캐시(④)가 러너 이미지를 필요로 하므로 **여기서 굽는다**. [C-2 §7]

```bash
REV="$(git rev-parse HEAD)"
# 러너 이미지 (빌드 컨텍스트 = 저장소 루트)
docker build --build-arg CV_SOURCE_REVISION="$REV" \
  -f docker/runner/Dockerfile -t cv-infra-runner:<tag> .
# 빌트인 stub SUT (self-test 상대역 — 레시피·계약 = docker/selftest_stub/README.md §5)
docker build -f docker/selftest_stub/Dockerfile --build-arg CV_SOURCE_REVISION="$REV" \
  -t cv-infra-selftest-stub:<tag> docker/selftest_stub
# 핀 기록 (로컬 태그엔 RepoDigest 가 없다 → Image Id 가 핀이다)
docker image inspect cv-infra-runner:<tag> \
  --format 'Id={{.Id}} rev={{index .Config.Labels "org.opencontainers.image.revision"}}'
```

- 빌드한 러너 태그를 `docker/.env` 의 `CV_RUNNER_IMAGE` 에 **그대로** 적는다(제자리 치환 —
  §② 경고). `latest` 는 금지다.
- `CV_SOURCE_REVISION` 이 비면 **빌드가 loud 하게 실패한다** — 스탬프 없는 이미지를 만들지
  않겠다는 게이트다. `.git` 이 없는 배포(타르볼)면 기록해 둔 릴리즈 sha 를 넣어라.
- 실측 규모·소요: 러너 **15.5 GB** — 캐시 웜 베이스에서 **2m10s**, `--no-cache` **1m6s**;
  stub **931 MB**. [C-2 §9]
- **같은 입력으로 두 번 구워도 이미지 다이제스트는 같지 않다**(핀되는 것은 입력 집합 =
  베이스 다이제스트 + `uv.lock` + apt 버전 + 소스 커밋). 실측된 것은 **동작 동등성**이다 —
  두 빌드의 apt 매니페스트 252/252 · 전체 dpkg 413/413 · wheel 50 파일 sha256 · 같은 잡의
  판정·지표 동일. [C-2 §7·§9]

### ④ 캐시 트리 — 이것을 빼면 모든 잡이 거부된다

```bash
CV_ISAAC_CACHE_ROOT=<②'에 적은 그 경로>              # .env 는 셸이 source 하는 파일이 아니다
CV_EULA_CONSENT=<②의 프롬프트에 입력한 그 단어> \
CV_MEASURE_IMAGE=cv-infra-runner:<tag> \
  bash scripts/measure/warm_cache.sh "$CV_ISAAC_CACHE_ROOT" warm
ls -ln "$CV_ISAAC_CACHE_ROOT/cache/"    # kit / home / computecache 셋이 uid 1234 여야 한다
```

- **왜 필요한가**: 제어 평면은 잡마다 캐시 세 티어를 복사해 러너에 준다. 없으면 잡은 시작
  전에 거부된다(`cache base tier … does not exist`). **`mkdir -p` 로 때우지 마라** — 소유자가
  러너 uid(1234)가 아니면 캐시가 조용히 꺼진다. 이 스크립트는 docker 루트 헬퍼로 chown 하므로
  호스트 sudo 가 필요 없다. [C-2 §3-②'']
- **`warm` vs `provision`**: `provision` 은 빈 트리만 만든다(첫 잡이 콜드). 실사용 배포는
  `warm` 이다 — **공유 캐시는 정상 운영으로 스스로 데워지지 않는다**(잡은 공유 트리를 읽기
  전용 원본으로만 쓰고 자기 사본을 버린다; 잡을 돌려도 공유 트리가 0 바이트로 남는 것이 세 번
  재현됐다). [C-2 §3-②'']
- **본전 계산(RTX 4080 호스트 · carter 잡 실측)**: 콜드 **643.86 s** vs 웜 평균 **70.49 s**
  (9.13×), 부팅 구간만 보면 **22.18×**. 워밍 1회가 **547.08 s** 인데 잡당 절약이 **573 s** 라
  **첫 잡에서 회수**된다. 트리 크기는 **1,760,066,608 B / 1,424 파일**. [C-2 §9-p5c18]
- **콜드 비용이 어느 위상에 몰리는지는 호스트·잡마다 다르다** — 같은 코드로 한쪽에서는
  `scene_load`(66.5×)가, 다른 호스트에서는 `robot_spawn` 이 지배항이었다. 일반 명제로 읽지 말고
  **자기 호스트에서 재라.** [C-2 §9]
- 새 호스트가 여기서 두 번 걸린다: `CV_ISAAC_CACHE_ROOT` 를 안 주면 **exit 1**, 러너 이미지
  태그를 안 주면 docker 가 레지스트리를 찾다가 **exit 125**(`pull access denied` — 인증 문제로
  읽히지만 실제 원인은 *"그 태그는 여기 없다"*). [C-2 §3-②'']

### ⑤ 기동

```bash
docker compose -f docker/compose.yaml config     # 드라이런: 렌더 결과 확인 (exit 0 이어야 함)
CV_SOURCE_REVISION="$(git rev-parse HEAD)" \
  docker compose -f docker/compose.yaml up -d --build
```

- 올라오는 것은 **제어 평면 하나**(orchestrator = 제출/스케줄러 API + 운영 read model)뿐이다.
  러너와 SUT 는 compose 서비스가 아니라 **잡마다** 오케스트레이터가 호스트 데몬에 직접 스폰하고
  잡 전용 브리지 네트워크에 격리한다. `compose.yaml` 에 `runner:` 를 추가하지 마라.
  [docker/compose.yaml 상단]
- `CV_SOURCE_REVISION` 접두는 **빌드할 때만** 필요하다(빠뜨리면 빌드가 loud 실패). 빌드 없는
  재기동(`up -d`·`ps`·`down`)에는 필요 없고, **`docker/.env` 에는 넣지 마라**. [C-2 §3-③]
- 기동 전 **같은 호스트에 다른 제어 평면이 떠 있으면 안 된다** — 부팅 시 라벨 스윕이 상대의 잡
  컨테이너를 지운다. 확인·공존 조건 = [C-2 §3-③].

---

## 3. 설치 검증 — `cv-infra selftest`

`cv-infra` CLI 는 호스트에 설치하지 않는다. **제어 평면 이미지 자신**을 일회용으로 띄운다 —
그래서 API 주소가 `127.0.0.1` 이 아니라 서비스 이름 `orchestrator` 다. [C-2 §4]

```bash
docker compose -f docker/compose.yaml run --rm --no-deps \
  -e CV_SELFTEST_SUT_IMAGE=cv-infra-selftest-stub:<tag> \
  orchestrator cv-infra selftest --api http://orchestrator:8000
echo $?
```

**기대 결과**

- **종료 코드 `0`** = 라운드트립 green(`REQ-SELFTEST-001~003` · `NFR-SELFTEST-001`).
  계약: `0` green · `1` stub 판정 fail · `2` 계약/422 · `3` 인프라(핸들 미설정·오케스트레이터
  부재 등).
- 잡 컨테이너는 **2개**(러너 + stub SUT)가 잡 전용 네트워크에 뜬다 — **외부 SUT·소비자
  저장소 의존 0**.
- 실측 소요: 웜 캐시에서 **27.7 / 42.7 / 27.7 / 27.7 s**(4/4 green), 콜드에서 **141.05 s**.
  [C-2 §9]
- ⚠ **`CV_SELFTEST_SUT_IMAGE` 를 `docker/.env` 에 적는 것으로는 되지 않는다** — 이 값은 봉투를
  만드는 **클라이언트 프로세스**가 읽으므로 위처럼 일회용 CLI 컨테이너에 `-e` 로 줘야 한다.
  미설정이면 **추측하지 않고 exit 3** 이다. [C-2 §3-④]
- 자명 시나리오라서 미션이 ~0.2 s 다 — **녹화 경로는 사실상 검증되지 않는다.** 거기까지
  확인하려면 실제 SUT 잡을 돌려라(절차 = [C-2 §4]).

**한 줄 더 보라 — self-test 가 답하지 않는 "어떤 구성으로 도는가"** [C-2 §4]:

```bash
docker logs cv-infra-orchestrator 2>&1 | grep -m1 serve-config   # k·마운트·캐시·러너 이미지 핀
curl -sS "http://127.0.0.1:${CV_PUBLISH_PORT:-8000}/monitor.json" \
  | python3 -c 'import json,sys;r=json.load(sys.stdin)["resources"];print(r["vram_total_mib"],"MiB")'
#   -> 이 호스트의 nvidia-smi 총 VRAM 과 같아야 한다. 다르면 남의 평면을 보고 있는 것이다.
```

이제 실제 시나리오를 돌릴 준비가 됐다 — 사용법은 [user-guide.md](user-guide.md).

---

## 4. 업그레이드

**핵심 사실 하나**: 릴리즈 태그를 옮겨도 **YAML 평면만** 움직인다. 잡이 실제로 실행하는 코드는
**이미지 안의 wheel** 이라 **재빌드로만** 따라온다. 평면은 넷이고 갱신 경로가 다르다 —
① YAML(태그 이동) · ② 런타임 체크아웃 · ②' 제어 평면 이미지 · ③ 러너 이미지. 정본 절차 =
[plane-sync.md](deploy/plane-sync.md).

```bash
# 1) 소스 갱신 — 옮겨진 태그는 --force 없이는 조용히 안 온다
git fetch --tags --force && git checkout <새 태그>
# 2) 러너 이미지 재빌드(§2-③) → docker/.env 의 CV_RUNNER_IMAGE 를 upsert(한 줄 치환)
#    → check_consent.sh 로 동의가 살아남았는지 확인
# 3) 제어 평면 재빌드·교체
CV_SOURCE_REVISION="$(git rev-parse HEAD)" \
  docker compose -f docker/compose.yaml up -d --build
# 4) 세 평면이 같은 릴리즈를 가리키는지 게이트로 확인
bash scripts/check_plane_skew.sh --src <deploy-root> --tag <릴리즈 sha> \
  --image cv-infra-runner:<tag> --orchestrator-image cv-infra-orchestrator:<tag>
# 5) self-test 재실행(§3)
```

- **태그 이동 소비(`@v1`) vs 불변 핀**: 소비자 CI 는 `uses: …@v1` 로 자동으로 받지만, **호스트
  배포는 자동으로 따라오지 않는다**(위 2~3단계가 사람의 일이다). 재현 가능한 배포를 원하면
  호스트는 **불변 태그**를 체크아웃하라. [releases.md · version-compatibility.md]
- 릴리즈 노트를 **먼저 읽어라** — 그 릴리즈가 어느 평면을 건드렸는지가 재빌드 필요 여부를
  결정한다(예: `v1.2.1` 은 워크플로 YAML 평면만 고쳤고, `v1.2.0` 은 러너·계약을 건드렸다).
  [releases.md]
- ⚠ **자산이 늘어난 릴리즈는 캐시 재워밍이 배포 절차의 일부다**(§2-④ 를 다시 돌린다). `v1.2.0`
  실측: 등재 자산이 콜드면 배치 부팅 **67.326 s**, 웜이면 **29.035 s**. [releases.md `v1.2.0`]
- ⚠ **`up` 은 상주 컨테이너를 교체한다** — 컨테이너에 붙어 있던 egress 감사·계측은 그 순간
  죽는다. 재무장까지가 한 세트다(절차 = [C-2 §4-5]).

---

## 5. 트러블슈팅 — 최초 설치에서 실제로 만나는 다섯

| 증상 | 원인 | 조치 |
|---|---|---|
| `config`/`up` 이 **필수 변수(동의 키) 부재**로 exit 1 | 동의 미기록 — 또는 `.env` 를 덮어써서 소멸 | §2-② 를 (다시) 실행. **버그가 아니라 게이트다** |
| `up` 이 `could not select device driver "nvidia"` | 컨테이너 툴킷 미설치/미등록 | §2-① 재실행. 이 배포는 GPU 호스트를 전제한다(`NFR-DEPLOY-005`) |
| 잡이 `cache base tier … does not exist` 로 거부 | §2-④ 를 건너뛰었다(또는 `mkdir` 만 했다 = 소유자 불일치) | `warm_cache.sh … warm` 을 돌리고 `ls -ln` 으로 uid 1234 확인 |
| `cv-infra selftest` 가 **exit 3** | stub 이미지 핸들이 **CLI 컨테이너 안에서** 미설정(`.env` 에 적은 것은 도달하지 않는다) | §3 처럼 `-e CV_SELFTEST_SUT_IMAGE=…` 로 주입 |
| `infra_error: … oracle_plugin_dir <dir> does not exist` (잡 컨테이너가 아예 안 뜬다) | 시나리오 디렉토리가 **제어 평면 컨테이너 안에 없다** — `submit` 은 시나리오의 부모 디렉토리를 함께 보내고 검사는 컨테이너 안에서 돈다 | 시나리오를 `CV_OUT_DIR` 등 이미 마운트된 루트 아래로 옮기거나, CI 러너 워크스페이스면 `CV_CI_WORKSPACE_ROOT` 를 설정 |

**전체 증상 표(15행)는 [C-2 §8]** 에 있다 — 평면 스큐·포트 점유·감사 무장·캐시 `:ro` 함정 등
운영 중에 만나는 것들이 거기 있다. 다른 GPU 카드로 옮길 때는 [gpu-profiles.md](deploy/gpu-profiles.md),
호스트 프로비저닝 내부(apt 핀·sudo 드롭인·드라이버 재정렬)는
[scripts/workstation_setup/README.md](../scripts/workstation_setup/README.md).

> **인용된 증적 경로에 대하여**: C-2 가 인용하는 워크스테이션 증적 경로 일부는 2026-08-20
> **의도적으로 만료**됐다(측정 사실은 유효 · 재현 불가). 구분 = [evidence-anchors.md](evidence-anchors.md).
