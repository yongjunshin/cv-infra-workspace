# CV-Infra 사용 가이드

> **독자** — 자기 로봇 SW(SUT)를 cv-infra로 검증하려는 개발자. 플랫폼을 **설치**하는 절차는
> [`installation.md`](installation.md), 운영 심화는 [`deploy/README.md`](deploy/README.md).
>
> 이 문서의 모든 키·값·문면은 저장소 정본(코드/워크플로)에서 복사한 **사본**이며, 정본 위치를
> 각 절에 밝혀 둔다. 값이 의심되면 정본을 보라.

검증 요청의 단위는 **시나리오 YAML 한 장**이다. 그 안에 *어디서(scene) · 무엇을(robot) · 어디로
(goal) · 무엇을 통과로 볼지(acceptance_criteria) · 어떤 이미지로(sut)* 를 적으면, 플랫폼이

1. **접수(admit)** — 시뮬레이터를 켜기 전에 문서를 6단계로 검증하고(잘못된 문서는 GPU를 1초도
   쓰지 않고 exit 2로 거부),
2. **파생** — 분포를 선언했다면 `seed`에서 표본 n개를 결정적으로 만들고,
3. **실행** — 러너 + SUT 컨테이너 쌍에서 Isaac Sim 미션을 돌리고,
4. **판정·게시** — 표본별 결과를 요청 단위로 취합해 pass/fail·회귀를 PR로 돌려준다.

---

## 1. 시작하기 — 시나리오 YAML 해부

### 1.1 최소 문서

아래는 그대로 제출하면 **접수를 통과하는 완전한 문서**다(이 문서의 모든 YAML 예제는 실제
`load_request` admit 검증을 통과한 것이다 — 죽은 예제를 싣지 않는다).

```yaml
apiVersion: cv-infra/v1

scenario:
  scene: nova_carter_warehouse            # 씬 = 레지스트리 이름 또는 .usd 직접 참조
  robot: nova_carter
  goal: { x: -6.0, y: 5.0, yaw: 1.5708 }  # map 프레임 목표 포즈(m, rad)
  seed: 42                                # 결정성 시드
  timeout_s: 120                          # 미션 예산(sim 시간)

sut:
  image_ref: ghcr.io/<org>/<image>@sha256:<64-hex-digest>   # 이미지 REF만 — 빌드 컨텍스트 아님

acceptance_criteria:
  - oracle: reached_goal
    params: { position_tolerance_m: 0.75 }
  - oracle: no_collision
    params: { chassis_path: /World/Nova_Carter_ROS/chassis_link }
```

### 1.2 블록별로 무엇을 쓰나

| 블록 | 필수 | 사용 관점 설명 |
|---|---|---|
| `apiVersion` | **예** | 이 문서가 어느 계약 버전으로 쓰였는지. **생략하면 거부된다**(조용히 현행으로 간주하지 않는다). 값은 `cv-infra/v1` → §7 |
| `scenario` | **예** | 세계와 미션. `scene`/`robot`(자산 지정) · `goal`(x·y·yaw, 기본 프레임 `map`) · `seed`(결정성) · `timeout_s`(**sim 시간** 예산, wall-clock 아님). 선택: `initial_pose`(출발 포즈 3축) · `obstacles`(§3) · 레거시 `debug_obstacle`(상자 1개) |
| `sut` | **예** | 검증 대상 로봇 SW의 **컨테이너 이미지 ref**. 플랫폼은 이것을 pull해 띄울 뿐 빌드하거나 내부를 고치지 않는다(블랙박스). 선택 `image_id`로 정확한 Image Id를 핀할 수 있다 |
| `interface` | 아니오(기본값) | SUT와 시뮬레이터를 잇는 **ROS 2 배선**. 생략하면 ROS/Nav2 관례 기본값(`jazzy` · `/cmd_vel` · `/navigate_to_pose` · `/clock` …)이 쓰인다. **`odom_topics`·`sensors`는 기본값이 없다** — 자기 로봇의 토픽을 여기에 적어야 센서 스트림이 흐른다 |
| `acceptance_criteria` | **예**(≥1) | "무엇을 통과로 보는가". 빌트인 `reached_goal`·`no_collision` + 커스텀 oracle(§4). **기준도 입력**이다 |
| `execution_settings` | 아니오 | `repeats`(표본 수) · `min_pass_ratio`(통과 임계) · `fixed_dt` → §2.5 |

- 모든 블록은 **모르는 키를 조용히 무시하지 않고 거부**한다(오타가 침묵하지 않는다).
- 정본: 스키마 = [`cv_infra/contract/schema.py`](../cv_infra/contract/schema.py) ·
  `interface` = [`cv_infra/contract/adapter_schema.py`](../cv_infra/contract/adapter_schema.py) ·
  접수 파이프라인 = [`cv_infra/contract/loader.py`](../cv_infra/contract/loader.py).
