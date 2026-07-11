# 버전 호환 매트릭스 — 3축 독립 (R17, M8-D5)

cv-infra의 버전은 **서로 독립인 3축**으로 관리된다(M8 §3.6 / 리스크 R17 — 단일
버전 하드코딩 금지). 이 문서의 버전 값은 코드 정본에서 복사한 **사본**이며,
`tests/test_version_matrix_doc.py`가 사본↔정본 일치를 기계적으로 단정한다
(G-25: 한쪽만 고치면 CI가 실패한다). 이 파일에는 **현존 값만** 기입한다 —
미래 버전·미발행 태그를 발명하지 않는다(G-24).

## 3축 정의 (독립 — 함께 움직이지 않는다)

| 축 | 현행 값 | 정본(source of truth) |
|---|---|---|
| ① Action 태그 (`@vN`) | 미발행 (Phase 5 예정) | `cv-infra-workspace` 릴리즈 태그. 태그 대상 표면(reusable workflow `.github/workflows/verify.yml` · composite `actions/verify`)도 아직 없다 — 표면이 랜딩되면 바인딩 테스트가 이 행의 갱신을 강제한다 |
| ② CLI/패키지 버전 | `0.0.0` | `cv_infra/__init__.py`의 `__version__` — `pyproject.toml` `[tool.hatch.version]`이 여기에 위임하는 단일 정본 |
| ③ 계약 `apiVersion` | `cv-infra/v1` | `cv_infra/contract/apiversion.py`의 `API_VERSION`; 수용/유예 테이블 = `cv_infra/contract/version.py`의 `SUPPORTED` / `DEPRECATED` |

**왜 3축이 독립인가(R17)**: 소비자는 Action을 태그로 핀하고(`uses: …@vN`),
플랫폼은 CLI/패키지 버전으로 릴리즈되며, 각 시나리오 문서는 자신이 작성된
계약 `apiVersion`을 선언한다. Action 태그 이동이 계약 파괴를 의미하지 않고,
새 `apiVersion` 도입이 소비자 워크플로 핀 변경을 강제하지 않는다. 세 축을
한 숫자로 묶으면 어느 한 축의 무해한 이동이 나머지 축의 가짜 비호환을
만든다.

## 현행 호환 표

| Action 태그 | CLI/패키지 | 수용 `apiVersion` | deprecated(warn) | 그 외 apiVersion |
|---|---|---|---|---|
| 미발행 (Phase 5 예정) | `0.0.0` | `cv-infra/v1` | 없음 | reject — exit 2 + 친절 에러 + 마이그레이션 포인터 |

- 수용/warn/reject 의미론 = 3-state resolver(`cv_infra/contract/version.py`,
  NFR-INTAKE-002): 지원·현행 → accept / 지원·deprecated → accept + WARNING
  (sunset 날짜 + 마이그레이션 링크) / 미지·부재 → reject(exit 2, 친절 에러).
- deprecation 정책(NFR-INTAKE-002): 파괴적 변경은 MAJOR 범프에서만 · 최소
  N-1 minor 지원 · sunset 창 ≥ 2 릴리즈.

## 행 추가 정책

어느 축이든 움직이면 이 문서를 갱신한다(바인딩 테스트가 값 불일치를 잡는다):

1. **Action 태그 발행/이동** — Phase 5 최초 `@v1` 발행 시 ① 축 값을 태그로
   교체하고 호환 표에 행을 추가한다.
2. **패키지 릴리즈** — `__version__` 범프 시 ② 축 값과 호환 표를 갱신한다.
3. **`apiVersion` 이동** — 새 버전이 `SUPPORTED`에 들어오거나 기존 버전이
   `DEPRECATED`로 이동하면(그 시점에 sunset·마이그레이션 링크가 코드에
   실존한다) 해당 값을 그대로 옮겨 적는다.

값은 항상 코드 정본에서 복사한다 — 이 문서에서 먼저 발명하지 않는다.
