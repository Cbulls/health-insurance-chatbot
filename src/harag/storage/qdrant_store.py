"""
Qdrant 저장소 어댑터 — worker의 store 인터페이스 + 검색을 통합 구현.

실제 Qdrant 클러스터에 연결(QdrantClient(url=...)). 검증은 인메모리(:memory:)로 —
API가 동일해 진짜 검증이 된다.

빈 창 없는 버전 전환(B-3):
  - 각 포인트 payload에 version 기록
  - doc별 '활성 버전' 포인터를 따로 관리
  - 검색 결과를 활성 버전으로 후처리 필터 → 활성화 전엔 옛 버전만 노출
  - activate는 포인터만 원자적으로 바꿈(데이터 이동 없음 = 빈 창 없음)

ACL: payload의 acl_tags에 MatchAny 필터(검색 시점 pre-filter).
증분: content_hash payload로 기존 임베딩 조회.

CTO 리뷰 반영(원 설계 qdrant_store.py 대비 수정):
  1. 비결정적 sparse 해시 `abs(hash(t))`(프로세스별 랜덤) → 결정적 `hash_str`(blake2b).
     쿼리·적재가 같은 규칙을 써야 sparse 매칭이 재현 가능(프로세스 무관).
  2. 원문/메타를 프로세스 인메모리 dict(self._chunks)에 두던 것을 제거 →
     청크 text·메타를 payload에 저장하고 retrieve()에서 복원(재시작 내구성·수평확장).
  3. retrieve() dense 전용 → prefetch(dense+sparse) + FusionQuery(RRF)로 실제 하이브리드.
     mode 인자(dense/sparse/hybrid)를 존중한다.

주의: self._active(활성 버전 포인터)는 설계 구조상 인메모리로 남기되, 진실 원천은
metadata_store(PG)의 documents.active_version이다. 완전 영속화는 Phase 2 배선.
self._emb_by_hash(증분 재사용 풀)는 프로세스 내 캐시 — 재시작 시 payload에서
재구성 가능(Phase 2). 지금은 워커 프로세스 수명 동안만 유지.
"""
from __future__ import annotations

from qdrant_client import QdrantClient, models

from harag.contracts.boundaries import Chunk, EmbeddedChunk, ScoredChunk, AuthContext
from harag.schemas.chunk import ChunkMetadata
from harag.embedding.api_embedder import hash_str


def _sparse_index(term: str) -> int:
    """결정적 sparse 인덱스 — abs(hash(t)) 대체(프로세스 무관, blake2b 기반)."""
    return hash_str(term) % (2 ** 31)


class QdrantVectorStore:
    def __init__(self, dim: int, collection: str = "docs", url: str | None = None):
        # url 없으면 인메모리(개발·테스트). 운영은 url로 실제 클러스터.
        self.client = QdrantClient(url) if url else QdrantClient(":memory:")
        self.coll = collection
        self._dim = dim
        self._active: dict[str, int] = {}      # doc_id -> 활성 버전(진실원천=metadata_store)
        self._point_id = 0
        self._emb_by_hash: dict[str, EmbeddedChunk] = {}  # 증분 재사용 풀(프로세스 캐시)
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
            self._emb_by_hash[ec.chunk.meta.content_hash] = ec
            # sparse_terms(형태소→가중치)를 Qdrant sparse 벡터로.
            # 결정적 해시로 인덱싱 → 쿼리 시 같은 규칙으로 재현(수정 1).
            terms = list(ec.sparse_terms.keys())
            idx = [_sparse_index(t) for t in terms]
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
                    # 원문·메타를 payload에 저장(수정 2) → 재시작 내구성·수평확장
                    "text": ec.chunk.text,
                    "meta": ec.chunk.meta.model_dump(mode="json"),
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

    # ── 검색(하이브리드 RRF + 활성 버전 + ACL) ──
    def _acl_filter(self, auth: AuthContext) -> models.Filter:
        return models.Filter(must=[models.FieldCondition(
            key="acl_tags", match=models.MatchAny(any=list(auth.acl_tags)))])

    def retrieve(self, query: str, auth: AuthContext, k: int = 10,
                 mode: str = "hybrid") -> list[ScoredChunk]:
        flt = self._acl_filter(auth)
        dense = self._dense_of(query)
        sparse = self._sparse_of(query)
        # 활성 버전 후처리를 위해 넉넉히 회수(엔진 호환 안전책)
        limit = k * 4

        if mode == "dense" or not sparse.indices:
            res = self.client.query_points(
                self.coll, query=dense, using="dense", query_filter=flt,
                limit=limit, with_payload=True).points
        elif mode == "sparse":
            res = self.client.query_points(
                self.coll, query=sparse, using="sparse", query_filter=flt,
                limit=limit, with_payload=True).points
        else:
            # 실제 하이브리드: dense+sparse 후보를 RRF로 융합(수정 3)
            res = self.client.query_points(
                self.coll,
                prefetch=[
                    models.Prefetch(query=dense, using="dense", filter=flt, limit=limit),
                    models.Prefetch(query=sparse, using="sparse", filter=flt, limit=limit),
                ],
                query=models.FusionQuery(fusion=models.Fusion.RRF),
                limit=limit, with_payload=True).points

        out: list[ScoredChunk] = []
        for p in res:
            pl = p.payload or {}
            # 활성 버전만 통과(빈 창/조기 노출 방지)
            if self._active.get(pl.get("doc_id")) != pl.get("version"):
                continue
            meta_dict = pl.get("meta")
            if not meta_dict:
                continue
            # payload에서 원문·메타 복원(수정 2)
            chunk = Chunk(meta=ChunkMetadata(**meta_dict), text=pl.get("text", ""))
            out.append(ScoredChunk(chunk=chunk, score=float(p.score or 0.0)))
            if len(out) >= k:
                break
        return out

    def _dense_of(self, text: str) -> list[float]:
        # 검색 질의 임베딩은 실제론 임베딩 서버 호출(적재와 동일 모델).
        # 이 라이브러리 store는 모델을 주입받지 않으므로 더미 규칙을 쓰되,
        # 의미 매칭은 sparse(결정적 해시)가 담당한다. 운영 hot path는
        # retrieval/qdrant_store.py(실모델 dense)를 사용.
        return [float(len(text) % 5)] * self._dim

    def _sparse_of(self, text: str) -> models.SparseVector:
        # 적재와 동일한 결정적 해시 규칙으로 쿼리 sparse 구성(재현성).
        terms = [t for t in text.split() if t]
        seen: dict[int, float] = {}
        for t in terms:
            seen[_sparse_index(t)] = 1.0
        return models.SparseVector(indices=list(seen.keys()),
                                   values=list(seen.values()))