- 실사용 예: 소비자 예제 저장소 `cv-infra-user`의 `scenarios/*.yaml`(측정값이 주석으로 붙어 있다).

### 1.3 문서가 잘못됐을 때 (exit 2)

접수는 **시뮬레이터 부팅 전**에 끝난다. 위반은 raw 트레이스백이 아니라 *필드 경로 + 기대값 +
고칠 예시 + 파일:줄:열* 로 돌아온다(아래는 실측 출력 — 긴 줄은 표시용으로 접었다):

```
$ cv-infra submit scenarios/my_scenario.yaml ; echo $?
cv-infra submit: scenario.seed: expected Input should be a valid integer, unable to parse string
as an integer, got 'forty-two' | example: seed: 42 | at scenarios/my_scenario.yaml:7:9
2
```

CI에서는 같은 위반이 PR diff 위 **인라인 annotation**으로 찍힌다(§6.5).

---

## 2. 랜덤화 — 한 장에 "분포"를 쓴다

고정 시나리오 1장은 조건 1개다. 값 자리에 분포를 적으면 그 한 장이 **조건의 분포**가 되고,
플랫폼이 `seed`에서 표본 n개를 파생해 컨테이너 한 쌍의 **부팅 1회**로 연속 실행한다.

### 2.1 표기는 세 단어뿐

| 쓰는 법 | 뜻 | 예 |
|---|---|---|
| `x: -6.0` | 정적 — 모든 표본에서 같은 값 | `x: -6.0` |
| `x: {uniform: [lo, hi]}` | 닫힌 구간에서 균등 draw(실수) | `{uniform: [-6.3, -5.7]}` |
| `y: {choice: [a, b, …]}` | 목록에서 하나 고르기(값은 **verbatim**) | `{choice: [5.0, 3.0]}` |
| `count: {randint: [lo, hi]}` | 정수 개수 draw, **양끝 포함** — 장애물 `count` 전용 | `{randint: [0, 5]}` |

- `lo == hi`는 합법(한 축만 고정), `lo > hi`는 오타로 **loud 거부**.
- 모르는 표기(`{gaussian: …}`)는 접수 단계에서 거부되며, 메시지가 **받는 단어 목록을 나열**한다.
- 랜덤을 허용하는 자리: `initial_pose.x/y/yaw` · `goal.x/y/yaw` · `debug_obstacle.x/y` ·
  `obstacles[i].x/y/yaw` + `obstacles[i].count`(`randint`). 그 외 필드에 분포를 쓰면 거부된다.
  (정본: `RANDOM_FIELDS`/`OBSTACLE_FIELDS` = [`cv_infra/contract/derive.py`](../cv_infra/contract/derive.py))

### 2.2 예제 (완전 문서)

```yaml
apiVersion: cv-infra/v1

scenario:
  scene: nova_carter_warehouse
  robot: nova_carter
  goal:
    x: -6.0                              # 정적 축 — 모든 표본에서 같은 값
    y: { choice: [5.0, 3.0] }            # 검증된 목표 2점 중 하나
    yaw: 1.5708
  seed: 42                               # 같은 seed + 같은 문서 = 같은 표본들
  timeout_s: 120
  initial_pose:                          # 출발 포즈를 3축으로 흔든다
    x: { uniform: [-6.3, -5.7] }
    y: { uniform: [-1.3, -0.7] }
    yaw: { uniform: [2.9916, 3.2916] }

execution_settings:
  repeats: 5                             # = 표본 수
  min_pass_ratio: 0.8                    # 5개 중 4개 이상 pass 면 이 요청은 pass

sut:
  image_ref: ghcr.io/<org>/<image>@sha256:<64-hex-digest>

acceptance_criteria:
  - oracle: reached_goal
    params: { position_tolerance_m: 0.75 }
  - oracle: no_collision
    params: { chassis_path: /World/Nova_Carter_ROS/chassis_link }
```

> **함정 — `choice`는 축마다 독립으로 뽑는다.** `x`와 `y` 양쪽에 `choice`를 쓰면 "점 목록"이
> 아니라 **교차곱**이 된다. 위 예제가 두 목표점의 공통 `x`를 정적으로 두고 `y` 한 축에만
> `choice`를 쓴 이유다.

### 2.3 결정성 — 같은 seed, 같은 표본

