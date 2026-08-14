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
| **약속 안 함** | **④ `cv-infra selftest` 는 아직 없다.** 예약된 자리만 있고 실행하면 exit **3**(미구현)이 난다 — §3-④ 참조. 배포가 살아있음을 확인하는 **현재의 정직한 방법은 §4**다. |
| **약속 안 함** | **러너 이미지(15.5 GB)를 새 호스트로 옮기는 경로가 아직 없다.** 저장소에 레지스트리 push 경로가 0 이다 — §7 에 선택지와 실측 비용만 정리돼 있고, 채택은 미결정이다. |
| **약속 안 함** | 인증. 제출 API 는 authn 이 없다(단일 호스트 MVP). 그래서 기본 공개 주소가 `127.0.0.1` 이다. |

---

## 1. 사전 요구 (호스트)

| 항목 | 요구 | 확인 명령 |
|---|---|---|
| OS | 프로비저닝 스크립트가 단언하는 배포판/코드네임/아키텍처 (핀 = `scripts/workstation_setup/common.sh`) | `. /etc/os-release; echo $ID/$VERSION_CODENAME $(dpkg --print-architecture)` |
| NVIDIA 드라이버 | **R580 브랜치**(플로어 이상 **AND** major == 580), open kernel module. 프로비저닝은 드라이버를 **절대 설치·업그레이드하지 않는다** — 단언만 한다 | `nvidia-smi --query-gpu=driver_version --format=csv,noheader` |
| Docker CE + Compose v2 | 핀된 버전(`common.sh`) | `docker version --format '{{.Server.Version}}'` · `docker compose version` |
| NVIDIA Container Toolkit | `nvidia` 런타임 등록 | `docker info --format '{{json .Runtimes}}' \| grep -o '"nvidia"'` |
| GPU 패스스루 | 호스트에 CUDA 미설치 상태에서 컨테이너가 GPU 를 본다 | `docker run --rm --gpus all <핀된 CUDA 베이스> nvidia-smi -L` |
| 디스크 | 러너 이미지 15.5 GB + 잡 산출물(MCAP·mp4)이 자라는 여유 | `df -h /` |
| 네트워크 | `nvcr.io`(Isaac 베이스) · `download.docker.com` · `nvidia.github.io` · PyPI 로의 egress | — |

`①` 이 이 전부를 멱등하게 충족시킨다(아래).

---

## 2. 4단계 흐름 한눈에

```
① scripts/workstation_setup/provision.sh          호스트 선결(Docker CE + Toolkit + 패스스루 + Isaac 베이스 pull)
②' 설정:  docker/.env 작성  +  scripts/detect_gpu.sh >> docker/.env
②  scripts/consent/accept_eula.sh                 NVIDIA EULA — 운영자만, 1회
③  docker compose -f docker/compose.yaml up -d --build     ← 제어 평면 기동
④  cv-infra selftest                              ★ 미구현(p5c15+) — 대신 §4
```

**순서가 중요하다.** `②'`(설정)를 `②`(동의) **앞에** 두는 것이 기본 순서다 —
`docker/.env.example` 의 USE 블록과 같다. 이미 동의를 마친 호스트에서 설정을 나중에
넣어야 한다면 **§3-②'의 경고**를 반드시 읽어라(파일을 다시 만들면 동의 키가 사라진다).

---

## 3. 단계별 절차

### ① 호스트 프로비저닝

```bash
cd <deploy-root>                     # 이 저장소의 체크아웃
bash scripts/workstation_setup/provision.sh
```

- 멱등하다. 이미 충족된 호스트에서 다시 돌려도 안전하다.
- 사전에 **sudo 드롭인 1회 설치**가 필요하다(비대화 SSH 는 암호 프롬프트에 답할 수 없다).
  절차 = `scripts/workstation_setup/README.md` Step A.
- 드라이버가 요구 브랜치가 아니면 **loud 하게 멈춘다**. 프로비저닝은 드라이버를 고치지
  않는다 — 유일하게 허가된 드라이버 스크립트는 `realign_driver_r580.sh` 다.

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
| `CV_ISAAC_CACHE_ROOT` | **웜 캐시 = 읽기 전용 복사 원본** | 러너 uid 소유. 잡별 쓰기 사본이 아래 스크래치로 만들어진다 |
| `CV_ISAAC_CACHE_SCRATCH_ROOT` | 잡별 쓰기 가능 캐시 스크래치 | `k × 웜캐시 크기` 만큼 여유 필요 |

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
> * 대신 **멱등 upsert** 로 고쳐라(같은 관용구가 `scripts/consent/accept_eula.sh::env_set`
>   에 있다 — 해당 키 줄만 지우고 다시 append, 그리고 `install -m 600`):
>
>   ```bash
>   cp -a docker/.env docker/.env.bak.$(date +%s)      # 먼저 백업
>   tmp=$(mktemp)
>   grep -v -E '^[[:space:]]*(export[[:space:]]+)?CV_MAX_CONCURRENT=' docker/.env > "$tmp"
>   printf 'CV_MAX_CONCURRENT=%s\n' "<새 값>" >> "$tmp"
>   install -m 600 "$tmp" docker/.env && rm -f "$tmp"
>   ```
> * 고친 **직후 매번** 게이트를 다시 돌려라:
>   `bash scripts/consent/check_consent.sh; echo $?` → **0** 이어야 한다.
> * `scripts/detect_gpu.sh >> docker/.env` 는 순수 append 라 안전하다.

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

