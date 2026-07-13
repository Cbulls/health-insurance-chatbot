# harag — 구현 종합 보고서

한글 행정문서 RAG 챗봇. 요구사항 분석부터 핵심 로직 구현·CI까지의 전체 산출물을
코드·테스트·기능 단위로 집계한 문서.

- **소스 코드:** 32개 모듈, 약 2,916줄(테스트 제외)
- **테스트:** 계약 79개 + 동작 87개 + 통합(Qdrant) — 전부 통과
- **상태:** 두 경로(읽기·쓰기) 핵심 로직 완성·검증. 실제 어댑터(HWP 디코더·모델·IdP 백엔드)만 끼우면 동작.

---

## 1. 테스트 전략 — 두 층위

테스트를 두 층위로 분리해 섞지 않는다.

| 층위 | 성격 | 개수 | 실행 |
|---|---|---|---|
| **계약 테스트** | 설계 불변식. 구현과 무관하게 항상 참이어야 함 | 79 | `scripts/run_contract_tests.py` |
| **동작 테스트** | 구현 동작. TDD로 빨강→초록 | 87 | `pytest tests/behavior` |
| **통합 테스트** | 외부 의존(Qdrant 인메모리) | Qdrant | `tests/integration` |

계약 테스트는 "권한 없는 청크는 검색 불가" 같은 규칙이 코드로 강제되는지 검증한다(위반 객체는 생성 자체가 불가능). 동작 테스트는 각 컴포넌트가 명세대로 동작하는지 검증한다.

---

## 2. 계약 테스트 (79개) — 설계 불변식

| 파일 | 개수 | 검증 내용 |
|---|---|---|
| `test_schemas.py` | 15 | IR·청크 스키마 불변식(표 평탄화 금지, 필수 필드, fail-closed) |
| `test_boundaries.py` | 17 | 컴포넌트 경계 계약(AuthContext 필수, 타입 안전) |
| `test_acl.py` | 7 | ACL 일관성(dense·sparse 양쪽 동일 필터, 누수 차단) |
| `test_capacity.py` | 10 | 용량 계산기(역산 로직) |
| `test_eval.py` | 8 | 평가 하니스(유형별 분리 측정) |
| `test_measurement.py` | 9 | 측정 방법론(신뢰구간 게이트, 워밍업 제외, 백분위) |
| `test_poc.py` | 7 | PoC 판정(층화·최악 층·abstention 거부권) |
| `test_api_gateway.py` | 6 | 게이트웨이 신뢰 경계(인증 강제, fail-closed, 누설 방지) |
| `test_versioning.py` | — | 버전 전환 동시성(빈 창 0% — 스레드 스트레스) |

---

## 3. 동작 테스트 (87개) — 컴포넌트별

### 쓰기 경로 (인덱싱)

| 파일 | 개수 | 핵심 검증 |
|---|---|---|
| `test_parser.py` | 6 | 표 셀 좌표 보존, 노이즈 분리, struct_path, 전순서, 실패 처리 |
| `test_chunker.py` | 6 | 조항 단위 유지, 표 독립 청크, 노이즈 제외, 계보 보존 |
| `test_embedder.py` | 5 | dense+sparse 생성, 형태소 어간 매칭 |
| `test_worker.py` | 5 | 파이프라인 연결, 실패 격리, 버전 전환 호출 |
| `test_pii.py` | 9 | 한국 PII 마스킹, 오탐 방지, 인덱싱 통합 |
| `test_incremental.py` | 5 | 변경분만 재임베딩, 정합성 유지 |

### 읽기 경로 (질의)

| 파일 | 개수 | 핵심 검증 |
|---|---|---|
| `test_behavior.py` | 9 | 검색기(하이브리드·ACL) + 생성기(abstention·인용 검증) |
| `test_reranker.py` | 5 | top-N 컷, 최저점수 필터, 재정렬, degradation |
| `test_rewriter.py` | 6 | 멀티턴 재작성, 폴백, 대화 격리, 권한 안전 |
| `test_pipeline.py` | 4 | 검색+생성 조립, abstention, 권한 전달 |
| `test_llm_client.py` | 6 | 백오프, 비용 상한, 타임아웃, 위조 인용 차단 |

### 횡단 관심사