- 표본 `i`는 `sha256("cv-derive/1:<seed>:<i>")`로 **독립 시딩**된다. 표본 3을 만들려고 0~2를 먼저
  만들 필요가 없고, 어느 호스트에서 돌려도 같은 (문서, seed, i) → 같은 표본이다.
- `uniform` draw는 **소수 4자리로 반올림**되고(0.1 mm / 0.006°), `choice` 값은 소비자가 쓴
  리터럴 그대로 쓰인다.
- 파생된 표본에는 **스탬프**가 찍힌다: `scenario.derivation: {version: cv-derive/1, index: <i>}`.
  이 블록은 **플랫폼만 쓸 수 있다** — 제출 문서에 직접 적으면 거부된다.
- 분포를 하나도 쓰지 않은(정적) 문서는 파생 경로를 아예 타지 않는다 — 와이어 바이트와
  `request_identity_key`(§6.3)가 이전과 동일하게 보존된다.

실측(2026-08-30, 위 §2.2 예제 · `cv-derive/1` · seed 42): 표본 0 =
`initial_pose (x -6.1821, y -1.2083, yaw 3.2691)` · `goal.y 5.0` · `derivation {cv-derive/1, 0}`.
같은 문서를 다시 제출해도 같은 값이다. (제출 전 미리보기 CLI는 아직 없다 — §8.)

### 2.4 SUT 비결정성은 다른 문제다

같은 seed는 **입력**을 재현한다. SUT(블랙박스 로봇 SW)가 같은 입력에서 같은 결과를 낸다는 뜻은
아니다 — 그것을 다루는 사양이 `repeats` + `min_pass_ratio`다.

### 2.5 `repeats` · `min_pass_ratio` 조합

| 시나리오 | `repeats` | `min_pass_ratio` | 이 조합의 의미 |
|---|---|---|---|
| 정적 | 1 | — | 단건 검증 |
| 정적 | n | — | 같은 조건 n번 — 흔들리는 판정(flaky) 탐지 |
| 분포 | n | 없음 | 표본 n개, **하나라도 fail이면 fail**(any-fail) |
| 분포 | n | 예: `0.8` | 표본 n개 중 80% 이상 pass면 요청 verdict = pass |

`min_pass_ratio`는 `0 < r <= 1`. 선언하면 그 임계가 **Check 표면과 exit 계약 양쪽에 일관되게**
적용되고, 표본 분포(`pass`/`fail` 열)는 숨기지 않고 그대로 보인다(§6.2).

---

## 3. 장애물 — 종류 · 개수 · 자세

`scenario.obstacles` 목록의 각 항목이 *"이런 물체를 이 규칙으로 n개"* 를 선언한다. 파생 시
각 항목이 `count`개의 구체 장애물로 전개되므로 **표본마다 장애물 개수가 달라질 수 있다**
(`n=0`이 뽑힌 표본은 그 항목이 아예 없다 — 합법).

```yaml
apiVersion: cv-infra/v1

scenario:
  scene: nova_carter_warehouse
  robot: nova_carter
  goal: { x: -6.0, y: 5.0, yaw: 1.5708 }
  seed: 42
  timeout_s: 120
  obstacles:                             # 레거시 debug_obstacle 과는 택일(동시 선언 = 거부)
    - asset: chair                       # 큐레이션 레지스트리 이름
      x: { uniform: [-2.6, -2.0] }
      y: { uniform: [1.0, 3.0] }
    - asset: desk
      count: { randint: [0, 5] }         # 표본마다 0~5개 — 개수 자체가 랜덤(양끝 포함)
      x: { uniform: [-1.5, 0.0] }
      y: { uniform: [5.5, 10.5] }
      yaw: { uniform: [-3.1416, 3.1416] }
    - asset: box                         # 내장 큐보이드 — 치수 선언은 box 전용
      count: 2
      x: { uniform: [-1.0, 1.0] }
      y: { uniform: [0.0, 4.0] }
      yaw: { choice: [0.0, 1.5708] }
      height: 0.10                       # ★ 판정을 바꾸는 축 — §3.2
      width: 1.214
      depth: 3.495

execution_settings:
  repeats: 5
  min_pass_ratio: 0.8

sut:
  image_ref: ghcr.io/<org>/<image>@sha256:<64-hex-digest>

acceptance_criteria:
  - oracle: reached_goal
    params: { position_tolerance_m: 0.75 }
  - oracle: no_collision
    params:
      chassis_path: /World/Nova_Carter_ROS/chassis_link
      collision_excluded_paths: [/World/Nova_Carter_ROS]
```

### 3.1 규칙

