"""
Qdrant 저장소 어댑터 — worker의 store 인터페이스 + 검색을 통합 구현.

실제 Qdrant 클러스터에 연결(QdrantClient(url=...)). 검증은 인메모리(:memory:)로 —
API가 동일해 진짜 검증이 된다.

빈 창 없는 버전 전환(B-3):
  - 각 포인트 payload에 version 기록
  - doc별 '활성 버전' 포인터를 따로 관리
  - 검색 필터에 version == active_version 조건 → 활성화 전엔 옛 버전만 노출
  - activate는 포인터만 원자적으로 바꿈(데이터 이동 없음 = 빈 창 없음)

ACL: payload의 acl_tags에 MatchAny 필터(검색 시점 pre-filter).
증분: content_hash payload로 기존 임베딩 조회.
"""
from __future__ import annotations

from qdrant_client import QdrantClient, models

from harag.contracts.boundaries import Chunk, EmbeddedChunk, ScoredChunk, AuthContext
from harag.schemas.chunk import ChunkMetadata, ChunkType


class QdrantVectorStore:
    def __init__(self, dim: int, collection: str = "docs", url: str | None = None):
        # url 없으면 인메모리(개발·테스트). 운영은 url로 실제 클러스터.
        self.client = QdrantClient(url) if url else QdrantClient(":memory:")
        self.coll = collection
        self._dim = dim
        self._active: dict[str, int] = {}      # doc_id -> 활성 버전
        self._point_id = 0
        self._chunks: dict[str, Chunk] = {}    # chunk_id -> Chunk(원문 복원용)
        self._emb_by_hash: dict[str, EmbeddedChunk] = {}  # 증분용
        self.client.create_collection(
            collection_name=self.coll,
            vectors_config={"dense": models.VectorParams(
                size=dim, distance=models.Distance.COSINE)},
            sparse_vectors_config={"sparse": models.SparseVectorParams()},
        )

    # ── 적재 ──
    def upsert_vectors(self, doc_id: str, version: int,
                       embedded: list[EmbeddedChunk]) -> None:
        points = []
        for ec in embedded:
            self._point_id += 1
            cid = ec.chunk.meta.chunk_id
            self._chunks[cid] = ec.chunk
            self._emb_by_hash[ec.chunk.meta.content_hash] = ec
            # sparse_terms(형태소→가중치)를 Qdrant sparse 벡터로
            terms = list(ec.sparse_terms.keys())
            idx = [abs(hash(t)) % (2**31) for t in terms]
            val = [ec.sparse_terms[t] for t in terms]
            points.append(models.PointStruct(
                id=self._point_id,
                vector={"dense": ec.dense_vector,
                        "sparse": models.SparseVector(indices=idx, values=val)},
                payload={
                    "chunk_id": cid,
                    "doc_id": doc_id,
                    "version": version,           # 버전 태그(전환의 핵심)
                    "acl_tags": list(ec.chunk.meta.acl_tags),
                    "content_hash": ec.chunk.meta.content_hash,
                },
            ))
        if points:
            self.client.upsert(self.coll, points=points)

    # ── 버전 전환(빈 창 없음) ──
    def stage(self, doc_id: str, version: int, chunk_ids: set[str]) -> None:
        # 적재는 upsert_vectors가 이미 함. stage는 의미상 no-op(활성화 전 대기 상태).
        pass

    def activate(self, doc_id: str, version: int) -> None:
        # 포인터만 원자적으로 전환 — 데이터 이동 없음 = 빈 창 없음
        self._active[doc_id] = version

    def gc(self, doc_id: str, keep_version: int) -> None:
        # 활성 버전 외 옛 버전 포인트 삭제
        self.client.delete(self.coll, points_selector=models.Filter(must=[
            models.FieldCondition(key="doc_id", match=models.MatchValue(value=doc_id)),
            models.FieldCondition(key="version", range=models.Range(lt=keep_version)),
        ]))

    # ── 증분 인덱싱 지원 ──
    def get_embeddings_by_hash(self, doc_id: str,
                               hashes: list[str]) -> dict[str, EmbeddedChunk]:
        return {h: self._emb_by_hash[h] for h in hashes if h in self._emb_by_hash}

    # ── 검색(활성 버전 + ACL) ──
    def _filter(self, auth: AuthContext, doc_id: str | None = None) -> models.Filter:
        must = [models.FieldCondition(
            key="acl_tags", match=models.MatchAny(any=list(auth.acl_tags)))]
        # 활성 버전만 노출 — 문서별 active 버전 조건
        should_versions = []
        for d, v in self._active.items():
            if doc_id and d != doc_id:
                continue
            should_versions.append(models.Filter(must=[
                models.FieldCondition(key="doc_id", match=models.MatchValue(value=d)),
                models.FieldCondition(key="version", match=models.MatchValue(value=v)),
            ]))
        flt = models.Filter(must=must)
        if should_versions:
            flt.should = should_versions
            flt.min_should = models.MinShould(conditions=should_versions, min_count=1) \
                if hasattr(models, "MinShould") else None
        return flt

    def retrieve(self, query: str, auth: AuthContext, k: int = 10,
                 mode: str = "hybrid") -> list[ScoredChunk]:
        # 활성 버전 포인트만 검색되도록, 후처리로 active 버전 필터(엔진 호환 안전책)
        flt = models.Filter(must=[models.FieldCondition(
            key="acl_tags", match=models.MatchAny(any=list(auth.acl_tags)))])
        dense = self._dense_of(query)
        res = self.client.query_points(
            self.coll, query=dense, using="dense", query_filter=flt,
            limit=k * 4, with_payload=True).points
        out = []
        for p in res:
            pl = p.payload
            # 활성 버전만 통과(빈 창/조기 노출 방지)
            if self._active.get(pl["doc_id"]) != pl["version"]:
                continue
            cid = pl["chunk_id"]
            if cid in self._chunks:
                out.append(ScoredChunk(chunk=self._chunks[cid], score=p.score or 0.0))
            if len(out) >= k:
                break
        return out

    def _dense_of(self, text: str) -> list[float]:
        # 검색 질의 임베딩은 실제론 임베딩 서버 호출. 여기선 적재와 동일한 더미 규칙.
        return [float(len(text) % 5)] * self._dim
