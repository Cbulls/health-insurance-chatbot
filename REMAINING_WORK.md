# 남은 구현 · 사용자 구축 가이드

현재 **PDF RAG MVP는 동작한다** (업로드 → 임베딩 → Qdrant → LLM 답변/스트리밍).
이 문서는 “누가 무엇을 해야 하는지”를 분리한다.

- **A**: 사용자가 직접 구축·유지 (계정·인프라·도메인)
- **B**: 코드로 더 구현할 항목 (우선순위)
- **C**: 한 줄 요약 · 다음 스프린트 후보

상세 엔터프라이즈 로드맵은
[앞으로 해야 할 것들/PRODUCTION_ROADMAP.md](앞으로%20해야%20할%20것들/PRODUCTION_ROADMAP.md)를 본다.

---

## A. 사용자가 직접 구축·유지해야 하는 것

코드가 대신 해줄 수 없고, 계정·인프라·도메인 작업이 필요한 항목.

### 필수 (지금 MVP를 계속 쓰려면)

| 항목 | 내용 |
|------|------|
| LLM/임베딩 API 키 | Gemini면 [Google AI Studio](https://aistudio.google.com/). 무료 쿼터(~10 RPM) 초과 시 `llm_unavailable`. 키는 `.env`에만, git에 올리지 말 것 |
| Qdrant | Cloud 무료 클러스터(1GiB RAM / 4GiB Disk) URL·API 키, 또는 로컬 `docker compose`의 qdrant. **한도 초과는 과금이 아니라 장애** — `GET /health`의 `capacity`와 대시보드 모니터링 |
| `.env` 유지 | `LLM_*` / `EMBEDDING_*` / `QDRANT_*` / `RATE_LIMIT_QPM`. 임베딩 차원(`EMBEDDING_DIM`) 변경 시 **컬렉션 삭제 후 재인덱싱** |
| 실행 환경 | `pip install -e .` + `uvicorn harag.api.main:app --app-dir src` 또는 `docker compose up --build`. Python 3.11+ |
| 실제 PDF | 텍스트 추출 가능한 PDF만. **스캔(이미지) PDF는 실패** — OCR은 미구현 |

### 권장 (데모 넘어 팀/기관 사용)

| 항목 | 내용 |
|------|------|
| 시크릿 관리 | 키가 채팅·레포에 노출됐다면 **재발급**. 운영은 Secret Manager / GitHub Secrets / K8s Secret |
| 문서 등록부(선택) | 기본은 SQLite. Postgres: `docker compose --profile postgres up -d postgres`, `.env`에 `DATABASE_URL=postgresql+psycopg://harag:harag@localhost:5432/harag`, `pip install -e '.[postgres]'` |
| 도메인 골드셋 | 건강보험 질의–정답–근거 페이지 라벨링. 없으면 “정확한지”를 영영 모름 |
| 법무/약관 | 행정문서를 외부 LLM API에 보내도 되는지, 학습 비사용 약관 확인 |
| 모니터링 | Qdrant 디스크 80% 알림, LLM 프로바이더 쿼터 대시보드 |
| HWPX 정책 | 기관 문서가 HWP면 **가능하면 HWPX 변환 요청** — 파싱 난이도가 급감 (로드맵 최우선 SPOF) |

### 인프라 (다중 사용자·상시 서비스로 갈 때)

코드 어댑터/라이브러리는 있으나 **실물 프로비저닝은 사용자 측**:

| 인프라 | 지금 MVP | 운영 시 |
|--------|----------|---------|
| 문서 메타 | SQLite `./data/harag.db` 기본 (`DATABASE_URL`로 Postgres) | 운영 PG + 질의 audit 훅 |
| 원본 파일 | 임시 스풀 후 삭제 | S3/MinIO 보존 |
| 인덱싱 | 인프로세스 스레드풀 | 메시지 큐 + indexing worker |
| 인증 | `X-Owner-Id` 또는 `AUTH_JWT_SECRET` | 조직 IdP(OIDC)가 JWT 발급 |
| 임베딩/리랭커 | 외부 API + 어절 리랭커 | (선택) GPU self-host |

문서 목록은 DB에 영속화된다(재시작 후 유지). Postgres는 선택:
`docker compose --profile postgres up -d postgres` + `DATABASE_URL=postgresql+psycopg://...`.

---

## B. 코드로 더 구현해야 할 것

라이브 경로: `api/main` → `retrieval/qdrant_store`(dense) + Gemini/OpenAI 호환 LLM.
아래는 라이브러리만 있거나 아예 없는 항목이다.

### P0 — 품질·운영 체감에 바로 영향

1. ~~**문서 삭제 API**~~ — `DELETE /v1/documents/{id}` + UI 삭제 버튼 (완료)
2. ~~**하이브리드 검색 연결**~~ — dense+sparse RRF (신규 컬렉션). 기존 dense-only 컬렉션은 폴백 (완료)
3. ~~**문서 상태 영속화**~~ — `MetadataStore` + SQLite 기본 / `DATABASE_URL` Postgres (완료)
4. **인용 강화** — LLM `citations=[]`라 검색 청크를 출처로 노출. 구조화 인용(청크 ID)으로 강화.

> 하이브리드를 Qdrant Cloud에서 쓰려면 sparse가 포함된 **새 컬렉션**이 필요합니다
> (예: `QDRANT_COLLECTION=harag_hybrid_768`). 기존 dense-only 컬렉션은 dense로만 검색합니다.

### P1 — 설계상 Phase 2 (엔터프라이즈)

| 순위 | 항목 | 상태 |
|------|------|------|
| 1 | HWP/HWPX 파서 + 표 복원 PoC | `parsing/parser.py` 어댑터만 (**SPOF**) |
| 2 | 큐 + IndexingWorker | 테스트용 라이브러리, 라이브 미배선 |
| 3 | S3 원본 보존 | `object_store` 인터페이스만 |
| 4 | 버전 태깅·원자 전환·GC | `indexing/versioning` 라이브러리, 라이브 미사용 |
| 5 | 조직 ACL (부서/역할) | JWT 검증 배선됨, IdP 클레임·문서 태깅 정책 미완 |
| 6 | GPU CrossEncoder 리랭커 | `HttpCrossEncoder`+`RERANKER_SERVER_URL` 배선됨. URL 없으면 Lexical 폴백. **서버·모델 기동은 사용자 구축** |
| 7 | OCR (스캔 PDF) | 없음 |
| 8 | 골드셋 CI 품질 게이트 | `eval/*` 스켈레톤, 루트 `.github` 없음 |
| 9 | 부하테스트·SLO 동결 | `eval/perf` 자리표시자 |

### P2 — 정리·위생 (선택)

- 루트 한국어 스냅샷 폴더·zip 아카이브 (실행과 무관, 레포 비대화)
- 프론트: JWT 모드일 때 Bearer 입력 UI (지금은 `X-Owner-Id`만)

---

## C. 한 줄 결론

| 구분 | 요약 |
|------|------|
| **지금 바로 쓰기** | `.env` 유지, 텍스트 PDF 업로드, 쿼터·디스크 모니터링. 용량 부족 시 UI에서 문서 삭제 |
| **사용자 구축** | 시크릿, (운영 시) PG·S3·큐·IdP, 골드셋, HWP면 HWPX 정책, 법무 |
| **코드 다음 타석** | ① 인용 강화 ② HWP PoC ③ 큐/워커 ④ 질의 audit |

다음 구현 스프린트 후보:

- **「구조화 인용 + HWP PoC」** — 답변 품질·행정문서 포맷
- **「질의 audit + 다중 인스턴스」** — 감사 로그·공유 DB 운영