| 규칙 | 내용 |
|---|---|
| `asset` 3형태 | ① 큐레이션 레지스트리 이름 — **`chair` · `desk` · `forklift`**(bbox·z_offset·콜라이더를 실측한 뒤 등재) ② 직접 `.usd`/`.usda`/`.usdz` 참조(자기 자산) ③ **`box`**(내장 큐보이드). `scenario.scene`과 같은 해석 규칙이며, 미지 이름은 아는 이름을 나열하며 거부된다 |
| 치수 | `height`/`width`/`depth`는 **`box` 전용**이다. USD 자산은 자기 extent를 갖고 오므로 비-box에 선언하면 조용히 무시하지 않고 거부한다. `z`는 없다 — 바닥 접촉이 결정한다 |
| 개수 | `count`는 정수 또는 `{randint: [lo, hi]}`. 계약이 그룹당 `0..32`로 제한하고, 러너가 **표본당 총 풀 32 prim**(버킷 합)을 넘는 계획을 부팅 전에 거부한다. 둘 다 오타 방어용 **구조적 상한**(측정값 아님) |
| 정적·부동 | 장애물은 **밀리지 않는 정적 충돌체**다. 동적 물리(`RigidBodyAPI`/`ArticulationRootAPI`)를 실은 USD 자산은 부팅 시점에 거부된다 |
| 택일 | 레거시 `debug_obstacle`(상자 1개)과 `obstacles`를 **동시에 선언하면 거부**된다(에러가 옮겨 적을 dict를 그대로 준다) |
| 충돌 집계 | 장애물 접촉은 `no_collision` 오라클의 `collision_count`에 **실제 충돌로 집계된다**(그게 목적). 빼려면 그 오라클의 `collision_excluded_paths`에 **`/World/cv_obstacles`** 한 줄을 추가한다 — 풀 전체가 이 한 스코프 아래 산다 |
| 인스턴스 간 간격 | 계약에 최소 간격 개념이 **없다** — 한 그룹의 인스턴스들은 독립 draw라 겹쳐 설 수 있다(정적 콜라이더끼리는 접촉 이벤트가 없어 판정 영향 0, 비용은 영상에서 겹쳐 보이는 것) |

### 3.2 ★ 높이가 판정을 바꾼다 (실측)

장애물이 SUT의 **2D 라이다 스캔 밴드**에 걸리면 로봇에게 *지도에 없는 물체*로 보이고, 그러면
측위(AMCL)가 어긋난다 — **경로를 막지 않아도** 판정이 흔들린다.

캐리어 SUT(nova_carter) 실측 밴드 = **z ∈ [0.1256, 2.0256] m**
(`XT_32` prim world z `0.5256` − `pointcloud_to_laserscan` `min_height` `0.4`). 러너가 상자를
`height/2`에 놓아 바닥에 붙이므로 **상자 윗면 = 선언 높이**다.

| 선언 높이 | `/scan` 검출 | 표본 통과 | 미션 종료 시 측위 믿음 오차(최대) |
|---|---|---|---|
| `height: 0.15`(box 기본값) | 밴드 안으로 2.4 cm → **17 %** | — | **5.66 m** |
| `height: 0.10` | **0/241 = 0 %** | **5/5** | **0.73 m** |

- **CI 게이트로 쓸 시나리오의 장애물은 `height: 0.10` 이하**를 권고한다(보이지 않는 충돌 프로브).
- ⚠ **밴드 값은 SUT마다 다르다.** 위 숫자는 이 데모 SUT의 라이다 높이·`min_height`에서 나온
  것이다 — 자기 로봇의 값으로 다시 계산하라. 센서 파라미터는 우리 계약이 아니라 **SUT 소유**다.
- 기본값 `0.15`는 바꾸지 않았다(`height`를 생략한 기존 문서의 바이트·거동 보존). 보이지 않는
  프로브가 필요하면 **명시**하라.
- 정본·측정 문면: [`cv_infra/runner/sim_runtime.py`](../cv_infra/runner/sim_runtime.py)
  (`DEBUG_OBSTACLE_DEFAULT_HEIGHT` 주석) · [`releases.md`](releases.md)의 2026-08-29 정정 이력.

---

## 4. 커스텀 오라클

빌트인 `reached_goal`/`no_collision`으로 표현되지 않는 기준은 **플러그인**으로 쓴다. 두 가지 참조
형태가 있다.

| 형태 | 쓰는 법 | 언제 |
|---|---|---|
| 엔트리포인트 이름 | `oracle: my_oracle` | 자기 배포판이 `cv_infra.oracles` 엔트리포인트 그룹에 등록했을 때 |
| `module:Class` 경로 | `oracle: "max_time_to_goal:MaxTimeToGoalOracle"` | **설치 없이** 시나리오 옆 `.py` 파일을 그대로 쓸 때 |

