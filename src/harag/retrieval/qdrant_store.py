"""
Qdrant 저장소 어댑터(MVP) — 인덱싱 + dense 검색을 통합 구현.

CTO 리뷰 반영(원 qdrant_store.py 대비 수정):
  - 원문/상태를 프로세스 인메모리 dict에 두던 것을 제거 → 청크 원문·메타를 Qdrant
    payload에 저장하고 검색 시 payload에서 복원(재시작 내구성·수평확장 가능).
  - ACL을 조직 태그 전체가 아니라 MVP owner 태그(acl_tags MatchAny)로 pre-filter.
  - 포인트 id를 chunk_id 기반 결정적 UUID로 → 재인덱싱 시 멱등 upsert.
  - 쿼리 임베딩을 적재와 '동일 모델'로 통일(더미 규칙 제거).

Phase 2: 하이브리드(dense+sparse)+RRF, 버전 태깅/원자적 전환/GC, 결정적 sparse 해시.
"""
from __future__ import annotations

import threading
import uuid

from qdrant_client import QdrantClient, models

from harag.contracts.boundaries import (
    Chunk, EmbeddedChunk, ScoredChunk, AuthContext,
)
from harag.schemas.chunk import ChunkMetadata

_NAMESPACE = uuid.UUID("6ba7b810-9dad-11d1-80b4-00c04fd430c8")  # 청크 id → 결정적 point id


class QdrantVectorStore:
    def __init__(self, embedding_model, dim: int, collection: str = "harag_pdf_mvp",
                 url: str | None = None):
        # url 없으면 인메모리(개발). 운영은 url로 실제 클러스터.
        self.client = QdrantClient(url=url) if url else QdrantClient(":memory:")
        self.coll = collection
        self._dim = dim
        self._embed = embedding_model
        # 쿼리 오프로드(to_thread)+인제스트 스레드풀로 client가 여러 스레드에서
        # 동시 호출됨. 로컬(:memory:) 클라이언트는 스레드 안전 보장이 없어 직렬화.
        self._lock = threading.Lock()
        self._ensure_collection()

    def _ensure_collection(self) -> None:
        if self.client.collection_exists(self.coll):
            return
        self.client.create_collection(
            collection_name=self.coll,
            vectors_config={"dense": models.VectorParams(
                size=self._dim, distance=models.Distance.COSINE)},
        )
        # owner/ACL 필터 성능·정확도용 keyword 인덱스
        try:
            self.client.create_payload_index(
                self.coll, "acl_tags", models.PayloadSchemaType.KEYWORD)
            self.client.create_payload_index(
                self.coll, "document_id", models.PayloadSchemaType.KEYWORD)
        except Exception:  # noqa: BLE001 — 인덱스 없어도 필터는 동작
            pass

    # ── 적재 ──
    def index(self, embedded: list[EmbeddedChunk]) -> int:
        points = []
        for ec in embedded:
            meta = ec.chunk.meta
            points.append(models.PointStruct(
                id=str(uuid.uuid5(_NAMESPACE, meta.chunk_id)),  # 멱등
                vector={"dense": ec.dense_vector},
                payload={
                    "chunk_id": meta.chunk_id,
                    "document_id": meta.document_id,
                    "acl_tags": list(meta.acl_tags),
                    "text": ec.chunk.text,
                    "meta": meta.model_dump(mode="json"),  # 원문 메타 보존(재구성용)
                },
            ))
        if points:
            with self._lock:
                self.client.upsert(self.coll, points=points)
        return len(points)

    # ── 검색(dense + owner/ACL pre-filter) ──
    def retrieve(self, query: str, auth: AuthContext, k: int = 10) -> list[ScoredChunk]:
        dense = self._embed.encode([query])[0]
        flt = models.Filter(must=[models.FieldCondition(
            key="acl_tags", match=models.MatchAny(any=list(auth.acl_tags)))])
        with self._lock:
            res = self.client.query_points(
                collection_name=self.coll, query=dense, using="dense",
                query_filter=flt, limit=k, with_payload=True,
            ).points

        out: list[ScoredChunk] = []
        for p in res:
            pl = p.payload or {}
            meta_dict = pl.get("meta")
            text = pl.get("text", "")
            if not meta_dict:
                continue
            chunk = Chunk(meta=ChunkMetadata(**meta_dict), text=text)
            out.append(ScoredChunk(chunk=chunk, score=float(p.score or 0.0)))
        return out

    def count_for_owner(self, acl_tags: list[str]) -> int:
        flt = models.Filter(must=[models.FieldCondition(
            key="acl_tags", match=models.MatchAny(any=acl_tags))])
        with self._lock:
            return self.client.count(self.coll, count_filter=flt, exact=True).count
