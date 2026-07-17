# 스프린트 Q — 답변 정확도·성능 코드 개선

이 문서는 RAG **품질(정확도)** 과 **지연(성능)** 을 코드로 다듬은 **스프린트 Q** 구현을 정리한다.  
파싱·청킹·Redis 워커 쪽 이전 작업은 [`ACCURACY_REDIS_WORKER.md`](ACCURACY_REDIS_WORKER.md)를 본다.

제품 기능(지식 라이브러리·SSO·공유 문서함)과는 별 트랙이다.

---

## 1. 목표와 범위

| 축 | 목표 |
|----|------|
| 정확도 | 조·항이 쪼개져도 근거가 반쪽이 되지 않게 하고, 인용 없는 “근거 있는 척” 답을 UI에 올리지 않으며, “검색 OK·답 틀림”을 품질 게이트에서 잡을 수 있게 한다 |
| 성능 | 불필요한 LLM 호출·토큰 스트림 오버헤드·동일 질의 임베딩 재계산을 줄인다 |

**의도적으로 하지 않은 것**

- Dual-LLM 상시 판정, Guardrails SaaS
- 답변 전체 Redis 캐시 (ACL·문서 개정과 충돌)
- 라이브 경로가 아닌 `retrieval/hybrid.py` 토이 모듈

---

## 2. 파이프라인 Before / After

### Before

```text
질의 → Rewrite(LLM, 이력 있으면 항상)
    → Hybrid RRF
    → Rerank (후보 상한은 부하 시에만)
    → Jaccard 중복제거만
    → Answer LLM
    → OutputGuard
         └ 인용 마커 없으면 검색된 전 청크를 citation으로 노출
```

### After (스프린트 Q)

```text
질의 → Rewrite(LLM, 지시어·초단문일 때만)
    → 보험 동의어 확장 (검색용, LLM 없음)
    → Hybrid RRF  (+ 질의 임베딩 LRU/TTL 캐시)
    → Rerank (항상 후보 상한; 표는 긴 clip)
    → prepare_context: 중복제거 → 부모/이웃 확장 → 토큰 예산 패킹
    → Answer LLM  (스트림은 Queue 브리지)
    → OutputGuard
         └ 비기권 답은 유효 [문서 N] ≥ 1 필수
```

오케스트레이션은 [`src/harag/api/pipeline.py`](../src/harag/api/pipeline.py), 배선은 [`src/harag/api/main.py`](../src/harag/api/main.py).

---

## 3. 정확도 개선

### 3.1 A1 / A3 — 컨텍스트 확장·토큰 패킹

**문제:** `parent_chunk_id`는 청킹에만 있고 생성 경로에서 쓰지 않았다. Jaccard 중복제거만으로는 노이즈 청크·잘린 근거를 막기 어렵다.

**해결:** [`context_compact.py`](../src/harag/retrieval/context_compact.py)

| 단계 | 함수 | 동작 |
|------|------|------|
| 중복 제거 | `dedupe_scored_chunks` | content_hash + Jaccard |
| 이웃 확장 | `expand_neighbors` | `parent_chunk_id` + 동일 조/장(`struct_path` 첫 세그먼트) 이웃 최대 1~2개 (후보 풀 안) |
| 예산 패킹 | `pack_by_token_budget` | 점수 순으로 대략 토큰(`len//2`) 예산까지 |
| 통합 | `prepare_context` | pipeline이 리랭크 직후 호출 |

**페이로드:** [`qdrant_store.py`](../src/harag/retrieval/qdrant_store.py)의 payload v2에 `parent_chunk_id`를 넣어 검색 결과에서 복원한다.  
이미 인덱싱된 문서는 **재업로드**해야 부모 링크가 포인트에 실린다. 같은 검색 풀 안의 형제 항 확장은 `struct_path`만으로도 동작한다.

### 3.2 A2 / P4 — 리랭크 품질·후보 상한

**문제:** LLM 리랭크가 본문을 600자로 잘라 표 순위가 약하고, TOP_K 전부에 대해 CE/LLM을 돌리면 비용·지연이 크다.

**해결**

