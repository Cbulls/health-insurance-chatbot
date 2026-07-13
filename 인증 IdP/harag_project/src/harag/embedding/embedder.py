"""
임베더 — 청크를 dense + sparse 벡터로(FR-5, 하이브리드).

dense(의미)는 임베딩 모델, sparse(키워드)는 형태소 분석기. 둘 다 어댑터 뒤로 분리:
실제 구현은 self-host GPU 모델(KURE/BGE-M3)과 Kiwi/Mecab. 모델 선정은 PoC/벤치마크 후.

sparse를 형태소 토큰으로 만드는 게 한국어 검색의 핵심 — "출장비를"/"출장비가"가
같은 어간 "출장비"로 매칭되게 한다(공백 토큰화로는 조사 차이로 깨짐).
"""
from __future__ import annotations

from collections import Counter
from typing import Protocol

from harag.contracts.boundaries import Chunk, EmbeddedChunk


class EmbeddingModel(Protocol):
    """dense 임베딩 모델. texts -> 벡터 목록. 실제론 self-host GPU."""
    dim: int
    def encode(self, texts: list[str]) -> list[list[float]]: ...


class Morph(Protocol):
    """형태소 분석기. text -> 어간 토큰 목록. 실제론 Kiwi/Mecab."""
    def tokens(self, text: str) -> list[str]: ...


class HybridEmbedder:
    def __init__(self, model: EmbeddingModel, morph: Morph):
        self._model = model
        self._morph = morph

    def embed(self, chunks: list[Chunk]) -> list[EmbeddedChunk]:
        if not chunks:
            return []
        # dense: 모델 일괄 인코딩(배치 효율)
        dense_vecs = self._model.encode([c.text for c in chunks])
        out: list[EmbeddedChunk] = []
        for chunk, dense in zip(chunks, dense_vecs):
            sparse = self._sparse_terms(chunk.text)
            out.append(EmbeddedChunk(
                chunk=chunk,                  # 메타·계보 그대로 보존
                dense_vector=dense,
                sparse_terms=sparse,
            ))
        return out

    def _sparse_terms(self, text: str) -> dict[str, float]:
        """형태소 토큰의 빈도 가중치(BM25 sparse 입력). 어간 기반 매칭."""
        toks = self._morph.tokens(text)
        if not toks:
            return {}
        counts = Counter(toks)
        # 빈도를 가중치로(실제 BM25 가중은 인덱스에서; 여기선 term frequency)
        return {term: float(c) for term, c in counts.items()}
