# 건강보험 설명 챗봇 (health-insurance-chatbot)

> 한글 행정문서 RAG 챗봇 · PDF 업로드 MVP (내부 코드명 `harag`)

사용자가 건강보험 안내문 등 PDF를 업로드하면 챗봇이 그 내용에 **근거해서** 출처와
함께 답하고, 근거가 없으면 **지어내지 않고 "모른다"고 답하는(abstain)** RAG 서비스의
실행 가능한 MVP.

> 이 MVP는 [MASTER_DESIGN.md](MASTER_DESIGN.md)의 엔터프라이즈 설계(harag) 중
> "PDF → RAG 챗봇" 수직 슬라이스를 실제로 돌아가게 만든 것이다. HWP 파싱, 조직 ACL,
> 하이브리드+RRF 검색, 버전전환/GC, self-host GPU 임베딩 등은 [Phase 2](#phase-2-로드맵)로 미룬다.

## 무엇이 돌아가는가

- PDF 업로드 → 파싱(pdfplumber) → 구조 기반 청킹 → 임베딩 → Qdrant 적재(비동기)
- 질의 → 임베딩 → Qdrant dense 검색(소유자 격리) → LLM 생성 → 출처/abstain을 SSE 스트리밍
- 브라우저 UI(업로드 드래그&드롭 + 스트리밍 채팅 + 출처 표시)
- **키 없이도 데모 가능**: 임베딩/LLM API 키가 없으면 로컬 폴백(결정적 해시 임베딩 +
  추출형 답변)으로 자동 전환된다. 키를 넣으면 곧바로 운영 품질 경로로 바뀐다.

```
브라우저 ─▶ FastAPI 게이트웨이
             ├─ POST /v1/documents  ─▶ (BackgroundTasks) 파싱→청킹→임베딩→Qdrant
             └─ POST /v1/query/stream ─▶ 임베딩→Qdrant검색(owner필터)→LLM생성→SSE
          저장: Qdrant   외부: 임베딩 API · LLM API
```

## 빠른 시작 (Docker, 권장)

```bash
cp .env.example .env      # (선택) 키 입력. 비워두면 로컬 폴백으로 동작
docker compose up --build
# 브라우저에서 http://localhost:8000 접속 → PDF 업로드 → 질문
```

운영 품질(실제 임베딩/LLM)을 쓰려면 `.env`에 키를 채운다. Gemini(Google AI Studio) 예:

```dotenv
LLM_API_BASE=https://generativelanguage.googleapis.com/v1beta/openai
LLM_API_KEY=...                  # AI Studio 키
LLM_MODEL=gemini-3.5-flash
LLM_REWRITE_MODEL=gemini-3.1-flash-lite
EMBEDDING_API_BASE=https://generativelanguage.googleapis.com/v1beta/openai
EMBEDDING_API_KEY=...            # 같은 Gemini 키
EMBEDDING_MODEL=gemini-embedding-001
EMBEDDING_DIM=768
EMBEDDING_SEND_DIMENSIONS=true
QDRANT_URL=https://....cloud.qdrant.io
QDRANT_API_KEY=...
RATE_LIMIT_QPM=5                 # 질문당 ≤3콜 → Gemini ~10 RPM 아래로
```

과금·한도 운영은 [docs/GEMINI_COST_STRATEGY.md](docs/GEMINI_COST_STRATEGY.md).
OpenAI 호환이면 `*_API_BASE` / `*_MODEL`만 바꿔 끼운다(예: Upstage Solar).
코드는 프로바이더 무관하다.

> 임베딩 모델(차원)을 바꾸면 기존 Qdrant 컬렉션과 차원이 어긋난다.
> 컬렉션을 삭제하거나 `QDRANT_COLLECTION`을 새 이름으로 바꾼 뒤 재인덱싱한다.
> 자세한 남은 일·사용자 구축 항목은 [REMAINING_WORK.md](REMAINING_WORK.md).

## 빠른 시작 (로컬, Docker 없이)

Python 3.11+ 필요.

```bash
python -m venv .venv && source .venv/bin/activate
pip install -e .
# QDRANT_URL 비우면 인메모리(:memory:)로 동작(단일 프로세스)
uvicorn harag.api.main:app --reload
# http://localhost:8000
```

## 스모크 테스트

API가 떠 있는 상태에서:

```bash
pip install httpx
BASE=http://localhost:8000 python scripts/smoke.py
```

샘플 PDF 생성 → 업로드 → ready 폴링 → 관련 질의(답변) → 무관 질의(abstain)를 검증한다.

## API 요약

| 메서드 | 경로 | 설명 |
|---|---|---|
| POST | `/v1/documents` | PDF 업로드(multipart). 202 + `document_id`. 비동기 처리 |
| GET | `/v1/documents` | 내 문서 목록/상태 |
| GET | `/v1/documents/{id}` | 특정 문서 상태(processing/ready/failed) |
| DELETE | `/v1/documents/{id}` | 본인 문서 삭제(Qdrant 포인트 + 상태) |
| POST | `/v1/query` | 질의 → 답변+출처(JSON) |
| POST | `/v1/query/stream` | 질의 → SSE(token/citations/abstain/done) |
| GET | `/health` | 헬스체크 |

소유자 격리는 `X-Owner-Id` 헤더로 한다(프론트가 브라우저별 UUID 발급). 없으면
`anonymous` 공유 소유자로 폴백. `AUTH_JWT_SECRET`을 설정하면 Bearer JWT 검증으로
전환된다(조직 IdP는 Phase 2 / 사용자 구축).

## Phase 2 · 남은 일

실행 가능한 MVP 이후의 **사용자 구축**과 **코드 미비**는
[REMAINING_WORK.md](REMAINING_WORK.md)에 우선순위별로 정리했다.
엔터프라이즈 상세는
[앞으로 해야 할 것들/PRODUCTION_ROADMAP.md](앞으로%20해야%20할%20것들/PRODUCTION_ROADMAP.md).

코드 다음 타석(요약): 구조화 인용 → HWP PoC → 큐/워커.

## 한계 (정직한 경계)

- 하이브리드는 **신규 컬렉션**에서 활성. 기존 dense-only 컬렉션은 dense 폴백(경고 로그).
- 문서 목록은 SQLite(`./data/harag.db`) 또는 `DATABASE_URL` Postgres에 영속화. 예전 인메모리만 쓰던 목록은 재업로드 필요.
- 문서 삭제: `DELETE /v1/documents/{id}` 또는 UI 삭제 버튼.
- 스캔(이미지) PDF는 텍스트 추출 불가 → `failed` (OCR 미구현).
- 로컬 폴백 임베딩/LLM은 데모용. 운영은 API 키 필요.

## 구조

```
src/harag/
  config/settings.py        환경변수 → 설정(키 없으면 로컬 폴백)
  schemas/                  IR·청크 Pydantic 계약(불변식)
  contracts/boundaries.py   경계 타입·검증 함수
  parsing/pdf_parser.py     PDF → DocumentIR (pdfplumber)
  chunking/chunker.py       구조 기반 청킹(페이지 프록시)
  embedding/                HybridEmbedder + API/로컬 임베딩 모델
  retrieval/qdrant_store.py Qdrant 적재 + dense 검색(payload 저장·owner 필터)
  generation/               생성기(abstention·인용검증) + LLM 방어 클라이언트
  llm/                      OpenAI 호환 전송 + 로컬 추출형 폴백 + 팩토리
  security/injection.py     프롬프트 인젝션 완화
  api/                      FastAPI 게이트웨이(라우트·미들웨어·인증·조립)
frontend/                   업로드 + 스트리밍 채팅 UI(정적, API가 서빙)
scripts/                    샘플 PDF 생성 + 스모크 테스트
```

## 설계 모듈 이식 (엔터프라이즈 라이브러리)

설계(harag_project)에만 있던 모듈을 `src/harag`로 이식했다. MVP 실행 경로는 그대로 두고,
리랭커·멀티턴만 라이브 파이프라인에 연결했으며 나머지는 임포트·테스트 가능한 라이브러리다.

- **라이브 연결**: `retrieval/reranker.py`(CrossEncoderReranker) + `llm/local_rerank.py`
  (LexicalCrossEncoder 폴백), `retrieval/rewriter.py`(QueryRewriter+ConversationStore) →
  `api/main._build_and_inject`에서 생성해 `QueryPipelineImpl`에 주입(키 없이도 실제 동작).
- **인덱싱 라이브러리**: `indexing/{worker,versioning}.py`, `storage/{qdrant_store,object_store,
  ingest_queue,metadata_store}.py`, `parsing/parser.py`(HWP StructuringParser).
- **성능/관측/보안**: `eval/{poc,harness,quality,capacity,perf}.py`, `observability/tracing.py`,
  `security/pii.py`, `contracts/acl.py`, `api/auth_jwt.py`.
- **CTO 결함 수정**(`storage/qdrant_store.py`): 비결정적 sparse 해시 → `hash_str`(blake2b) 결정적,
  인메모리 원문 → payload 저장·복원, dense 전용 → dense+sparse **RRF 하이브리드**.
- **계약 복원**: `contracts/boundaries.py`에 `verify_boundary3`(dim·model_id·sparse 존재) 재추가.

### 회귀 테스트

이식·수정 모듈의 동작·계약 검증 테스트를 `tests/`로 포함했다(Python 3.11+ 환경 필요):

```bash
pip install -e ".[dev]"
pytest -q            # tests/behavior/* + tests/contract/*
```

`tests/behavior/`는 리랭커(RR-01~04)·멀티턴(RW-01~06)·워커·증분·qdrant_store(빈 창 없는 버전전환
QS-01~05)·storage·pii·auth_jwt·observability·parser를, `tests/contract/`는 acl·capacity·poc·eval·
versioning 불변식을 검증한다.