```yaml
acceptance_criteria:
  - oracle: reached_goal
    params: { position_tolerance_m: 0.75 }
  - oracle: no_collision
    params: { chassis_path: /World/Nova_Carter_ROS/chassis_link }
  - oracle: "max_time_to_goal:MaxTimeToGoalOracle"   # 시나리오 옆 max_time_to_goal.py 의 클래스
    params: { max_time_to_goal_s: 30.0 }             # 플러그인이 자기 params 를 검증한다
```

**어디에 두나** — 시나리오 YAML **옆(같은 디렉토리)**. 플랫폼이 접수 시 그 디렉토리를
`sys.path`에 얹고, 러너 컨테이너에 **같은 절대경로로 읽기전용 bind-mount**한 뒤
`CV_ORACLE_PLUGIN_DIR`로 알려 준다(러너에만 — SUT에는 절대 새지 않는다). 설치도, 파생 이미지도,
러너 이미지 수정도 없다. REST로 직접 제출할 때는 요청별 앵커를 `oracle_plugin_dirs`(요청 배열과
같은 길이)로 넘긴다.

**플러그인이 지켜야 할 규칙** (예제: 소비자 저장소 `scenarios/max_time_to_goal.py`)

- `cv_infra.oracles.base.OracleBase`를 상속하고 `name`·`version`·`validate_params`·`evaluate`를 구현.
- **모듈 스코프에서 `omni.*`/`isaacsim.*`를 import 하지 마라** — 러너는 시뮬레이터가 뜨기 **전에**
  평가 엔진을 구성하므로 부팅 전에 죽는다. 모듈 스코프는 `cv_infra.*` + stdlib만.
- 결정적 순수 파이썬(시계·난수·네트워크 없음). 텔레메트리와 병합된 criteria 뷰만 읽는다.
- `validate_params`는 **방어적으로** — 잘못된 params는 조용히 기본값으로 넘어가지 말고 명확한
  메시지로 거부하라. 이 검사는 **부팅 전**에 불리고, raise는 exit 2로 매핑된다.
- 로드 실패(미지 이름·import 오류·`OracleBase` 아님)는 접수 단계에서 **exit 2**로 거부된다.

---

## 5. 실행

### 5.1 CLI — 7개 명령

`cv-infra`가 **단일 계약 표면**이다(GitHub Action은 이 CLI의 얇은 래퍼다). `--help`가 정본.

| 명령 | 하는 일 |
|---|---|
| `cv-infra run <scenario.yaml> --runner-image <ref>` | 시나리오 한 장을 **오케스트레이터 없이** 끝까지 실행(감독자가 SUT + 러너를 함께 띄운다). `--out-dir`(기본 `./cv-infra-out`) · `--job-id` |
| `cv-infra submit <envelope.yaml \| scenario.yaml …>` | 오케스트레이터에 제출. 시나리오 경로/글롭을 여러 개 주면 CLI가 **size-N 봉투를 합성**한다(사전식 경로 순서). `--wait`로 최종 판정까지 대기하고 그 결과가 **exit code**가 된다 |
| `cv-infra status <envelope_id>` | 진행 상황 조회(정보용 — verdict로 게이트하지 않는다) |
| `cv-infra wait <envelope_id>` | 최종 취합 verdict까지 블록하고 exit 0/1/3 |
| `cv-infra report <envelope_id> [--json]` | 취합된 리포트 출력(`--json`은 원본 JSON) |
| `cv-infra monitor` | 큐·자원·헬스 **운영뷰**(정보용) |
| `cv-infra selftest` | 빌트인 stub 라운드트립 — **외부 SUT 0 의존**. exit는 `submit --wait`와 같다 |

자주 쓰는 옵션

- `--sut-image REF` — 제출되는 모든 시나리오의 `sut.image_ref`를 덮어쓴다. 우선순위:
  **플래그 > `$CV_INFRA_SUT_IMAGE` > 시나리오 값**. ref 문자열일 뿐 pull·검사하지 않는다.
- `--trigger-source {human-manual,ci-cd}` — 누가 돌렸는지(기본 `human-manual`, Action은 `ci-cd`를
  넘긴다). 그대로 기록되어 리포트에 남는다.
- `--timeout S` — 최종 판정 대기 상한. 초과 = **exit 3**.
- `--api URL` — 오케스트레이터 주소(기본 `$CV_INFRA_API`, 없으면 `http://127.0.0.1:8000`).
- `--errors-json PATH` — 계약 오류(exit 2) 시 기계가 읽는 8키 에러 목록을 파일로. 기본값은
  `$GITHUB_ACTIONS`가 설정된 환경에서만 `./errors.json`(standalone은 off).

