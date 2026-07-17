# 정확도 개선 + Redis/워커 구현 설명

이 문서는 **PDF 기반 답변 정확도 향상**과 **Redis 캐시·인덱싱 워커**를 어떻게 구현했는지 정리한다.  
Kafka·MSA(Spring Boot 서비스 분리)·수식 LaTeX·VLM(차트 OCR)은 범위에서 제외했다.

후속(검색·생성 경로의 컨텍스트 확장·인용 강제·지연 절감)은 [`SPRINT_Q_ACCURACY_PERF.md`](SPRINT_Q_ACCURACY_PERF.md)를 본다.

---

## 1. 목표

| 목표 | 내용 |
|------|------|
| 정확도 | 업로드 PDF 기반 RAG 답변 정확도 **90% 이상**(샘플 골드셋·실 API 스택 기준) |
| 아키텍처 | Kafka 없이 **Redis 큐 + 워커**로 파싱·임베딩을 API와 분리 |
| 하위 호환 | `REDIS_URL`이 비어 있으면 기존처럼 **인프로세스 ThreadPool** 인제스트 |

### 평가 결과 (실 API, `scripts/eval_accuracy.py`)

| 유형 | hit@k | MRR | abstention | correct |
|------|-------|-----|------------|---------|
| body | 1.0 | 1.0 | 1.0 | 1.0 |
| table | 1.0 | 1.0 | 1.0 | 1.0 |
| multi_clause | 1.0 | 1.0 | 1.0 | 1.0 |
| absent | 1.0 | 1.0 | 1.0 | 1.0 |

결과 파일: `eval_results/after_layout_api.json`  
(로컬 해시 임베딩·추출형 LLM에서는 생성 품질이 낮아 correct가 낮게 나올 수 있다. 검색 hit@k는 유지.)

---

## 2. Before / After 요약

| 항목 | Before | After |
|------|--------|-------|
| 다단 레이아웃 | y좌표만 정렬 → 열 교차 읽기 | x-gap 클러스터링 후 **열 왼쪽→오른쪽, 열 안 위→아래** |
| 헤더/푸터/페이지번호 | PDF 경로에서 미제거 | `header_footer` + `is_noise=True`로 마킹, 청킹 시 스킵 |
| 표 직렬화 | `헤더: 값 \| …` 한 줄 | **Markdown 표** (`\| 헤더 \|` / `\|---\|` / 데이터 행) |
| 구조 경로 | 조 단위 flat `struct_path` | **장 > 조** 상속, `parent_chunk_id` 채움 |
| 토큰 분할 | 문장 1개 오버랩 | **max 500 / overlap 50** 토큰 슬라이딩 윈도우 |
| 의미 분할 | 없음 | API 임베딩 시 문장 유사도 임계값 미만에서 분할(로컬 해시면 스킵) |
| 인제스트 | API 프로세스 ThreadPool만 | `REDIS_URL` 있으면 **Redis Streams + worker**(at-least-once) |
| 상태/레이트/대화 | SQLite + 인메모리 | Redis 캐시·Lua 토큰버킷·pipeline 대화 이력(없으면 폴백) |

버전:

- 파서: `pdf-mvp-0.3` (`src/harag/parsing/pdf_parser.py`)
- 청커: `struct-sem-0.3` (`src/harag/chunking/chunker.py`)

**기존에 업로드한 문서는 재업로드해야** 새 파싱·청킹이 적용된다.

---

## 3. PDF 파싱 (`pdf-mvp-0.3`)

### 3.1 다단(Multi-column)

1. `extract_text_lines()`로 줄별 bbox(`x0`, `x1`, `top`, `bottom`)를 수집한다.
2. 페이지 너비의 **12%** 이상 x-gap이 있으면 열 경계로 본다.
3. 열을 왼쪽→오른쪽으로 정렬한 뒤, 각 열 안에서 top 오름차순으로 읽는다.
4. 열이 1개면 기존처럼 top 정렬만 한다.

핵심 함수: `PdfParser._order_lines_multicolumn`.

### 3.2 노이즈 클렌징

- 페이지 상·하 **8%** 밴드의 짧은 줄을 후보로 모은다.
- **2페이지 이상 반복**되거나 단독 페이지 번호 패턴(`- 3 -`, `3/12`, `Page 7`)이면 노이즈로 판정한다.
- 해당 줄은 `BlockType.header_footer`, `is_noise=True`로 IR에 넣는다(청커가 스킵).

### 3.3 기존 유지

- 조·항 헤더(`제n조`, `Article n`) → `struct_path`
- 표 bbox 밖 텍스트만 본문으로(표-본문 중복 제거)
- 스캔/암호 PDF → `ParseStatus.failed`

---

## 4. 청킹 (`struct-sem-0.3`)

### 4.1 표 Markdown

```markdown
[Article 5]
| Item | Annual Limit | Copayment |
| --- | --- | --- |
| Outpatient | 200,000 KRW per visit | 30 percent |
```

헤더 행 + 구분선 + 데이터 행으로 행렬 관계를 보존한다.

### 4.2 상위 헤더 상속

- `제n장` / `Section n`을 chapter 스택으로 유지한다.
- 하위 조 청크의 `struct_path` 예: `제3장 > 제12조`
- 청크 텍스트 앞에 `[제3장 > 제12조]` 프리픽스를 붙여 검색 임베딩에 문맥을 넣는다.
- `ChunkMetadata.parent_chunk_id`에 직전 상위/형제 본문 청크 id를 넣는다.

### 4.3 슬라이딩 윈도우

- 기본 `max_tokens=500`, `overlap_tokens=50` (토큰 ≈ `len(text)//2`)
- 상한 초과 시 문장 단위로 윈도우를 밀며, 끝부분 `overlap_tokens`만큼 다음 청크에 겹친다.

