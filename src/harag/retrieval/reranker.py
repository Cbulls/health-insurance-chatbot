"""
리랭커 — 검색 후보(top-K)를 정밀 재순위해 top-N 선별(FR-7).

검색(hybrid)은 '빠르게 넓게 회수', 리랭커는 '느리지만 정확하게 선별'.
cross-encoder는 질의-청크 쌍을 함께 보고 관련도를 매겨, dense/sparse의
독립 인코딩보다 정밀하다. 대신 비싸므로 후보 수를 제한한다.

cross-encoder 모델은 어댑터(CrossEncoder Protocol) 뒤로 — GPU·모델 의존.
실제론 self-host reranker 서버(reranker_server_url) 호출.
"""
from __future__ import annotations

from typing import Protocol

from harag.contracts.boundaries import ScoredChunk


class CrossEncoder(Protocol):
    """질의-텍스트 쌍의 관련도 점수. 실제론 GPU cross-encoder."""
    def score_pairs(self, query: str, texts: list[str]) -> list[float]: ...


class CrossEncoderReranker:
    def __init__(self, model: CrossEncoder, top_n: int = 5,
                 min_score: float = 0.0, max_candidates_under_load: int = 20,
                 retrieval_blend: float = 0.0):
        self._model = model
        self._top_n = top_n
        self._min_score = min_score
        self._max_under_load = max_candidates_under_load
        # 0=cross-encoder 점수만, 1=retrieval 점수만. 어절 리랭커처럼 약한
        # 신호는 0.5~0.7로 dense 점수를 보존해 한/영 교차 질의에서 붕괴를 막는다.
        self._blend = max(0.0, min(1.0, retrieval_blend))

    def rerank(self, query: str, candidates: list[ScoredChunk],
               under_load: bool = False) -> list[ScoredChunk]:
        if not candidates:
            return []

        # RR-04: 부하 시 입력 K 축소(graceful degradation).
        # retrieval 점수 상위만 cross-encoder에 넘겨 모델 비용·지연을 줄인다.
        pool = candidates
        if under_load and len(candidates) > self._max_under_load:
            pool = sorted(candidates, key=lambda s: s.score, reverse=True)[
                :self._max_under_load]

        # cross-encoder로 질의-청크 정밀 재채점(한 번의 배치 호출)
        scores = self._model.score_pairs(query, [s.chunk.text for s in pool])

        # 새 점수로 ScoredChunk 갱신(선택적 retrieval 점수 블렌드)
        blend = self._blend
        rescored = [
            ScoredChunk(chunk=sc.chunk,
                        score=(1.0 - blend) * new + blend * sc.score)
            for sc, new in zip(pool, scores)
        ]

        # RR-02: 최저 점수 미달 제외
        kept = [s for s in rescored if s.score >= self._min_score]

        # RR-03: 관련도로 재정렬, RR-01: top-N 컷
        kept.sort(key=lambda s: s.score, reverse=True)
        return kept[:self._top_n]
