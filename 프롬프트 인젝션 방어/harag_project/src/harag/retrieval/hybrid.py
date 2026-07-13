"""
retriever.py — 하이브리드 검색기.

B-4 원칙: 단일 ACL 필터 명세를 dense·sparse 양쪽 경로에 동일 적용(누수 A 차단).
필터는 pre-filter(검색 시점)로 적용 — 권한 외 청크는 후보에 진입하지 못한다(누수 B 차단).
"""
from __future__ import annotations

from qdrant_client import QdrantClient, models

from harag.contracts.boundaries import Chunk, ScoredChunk, AuthContext

_DIM = 16
_VOCAB = {}  # 토큰 -> sparse 인덱스(동적 확장)


def _tok(text):
    # 아주 단순한 토크나이저(테스트용). 실제론 Kiwi/Mecab.
    return [t for t in text.replace(".", " ").split() if t]

def _sparse_of(text):
    idx, val = [], []
    for t in set(_tok(text)):
        _VOCAB.setdefault(t, len(_VOCAB) + 1)
        idx.append(_VOCAB[t]); val.append(1.0)
    return models.SparseVector(indices=idx, values=val)

def _dense_of(text):
    # 결정적 더미 dense(테스트용). 토큰 해시 기반.
    v = [0.0] * _DIM
    for t in _tok(text):
        v[hash(t) % _DIM] += 1.0
    n = sum(x * x for x in v) ** 0.5 or 1.0
    return [x / n for x in v]


class HybridRetriever:
    def __init__(self):
        self.client = QdrantClient(":memory:")
        self.coll = "docs"
        self.client.create_collection(
            collection_name=self.coll,
            vectors_config={"dense": models.VectorParams(size=_DIM, distance=models.Distance.COSINE)},
            sparse_vectors_config={"sparse": models.SparseVectorParams()},
        )
        self._chunks = {}

    def index(self, chunks: list[Chunk]):
        points = []
        for i, ch in enumerate(chunks):
            self._chunks[ch.meta.chunk_id] = ch
            points.append(models.PointStruct(
                id=i + 1,
                vector={"dense": _dense_of(ch.text), "sparse": _sparse_of(ch.text)},
                payload={"chunk_id": ch.meta.chunk_id, "acl_tags": ch.meta.acl_tags},
            ))
        self.client.upsert(self.coll, points=points)

    def _acl_filter(self, auth: AuthContext):
        return models.Filter(must=[models.FieldCondition(
            key="acl_tags", match=models.MatchAny(any=list(auth.acl_tags)))])

    def retrieve(self, query, auth: AuthContext, k=10, mode="hybrid") -> list[ScoredChunk]:
        flt = self._acl_filter(auth)
        if mode == "dense":
            res = self.client.query_points(self.coll, query=_dense_of(query),
                using="dense", query_filter=flt, limit=k).points
        elif mode == "sparse":
            res = self.client.query_points(self.coll, query=_sparse_of(query),
                using="sparse", query_filter=flt, limit=k).points
        else:
            res = self.client.query_points(self.coll, prefetch=[
                models.Prefetch(query=_dense_of(query), using="dense", filter=flt, limit=k*2),
                models.Prefetch(query=_sparse_of(query), using="sparse", filter=flt, limit=k*2),
            ], query=models.FusionQuery(fusion=models.Fusion.RRF), limit=k).points
        return [ScoredChunk(chunk=self._chunks[p.payload["chunk_id"]], score=p.score or 0.0)
                for p in res]
