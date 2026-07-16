# 리랭커 CTO 리뷰 (진단)

측정·코드 기준 시점: 고도화 구현과 함께 동결.

## 한 줄 판정

**오케스트레이션(어댑터·top-N·파이프라인 위치)은 맞고, 운영 모델이 어절 겹침 폴백이라 “정밀 재순위” 설계 의도는 미충족이었다.**  
top-K 밖 청크는 리랭커가 구원하지 못한다 — 정확도 상한은 임베딩·하이브리드·재작성이 먼저 결정한다.

## 잘 된 점

- `CrossEncoder` Protocol로 모델 교체 지점이 명확 ([`src/harag/retrieval/reranker.py`](../src/harag/retrieval/reranker.py))
- rewrite → retrieve → rerank → generate 순서 ([`pipeline.py`](../src/harag/api/pipeline.py))
- RR-01~04 계약 테스트 ([`tests/behavior/test_reranker.py`](../tests/behavior/test_reranker.py))
- 키 없이도 파이프라인이 동작하도록 Lexical 폴백 + 한↔영용 `retrieval_blend` (MVP 정직성)

## 미흡했던 점 (고도화 전)

| 갭 | 영향 |
|----|------|
| `LexicalCrossEncoder`만 배선 | 동의어·교차언어 약함, soft reorder |
| `RERANKER_SERVER_URL` 없음 | 설계의 GPU/TEI CE 미연결 |
| 리랭커 `min_score=0.0` | RR-02 라이브 비활성 |
| RRF·lexical·`MIN_SCORE` 스케일 혼선 | false abstain / junk 통과 |
| `under_load` 미연결 | RR-04 사문화 |
| `stage("rerank")` 없음 | TTFT 분해 불가 |

## 고도화 후 (이 스프린트) — 구현 완료

1. **Phase A** — local hash·Identity rewrite 경고, `hybrid=` 기동 로그 ([`main.py`](../src/harag/api/main.py))
2. **Phase B** — [`HttpCrossEncoder`](../src/harag/llm/http_rerank.py) + `RERANKER_*` ([`.env.example`](../.env.example)), CE 시 blend=0·점수 sigmoid
3. **Phase C** — `rerank_min_score`, inflight `under_load`, `stage(rerank)` 로그, 테스트 보강
4. **Phase D** — `ndcg_at_k` / `context_noise_rate` ([`harness.py`](../src/harag/eval/harness.py)), [`dedupe_scored_chunks`](../src/harag/retrieval/context_compact.py), [`eval/goldsets/rerank_sample.json`](../eval/goldsets/rerank_sample.json)

운영: URL이 없으면 Lexical 폴백 유지. URL이 있으면 TEI 호환 `/rerank`로 전환.