**REST도 같은 표면이다**(CLI ↔ REST 동등): `POST /envelopes`(202) · `GET /envelopes/{id}` ·
`GET /envelopes/{id}/report` · 운영뷰 `GET /monitor.json`. 계약 위반은 같은 에러 객체로 422다.

### 5.2 CI 배선 — 재사용 워크플로 (권장)

소비자 워크플로에서 **잡 하나**가 통합 표면의 전부다. 이 잡은 `runs-on`도 `steps`도 갖지 않는다 —
플랫폼의 워크플로가 곧 잡 정의다.

```yaml
jobs:
  verify:
    needs: build-sut                       # 아래 ①②를 제공하는 소비자 소유 잡
    permissions: { checks: write, pull-requests: write, contents: read }
    uses: <org>/cv-infra-workspace/.github/workflows/verify.yml@v1
    with:
      sut_image: ${{ needs.build-sut.outputs.image_ref }}   # 이미지 REF 만
      scenarios: scenarios/*.yaml
      runner_label: cv-infra-gpu
```

| 입력 | 기본값 | 뜻 |
|---|---|---|
| `sut_image` | (필수) | 미리 빌드된 SUT 이미지 **ref**. GPU 박스에서 소스로 빌드·실행하지 않는다 |
| `scenarios` | `scenarios/*.yaml` | `cv-infra submit`에 넘길 경로/글롭 |
| `scenarios_artifact` | `cv-infra-verification-inputs` | 시나리오 선언을 실어 나르는 **아티팩트 이름**. GPU 잡은 PR 소스를 체크아웃하지 않으므로, 시나리오는 빌드 잡이 업로드한 이 아티팩트로만 도착한다 |
| `runner_label` | `cv-infra-gpu` | GPU 워크스테이션을 고르는 self-hosted 러너 **라벨**(IP 아님) |
| `api` | `""` | 오케스트레이터 주소(빈 값 = 같은 호스트 기본값) |
| `timeout_s` | `3600` | 최종 판정 대기 상한. 초과 = exit 3 |

호출하는 쪽(소비자 소유 `build-sut` 잡)이 준비할 것은 둘뿐이다.

1. **SUT 이미지 ref** — 로봇 이미지를 빌드·push하고 digest 핀 ref를 잡 `outputs`로 내준다.
2. **시나리오 아티팩트** — 검증에 태울 시나리오(+ 커스텀 oracle `.py`)를 `scenarios/` 접두사가
   보존되도록 업로드한다. 아티팩트 이름 기본값 = `cv-infra-verification-inputs`. **이 업로드
   목록이 곧 배송**이다 — 여기 없는 시나리오는 검증되지 않는다(에러가 아니라 그냥 없는 것).

실작동 예제 전체(빌드 잡 포함)는 소비자 예제 저장소 `cv-infra-user`의 `.github/workflows/verify.yml`.

- 트리거는 **평범한 `pull_request`** 를 쓴다. 포크 PR 소스를 GPU 박스에서 특권 컨텍스트로
  실행하는 변형 트리거는 쓰지 않는다(보안 경계).
- **배송한 시나리오만 검증된다.** 잡은 워크스페이스를 먼저 비우고, 글롭이 집은 파일이 전부
  이번 실행의 아티팩트가 준 것인지 확인한 뒤에만 제출한다(어긋나면 exit 2로 멈춘다). 즉
  *"업로드 목록에서 뺀 시나리오"* 는 다음 push부터 검증 대상이 아니다.
- 상세 문면 정본: [`.github/workflows/verify.yml`](../.github/workflows/verify.yml).

### 5.3 CI 배선 — composite action (고급)

잡을 직접 소유하고 싶으면(다른 스텝과 섞고 싶을 때) 한 스텝으로 넣는다. 호출 모델은
재사용 워크플로와 **섞지 않는다**(둘 중 하나만).

```yaml
jobs:
  verify:
    runs-on: [self-hosted, cv-infra-gpu]
    permissions: { checks: write, pull-requests: write, contents: read }
    steps:
      - uses: <org>/cv-infra-workspace/actions/verify@v1
        with:
          sut_image: ghcr.io/<org>/<image>@sha256:<64-hex-digest>
          scenarios: scenarios/*.yaml
```

정본: [`actions/verify/action.yml`](../actions/verify/action.yml).

### 5.4 GitHub 없이도 같은 검증

CI 전용 기능은 없다. `cv-infra run` / `submit --wait`는 GitHub 없이 같은 판정을 같은 exit code로
낸다 — CI에서 빨간 것을 로컬에서 그대로 재현할 수 있다는 뜻이다.

---

## 6. 결과 읽는 법

