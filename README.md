# CV-Infra

**로봇 SW가 CI/CD에서 업그레이드될 때마다 Isaac Sim 시뮬레이션으로 자동 검증하는, Docker 배포형
지속 검증 인프라.** 이 저장소(`cv-infra-workspace`)는 그 **플랫폼 엔진 + 운영 산출물**이다.

로봇 SW 프로젝트가 PR을 올리면, 그쪽 CI가 로봇 이미지(SUT)를 빌드해 cv-infra에 **이미지 ref +
시나리오 선언**을 넘긴다. 플랫폼은 GPU 워크스테이션에서 Isaac Sim 시뮬레이션을 돌려 "목표에
도달했나 · 부딪혔나" 같은 **수용 기준(oracle)** 을 판정하고, 결과를 그 PR의 **Check · 코멘트 ·
아티팩트**로 돌려준다. 소비자 저장소가 유지하는 통합 표면은 워크플로 잡 하나(`uses: …@v1`)뿐이다.

- **블랙박스 SUT** — 로봇 SW는 컨테이너 이미지 **ref**로만 들어온다. 플랫폼은 그것을 빌드하지도,
  내부 설정을 고치지도 않는다(GPU 호스트는 PR 소스를 체크아웃하지 않는다).
- **결정적 랜덤화** — 시나리오 한 장에 분포(`{uniform: […]}` · `{choice: […]}` · `{randint: […]}`)를
  선언하면 `seed`에서 표본 n개가 결정적으로 파생된다. **같은 seed + 같은 문서 = 같은 표본.**
- **판정이 PR로 온다** — pass/fail · 표본 분포 · **회귀(baseline 대비)** · rosbag(MCAP)/mp4가
  Check Run · sticky 코멘트 · step summary · 아티팩트로 돌아온다.
- **한 계약, 두 진입** — `cv-infra` CLI가 **단일 표면**이고 GitHub Action은 그 얇은 래퍼다. GitHub
  없이 로컬에서 같은 검증을 같은 exit code로 돌릴 수 있다.

**고정 기반**(재사용 — 재구현하지 않는다): Isaac Sim **5.1.0**(digest 핀) · ROS 2 **Jazzy** ·
Python **3.11** · NVIDIA 드라이버 **R580 브랜치** · GitHub Actions · SQLite.

## 아키텍처 한 컷

```
소비자 PR ─(SUT 이미지 ref + 시나리오 YAML)─▶ CLI / Action ─▶ 오케스트레이터 ─▶ 러너 + Isaac Sim
                                                (M8)          (M3: 큐·동시성 k)     (M2 + SUT 컨테이너)
   PR Check · sticky 코멘트 · 아티팩트 ◀────────  리포트·회귀 판정  ◀───── result.json · MCAP · mp4
                                                (M4, baseline = 내부 SQLite)
```

| 모듈 | 이 저장소의 소재 | 하는 일 |
|---|---|---|
| **M1** 계약 | `cv_infra/contract/` | 시나리오/결과 스키마(pydantic v2) · `apiVersion` 3-상태 해석 · 6단계 admit 게이트 · 친절 에러 |
| **M2** 러너 | `cv_infra/runner/`, `cv_infra/oracles/`, `docker/runner/` | Isaac Sim 구동 · ROS 2 어댑터 · 텔레메트리/녹화 · oracle 평가 → `result.json` |
| **M3** 오케스트레이터 | `cv_infra/orchestrator/`, `docker/orchestrator/` | REST(`/envelopes`) · fan-out · 자원인지 스케줄 · 컨테이너 감독 · SQLite 저장 |
| **M4** 리포팅·회귀 | `cv_infra/report/` | 요청 롤업 · `request_identity_key` · baseline 대비 회귀 판정 · GitHub 게시 페이로드 4종 |
| **M5** 배포·패키징 | `docker/`, `scripts/`, `profiles/`, `docs/deploy/` | 이미지 적층 · Compose 배포 · GPU 프로파일 · EULA 동의 게이트 · 캐시 |
| **M6** 운영 모니터링 | `cv_infra/orchestrator/monitor.py` | 큐·자원·헬스 운영뷰(`/monitor`, `cv-infra monitor`) — 도메인 결과뷰와 분리 |
| **M7** 자가검증 | `cv_infra/orchestrator/selftest.py`, `docker/selftest_stub/` | 빌트인 stub 라운드트립(`cv-infra selftest`) — 외부 SUT 0 의존 |
| **M8** CI/CD·DX | `cv_infra/cli/`, `.github/workflows/verify.yml`, `actions/verify/` | CLI 단일 표면 + exit 계약 · 재사용 워크플로 / composite action · 친절 에러 → CI annotation |

## 설치 (요약)

**요구사항** — NVIDIA GPU 호스트 1대 · 드라이버 **R580 브랜치**(하한 `580.65.06`, open kernel
module) · Docker CE + Compose v2 + NVIDIA Container Toolkit · 러너 이미지 **15.5 GB** + 산출물
디스크 여유 · `nvcr.io`/PyPI/Omniverse 자산으로의 egress. **호스트에 CUDA·Isaac Sim을 설치하지
않는다** — 필요한 것은 드라이버와 컨테이너 런타임뿐이다.

