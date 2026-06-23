# cv-infra-workspace

CV-Infra **플랫폼 엔진 + 운영 산출물** (public). CI/CD에서 로봇 SW가 업그레이드될 때마다 **Isaac Sim 시뮬레이션**으로 시나리오·안전·미션을 자동 검증하고 결과(pass/fail·회귀)를 CI/CD로 돌려주는 범용·Docker 배포형 지속 검증 인프라의 *플랫폼* 저장소.

> ⚠️ **스캐폴드 대기 (구현 계획 Phase 0)** — 현재는 3-저장소 토폴로지만 수립된 상태입니다. 코어 구현(`cv_infra/*` · `docker/*` · `scripts/*` · `.github/*` · `actions/*`)은 구현 계획 Phase 0부터 채워집니다.

## 개요
- **고정 기반(재사용, do-not-reinvent)**: Isaac Sim **5.1.0** + ROS 2 **Jazzy**. 엔진은 NVIDIA 소유 — 번들·동반배포만.
- **8 모듈**: M1 계약 · M2 Simulation Runner · M3 Orchestrator · M4 Reporting · M5 Deployment · M6 Monitoring · M7 Self-Test · M8 CI/CD·DX.
- **담당 팀**: Contract · Simulation Runner · Orchestration · Reporting · Infra/Deployment · DX·CI/CD · QA (CV-User 제외 전 팀).
- 운영자는 이 저장소의 **릴리즈를 받아** NVIDIA GPU 호스트에 설치(가축 모델).

## 설계·계획 문서
설계·요구사항·구현 계획은 상위 **private 메타 저장소 `cv-infra-project`** 의 `implementation-plan/`(특히 `07-repository-and-environments.md` 저장소 토폴로지, `04-team-and-responsibilities.md` R&R)에 있습니다.
