# cv-infra

**Isaac Sim standalone 스크립트 한 개를 입력 공간으로 분해해 CI에서 돌리고, 판정을 PR로 돌려주는
최소 검증 인프라.** 이 저장소(`cv-infra-workspace`)가 플랫폼이다 — CLI(`cv-infra`) 하나와
재사용 워크플로 하나.

플랫폼이 하는 일은 넷뿐이다.

1. **실행 환경** — 핀된 Isaac Sim 이미지를 GPU 워크스테이션에서 케이스마다 컨테이너로 띄운다
   (체크아웃 `:ro` 마운트 + 케이스별 출력 dir + Omniverse 캐시).
2. **입력 분해** — 소비자가 선언한 [PICT](https://github.com/microsoft/pict) 모델을 커버링 배열로
   펼쳐 케이스를 만들고, 축을 그대로 스크립트의 `--<축>=<값>` argv로 넘긴다.
3. **결과 수거** — 케이스마다 출력 dir을 zip으로, 컨테이너 로그를 `.sim.log`로 거둔다.
4. **판정 정리** — 오라클이 stdout에 찍은 평평한 JSON dict를 타입으로 읽어 체크/지표/메모로
   접고, 베이스라인과 비교해 exit code·Check Run·스티키 코멘트로 발행한다.

**하지 않는 일**: 로봇 SW를 빌드하지 않고, 씬·미션·센서·오라클의 도메인 의미를 알지 못하며,
상주 서비스를 띄우지 않는다. 시뮬레이션이 무엇을 하는지는 전부 소비자의 `sim_script` 안에 있다.

고정 기반(재사용 — 재구현하지 않는다): Isaac Sim **5.1.0**(digest 핀) · ROS 2 **Jazzy** ·
Python **3.11** · NVIDIA 드라이버 **R580 브랜치**(≥ 580.65.06 AND major == 580) ·
PICT(핀 커밋) · GitHub Actions · SQLite.

## 소비자가 쓰는 것 = 파일 3개 + 워크플로 잡 1개

```
verify/
  sim.py            # 표준 Isaac standalone 스크립트 — 자기 SimulationApp을 띄우고 닫는다
  param_space.pict  # 입력 공간(PICT 모델). 파라미터 = sim.py의 플래그
  oracle.py         # 판정자(선택). stdout에 평평한 JSON dict 한 줄
  out/.gitkeep      # 출력 디렉터리. 커밋돼 있어야 한다(아래 §.gitkeep)
```

```yaml
jobs:
  verify:
    permissions: { checks: write, pull-requests: write, contents: read }
    # 브랜치 참조 — 릴리스 태그가 서면 그것으로 바꾼다
    uses: yongjunshin/cv-infra-workspace/.github/workflows/verify.yml@main
    with:
      sim_script: verify/sim.py     # executable entrypoint; Python/Bash/etc.
      sim_input_space: verify/param_space.pict
      sim_output_dir: verify/out
      sim_image: nvcr.io/nvidia/isaac-sim:5.1.0@sha256:f3563cb…   # 다이제스트 핀(태그 불가)
      oracle_script: verify/oracle.py     # 빼면 스윕 모드(게이트하지 않음)
```

### runtime command mode

기존 `sim_script`/`oracle_script` 계약은 그대로 유지한다. 여러 사용자 컨테이너를 띄우거나
외부 모델 서버와 통신해야 하는 앱은 `run_command`/`judge_command`를 선언할 수 있다. 이때
인프라는 명령의 의미를 해석하지 않고, 사용자 runtime image 안에서 명령을 실행한다.

- `CASE=/cv/case.json`: `case_id`, `repeat`, `seed`, `inputs`를 담은 현재 PICT 케이스
- `OUT=/cv/checkout/<sim_output_dir>`: 케이스별 읽기/쓰기 결과 경계
- `CV_SEED=<int>`: 재현용 seed
- `CV_CHECKOUT_HOST`, `CV_OUT_HOST`: runtime command가 Docker Compose 등으로 호스트
  daemon에 자식 컨테이너를 만들 때 사용할 호스트 경로

runtime image는 워크플로의 `runtime_dockerfile`과 `runtime_context`로 소비자가 정의할 수
있고, 명령은 Docker socket을 통해 자체 Compose/모델 클라이언트를 실행할 수 있다. 따라서
ROS·모델·미션·네트워크의 의미는 전부 사용자 스크립트와 이미지가 소유한다. `judge_command`는
결과 파일을 읽어 stdout에 flat JSON verdict 한 줄을 출력한다. Docker socket과 외부 네트워크를
허용할지는 GPU runner 운영 정책의 책임이며, 플랫폼 계약은 그 세부사항을 고정하지 않는다.

동작하는 예시는 이 저장소의 [`examples/selftest/`](examples/selftest/)(낙하 큐브 — 클라우드
자산·ROS·로봇 0 의존)에 있고, `cv-infra selftest`가 바로 그것을 돈다.

## 입력 (워크플로 `with:` ↔ CLI 플래그 1:1)

워크플로는 자기 어휘를 만들지 않는다. 아래 표의 왼쪽과 가운데는 같은 것이고,
`tests/test_gh_wiring_static.py`가 그 1:1을 기계적으로 붙잡는다.

| 워크플로 입력 | CLI 플래그 | 기본 | 뜻 |
|---|---|---|---|
| `sim_script` **(필수)** | `--sim-script` | — | 체크아웃 상대경로의 실행 가능한 entrypoint. 케이스마다 `<sim_script> --<축>=<값> ...`로 호출하며, interpreter는 파일의 shebang이 정한다. env `CV_SEED=<int>`도 전달한다 |
| `sim_input_space` **(필수)** | `--input-space` | — | PICT 모델. 축 이름은 CLI 플래그로 안전해야 한다(`^[A-Za-z][A-Za-z0-9_-]*$`, `help`/`h` 금지) |
| `sim_output_dir` **(필수)** | `--output-dir` | — | 체크아웃 기준 **엄격한 상대 하위경로**(절대경로·`..`·맨 `.` 모두 금지)이며 체크아웃에 **디렉터리로 존재**해야 한다 |
| `oracle_script` | `--oracle-script` | 없음 | 실행 가능한 판정 entrypoint. 있으면 **게이트 모드**, 없으면 **스윕 모드** |
| `pict_k` | `--pict-k` | `2` | 커버링 배열 강도. **요구값 그대로**(조용한 하향 없음) |
| `repeats` | `--repeats` | `1` | 케이스당 반복. 1도 그대로 존중하고 `single_sample`로 라벨한다 |
| `budget` | `--budget-s` | 없음 | 벽시계 상한(초). 케이스 착수 **전에만** 검사 |
| `sim_image` **(필수)** | `--sim-image` | — | **다이제스트 핀 필수**(`<name>@sha256:<64 hex>` — 태그는 거절). 소비자가 자기 스크립트를 개발한 바로 그 이미지를 선언한다(아래 §이미지 패리티). 다이제스트 얻는 법: `docker inspect --format '{{index .RepoDigests 0}}' nvcr.io/nvidia/isaac-sim:5.1.0` |
| `concurrency` | `--concurrency` | `1` | 동시 케이스 수. 전 컨테이너가 GPU 하나를 시분할하므로 상한은 VRAM |
| `report_only` | `--report-only` | `false` | bool 키가 없는 verdict(지표·메모만)를 거절 대신 허용 |
| `runner_label` | — | `cv-infra-gpu` | GPU 워크스테이션을 고르는 러너 라벨 |
| (워크플로가 계산) | `--update-baseline` | `false` | **non-PR 이벤트에서만** 전달된다(아래 §베이스라인) |

CLI 전용 운영 플래그: `--checkout`(기본 `.`) · `--run-dir`(기본 `./.cv-infra-run`) ·
`--case-timeout-s`(1800) · `--oracle-timeout-s`(300) · `--shm-size`(`8g`) ·
`--max-zip-mb`(512).

환경변수: `ACCEPT_EULA`·`PRIVACY_CONSENT`(없으면 exit 3) · `CV_PICT_BIN`(PICT 바이너리) ·
`CV_BASELINE_DB`(기본 `~/.cv-infra/baselines.sqlite3`) ·
`CV_ISAAC_CACHE_ROOT`(6-way 트리는 그 아래 **이미지별** `<digest12>/`에 있다) ·
`CV_ISAAC_CACHE_SCRATCH_ROOT`(둘 다 없으면 캐시 마운트 0개) ·
`GITHUB_SHA`(리포트·베이스라인 행의 출처 라벨).

## verdict — 예약 키 0개, **타입이 의미다**

오라클은 stdout에 **평평한 dict** 하나를 JSON으로 찍는다(마지막으로 파싱되는 JSON 줄을 채택).
키 이름은 전적으로 소비자 것이고, 플랫폼은 값의 타입만 읽는다.

| 값의 타입 | 뜻 | 게이트하나 |
|---|---|---|
| `bool` | **체크** — 케이스는 모든 bool이 true일 때 pass | ✅ (false 하나면 exit 1) |
| `int` / `float` | **지표** — 이름별 평균을 베이스라인과 비교해 delta 보고 | ❌ (보고만) |
| `null` | **판정 불가** — 통과율 계산에서 제외되며 false가 아니다 | ❌ |
| `str` | **메모** — 리포트에 그대로 표시 | ❌ |
| 그 외(list/dict/중첩) | 계약 위반 → 그 케이스는 **ERROR** ("flat dict only", 키 이름 명시) | — |

`bool`은 숫자보다 **먼저** 판정된다(파이썬에서 `True`는 `int`이기도 하다). 게이트 모드에서
판정된 dict가 하나 이상 있는데 bool 키가 **전무**하면 exit 2로 시끄럽게 거절한다 — 아무것도
검사하지 않는 초록 게이트는 게이트가 아니기 때문이다. 지표·메모만 원하면 `report_only: true`.

3개의 레인: `rc_sim != 0` → **ERROR**(회귀 판정에서 제외) · `rc_oracle != 0` → **ERROR** ·
그 외 → dict 파싱.

## ⚠ sim 스크립트의 exit code는 판정을 실을 수 없다

> **`SimulationApp.close()`는 프로세스를 status 0으로 끝낸다.** 그 뒤의 `sys.exit(1)`은
> 실행되지 않는다. 게다가 stock 이미지의 `python.sh`는 비0 종료를 전부 `1`로 뭉갠다.
>
> 그래서 플랫폼은 **`rc_sim`을 "돌았나/죽었나"로만 읽고, pass/fail로는 읽지 않는다.**
> 판정은 오라클이 파일을 읽어서 한다.
>
> 결과적으로 sim 스크립트가 지켜야 할 것: **판정에 쓸 산출물을 `close()` 이전에 쓴다.**
> `os._exit()`로 상태를 밀어넣으려는 시도는 하지 말 것 — 계약이 그 값을 보지 않는다.

## headless 정책과 `--gui`

케이스 컨테이너에는 디스플레이가 없다. 즉 **환경 자체가 headless**이고, GUI로 부팅하는
스크립트는 행(hang)하거나 죽는다(→ ERROR/타임아웃 레인). 권장 패턴은 토글 하나다.

```python
parser.add_argument("--gui", action="store_true")   # CI는 절대 넘기지 않는다
...
simulation_app = SimulationApp({"headless": not args.gui})
```

admit 단계에서 스크립트 텍스트에 `headless.*False` 패턴이 보이면 **경고**한다(거절이 아니다 —
그것은 grep이지 증거가 아니다). 케이스마다 컨테이너 로그를 `logs/<case>.sim.log`로 저장해
아티팩트에 넣으므로, 부팅이 죽었다면 그 파일에 이유가 있다.

## 로컬 패리티

CI가 케이스마다 실행하는 것은 이 한 줄이다. `sim_script`는 실행 가능한 파일이며
Python/Bash 등 언어와 interpreter는 파일의 shebang이 결정한다.

```
<sim_script> --<축>=<값> ...                              # working dir = 체크아웃 루트
```

같은 것을 워크스테이션에서 그대로 돌릴 수 있다(저장소 루트에서, GUI 옵션):

```bash
docker run --rm --gpus all -e ACCEPT_EULA=Y -e PRIVACY_CONSENT=Y -e CV_SEED=7 \
  -v "$PWD:/cv/checkout" -w /cv/checkout --shm-size=8g \
  --entrypoint /bin/sh \
  nvcr.io/nvidia/isaac-sim:5.1.0@sha256:f3563cb2ba0c18af0b2fb321360dcb73a917b899f879e3213623d6bee484fa54 \
  -lc 'exec "$0" "$@"' examples/selftest/sim.py --drop_height=1.5 --cube_scale=0.5

python3 examples/selftest/oracle.py --drop_height=1.5 --cube_scale=0.5   # 오라클은 stdlib만
```

출력 경로를 **체크아웃 루트 기준 상대경로**로 쓰는 것이 이 패리티의 전부다: 플랫폼은 케이스별
호스트 디렉터리를 `<checkout>/<sim_output_dir>` 위에 rw로 오버레이할 뿐이므로, 로컬에서는
같은 명령이 그냥 작업 트리에 쓴다.

## 이미지 패리티

`sim_image`에 **기본값이 없는** 이유: 플랫폼이 고른 이미지는 **아무도 그 스크립트로 검증한 적
없는** 이미지다. Isaac Sim은 메이저가 바뀌면 파이썬 API를 깬다. 게다가 **태그는 움직인다** —
`isaac-sim:5.1.0`이 조용히 다시 푸시되면 어제 초록이던 스크립트가 오늘 ERROR 레인으로 간다.
다이제스트는 움직이지 않는다.

무엇이 보장되고 무엇이 안 되는지:

- **보장**: 소프트웨어 환경(Isaac/Kit 빌드·번들 파이썬·확장)이 로컬에서 돌린 것과 **같은
  바이트**다.
- **보장 안 됨**: 호스트 쪽 차이 — GPU 모델·VRAM·드라이버. 러너 호스트 드라이버는 **R580
  브랜치**이고, 컨테이너는 그것을 그대로 본다.
- **비용**: 새 이미지는 **콜드 런 한 번**(자산 다운로드 + 셰이더/컴퓨트 캐시 채우기)과 디스크
  **약 17–20 GB**를 쓴다. 캐시는 이미지별로 분리되므로(아래 §러너 프로비저닝) 이미지를 바꾸면
  캐시도 새로 채운다. **낡은 이미지·캐시 정리(prune)는 운영자 몫**이다 — 플랫폼은 지우지 않는다.

## 출력 디렉터리와 `.gitkeep`

체크아웃은 컨테이너에 **읽기 전용**으로 들어간다. 그 위의 마운트 포인트(`sim_output_dir`)는
컨테이너가 만들 수 없으므로 **커밋돼 있어야 한다** — 없으면 admit이 exit 2로 친절히 거절한다.

```
verify/out/.gitkeep        # 커밋한다
```

```gitignore
verify/out/*
!verify/out/.gitkeep       # 디렉터리는 커밋, 내용물은 런 잔여물
```

## 실측 (etri6000, 2026-09-07)

`cv-infra selftest`(번들 예제: 낙하 큐브 6케이스 = drop_height 3 × cube_scale 2, `repeats 1`,
`pict_k 2`, `concurrency 1`)의 **첫 라이브 GPU 실행**. 호스트 = RTX PRO 6000 Blackwell
(97,887 MiB) · 드라이버 580.159.03 · 이미지 `isaac-sim:5.1.0@sha256:f3563cb2…`(이미 로컬,
pull 0초).

| 항목 | 측정값 |
|---|---|
| 전체 벽시계 | **95초** / 6케이스 (케이스당 14.27–16.31초, 평균 14.78) |
| Isaac 부팅(`app ready`) | **10.5초** (웜 10.46–11.19 · 콜드 10.56–10.91) |
| 시뮬레이션(121 스텝) | 약 1.8초 · 컨테이너 teardown 약 0.5초 |
| VRAM (Isaac 프로세스) | **3,993 MiB** 피크, 6케이스 전부 동일 |
| 캐시 증가분(콜드→) | **11 MB** (kit 11M · compute 72K · home 32K) |

읽는 법, 정직하게:

- **VRAM 3,993 MiB는 1 Hz 샘플링의 하한**이지 증명된 최대가 아니다. 케이스가 ~14초라 그보다
  짧은 첨두는 놓칠 수 있다.
- **이 워크로드에서는 웜 캐시가 부팅을 빠르게 하지 않는다** — 콜드와 웜의 `app ready`가 통계적으로
  같고, 빈 캐시 트리가 11 MB만 자랐다. 부팅 시간은 이미지 레이어에서 올라오는 Kit 확장 기동이
  지배한다. **일반화 금지**: 이 예제는 로컬 지오메트리만 쓰는 물리 전용 씬이다. 클라우드 자산을
  여는 실제 씬은 첫 런에 수 분의 자산 다운로드를 낸다.
- 콜드 런 한 케이스가 **59.4초**로 튀었다(`app ready` 이후 약 45초 정체). 웜 두 번에서 재현되지
  않았고 원인을 귀속하지 못했다 — 케이스당 상한을 예산으로 잡을 때 참고할 것(`--case-timeout-s`
  기본 1800초라 위험 구간은 아니다).
- **concurrency > 1은 측정되지 않았다.** 위 스크래치 제약 때문에 k=1로만 돌렸다. VRAM 여유
  (~90 GiB)로는 산술상 여러 케이스가 들어가지만 그건 추정이지 측정이 아니다.

증거: 컨테이너 내부 로그가 자기 GPU 표(`NVIDIA RTX PRO 6000 Blackwell … 97887 MB`,
`Driver Version: 580.159.03`)와 Warp/CUDA 초기화를 찍었고, 자유낙하 z의 2계 차분이
0.002725 m = 9.81·(1/60)²로 정확히 일치했으며, 여섯 케이스 모두 `z_final == cube_scale/2`
(정지 시 반높이)로 끝났다.

## exit 계약

프로세스 종료 코드가 판정이고, Check Run의 conclusion은 여기서 유도된다(`report.json`의
`summary.exit_code`가 단일 원천).

| exit | 뜻 | Check conclusion |
|---|---|---|
| `0` | **PASS** — 판정된 체크가 모두 true(ERROR 케이스가 있으면 제목·요약에 크게 표시되지만 깨끗한 게이트를 뒤집지는 않는다) | `success` |
| `1` | **FAIL** — 판정된 체크가 false거나, 베이스라인 대비 체크 회귀 | `failure` |
| `2` | **CONTRACT** — 요청 거절. **GPU 0초**에서 끝나고 `errors.json`이 파일·줄·열을 실어 PR diff에 인라인 annotation으로 붙는다 | `failure` |
| `3` | **INFRA** — 플랫폼이 판정할 수 없었다(EULA env 부재·PICT 부재·docker 데몬·전 케이스 ERROR). **소비자 판정이 아니다** | `neutral` |

**스윕 모드**(`oracle_script` 없음)와 `report_only`는 2/3가 아닌 한 항상 `0`이고, Check Run은
`neutral` + "**this check does not gate**" 배너로 발행된다.

## 베이스라인 (회귀)

러너 호스트의 SQLite 파일(`CV_BASELINE_DB`, 기본 `~/.cv-infra/baselines.sqlite3`)에
`(case_id, verdict 키)` 한 행씩. `case_id`는 그 케이스의 축값 할당을 정렬해 해시한 값이라
케이스 순서나 배열 크기가 바뀌어도 같은 조합은 같은 행을 본다.

- **체크는 통과율을 비교한다.** `현재 < 베이스라인`이면 회귀(exit 1), `>`면 improved, 같으면 ok.
- **`repeats == 1`은 라벨만 붙인다** — detail에 `single_sample: true`. 게이트는 그대로 한다.
- **지표는 절대 게이트하지 않는다.** 평균의 변화를 delta로 보고할 뿐이고, 임계는 소비자가
  오라클에서 `bool`로 쓰면 된다.
- **부재 = skip**(`no_baseline`), ERROR 케이스도 skip. 손상·잠금·더 새 스키마 등 **어떤 예외도**
  stderr 한 줄 + "unavailable, all skipped"로 비켜난다 — 베이스라인은 더해지는 신호이지
  런을 못 내는 이유가 되면 안 된다.
- **PR은 베이스라인을 절대 갱신하지 않는다.** 워크플로가 `--update-baseline`을
  `github.event_name != 'pull_request'`일 때만 넘긴다(심사받는 런이 심사 기준을 옮기면 안 된다).

## 산출물

`--run-dir`(CI는 `$RUNNER_TEMP/cv-verify`) 아래에 이렇게 떨어지고, 아티팩트
`cv-infra-verification-results`로 업로드된다.

```
report.json                 # schema 1 — 입력·요약·매트릭스·베이스라인·아티팩트 목록
errors.json                 # exit 2일 때만. annotation dict 목록
payloads/check-run.json     # Check Run 페이로드(conclusion 포함)
payloads/sticky-comment.md  # PR 스티키 코멘트(마커로 in-place upsert)
payloads/step-summary.md    # $GITHUB_STEP_SUMMARY
zips/<case>.zip             # 케이스별 출력 dir(비어 있어도 만든다 — 정직한 수거)
logs/<case>.sim.log         # 케이스별 컨테이너 로그
```

## 러너 프로비저닝 (GPU 워크스테이션)

`cv-infra verify`는 **호스트에 이미 있는 것**을 전제한다. 하나라도 빠지면 이름이 붙은 exit 3이지
조용한 통과가 아니다.

| 전제 | 어떻게 마련하나 |
|---|---|
| 드라이버 R580 + Docker CE + NVIDIA Container Toolkit + 이미지 pull | [`scripts/workstation_setup/`](scripts/workstation_setup/README.md) (`provision.sh` · `realign_driver_r580.sh` · `pull_isaac.sh` · `test_gpu_passthrough.sh`) |
| NVIDIA EULA·텔레메트리 동의 | `bash scripts/consent/accept_eula.sh` → 기록 + `ACCEPT_EULA`/`PRIVACY_CONSENT`를 러너 서비스 환경에 로드. 상태 확인 = `scripts/consent/check_consent.sh` |
| PICT 바이너리 | `bash scripts/workstation_setup/install_pict.sh` (핀 커밋 clone+make, `export CV_PICT_BIN=…` 줄을 출력) |
| Omniverse 캐시 트리 (**이미지별**) | `bash scripts/measure/warm_cache.sh <cache-root>/<digest12> provision` 로 그 이미지의 6-way 트리 생성(+uid 1234 소유). `<digest12>` = `sim_image` 다이제스트의 앞 12 hex — Kit 셰이더·CUDA 컴퓨트·자산 캐시는 Isaac 빌드에 묶이므로 한 트리를 이미지끼리 공유하면 캐시가 아니라 오염이다. 서브트리가 없으면 조용히 콜드로 돌지 않고 **이 명령을 그대로 찍으며 멈춘다**(CLI는 uid 1234로 chown할 수 없으므로 만들지 않는다). 첫 런이 그 캐시를 채운다(케이스별 CoW 스크래치이므로 공유 베이스는 건드리지 않는다) |
| 캐시 스크래치 루트 (**선택 — 대개 켜지 말 것**) | 켜면(`CV_ISAAC_CACHE_SCRATCH_ROOT`) 케이스마다 웜 베이스를 `cp -a`로 복사해 쓴다. **실측(2026-09-07): CLI를 uid 1234가 아닌 계정으로 돌리면 동작하지 않는다** — 베이스의 uid-1234 `0700` 디렉터리를 읽지 못해 `cp -a`가 죽고, 설령 복사돼도 소유권 보존 가드가 거부한다(둘 다 시끄럽게 실패하고 스크래치를 폐기하므로 조용한 콜드 런은 없다). **root 권한을 가진 CLI 신원이 아니면 unset으로 두고 concurrency=1로 돌린다.** 켤 거라면 루트는 운영자가 직접 만든다(`mkdir -p` + uid 1234 쓰기 가능) — 어떤 스크립트도 만들지 않는다 |
| GitHub self-hosted 러너 (`cv-infra-gpu` 라벨) | `bash scripts/workstation_setup/register_gh_runner.sh` |
| `cv-infra` 콘솔 스크립트 + `import cv_infra` 가능한 python(같은 venv) | 이 저장소를 체크아웃해 `uv sync --frozen` |

동의는 **자동 수락되지 않는다.** 이 저장소에는 어떤 동의 값도 커밋돼 있지 않고, CI가 그것을
합성할 수도 없다 — 기록이 없으면 admit이 exit 3으로 멈춘다.

**러너 서비스 환경**: GitHub Actions 잡은 러너의 `.env`(`<runner-home>/.env`)에서 환경을 받고,
러너 Listener는 그 파일을 **기동 시 한 번만** 읽는다. 따라서 `ACCEPT_EULA`·`PRIVACY_CONSENT`·
`CV_PICT_BIN`·`CV_ISAAC_CACHE_ROOT`를 거기에 적었다면 **서비스를 재시작해야** 잡에 반영된다.
반영 전에는 `cv-infra verify`가 exit 3(INFRA)로 멈춘다 — 조용히 통과하지 않는다.

**수동 SSH 실행 주의(실측)**: 로그인 셸이 이 호스트에 없는 로케일(`ko_KR.UTF-8` 등)을 `LC_*`로
전달하면 PICT의 wide-char 읽기가 깨지면서 *"Order cannot be larger than number of parameters"*
라는 엉뚱한 에러가 난다. 수동 실행 시 `export LC_ALL=C.UTF-8`. GitHub Actions 경로는
`LANG=en_US.UTF-8`만으로 충분해 영향 없다.

## fork PR 신뢰 경계 (public 저장소라면 반드시 읽을 것)

verify 워크플로는 **호출한 저장소를 체크아웃해서 실행한다** — sim 스크립트·PICT 모델·오라클이
소비자의 파일이기 때문이다. 즉 PR을 검증한다는 것은 **그 head 브랜치의 코드를 GPU가 붙은
컨테이너 안에서 자기 러너 위에 돌린다**는 뜻이다.

fork PR을 받는 저장소라면 **Settings → Actions → Fork pull request workflows →
"Require approval for all external contributors"** 를 켜라. 신뢰되지 않은 PR이 이 머신에서
실행되는 것을 막는 것은 이 워크플로 파일이 아니라 **그 설정**이다. 워크플로는
`persist-credentials: false`로 잡의 토큰이 체크아웃된 `.git/config`에 남지 않게 하고,
`pull_request_target`은 어디에서도 쓰지 않는다.

## 개발

```bash
uv sync --frozen                                   # 커밋된 uv.lock 그대로
export CV_PICT_BIN=$HOME/.cache/cv-infra/pict/pict # 없으면 PICT 테스트는 skip된다

uv run ruff check .
uv run black --check .
uv run lint-imports --config .importlinter
env -u PYTHONPATH uv run coverage run -m pytest -q
env -u PYTHONPATH uv run coverage report           # fail_under = 100 (pyproject.toml)
```

**`env -u PYTHONPATH`가 붙는 이유**: 개발 호스트에서 ROS 2 환경을 source하면 그 `PYTHONPATH`가
셸에 남아 pytest 수집을 깨뜨린다(측정된 사실). `ubuntu-latest`에서는 no-op이지만
[`ci.yml`](.github/workflows/ci.yml)도 **똑같은 바이트**로 부른다 — 로컬과 CI가 다른 명령을
돌지 않게 하기 위해서다. 커버리지 플래그는 전부 `pyproject.toml [tool.coverage.*]`에만 있고
CI는 아무 옵션도 넘기지 않는다.

테스트는 **CPU 전용**이다: docker는 duck-typed fake 클라이언트로, Isaac은 아예 등장하지 않는다.
GPU에 대한 주장은 이 스위트가 아니라 워크스테이션에서 `cv-infra selftest`를 돌려서 한다.

```
cv_infra/
  contract/     # 최하층(형제 모듈만 import). errors · pict · inputs(모든 거절) · cases · verdict
  execution.py  # docker와 이야기하는 유일한 모듈(마운트·캐시·이미지 ensure·타임아웃·teardown)
  baselines.py  # SQLite(WAL) 베이스라인 — 전 접근 best-effort
  report/       # aggregate(접기 + exit 폴드) · github(Check Run/코멘트/step summary)
  cli/          # main(verify · selftest) · publish_glue(publish/annotate/stage-artifacts)
examples/selftest/   # 번들 예제 = `cv-infra selftest`가 도는 것
scripts/             # 러너 프로비저닝 · EULA 동의 · 캐시 · Isaac 스모크
```

레이어 경계는 취향이 아니라 `.importlinter`가 CI에서 집행한다(contract는 상위 모듈을 import할 수
없다).

## 라이선스·EULA 고지

Isaac Sim은 **NVIDIA 소유**이며 이 저장소는 그 이미지를 실행할 뿐 번들하지 않는다. 운영자가
`scripts/consent/accept_eula.sh`로 **명시적으로 동의**해야 하고, 이 배포는 어떤 경로로도 자동
수락하지 않는다.

## 관련 저장소

- **`cv-infra-workspace`**(이 저장소) — 플랫폼: CLI + 재사용 워크플로 + 러너 프로비저닝.
- **`cv-infra-user`** — 소비자 예시(carter). 플랫폼을 계약과 워크플로 참조로만 소비한다.