- [`reranker.py`](../src/harag/retrieval/reranker.py): `max_candidates`(기본 12), 부하 시 `max_candidates_under_load`(기본 8)로 **항상** 상한
- [`local_rerank.py`](../src/harag/llm/local_rerank.py): Markdown 표(`|` + 줄바꿈)는 **1800자**, 일반 본문은 600자

### 3.3 A4 — 단일턴 질의 동의어 확장

**문제:** 멀티턴 rewrite는 conversation이 있을 때만 돌고, “본인부담” 같은 짧은 보험 용어는 recall 상한이 낮다.

**해결:** [`query_normalize.py`](../src/harag/retrieval/query_normalize.py)의 `expand_query_for_search`  
원문을 유지한 채 동의어 토큰을 덧붙인다 (LLM 없음). pipeline의 rewrite 단계 직후·검색 직전에 적용.

예: `본인부담은 얼마인가요?` → `… 본인부담금 copayment` 등이 추가될 수 있다.

### 3.4 A5 — 정답(생성) 게이트 훅

**문제:** 골드셋의 `gold_answer` / `forbidden_claims`는 로드만 하고 `evaluate()`가 채점하지 않아, 검색은 통과하고 답이 틀려도 CI가 통과할 수 있었다.

**해결:** [`harness.py`](../src/harag/eval/harness.py)

- `answer_correct()`: 키워드 과반 포함 + `forbidden_claims` 부재
- `TypeReport.answer_acc`: 유형별 집계 (`gold_answer`가 있는 질의만)
- `QualitySLO.min_answer_acc`: **기본 0 = 미적용**(기존 게이트 호환). `> 0`이면 해당 유형을 차단
- 골드 로더가 `forbidden_claims`를 읽음 ([`run_quality_gate.py`](../src/harag/eval/run_quality_gate.py))
- 요약 JSON에 `answer_acc` 필드 출력

CI에서 켜는 예:

```python
QualitySLO(
    min_hit_at_k=0.70,
    min_mrr=0.60,
    min_abstention_acc=0.90,
    min_citation_faithful=0.90,
    min_answer_acc=0.80,  # 훅 활성화
)
```

### 3.5 A6 — 비기권 답변 인용 필수

**문제:** 마커가 없으면 pipeline이 **검색된 전 청크**를 citation으로 넘겨, 근거 없는 답도 UI상 근거가 있어 보였다.

**해결**

1. [`output_guard.py`](../src/harag/security/output_guard.py): 유효 인용이 없으면 `abstain_reason=missing_citation`
2. [`pipeline.py`](../src/harag/api/pipeline.py): citation fallback 제거 — 마커에 해당하는 청크만 `context_chunks`에 실음

동기 `generate`와 스트림 종료 가드 모두 동일 규칙을 탄다.

---

## 4. 성능 개선

### 4.1 P1 — Rewrite LLM 스킵

[`rewriter.py`](../src/harag/retrieval/rewriter.py)의 `needs_rewrite()`:

- 이력 없음 → 스킵
- 지시어·대용 표현(`그건`, `해당 조항` 등) 또는 **초단문(≤10자)** 후속만 LLM 호출
- 이력이 있어도 독립적인 긴 질의는 **원문 유지** (불필요 1 LLM 절감)

### 4.2 P2 — 스트림 Queue 브리지

**문제:** `answer_stream`이 토큰마다 `asyncio.to_thread(next, …)`를 호출해 TTFT·토큰 리듬이 나빴다.

**해결:** `_bridge_sync_token_iter` — 백그라운드 스레드가 동기 이터레이터를 읽고 `asyncio.Queue`에 넣고, 코루틴은 `await q.get()`만 한다.

### 4.3 P3 — 질의 임베딩 캐시

[`query_cache.py`](../src/harag/embedding/query_cache.py)의 `CachedEmbeddingModel`:

- `encode([query])`만 LRU + TTL 캐시
- 배치(`len ≠ 1`, 인덱싱)는 우회
- `main.py`에서 `QUERY_EMBED_CACHE_SIZE > 0`일 때 wrapping

재시도·동일 질의 반복에서 임베딩 API/로컬 인코딩 비용을 줄인다.

### 4.4 P4 — 리랭크 후보 상한

§3.2와 동일. 부하 플래그와 별도로 **평시에도** cap을 건다.

---