### ③ 기동

```bash
cd <deploy-root>                    # 저장소 루트에서 (빌드 컨텍스트가 루트다)
docker compose -f docker/compose.yaml config     # 드라이런: 렌더 결과 확인 (exit 0 이어야 함)
docker compose -f docker/compose.yaml up -d --build
```

- 올라오는 것은 **제어 평면 하나**(orchestrator = 제출/스케줄러 API + 운영 read model)뿐이다.
- 러너와 SUT 는 **compose 서비스가 아니다** — 잡마다 오케스트레이터가 호스트 데몬에
  직접 스폰하고, 잡 전용 브리지 네트워크에 격리한다. `compose.yaml` 에 `runner:` 를
  추가하면 자원인지 동시성 k 가 조용히 깨진다(파일 상단 주석 참조).
- **기동 전 반드시 확인**: 같은 호스트에 **다른 제어 평면이 떠 있으면 안 된다**.
  두 평면이 같은 docker 데몬을 보면 부팅 시의 라벨 스윕이 **상대의 잡 컨테이너를 지운다**.
  ```bash
  docker ps -a --filter label=cv-infra.job_id -q | wc -l      # 0 이어야 함
  docker ps --filter publish=8000 --format '{{.Names}}'        # 포트 점유자
  ss -ltn | grep ':8000'
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

### ④ self-test — **미구현**

```bash
cv-infra selftest        # → exit 3, "not implemented yet"
```

`cv-infra selftest` 는 CLI 에 **예약된 자리만 있고 아직 배선되지 않았다**(`REQ-SELFTEST-001~004`
미구현, p5c15+). 없는 것을 있다고 쓰지 않기 위해 명시한다. 지금 시점에서 "이 호스트에
배포가 살아있다"를 확인하는 방법은 **§4** 다.

---

## 4. 기동 직후 확인 (selftest 가 생기기 전까지의 정직한 대체)

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
curl -sS -o /dev/null -w '%{http_code}\n' http://127.0.0.1:8000/openapi.json   # 200

# 4) 운영 뷰
cvi monitor --api http://orchestrator:8000

# 5) 외부 egress 감사를 무장한다 (제어 평면을 새로 띄웠으면 반드시 다시)
bash scripts/netns_audit.sh arm cv-infra-orchestrator
#    이후 아무 때나:  bash scripts/netns_audit.sh read cv-infra-orchestrator
```

> `serve-config` 한 줄을 **읽지 않고 넘어가지 말 것.** 마운트나 캐시가 조용히 빠진
> 배포는 겉보기에 정상으로 돌면서 측정을 전부 콜드로 만든다. 이 줄이 그것을 눈에
> 보이게 하려고 존재한다.