```bash
git clone https://github.com/<org>/cv-infra-workspace && cd cv-infra-workspace
bash scripts/workstation_setup/provision.sh            # ① 호스트 선결(호스트 1회, root)
cp docker/.env.example docker/.env && $EDITOR docker/.env   # ②' REQUIRED 6개(경로·동시성) 기입
bash scripts/detect_gpu.sh >> docker/.env              #    측정된 GPU 노브 append(손으로 쓰지 말 것)
bash scripts/consent/accept_eula.sh                    # ② NVIDIA EULA 명시 동의(자동 수락 없음)
CV_EULA_CONSENT=<동의어> scripts/measure/warm_cache.sh "$CV_ISAAC_CACHE_ROOT" warm   # ②'' 캐시 워밍
CV_SOURCE_REVISION="$(git rev-parse HEAD)" docker compose -f docker/compose.yaml up -d --build  # ③
CV_SELFTEST_SUT_IMAGE=<stub 이미지> cv-infra selftest   # ④ 외부 SUT 0 의존 라운드트립
```

각 단계의 전제·검증 명령·실패 대처는 **[`docs/installation.md`](docs/installation.md)**, 운영
심화(평면 동기·GPU 이식·트러블슈팅)는 **[`docs/deploy/README.md`](docs/deploy/README.md)**.

## 사용 (요약)

**① 시나리오를 한 장 쓴다** — 검증 요청의 전부다(씬·목표·SUT 이미지·수용 기준).

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

**② CI에서 돌린다** — 소비자 워크플로가 유지하는 줄은 이게 전부다(잡-레벨 `uses:`, `runs-on`/
`steps` 없음).

```yaml
  verify:
    needs: build-sut                       # SUT 이미지를 빌드해 ref를 내주는 소비자 잡
    permissions: { checks: write, pull-requests: write, contents: read }
    uses: <org>/cv-infra-workspace/.github/workflows/verify.yml@v1
    with:
      sut_image: ${{ needs.build-sut.outputs.image_ref }}   # ghcr.io/…@sha256:… (ref-only)
      scenarios: scenarios/*.yaml
      runner_label: cv-infra-gpu
```

> `@v1` 은 v1.x 최신을 따라가는 major 별칭이다(현행 = `v1.2.1` — **이 값은 사본이고 정본은 태그
> 자신 + [`docs/releases.md`](docs/releases.md)**). 자기 잡을 직접 쓰고 싶으면 composite action
> [`actions/verify`](actions/verify/action.yml)를 한 스텝으로 넣는 경로도 있다.

**③ 또는 CLI로 돌린다** — GitHub 없이 같은 검증, 같은 exit code.

```bash
cv-infra run scenarios/my_scenario.yaml --runner-image <runner-image-ref>   # 단건, 오케스트레이터 없이
cv-infra submit scenarios/*.yaml --wait                                     # 제출 + 판정까지 대기
```

**exit 계약** — CLI 프로세스 종료 코드가 곧 판정이고, PR Check의 conclusion은 여기서 유도된다.

| exit | 뜻 | Check conclusion |
|---|---|---|
| `0` | **PASS** — 모든 검증 통과 | `success` |
| `1` | **FAIL** — 검증 실패 또는 baseline 대비 회귀 (SUT 판정) | `failure` |
| `2` | **CONTRACT** — 시나리오·계약 오류(잘못된 YAML, 미지 `apiVersion`) | `failure` + YAML 인라인 annotation |
| `3` | **INFRA** — 인프라 문제(오케스트레이터 다운·EULA 미동의·러너 크래시). **SUT 판정이 아니다** | `neutral` |

시나리오 문법 전체(랜덤화·장애물·커스텀 oracle)·CLI 7개 명령·결과 읽는 법은
**[`docs/user-guide.md`](docs/user-guide.md)**.

## 문서

- **[`docs/README.md`](docs/README.md)** — 문서 지도(사용자 문서 ↔ 운영·내부 문서).
- 사용자: [`docs/installation.md`](docs/installation.md) · [`docs/user-guide.md`](docs/user-guide.md) ·
  [`docs/releases.md`](docs/releases.md)(릴리즈 대장 — 어떤 태그를 쓰고 어떤 태그를 피할지의 정본) ·
  [`docs/version-compatibility.md`](docs/version-compatibility.md)(Action 태그 / CLI·패키지 /
  계약 `apiVersion` **3축 독립** 매트릭스).
- 운영·내부: [`docs/deploy/`](docs/deploy/)(배포 매뉴얼 · GPU 프로파일 · 평면 동기) ·
  [`docs/evidence-anchors.md`](docs/evidence-anchors.md) · 코드와 동거하는 README —
  [워크스테이션 프로비저닝](scripts/workstation_setup/README.md) ·
  [측정 하네스](scripts/measure/README.md) · [Isaac 스모크](scripts/isaac_smoke/README.md) ·
  [self-test stub](docker/selftest_stub/README.md).

## 라이선스·EULA 고지

Isaac Sim은 **NVIDIA 소유**이며 이 배포는 그것을 번들·동반배포만 한다. 설치 시 운영자가
`scripts/consent/accept_eula.sh`로 **NVIDIA EULA에 명시적으로 동의**해야 하고, **이 배포는 어떤
경로로도 자동 수락하지 않는다** — 동의 기록이 없으면 제어 평면 기동과 시뮬레이션 실행이 거부된다
(`scripts/consent/check_consent.sh` exit 3).

## 저장소

- **`cv-infra-workspace`**(이 저장소, public) — 플랫폼 엔진·배포 산출물·재사용 워크플로.
- **`cv-infra-user`**(public) — 소비자 예제 저장소. 플랫폼을 **계약 + 릴리즈 태그로만** 소비한다
  (상대경로 소스 참조 없음).
- 설계·요구사항·구현 계획은 private 메타 저장소 `cv-infra-project`에 있다.