### 6.1 exit 계약

| exit | 뜻 | Check conclusion |
|---|---|---|
| `0` | **PASS** — 모든 검증 통과 | `success` |
| `1` | **FAIL** — 검증 실패 또는 baseline 대비 회귀 (SUT 판정) | `failure` |
| `2` | **CONTRACT** — 시나리오·계약 오류 | `failure` + YAML 인라인 annotation |
| `3` | **INFRA** — 인프라 문제(오케스트레이터 다운·EULA 미동의·러너 크래시) | `neutral` |

- 봉투 단위 취합은 **`errored` > `fail` > `pass` 우선순위**다: 판정을 내지 못한 잡이 하나라도
  있으면 exit 3이고, **exit 3을 실패로 뭉개지 않는다**. Check에는
  `플랫폼/인프라 문제로 검증 미완료 — SUT 판정 아님`이 함께 표시된다.
- "당신의 YAML이 틀렸다(2)" / "당신의 로봇이 실패했다(1)" / "우리 플랫폼이 깨졌다(3)"를 구분하는
  것이 이 계약의 전부다. 정본: [`cv_infra/cli/exit_codes.py`](../cv_infra/cli/exit_codes.py).

### 6.2 PR에 보이는 것

- **Check Run** — 이름 `CV-Infra Verification`, conclusion은 위 표 그대로.
- **sticky 코멘트** — push마다 새로 쌓이지 않고 **제자리 갱신**된다.
- **step summary** — 코멘트와 같은 본문.

본문의 표(헤더 그대로):

```
| request | sut | verdict | repeats | pass | fail | flaky | identity | metrics |
```

- `repeats`/`pass`/`fail` = 표본 분포 그대로. `min_pass_ratio`를 선언한 행에는 표 아래
  *"pass ratio ≥ 0.8 declared"* 문구가 붙어 **"fail이 있는데 왜 pass인가"** 를 표가 스스로 설명한다.
- `identity` = 요청 지문(`request_identity_key`) 축약. 전체 키는 회귀 절과 아티팩트의
  report JSON에 있다.
- `metrics`는 **측정된 값만** 나온다(측정 안 된 키는 `n/a` — 0을 지어내지 않는다).

### 6.3 회귀(baseline) — 경계를 알고 읽어라

- 비교 축 = **`request_identity_key`**: 요청 문서에서 **SUT 축과 `apiVersion`을 뺀** 정규화 해시.
  즉 *"SUT만 다른 같은 요청"* 은 같은 키로 묶인다(그게 목적). `repeats`도 제외된다 — 표본 수를
  올린다고 baseline이 무효화되지 않는다.
- 판정: `pass → fail` = **회귀**, `fail → pass` = 개선, 같으면 unchanged.
- **baseline이 없으면 skip이고, 그것이 정상이다**(최초 실행·새 요청) — 실패가 아니다.
- baseline 갱신 정책: 없으면 확립(첫 판정이 fail이어도 확립 — 나중 수정이 "개선"으로 읽히게),
  pass면 전진, **fail은 기존 baseline을 덮지 않는다**(고칠 때까지 회귀가 계속 보인다).
  판정 불가(`errored`)는 절대 baseline이 되지 않는다.
- ⚠ **경계(C-1)**: baseline은 **이 cv-infra 배포의 내부 SQLite에만** 산다. 플랫폼은 소비자의
  CI 이력이나 git 이력을 **읽지 않는다.** 그러므로 새 배포·store 초기화 후 첫 실행에는 baseline이
  없고(=skip), 그것은 결함이 아니다.

### 6.4 아티팩트

CI 실행마다 `cv-infra-verification-results` 아티팩트가 올라간다: `report.json` + 게시 페이로드 +
선별된 실행 산출물(`result.json` · rosbag `.mcap` · 주행 `.mp4`).

- 선별 정책 = **실패 표본 전부 + 통과 대표 1개**(가장 낮은 repeat index). 나머지 통과 표본은
  올리지 않는다(중복 가치 대비 용량).
- 잡별 MCAP 상한(현행 32 MiB, 잠정)을 넘으면 **부분 잘라내기 대신 제외 + 경고**로 처리한다.
- 보존 기간은 GitHub Actions 기본값을 그대로 쓴다.

### 6.5 계약 오류의 CI 표면

exit 2일 때 `errors.json`(8키: `field_path`·`expected`·`got`·`example`·`doc_link`·`source_path`·
`source_line`·`source_col`)이 workflow annotation으로 렌더된다. 실측 출력(표시용 줄바꿈):