### 4.4 의미 기반 분할

- `ChunkingContext.embedding_model`이 API 임베딩일 때만 동작한다.
- 인접 문장 cosine 유사도가 `semantic_threshold`(기본 0.35) 미만이고 현재 조각이 충분히 길면 분할한다.
- `local-hash-*` 모델이면 건너뛴다(오프라인 데모 유지).

인제스트에서 임베딩 모델을 넘긴다:

```python
ChunkingContext(..., embedding_model=embedder._model)
```

---

## 5. Redis + 워커 (Kafka 없음)

상세 키·재시도·환경변수는 [`docs/REDIS_DESIGN.md`](REDIS_DESIGN.md)를 본다.

```text
Upload API → spool 파일 → Lua(XADD+inflight) → Redis Stream
    → worker XREADGROUP / XAUTOCLAIM → PdfIngestPipeline
    → XACK (+ DLQ on max attempts) → DocStatusCache (TTL 300s)
```

| 구성 | 파일 | 역할 |
|------|------|------|
| Redis 클라이언트 | `storage/redis_client.py` | 타임아웃/풀, 실패 TTL 재시도 |
| 인제스트 큐 | `storage/redis_ingest_queue.py` | Streams + Lua 멱등 + reclaim/DLQ |
| 상태 캐시 | `storage/redis_cache.py` | 문서 status TTL 300s |
| 레이트/대화 | `storage/redis_stores.py` | Lua 토큰버킷, pipeline RPUSH |
| PDF 파이프라인 | `indexing/pdf_pipeline.py` | API·워커 공용, `on_failed` |
| 워커 엔트리 | `python -m harag.indexing.run_worker` | Streams 소비·ack·retry |
| 조립 | `api/main.py` | Redis 유무·스풀 가드·`/health` |

### 사용자 설정 (직접 필요)

1. **Redis 기동**
   ```bash
   docker compose up -d redis
   # 또는 전체: docker compose up --build
   ```
2. **환경변수** (`.env`)
   ```bash
   # compose 네트워크 안 API/worker
   REDIS_URL=redis://redis:6379/0
   # 호스트에서 uvicorn만 돌릴 때
   REDIS_URL=redis://localhost:6379/0
   HARAG_SPOOL_DIR=/tmp/harag_spool   # REDIS_URL 있으면 필수
   REDIS_KEY_PREFIX=harag:
   INGEST_VISIBILITY_SEC=120
   INGEST_MAX_ATTEMPTS=3
   ```
3. **의존성**
   ```bash
   pip install redis
   # 또는
   pip install -e .
   ```

`REDIS_URL`을 비우면 Redis 없이 동작한다(인메모리 레이트·대화, ThreadPool 인제스트). 이 경우 **worker 컨테이너는 필요 없다**.

### Compose 서비스

- `redis` — Redis 7
- `api` — FastAPI (기본 `REDIS_URL=redis://redis:6379/0`)
- `worker` — `python -m harag.indexing.run_worker`
- 공유 볼륨 `harag_spool` — API가 쓴 업로드 임시 파일을 워커가 읽음
- 공유 볼륨 `harag_data` — SQLite 메타 DB

로컬에서 Redis만 켜고 API는 uvicorn으로 돌릴 때:

```bash
docker compose up -d redis qdrant
export REDIS_URL=redis://localhost:6379/0
export HARAG_SPOOL_DIR=/tmp/harag_spool
mkdir -p "$HARAG_SPOOL_DIR"
# 터미널 1
uvicorn harag.api.main:app --reload
# 터미널 2
python -m harag.indexing.run_worker
```

---

## 6. 주요 변경 파일 목록

**파싱·청킹**

- `src/harag/parsing/pdf_parser.py`
- `src/harag/chunking/chunker.py`
- `src/harag/api/ingest.py` → `indexing/pdf_pipeline.py` 위임

**Redis·워커**

- `src/harag/storage/redis_client.py` (신규)
- `src/harag/storage/redis_cache.py` (신규)
- `src/harag/storage/redis_ingest_queue.py` (신규)
- `src/harag/storage/redis_stores.py` (신규)
- `src/harag/indexing/pdf_pipeline.py` (신규)
- `src/harag/indexing/run_worker.py` (신규)
- `src/harag/api/main.py`, `api/ratelimit.py`, `api/routes_ingest.py`
- `src/harag/config/settings.py` (`REDIS_URL`)
- `docker-compose.yml`, `.env.example`, `pyproject.toml`, `requirements.txt`

**테스트·평가**

- `tests/behavior/test_accuracy_improvements.py` (표 MD·오버랩)
- `tests/behavior/test_parse_chunk_redis.py` (다단·상속·Redis 멱등)
- `scripts/eval_accuracy.py` / `eval_results/after_layout_*.json`

---

## 7. 제외 항목 (이번 범위 밖)

- Kafka / 기타 브로커
- MSA로 파싱·청킹·임베딩을 별도 Spring/HTTP 서비스로 분리
- 수식 → LaTeX 추출
- VLM으로 차트·도면 설명 생성 / OCR 스캔 PDF

---

## 8. 운영 시 주의

1. 파서·청커 버전 변경 후 **기존 문서는 재업로드**.
2. compose에서 `REDIS_URL`이 켜져 있으면 **worker가 떠 있어야** 업로드가 `ready`로 끝난다. worker 없이 API만 띄우면 문서가 `processing`에 남을 수 있다.
3. Qdrant 컬렉션을 예전 sparse 스키마로 만든 경우, IDF modifier를 쓰려면 컬렉션을 새로 만들거나 `QDRANT_COLLECTION` 이름을 바꾼 뒤 재인덱싱한다.