라운드트립을 실제로 태우려면 지금은 **외부 SUT 가 필요하다**(`④` 가 없애려는 바로 그
의존이며, 아직 없다). 외부 SUT 가 있다면 전체 관통은 이렇게 확인한다:

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
| 설정 변경 반영 | `.env` 를 **upsert 로** 고친 뒤(§3-②' 경고) `up -d` 재실행 |
| 제어 평면 코드 갱신 | 체크아웃 갱신 → `up -d --build` |
| **러너 이미지 핀 교체** | 새 이미지 빌드 → `.env` 의 `CV_RUNNER_IMAGE` 한 줄을 upsert → `up -d`. 스큐 확인: `bash scripts/check_plane_skew.sh --tag <release-sha> --image <ref>` |
| CLI 한 번 쓰기 | `docker compose -f docker/compose.yaml run --rm --no-deps orchestrator cv-infra <cmd> --api http://orchestrator:8000` (§4) |

> ⚠ **`up -d --build` 로 만든 제어 평면 이미지에는 리비전 라벨이 없다.** `compose build`
> 는 `--label` 을 걸어주지 않으므로 그 이미지에는 compose 자신의 라벨만 남는다
> (실측 2026-08-14). 제어 평면까지 스큐 추적을 하려면 별도로 태깅해 두어라:
> `docker build -f docker/orchestrator/Dockerfile -t cv-infra-orchestrator:<tag> \`
> `  --label org.opencontainers.image.revision=$(git rev-parse HEAD) .`
> `CV_ORCHESTRATOR_IMAGE` 로 그 태그를 가리키면 `up` 은 빌드 없이 그 이미지를 쓴다.
> (`check_plane_skew.sh` 가 보는 세 평면에 제어 평면 이미지는 **포함되지 않는다**.)

**동의를 남긴 채 스택을 내려라.** compose 파일이 동의 키를 요구하므로 `down`·`ps` 같은
서브커맨드도 키가 있어야 돈다. 호스트에서 동의를 지우기 **전에** 스택을 먼저 내려라.

**세 개의 배포 평면**(YAML / 런타임 / 러너 이미지)이 따로 논다는 사실과 그 동기화
절차는 [`plane-sync.md`](plane-sync.md) 에 있다. 릴리즈 태그를 옮겼다고 러너 이미지
안의 코드가 따라오지 않는다.

---

## 6. 다른 GPU 카드로

`scripts/detect_gpu.sh` 가 살아있는 GPU 에 물어보고 맞는 `profiles/*.yaml` 을 고른다.
지원 추가 = **`profiles/` 에 파일 하나 추가**(코드·compose 수정 0). 미측정 카드는
숫자를 비워 둔 채 프로파일만 두고, VRAM 가드는 **꺼진 채로** 둔다 — 추측한 예산으로
도는 것보다 낫다. 전체 절차 = [`gpu-profiles.md`](gpu-profiles.md).

---

## 7. 다른 호스트로 옮기기 — **러너 이미지 배송은 미결정**

제어 평면 이미지는 어디서나 소스에서 재빌드하면 된다(아래 실측: 161 MB / 8.3 s).
문제는 **러너 이미지**다. 이 저장소에는 **러너 이미지를 레지스트리에 push 하는 경로가
하나도 없고**, 현재 이미지는 로컬 빌드라 `RepoDigests` 가 비어 있다(= 어떤 레지스트리에도
존재한 적이 없다). 새 호스트는 그 15.5 GB 를 **어떻게든 받아야** `③` 이 잡을 돌릴 수 있다.

**선택지와 실측 비용** (채택은 아직 안 됐다 — 이 표는 결정의 입력이다):

| 경로 | 전송/시간 | 인증·전제 | 재현성 | 상태 |
|---|---|---|---|---|
| **A. 레지스트리 push/pull**(GHCR 등) | 압축 전송량: Isaac 베이스만으로 **7.62 GB**(실측 — 레지스트리 매니페스트의 압축 blob 합, 17 레이어, 최대 단일 레이어 7.50 GB). 우리가 얹는 레이어는 **비압축 661 MB**(실측, `docker history`) → 압축 후 크기 **미실측**. 소요: **미실측**. 참고 앵커 = 이 호스트의 GHCR 콜드 pull 실측 **0.70 MB/s**(p5c9, 다른 이미지) — 두 실측을 산술 투영하면 **≈3 시간**이나 이는 **투영이지 실측이 아니다** | push 권한 필요(write:packages 등). **NVIDIA 베이스 레이어를 제3자 레지스트리에 재배포하는 것이 EULA 상 허용되는지 미확인** — 이 경로를 고르기 전에 반드시 확인할 것 | pull 은 **다이제스트 고정**이라 바이트 동일 보장. 가장 강한 재현성 | 저장소에 push 경로 **0** |
| **B. `docker save` / `load`** | tar **15,550,096,384 B(15.55 GB)**, `save` 파이프 **30.4 s**(실측, 디스크 미기록). `load` 소요·전달 매체 소요 **미실측** | 없음(파일 복사). 15.5 GB 를 옮길 매체·대역폭 필요 | Image Id 보존 = 바이트 동일 | 즉시 가능. 스크립트 없음 |
| **C. 새 호스트에서 재빌드** | 베이스 pull **7.62 GB 압축**(실측) + apt/pip 수신(**미실측**) + 빌드 시간(**미실측**) | `nvcr.io` egress. 익명 pull 가능 | **바이트 동일하지 않다** — 같은 소스로 빌드한 이미지들의 Image Id 가 전부 다르다(실측: 동일 리비전 라벨을 갖는 빌드끼리도 Id 상이). 핀된 것은 *입력*(베이스 다이제스트 + `uv.lock` 해시)이지 *출력 다이제스트*가 아니다 | 기존 `docker/runner/Dockerfile` 로 가능 |

> 셋 다 지금은 **문서화된 수동 절차**다. 자동화(릴리즈 파이프라인)는 이 표를 보고
> 경로를 고른 다음의 일이다. **B 는 오늘 당장 가능하고, A 는 라이선스 확인이 선행**이며,
> C 는 "같은 이미지"를 약속하지 못한다.

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
| 실행 코드가 옛날 것 같다 | 세 평면 스큐 | [`plane-sync.md`](plane-sync.md) + `scripts/check_plane_skew.sh --tag <sha> --image <ref>` |

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
