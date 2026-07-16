"""
임베더 — 청크를 dense(+sparse) 벡터로.

MVP: dense + sparse(형태소 토큰 가중치)를 EmbeddedChunk에 채운다.
검색은 Qdrant dense+sparse RRF 하이브리드(컬렉션에 sparse가 있을 때).
dense/sparse 생성 로직은 어댑터(EmbeddingModel/Morph) 뒤로 분리한다.
"""
from __future__ import annotations

from collections import Counter
from typing import Protocol

from harag.contracts.boundaries import Chunk, EmbeddedChunk


class EmbeddingModel(Protocol):
    """dense 임베딩 모델. texts -> 벡터 목록."""
    model_id: str
    dim: int
    def encode(self, texts: list[str]) -> list[list[float]]: ...


class Morph(Protocol):
    """형태소 분석기. text -> 어간 토큰 목록(sparse용)."""
    def tokens(self, text: str) -> list[str]: ...


class HybridEmbedder:
    """Embedder Protocol 구현. dense는 모델, sparse는 형태소."""

    def __init__(self, model: EmbeddingModel, morph: Morph):
        self._model = model
        self._morph = morph
        # 운영 모델(Api/LocalHash)은 model_id를 갖지만, 워커에 주입되는 경량
        # 어댑터/테스트 대역은 dim만 가질 수 있어 getattr로 관대하게 처리.
        self.model_id = getattr(model, "model_id", "unknown-embedding-model")
        self.dim = getattr(model, "dim", 0)

    def embed(self, chunks: list[Chunk]) -> list[EmbeddedChunk]:
        if not chunks:
            return []
        dense_vecs = self._model.encode([c.text for c in chunks])
        out: list[EmbeddedChunk] = []
        for chunk, dense in zip(chunks, dense_vecs):
            out.append(EmbeddedChunk(
                chunk=chunk,
                dense_vector=list(dense),
                sparse_terms=self._sparse_terms(chunk.text),
            ))
        return out

    def _sparse_terms(self, text: str) -> dict[str, float]:
        toks = self._morph.tokens(text)
        if not toks:
            return {}
        return {term: float(c) for term, c in Counter(toks).items()}
