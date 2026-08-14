# 빌트인 stub SUT 이미지 (`docker/selftest_stub/`)

> **한 줄**: `cv-infra selftest` 가 **외부 SUT 0 의존**으로 라운드트립을 돌 수 있게, 플랫폼이
> 스스로 공급하는 SUT 컨테이너. M7 §3.5 **옵션 B**(초경량 컨테이너 + 잡 전용 네트워크 DDS).
>
> 트레이스: `REQ-SELFTEST-001`(빌트인 stub) · `REQ-SELFTEST-003`(라운드트립) ·
> `NFR-SELFTEST-001`(소비자 레포 0 의존) · `REQ-EXEC-004`(SUT 연결 — stub 핸들로 충족).

---

## 1. 왜 이게 필요했나

`DoD-P5-07` 이 라이브로 닫히지 않은 이유는 **하나**였다: 플랫폼에 SUT 핸들이 없었다.
오케스트레이터·러너·supervisor 는 이미 다 있고, self-test 봉투도 UC-01 경로를 그대로 탄다.
빠진 것은 **러너의 readiness 배리어에 응답할 상대**뿐이고, 그게 없으면 잡은
`SUT readiness barrier timed out`(`cv_infra/runner/main.py`) 으로 죽는다.

**러너 이미지를 SUT 자리에 넣는 우회는 막혀 있다**(실측): 15.5 GB 인데다 SUT 역할로 뜨면
즉시 종료 → supervisor 의 재시작 예산을 태우고 잡이 인프라 실패로 끝난다
(`cv_infra/orchestrator/supervisor.py`). **초경량은 취향이 아니라 요구사항**이다.

**왜 옵션 A(러너 내장 드라이버)가 아니라 B 인가**: M7 §3.5 **D-O** 가 배포·이식성 게이트에
옵션 B 를 **우선**으로 못박았다. 재배포가 실제로 깨뜨리는 것은 **컨테이너 경계 DDS +
잡 전용 네트워크 격리**인데, in-process 드라이버는 그 경계를 건너지 않으므로 그것을
증명하지 못한다. 이 이미지는 일반 잡과 **똑같은 토폴로지**로 뜬다.

---

## 2. 무엇을 만족시키나 (러너 계약 — 이 표가 이 이미지의 존재 이유다)

계약의 정본은 `cv_infra/runner/adapter/ros2.py` 다. **러너를 stub 에 맞춰 고치지 않는다 —
stub 이 러너에 맞춘다.**

| # | 러너가 요구하는 것 | 누가 공급 | 이 이미지에서 |
|---|---|---|---|
| 1 | `/clock` **흐름**(엔드포인트 존재가 아니라 유량 — G-19) | **러너(Isaac)** | 해당 없음 |
| 2 | `readiness.is_active_service` 의 **`std_srvs/Trigger`** 가 `success=True` | **stub** | 노드가 그 이름으로 서비스를 연다 |
| 3 | 그 서비스를 **소유한 노드**의 `get_parameters` 가 `use_sim_time` = **true** | **stub** | 노드 **이름을 서비스 경로의 부모 세그먼트로** 지어, 어댑터의 `get_parameters_service_for()` 유도가 이 노드에 떨어진다. rclpy 자체 파라미터 서비스가 답한다 |
| 4 | `nav2_msgs/action/NavigateToPose` 목표 **수락 → 종료 GoalStatus** | **stub** | 액션 서버가 ACCEPT → SUCCEEDED |

이름은 전부 **M1 어댑터 스키마 기본값**(`cv_infra/contract/adapter_schema.py`)과 1:1 이다.
빌트인 stub 요청은 `interface` 블록을 선언하지 않아 그 기본값으로 해석되므로 **설정 0 으로
맞물린다**. 한쪽만 바뀌면 배리어에서 **loud** 하게 죽는다(조용한 통과 없음).

### `use_sim_time` 이 왜 진짜로 중요한가
`false` 면 어댑터가 readiness 를 **거부**한다(`REQ-EXEC-005`: 강제하지 않고 *검증*만 한다).
G-19 가 측정한 실패가 정확히 그것이다 — sim-time SUT 는 `/clock` 없이 얼어붙는다.
그래서 이 노드는 `use_sim_time` 을 **파라미터 override 로** 잡는다(나중에 set 하는 방식이
아니다 — 그러면 배리어와 경주한다).

---

## 3. 판정은 어디서 오나 — **오라클은 약화되지 않는다**