| 파일 | 개수 | 핵심 검증 |
|---|---|---|
| `test_auth_jwt.py` | 8 | JWT 검증, alg=none 방어, 클레임 매핑, fail-closed |
| `test_injection.py` | 7 | 인젝션 패턴 탐지, 구조적 분리, 다층 방어 |
| `test_observability.py` | 6 | 단계별 trace, 메트릭, 감사 로그 |

---

## 4. 구현된 컴포넌트 (32개 모듈)

### 스키마·계약 (불변식)
- `schemas/ir.py` (177줄) — 구조 보존 중간표현(표 셀 좌표, 노이즈 플래그)
- `schemas/chunk.py` (83줄) — 청크 메타데이터(계보·ACL·버전·해시)
- `contracts/boundaries.py` (164줄) — 컴포넌트 경계(Chunk·ScoredChunk·AuthContext)
- `contracts/acl.py` (81줄) — ACL 일관성 계약

### 쓰기 경로
- `parsing/parser.py` (138줄) — 구조화 파서(디코더 어댑터 분리)
- `chunking/chunker.py` (114줄) — 구조 기반 청킹
- `embedding/embedder.py` (56줄) — 하이브리드 임베더(dense+sparse)
- `indexing/versioning.py` (84줄) — 빈 창 없는 버전 전환
- `indexing/worker.py` (154줄) — 인덱싱 오케스트레이터(PII·증분 포함)

### 읽기 경로
- `retrieval/hybrid.py` (77줄) — 하이브리드 검색(ACL pre-filter)
- `retrieval/reranker.py` (55줄) — cross-encoder 재순위
- `retrieval/rewriter.py` (71줄) — 멀티턴 질의 재작성
- `generation/generator.py` (64줄) — 생성기(안전 가드: abstention·인용 검증)
- `generation/llm_client.py` (105줄) — 외부 LLM 클라이언트(방어 로직)

### API 게이트웨이
- `api/main.py` (71줄) — 앱 조립, lifespan, IdP 자동 주입
- `api/auth.py` (91줄) — 신뢰 경계(require_auth)
- `api/auth_jwt.py` (71줄) — JWT 인증 백엔드
- `api/middleware.py` (59줄) — trace_id, 안전 에러 변환
- `api/routes_query.py` (85줄) — 질의 라우트(스트리밍 포함)
- `api/routes_ingest.py` (70줄) — 수집 라우트(비동기 202)
- `api/pipeline.py` (72줄) — 읽기 경로 오케스트레이션
- `api/deps.py`, `api/schemas.py` — 의존성 주입·API 스키마

### 평가·관측·보안·설정
- `eval/harness.py`·`capacity.py`·`quality.py`·`perf.py`·`poc.py` — 평가·측정 하니스
- `observability/tracing.py` (75줄) — trace·메트릭
- `security/pii.py` (63줄) — PII 마스킹
- `security/injection.py` (79줄) — 프롬프트 인젝션 방어
- `config/settings.py` (68줄) — 설정 외부화

---

## 5. 기능 목록 — 요구사항 대비

### 구현·검증 완료

| 기능 | 요구사항 | 핵심 |
|---|---|---|
| 구조 보존 파싱 | FR-2 | 표 셀 좌표, 조항 계층, 노이즈 분리 |
| 구조 기반 청킹 | FR-3 | 조항 단위, 표 독립, 계보 보존 |
| 하이브리드 임베딩 | FR-5 | dense+sparse, 형태소 |
| 하이브리드 검색 | FR-6 | RRF, ACL pre-filter |
| 리랭킹 | FR-7 | cross-encoder, degradation |
| 멀티턴 재작성 | FR-6 | 지시어 해소, 권한 안전 |
| 근거 기반 생성 | FR-8 | abstention, 인용 검증 |
| 외부 LLM 연동 | v4 | 백오프·비용상한·타임아웃 |
| 버전 전환 | NFR-8 | 빈 창 0% |
| 증분 인덱싱 | — | content_hash 비교 |
| 신뢰 경계 인증 | NFR-5 | JWT, fail-closed |
| PII 마스킹 | SEC-03 | 인덱싱 시점, 오탐 방지 |
| 인젝션 방어 | SEC-02 | 다층(탐지+분리+인용검증) |
| 단계별 관측 | NFR-6 | trace, 메트릭 |
| 품질·성능 측정 | NFR-3 | 신뢰구간 게이트, 워밍업 제외 |
| CI 파이프라인 | — | 차단 게이트 + 야간 품질 게이트 |