## 5. 설정 (환경 변수)

| 변수 | 기본 | 의미 |
|------|------|------|
| `CONTEXT_DEDUPE` | `true` | 생성 전 컨텍스트 압축 파이프라인 on/off |
| `CONTEXT_EXPAND` | `true` | 부모/이웃 확장 |
| `CONTEXT_MAX_TOKENS` | `3500` | 패킹 토큰 예산(대략) |
| `RERANK_MAX_CANDIDATES` | `12` | 리랭크 입력 상한 |
| `RERANK_MAX_CANDIDATES_UNDER_LOAD` | `8` | 부하 시 상한 |
| `QUERY_EMBED_CACHE_SIZE` | `256` | `0`이면 캐시 비활성 |
| `QUERY_EMBED_CACHE_TTL_S` | `300` | 캐시 TTL(초) |

정의: [`settings.py`](../src/harag/config/settings.py).

---

## 6. 테스트·측정

| 영역 | 위치 |
|------|------|
| 확장·패킹 | `tests/behavior/test_context_compact.py` |
| rewrite 스킵 | `tests/behavior/test_rewriter.py` (`RW07`) |
| 리랭크 상한 | `tests/behavior/test_reranker.py` (`RR05`) |
| 인용·동의어·캐시·정답 게이트 | `tests/behavior/test_sprint_q.py` |
| 스트림 + 인용 필수 픽스처 | `tests/behavior/test_pipeline_resilience.py` |

로컬 확인:

```bash
PYTHONPATH=src .venv/bin/python -m pytest tests/behavior -q
PYTHONPATH=src .venv/bin/python -m harag.eval.run_quality_gate
```

실 API 키워드 골드셋: `scripts/eval_accuracy.py`  
스테이지 지연: `/metrics`의 query stage 타이머 (생성 TTFT 추가는 후속 권장).

---

## 7. 주요 파일 맵

| 파일 | 역할 |
|------|------|
| `src/harag/retrieval/context_compact.py` | dedupe / expand / pack |
| `src/harag/retrieval/query_normalize.py` | 보험 동의어 확장 |
| `src/harag/retrieval/rewriter.py` | rewrite skip heuristic |
| `src/harag/retrieval/reranker.py` | 후보 상한 |
| `src/harag/llm/local_rerank.py` | 표 긴 clip |
| `src/harag/embedding/query_cache.py` | 질의 임베딩 캐시 |
| `src/harag/api/pipeline.py` | 오케스트레이션·스트림 브리지·citation 정책 |
| `src/harag/api/main.py` | 캐시·리랭크·context 설정 배선 |
| `src/harag/security/output_guard.py` | `missing_citation` |
| `src/harag/eval/harness.py` | `answer_correct` / `answer_acc` / SLO 훅 |
| `src/harag/retrieval/qdrant_store.py` | `parent_chunk_id` payload |

---

## 8. 아직 남은 갭 (의도적 후순위)

플랜에서 스프린트 Q에 넣지 않았거나 코드만으로 끝나지 않는 항목:

| 항목 | 비고 |
|------|------|
| P5 인젝션 스캔 1회 재사용 | pipeline → rewrite → `build_safe_messages` 중복 (소폭) |
| 워커 경로 semantic chunk | embed model 미전달 시 Redis 인제스트 청크 품질 |
| 한국어 토큰 추정 `len//2` | 청크 경계·예산 오차 — 근사치 유지 |
| TEI Cross-Encoder 상시 | `RERANKER_SERVER_URL` 운영 권장 (코드 경로는 기존) |
| 생성 TTFT 메트릭 | `/metrics` 확장 권장 |

---

## 9. 관련 문서

- [`ACCURACY_REDIS_WORKER.md`](ACCURACY_REDIS_WORKER.md) — 파싱·청킹·Redis 워커
- [`RERANKER_CTO_REVIEW.md`](RERANKER_CTO_REVIEW.md) — 리랭커 한도·평가
- [`SYSTEM_ADVANCEMENT.md`](SYSTEM_ADVANCEMENT.md) — 로드맵·SLO
- [`실행_가이드.md`](실행_가이드.md) — 로컬·compose 실행
- 오프라인 골드셋: `eval/goldsets/domain_health_insurance_v1.json`