stub 은 `/cmd_vel` 을 **한 번도 publish 하지 않는다**. 플래너도, 맵도 없다. 그런데도
`reached_goal` 이 통과하는 이유는 **self-test 시나리오가 로봇을 목표 지점에 스폰**하기
때문이다(`cv_infra/orchestrator/selftest.py`: `initial_pose == goal`). 즉 *"목표에 도달해
있다"* 가 프레임 0 부터 **구조적으로 참**이고, 임계값을 손보지 않는다.

핵심은 이것이다: **판정은 stub 의 대답에서 오지 않는다.** `reached_goal` 오라클은 Isaac
**GT 포즈**(`cv_infra/oracles/reached_goal.py` — SUT 의 `/odom` 은 쓰지 않는다)로 다시
계산한다. stub 이 액션에 `SUCCEEDED` 를 답하는 것은 **미션을 끝내는 신호일 뿐 verdict 가
아니다**(`fold_verdict` 는 오라클 결과만 접는다). 그러므로:

* stub 이 거짓말을 해도 **통과가 아니라 실패**한다. 로봇이 목표에 없으면 GT 가 그렇게 말한다.
* **일반 잡의 오라클에는 아무 변경이 없다.** self-test 는 `REQ-INTAKE-007`("판정 기준도 입력")
  에 따라 **자기 입력**으로만 자명해진다 — 코드가 아니라 시나리오로.
* 이 stub 이 재는 것은 **`REQ-SELFTEST-003` 이 재라고 한 것**(Isaac 기동 · 러너 실행 · 결과
  반환 · 컨테이너 경계 DDS)이지 **SUT 의 품질이 아니다**.

---

## 4. 핀 (CLAUDE §2-7)

| 축 | 값 | 출처 |
|---|---|---|
| 베이스 | `ros@sha256:31daab66…` (= `ros:jazzy`, ros-base-noble) | 저장소가 **이미** 고정해 둔 DoD-P1-05 DDS 핸드셰이크 피어 (`scripts/workstation_setup/common.sh`: `CV_ROS_JAZZY_IMAGE`/`CV_ROS_JAZZY_DIGEST`). 새 ROS 베이스를 발명하지 않았다 |
| apt | `ros-jazzy-nav2-msgs=1.3.12-…` · `ros-jazzy-geographic-msgs=1.0.6-…` | 2026-08-14 실측(`apt-cache policy` + `--dry-run`). **이 레이어의 apt 입력은 100 % 핀** — direct 1 + transitive 1 이 전부라서 가능했다(러너는 transitive 247 개라 부분 핀) |
| 소스 | `org.opencontainers.image.revision` 라벨 | 빌드 인자 `CV_SOURCE_REVISION` (비면 **빌드 실패** — G-66) |

베이스가 이미 주는 것(실측 2026-08-14): `rclpy` · `std_srvs` · `rcl_interfaces` ·
`action_msgs` · `geometry_msgs` · `rosgraph_msgs`. **추가 설치는 nav2_msgs 하나**다.

**핀이 사라지면** 빌드가 loud 하게 죽는다(packages.ros.org 는 rolling 이라 대체된 버전을
내린다). 재핀 절차는 실패 메시지에 그대로 있고, 레이어가 자기 dpkg 매니페스트를 찍는다.

---

## 5. 빌드 & 배선

```bash
# 빌드 (컨텍스트 = 이 디렉토리. 저장소 루트에서 아무것도 안 가져온다)
CV_SOURCE_REVISION="$(git rev-parse HEAD)" \
  docker build -f docker/selftest_stub/Dockerfile \
    --build-arg CV_SOURCE_REVISION="$CV_SOURCE_REVISION" \
    -t cv-infra-selftest-stub:<tag> docker/selftest_stub

# 확인
docker image inspect cv-infra-selftest-stub:<tag> \
  --format 'size={{.Size}} rev={{index .Config.Labels "org.opencontainers.image.revision"}}'
```

self-test 에 물리기 — 핸들은 **절대 추측되지 않는다**(FU-10, `CV_RUNNER_IMAGE` 와 동일 정책):

```bash
CV_SELFTEST_SUT_IMAGE=cv-infra-selftest-stub:<tag>
```

이 env 를 **어디에** 두느냐가 함정이다. `cv-infra selftest` 는 봉투를 만드는
**클라이언트 프로세스**에서 이 값을 읽는다(오케스트레이터 서비스가 아니다). 배포 매뉴얼이
권하는 컨테이너 CLI 로 돌린다면 그 일회용 컨테이너에 넣어야 한다:

```bash
docker compose -f docker/compose.yaml run --rm --no-deps \
  -e CV_SELFTEST_SUT_IMAGE=cv-infra-selftest-stub:<tag> \
  orchestrator cv-infra selftest --api http://orchestrator:8000
```

> `compose.yaml` 의 서비스 `environment:` 에 상주 노브로 넣는 방법도 있지만, 그건 **다른
> 팀이 방금 머지한 파일**이라 이번 사이클엔 건드리지 않았다(`docker/.env.example` 도
> "여기 있는 노브는 전부 compose 가 읽는다"가 계약이라 같은 이유로 비워 뒀다).
> 상주화는 PM 판단 사항이다.

미설정이면 self-test 는 **추측하지 않고 거부**한다(`SelfTestNotConfigured` → exit 3).
소비자 이미지로의 폴백은 `NFR-SELFTEST-001` 위반이라 코드가 금지한다.

---

## 6. 노브 — **전부 빌드 타임이다**

M3 는 SUT 를 **블랙박스**로 띄운다(`REQ-EXEC-005`): 커맨드 override 없음, env 는
`ROS_DOMAIN_ID` **하나뿐**. 그래서 런타임에 stub 에 뭔가를 넘길 채널이 **없다**.

| 노브 | 기본 | 언제 움직이나 |
|---|---|---|
| `CV_STUB_GOAL_HOLD_S` (build arg) | `0` | 목표 수락 후 성공까지 붙잡는 **벽시계** 초. **0 = 즉시 성공**이 정직한 기본값이다(로봇은 이미 목표에 있으므로 기다릴 것이 없고, 실측하지 않은 지속 시간을 지어내지 않는다 — G-64). **올려야 하는 신호**: 라이브 self-test 의 `result.json` 이 `reason=no_telemetry` 이거나 GT 샘플 수가 의심스럽게 적을 때. GT 샘플은 **미션 중**에만 쌓이고, 미션은 이 목표가 살아있는 동안만 지속된다. 벽시계라서 "sim 몇 초"가 아니라 "스텝 몇 개"를 사는 것임에 주의 |
| `CV_STUB_IS_ACTIVE_SERVICE` / `CV_STUB_GOAL_ACTION` / `CV_STUB_USE_SIM_TIME` (env) | M1 스키마 기본값 | 다르게 배선된 시나리오를 겨냥할 때. 이미지를 다시 굽거나(`ENV`) 수동 실행에서만 의미가 있다 |

```bash
# 예: 라이브 실측 후 hold 를 올릴 때 (값은 그때 측정된 값이어야 한다)
docker build -f docker/selftest_stub/Dockerfile --build-arg CV_STUB_GOAL_HOLD_S=<measured> ...
```

---

## 7. GPU 없이 검증하기 — `scripts/selftest_stub/probe_readiness.sh`

DoD-P1-05 가 이미 보여준 패턴이다(ros:jazzy 피어 + 브리지 네트워크로 컨테이너 경계 DDS 를
측정). SUT 표면은 순수 ROS 2 라 **GPU 가 필요 없다**.

```bash
CV_SELFTEST_STUB_IMAGE=cv-infra-selftest-stub:<tag> bash scripts/selftest_stub/probe_readiness.sh
#  0 = A/B/C/D/E 전부 통과   1 = 실패(증적 경로 출력)   2 = 사용법/전제 오류
```

프로브는 stub 을 **프로덕션과 같은 모양**으로 띄운다(커맨드 override 0, env 는
`ROS_DOMAIN_ID` 하나, GPU 없음, docker.sock 없음). 피어는 **같은 이미지**를 쓴다 —
nav2_msgs 타입서포트가 이미 들어 있어 세 번째 산출물이 필요 없다.

**프로브가 증명하지 않는 것**: readiness 1단계(`/clock` 흐름 = 러너/Isaac 몫)와 라운드트립
자체. **`cv-infra selftest` 가 GPU 호스트에서 초록이라는 주장은 별개이고 나중이다.**

---

## 8. 관련 문서

* 배포 4단계 흐름(C-2): [`docs/deploy/README.md`](../../docs/deploy/README.md)
* 컨테이너 경계 DDS 의 원조 증명: [`scripts/isaac_smoke/run_dds_handshake.sh`](../../scripts/isaac_smoke/run_dds_handshake.sh)
* self-test 입력·봉투(무엇이 제출되나): `cv_infra/orchestrator/selftest.py`
* 모듈 스펙: `implementation-plan/modules/M7-deployment-self-test.md` §3.5