```
::error file=scenarios/my_scenario.yaml,line=7,col=9::scenario.seed: expected Input should be a
valid integer, unable to parse string as an integer, got 'forty-two' | example: seed: 42 | at
scenarios/my_scenario.yaml:7:9
```

`file=` 경로는 **소비자 저장소 루트 기준**이라 PR diff의 그 줄에 그대로 붙는다.

---

## 7. 버전 — 3축은 독립이다

| 축 | 소비자가 쓰는 곳 | 움직임 |
|---|---|---|
| ① Action 태그 | `uses: …@v1` | 플랫폼 릴리즈. `@v1`은 v1.x 최신을 따라가는 **major 별칭**이고, 재현 가능한 불변 핀이 필요하면 `@vX.Y.Z` 형태의 구체 패치 태그를 쓴다(발행 목록 = 릴리즈 대장) |
| ② CLI/패키지 버전 | 배포된 `cv-infra` | 플랫폼 설치·재배포로 움직인다 |
| ③ 계약 `apiVersion` | 시나리오 첫 줄 | 문서가 쓰인 계약 버전. **`cv-infra/v1`** |

세 축은 함께 움직이지 않는다 — Action 태그 이동이 계약 파괴를 뜻하지 않고, 새 `apiVersion`
도입이 워크플로 핀 변경을 강제하지 않는다. 현행 값 표·발행 이력 정본 =
[`version-compatibility.md`](version-compatibility.md) · [`releases.md`](releases.md).

`apiVersion` 해석은 3-상태다.

| 상태 | 결과 |
|---|---|
| 지원·현행 | 그대로 접수 |
| 지원·deprecated | 접수 + **WARNING**(sunset 시점 + 마이그레이션 링크) — 검증은 정상 진행(exit 0 가능) |
| 미지 / 부재 | **거부(exit 2)** + 받는 값 목록 + 고칠 예시 |

> 현재 deprecated 목록은 **비어 있다** — `cv-infra/v1`이 유일한 현행 계약이다. 파괴적 변경은
> MAJOR 범프에서만 하고, sunset 창은 최소 2 릴리즈다.

---

## 8. 한계·주의 (알고 쓰기)

| 한계 | 지금의 우회 |
|---|---|
| **보이는 미지도 장애물은 안정적인 CI 게이트가 되기 어렵다** — 같은 입력에서 통과/실패가 진동한다(실측 p7c3 T5, 60 GPU 표본: 같은 문서를 연속 두 번 돌린 두 팔이 5/5 ↔ 1/5로 갈렸다) | 게이트 시나리오는 높이 ≤ `0.10` m로 가시성을 끊고(실측 5/5), 보이는 가구는 **비게이트 강건성 데모**로 쓴다. 표본 비율은 판정이 아니라 분포로 읽는다(§3.2) |
| SUT 비결정성은 플랫폼이 제거하지 않는다 | `repeats` + `min_pass_ratio`가 그것을 다루는 사양이다(§2.5) |
| 실패한 표본의 **구체 좌표**가 PR 표면에 아직 없다 | 실패 표본의 rosbag/영상 첨부로 확인 |
| 표본별 배치 내부 로그가 CI 첨부 밖에 있다 | 운영 호스트에서 확인 |
| 제출 전 *"내 seed가 어떤 표본을 만들지"* 미리보기 CLI가 없다 | 파생은 결정적이므로 첫 런의 기록이 곧 미리보기 |
| 장애물 치수는 랜덤화 불가(box 전용 정적 선언)·자산 `scale` 없음 | 크기 변형이 필요하면 `box` 치수를 그룹별로 나눠 선언 |
| 동적(밀리는) 장애물 미지원 — `RigidBody` 자산은 거부 | 정적 검증이 v1 사양(결정성 보호) |
| 한 그룹의 인스턴스가 서로 겹쳐 설 수 있다(최소 간격 개념 없음) | 창을 나누어 선언 |
| 회귀 baseline은 **이 배포의 내부 store 한정**(소비자 CI/git 이력 미조회) | 새 배포의 첫 실행은 baseline 부재 = skip(정상, §6.3) |
| `metrics.min_clearance_m`은 MVP 범위 밖 — 항상 비어 있다 | 근접도 판정이 필요하면 커스텀 oracle(§4) |
| 제출 API에 인증이 없다(단일 호스트 MVP) | 기본 주소가 `127.0.0.1`인 이유다 — 외부에 열지 마라 |

---

## 관련 문서

[`installation.md`](installation.md) 설치 · [`releases.md`](releases.md) 릴리즈 대장 ·
[`version-compatibility.md`](version-compatibility.md) 버전 매트릭스 ·
[`deploy/README.md`](deploy/README.md) 배포·운영 · [`../README.md`](../README.md) 개요.