### 미구현 — 환경 밖(§A)

| 항목 | 필요한 것 | 이유 |
|---|---|---|
| HWP 디코더 + PoC | 실제 행정문서 표본 + 라이브러리 | LLM 무관, 표본·디코더 부재 |
| 실제 임베딩/리랭커 모델 | GPU 환경 | self-host 추론 |
| 실제 LLM 연결 | API 키 + 외부 네트워크 | 샌드박스 네트워크 차단 |
| 배포·부하 실측 | 클라우드 리소스 | 성능 SLO 동결 |

이 항목들은 어댑터 인터페이스가 준비돼 있어, 실제 구현만 끼우면 동작한다.

---

## 6. 통합 검증으로 확인된 실제 동작

단위 테스트를 넘어, 컴포넌트를 이어 붙여 end-to-end로 확인한 동작:

- **쓰기 경로:** 파싱→청킹→임베딩 연결 — 노이즈 제외, 표 독립 청크, 계보 보존
- **읽기 경로:** HTTP 요청→인증→검색(ACL)→생성→출처 포함 응답
- **리랭킹 효과:** 검색이 무관 청크를 1등으로 줘도 리랭커가 정답을 끌어올림
- **멀티턴:** "그건 언제 정산해?" → "출장비 정산 기한은?"으로 재작성돼 검색
- **증분 인덱싱:** 3조항 중 1개 개정 시 그 청크만 재임베딩, 활성 버전은 전체(정합성)
- **PII:** 주민번호·전화번호가 적재 청크에서 마스킹됨
- **인증:** 유효 토큰 통과, 잘못된 서명·만료·alg=none 거부(401)
- **다층 방어:** 외부 LLM이 위조 인용을 반환해도 생성기 인용 검증이 차단

통합 과정에서 발견·수정한 실제 버그: **인증 실패가 401이 아닌 500으로 누설되던 문제**
(require_auth가 IdP의 AuthError를 처리 못 함) — 단위 테스트로는 안 잡히고
통합에서만 드러나는 종류. 401로 변환하도록 수정.

## 7. 정직한 한계 (코드로 못 막음)

설계가 의도적으로 경계 밖에 둔 것들. "다 막는다"고 포장하지 않는다.

- **추론 누수:** 권한 내 청크 조합으로 권한 외 사실 추론 — 청크 단위 ACL로 불가. 제품 정책.
- **PII 중 이름·주소:** 정규식은 형식 명확한 PII(주민번호·전화)만. 문맥 의존 PII는 NER 필요(범위 밖).
- **새로운 인젝션 우회:** 정규식 탐지는 알려진 패턴만. 그래서 인용 검증을 최종 방어로.
- **LLM-as-judge 신뢰도:** 채점자 자체 편향 — 사람 표본 교차검증(κ) 필요.
- **골드셋 대표성:** 측정 하니스는 유형 분리를 강제하나, 충분한 질의는 라벨링 투자에 의존.
- **실측 SLO:** 모든 성능·품질 임계는 플레이스홀더. 부하 테스트·골드셋 평가로 동결 필요.

---

## 8. 일관된 설계 원칙

1. **막을 수 있는 건 코드로 증명한다.** 불변식은 주석이 아니라 Pydantic validator — 위반 객체 생성 불가. (계약 79개)
2. **막을 수 없는 건 경계 밖으로 명시한다.** §6의 한계를 숨기지 않는다.
3. **외부 의존성은 어댑터 뒤로.** 디코더·모델·LLM·transport·IdP를 주입식 분리 → 표본·GPU·네트워크 없이 로직 검증. 실제 어댑터만 끼우면 동작.
4. **최적화가 정합성을 깨지 않는다.** 증분 인덱싱은 임베딩만 절약, 버전 전환은 여전히 전체 집합 원자적.
5. **다층 방어.** 인젝션·환각은 단일 방어가 아니라 탐지+구조분리+인용검증으로 겹겹이.

---

## 9. 실행

```bash
make install     # 의존성
make ci          # 차단 게이트 전체(계약 79 + 동작 87 + 통합)
make up          # 로컬 스택(Qdrant/Postgres/MinIO)
```

상세 설계는 `docs/MASTER_DESIGN.md`(전체 조망) 및 요구사항·기술·API·테스트 상세 문서 참조.
