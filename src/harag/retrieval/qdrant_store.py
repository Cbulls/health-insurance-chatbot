"""
Qdrant 저장소 어댑터(MVP) — 인덱싱 + dense/hybrid 검색.

효율화(호출·저장):
  - payload 스키마 v2: 슬림 필드만 저장(구버전 meta dump는 읽기 호환)
  - 문서 단위 replace(delete→upsert)로 재청킹 고아 포인트 GC
  - chunked upsert + 마지막 배치 wait
  - approximate count + TTL 캐시(임계 근접 시 exact)
  - 하이브리드 prefetch 배수·with_payload Include 튜닝
  - 기존 컬렉션에도 payload 인덱스 idempotent 보장

무료 사양(Qdrant Cloud free: 1GiB RAM, 4GiB disk) 대응:
  - 원격: on_disk 벡터·HNSW·payload + int8 RAM
  - disk_budget_mb로 max_points 역산(sparse·세그먼트 보정 포함)
"""
from __future__ import annotations

import logging
import threading
import time
import uuid
from contextlib import nullcontext
from datetime import datetime, timezone
from typing import Any

from qdrant_client import QdrantClient, models

from harag.contracts.boundaries import (
    Chunk, EmbeddedChunk, ScoredChunk, AuthContext,
)
from harag.embedding.api_embedder import hash_str, SimpleMorph
from harag.schemas.chunk import ChunkMetadata, ChunkType

logger = logging.getLogger("harag.qdrant")

_NAMESPACE = uuid.UUID("6ba7b810-9dad-11d1-80b4-00c04fd430c8")

_VECTOR_OVERHEAD = 1.5
_WARN_THRESHOLD = 0.8
_EXACT_RECHECK_RATIO = 0.9  # 이 비율 이상이면 capacity를 exact로 재확인
_DEFAULT_SPARSE_BYTES = 640  # ~80 terms × 8B
_DEFAULT_SEGMENT_FACTOR = 1.15
_DEFAULT_UPSERT_BATCH = 64
_DEFAULT_COUNT_TTL_S = 5.0
_PAYLOAD_SCHEMA_V2 = "v2"

# 검색 시 와이어로 받을 슬림 키(+구버전 meta)
_PAYLOAD_INCLUDE = [
    "schema", "chunk_id", "document_id", "acl_tags", "text",
    "citation_label", "source_document", "content_hash", "struct_path",
    "page_ref", "chunk_type", "source_block_ids", "security_level",
    "embedding_model_id", "parsing_version", "chunking_version",
    "created_at", "meta",
]


def _sparse_index(term: str) -> int:
    return hash_str(term) % (2 ** 31)


def _to_sparse_vector(terms: dict[str, float]) -> models.SparseVector:
    if not terms:
        return models.SparseVector(indices=[], values=[])
    indices, values = [], []
    for term, weight in terms.items():
        indices.append(_sparse_index(term))
        values.append(float(weight))
    return models.SparseVector(indices=indices, values=values)


def build_payload_v2(ec: EmbeddedChunk) -> dict[str, Any]:
    """슬림 payload — 풀 meta.model_dump 중복 제거."""
    meta = ec.chunk.meta
    return {
        "schema": _PAYLOAD_SCHEMA_V2,
        "chunk_id": meta.chunk_id,
        "document_id": meta.document_id,
        "acl_tags": list(meta.acl_tags),
        "text": ec.chunk.text,
        "citation_label": meta.citation_label,
        "source_document": meta.source_document,
        "content_hash": meta.content_hash,
        "struct_path": meta.struct_path,
        "page_ref": meta.page_ref,
        "chunk_type": meta.chunk_type.value if isinstance(
            meta.chunk_type, ChunkType) else str(meta.chunk_type),
        "source_block_ids": list(meta.source_block_ids),
        "security_level": meta.security_level,
        "embedding_model_id": meta.embedding_model_id,
        "parsing_version": meta.parsing_version,
        "chunking_version": meta.chunking_version,
        "created_at": meta.created_at.isoformat(),
    }


def chunk_from_payload(pl: dict[str, Any]) -> Chunk | None:
    """payload → Chunk. v1(meta dump) / v2(슬림) 모두 지원."""
    text = pl.get("text", "") or ""
    meta_dict = pl.get("meta")
    if isinstance(meta_dict, dict) and meta_dict.get("chunk_id"):
        try:
            return Chunk(meta=ChunkMetadata(**meta_dict), text=text)
        except Exception:  # noqa: BLE001
            logger.debug("v1 meta restore failed", exc_info=True)

    chunk_id = pl.get("chunk_id")
    document_id = pl.get("document_id")
    acl = pl.get("acl_tags") or []
    if not chunk_id or not document_id or not acl:
        return None

    block_ids = pl.get("source_block_ids") or ["payload-v2"]
    ctype_raw = pl.get("chunk_type") or "body"
    try:
        ctype = ChunkType(ctype_raw)
    except ValueError:
        ctype = ChunkType.body

    created_raw = pl.get("created_at")
    if isinstance(created_raw, str) and created_raw:
        try:
            created = datetime.fromisoformat(created_raw.replace("Z", "+00:00"))
        except ValueError:
            created = datetime.now(timezone.utc)
    else:
        created = datetime.now(timezone.utc)

    meta = ChunkMetadata(
        chunk_id=str(chunk_id),
        document_id=str(document_id),
        source_block_ids=list(block_ids),
        struct_path=str(pl.get("struct_path") or ""),
        chunk_type=ctype,
        page_ref=pl.get("page_ref"),
        security_level=str(pl.get("security_level") or "internal"),
        acl_tags=list(acl),
        source_document=str(pl.get("source_document") or ""),
        citation_label=str(pl.get("citation_label") or chunk_id),
        parsing_version=str(pl.get("parsing_version") or "v2"),
        chunking_version=str(pl.get("chunking_version") or "v2"),
        embedding_model_id=str(pl.get("embedding_model_id") or "unknown"),
        content_hash=str(pl.get("content_hash") or chunk_id),
        created_at=created,
    )
    return Chunk(meta=meta, text=text)


class CapacityExceededError(Exception):
    """디스크 예산 초과 — 인덱싱 거부(무료 사양 한도 보호)."""


class QdrantVectorStore:
    def __init__(
        self,
        embedding_model,
        dim: int,
        collection: str = "harag_pdf_mvp",
        url: str | None = None,
        api_key: str | None = None,
        disk_budget_mb: int = 0,
        payload_bytes_per_point: int = 1536,
        morph=None,
        hybrid: bool = True,
        sparse_bytes_per_point: int = _DEFAULT_SPARSE_BYTES,
        segment_factor: float = _DEFAULT_SEGMENT_FACTOR,
        upsert_batch_size: int = _DEFAULT_UPSERT_BATCH,
        hybrid_prefetch_mult: int = 2,
        count_cache_ttl_s: float = _DEFAULT_COUNT_TTL_S,
    ):
        self._remote = bool(url)
        self.client = (QdrantClient(url=url, api_key=api_key or None)
                       if url else QdrantClient(":memory:"))
        self.coll = collection
        self._dim = dim
        self._embed = embedding_model
        self._morph = morph or SimpleMorph()
        self._want_hybrid = hybrid
        self._hybrid = False
        self._sparse_bytes = max(0, sparse_bytes_per_point) if hybrid else 0
        self._segment_factor = max(1.0, segment_factor)
        self._payload_bytes = payload_bytes_per_point
        self._upsert_batch = max(1, upsert_batch_size)
        self._prefetch_mult = max(1, hybrid_prefetch_mult)
        self._count_ttl = max(0.0, count_cache_ttl_s)
        self._per_point = self._estimate_per_point(
            dim, payload_bytes_per_point, self._sparse_bytes, self._segment_factor)
        self._max_points = self._compute_max_points(
            disk_budget_mb, dim, payload_bytes_per_point,
            sparse_bytes=self._sparse_bytes,
            segment_factor=self._segment_factor,
        )
        self._lock = threading.Lock() if not url else nullcontext()
        self._count_cache: tuple[float, int] | None = None  # (mono_ts, count)
        self._count_lock = threading.Lock()
        self._ensure_collection()
        if self._max_points > 0:
            logger.info(
                "capacity guard: disk_budget=%dMB dim=%d per_point≈%d → max_points=%d",
                disk_budget_mb, dim, int(self._per_point), self._max_points)
        logger.info("search mode: %s (collection=%s prefetch_mult=%d)",
                    "hybrid(dense+sparse RRF)" if self._hybrid else "dense-only",
                    self.coll, self._prefetch_mult)

    @staticmethod
    def _estimate_per_point(dim: int, payload_bytes: int,
                            sparse_bytes: int, segment_factor: float) -> float:
        raw = dim * 4 * _VECTOR_OVERHEAD + sparse_bytes + payload_bytes
        return raw * segment_factor

    @staticmethod
    def _compute_max_points(
        budget_mb: int,
        dim: int,
        payload_bytes: int,
        sparse_bytes: int = _DEFAULT_SPARSE_BYTES,
        segment_factor: float = _DEFAULT_SEGMENT_FACTOR,
    ) -> int:
        """디스크 예산(MB) → 최대 포인트 수(sparse·세그먼트 보정 포함)."""
        if budget_mb <= 0:
            return 0
        per_point = QdrantVectorStore._estimate_per_point(
            dim, payload_bytes, sparse_bytes, segment_factor)
        return max(1, int(budget_mb * 1024 * 1024 / per_point))

    def _ensure_payload_indexes(self) -> None:
        """신규·기존 컬렉션 모두 keyword 인덱스 보장(idempotent)."""
        for field in ("acl_tags", "document_id"):
            try:
                self.client.create_payload_index(
                    self.coll, field, models.PayloadSchemaType.KEYWORD)
            except Exception:  # noqa: BLE001
                pass

    def _ensure_collection(self) -> None:
        if self.client.collection_exists(self.coll):
            self._verify_existing_dim()
            self._hybrid = self._want_hybrid and self._collection_has_sparse()
            if self._want_hybrid and not self._hybrid:
                logger.warning(
                    "컬렉션 '%s'에 sparse 벡터가 없어 dense 전용으로 동작합니다. "
                    "하이브리드를 쓰려면 컬렉션을 삭제하거나 QDRANT_COLLECTION을 "
                    "새 이름으로 바꾼 뒤 재인덱싱하세요.", self.coll)
            self._ensure_payload_indexes()
            return

        dense_params = models.VectorParams(
            size=self._dim, distance=models.Distance.COSINE,
            **({"on_disk": True} if self._remote else {}))
        create_kwargs: dict = {
            "collection_name": self.coll,
            "vectors_config": {"dense": dense_params},
        }
        if self._want_hybrid:
            # IDF modifier: 흔한 토큰(조사·일반어)의 기여를 낮춰 실질 BM25에
            # 근접시킨다. 서버가 문서 빈도를 집계하므로 클라이언트는 TF만 보낸다.
            create_kwargs["sparse_vectors_config"] = {
                "sparse": models.SparseVectorParams(
                    modifier=models.Modifier.IDF)}

        if self._remote:
            create_kwargs.update(
                on_disk_payload=True,
                hnsw_config=models.HnswConfigDiff(m=16, on_disk=True),
                quantization_config=models.ScalarQuantization(
                    scalar=models.ScalarQuantizationConfig(
                        type=models.ScalarType.INT8, quantile=0.99,
                        always_ram=True)),
                optimizers_config=models.OptimizersConfigDiff(
                    default_segment_number=1),
            )
            self.client.create_collection(**create_kwargs)
            logger.info("collection %s created (low-resource + %s)",
                        self.coll,
                        "hybrid sparse" if self._want_hybrid else "dense-only")
        else:
            self.client.create_collection(**create_kwargs)

        self._hybrid = self._want_hybrid
        self._ensure_payload_indexes()

    def _collection_has_sparse(self) -> bool:
        try:
            info = self.client.get_collection(self.coll)
            sparse = getattr(info.config.params, "sparse_vectors", None)
            if sparse is None:
                return False
            if isinstance(sparse, dict):
                return "sparse" in sparse
            return bool(sparse)
        except Exception:  # noqa: BLE001
            return False

    def _verify_existing_dim(self) -> None:
        try:
            info = self.client.get_collection(self.coll)
            params = info.config.params.vectors
            dense = params.get("dense") if isinstance(params, dict) else params
            existing = getattr(dense, "size", None)
        except Exception:  # noqa: BLE001
            return
        if existing is not None and existing != self._dim:
            raise RuntimeError(
                f"컬렉션 '{self.coll}'의 벡터 차원({existing})이 설정된 "
                f"EMBEDDING_DIM({self._dim})과 다릅니다. 임베딩 모델을 바꿨다면 "
                f"컬렉션을 삭제 후 재인덱싱하거나, EMBEDDING_DIM을 {existing}으로 "
                f"되돌리세요.")

    def _invalidate_count_cache(self) -> None:
        with self._count_lock:
            self._count_cache = None

    def count_total(self, *, exact: bool = False) -> int:
        """전체 포인트 수. 평상시는 approximate + TTL 캐시."""
        now = time.monotonic()
        if not exact and self._count_ttl > 0:
            with self._count_lock:
                if self._count_cache is not None:
                    ts, cached = self._count_cache
                    if now - ts < self._count_ttl:
                        return cached
        with self._lock:
            n = int(self.client.count(self.coll, exact=exact).count)
        with self._count_lock:
            self._count_cache = (time.monotonic(), n)
        return n

    def ensure_capacity_for(self, n_new: int) -> None:
        if self._max_points <= 0:
            return
        current = self.count_total(exact=False)
        projected = current + n_new
        # 임계 근접 시에만 exact 재확인(근사치 오차 방어)
        if projected >= self._max_points * _EXACT_RECHECK_RATIO:
            current = self.count_total(exact=True)
            projected = current + n_new
        if projected > self._max_points:
            raise CapacityExceededError(
                f"저장 한도 초과: {current}+{n_new} > {self._max_points} points "
                f"— 기존 문서를 삭제하거나 예산(QDRANT_DISK_BUDGET_MB)을 조정하세요")
        if projected >= self._max_points * _WARN_THRESHOLD:
            logger.warning(
                "capacity %d%% (%d/%d points) — 무료 사양 한도에 근접",
                int(projected * 100 / self._max_points), projected,
                self._max_points)

    def capacity_status(self) -> dict | None:
        if self._max_points <= 0:
            return None
        try:
            current = self.count_total(exact=False)
        except Exception:  # noqa: BLE001
            return None
        return {
            "points": current,
            "max_points": self._max_points,
            "used_pct": round(current * 100 / self._max_points, 1),
            "hybrid": self._hybrid,
            "approx": True,
            "per_point_estimate": int(self._per_point),
            "payload_schema": _PAYLOAD_SCHEMA_V2,
        }

    def _point_struct(self, ec: EmbeddedChunk) -> models.PointStruct:
        meta = ec.chunk.meta
        vector: dict = {"dense": ec.dense_vector}
        if self._hybrid:
            vector["sparse"] = _to_sparse_vector(ec.sparse_terms or {})
        return models.PointStruct(
            id=str(uuid.uuid5(
                _NAMESPACE,
                f"{meta.chunk_id}|{','.join(sorted(meta.acl_tags))}")),
            vector=vector,
            payload=build_payload_v2(ec),
        )

    def index(
        self,
        embedded: list[EmbeddedChunk],
        *,
        skip_capacity_check: bool = False,
        replace_document: bool = False,
    ) -> int:
        """임베딩 청크 적재.

        replace_document=True면 첫 청크의 document_id+ACL로 기존 포인트를
        지운 뒤 upsert(재청킹 고아 GC). skip_capacity_check는 ingest 사전검사 후.
        """
        if not embedded:
            return 0
        if not skip_capacity_check:
            self.ensure_capacity_for(len(embedded))

        if replace_document:
            meta0 = embedded[0].chunk.meta
            self.delete_document(meta0.document_id, list(meta0.acl_tags))

        points = [self._point_struct(ec) for ec in embedded]
        # 샘플 직렬화 크기(관측) — 첫 포인트 payload 대략
        if points:
            sample_n = len(str(points[0].payload).encode("utf-8"))
            logger.info(
                "index upsert n=%d batch=%d sample_payload_bytes≈%d schema=%s",
                len(points), self._upsert_batch, sample_n, _PAYLOAD_SCHEMA_V2)

        for i in range(0, len(points), self._upsert_batch):
            batch = points[i:i + self._upsert_batch]
            is_last = i + self._upsert_batch >= len(points)
            with self._lock:
                # 중간 배치는 wait=False로 인덱싱 파이프라인과 겹침(원격).
                # 인메모리 클라이언트는 wait 인자 호환을 위해 시도 후 폴백.
                try:
                    self.client.upsert(
                        self.coll, points=batch, wait=is_last or not self._remote)
                except TypeError:
                    self.client.upsert(self.coll, points=batch)
        self._invalidate_count_cache()
        return len(points)

    def delete_document(self, document_id: str, acl_tags: list[str]) -> int:
        flt = models.Filter(must=[
            models.FieldCondition(
                key="document_id", match=models.MatchValue(value=document_id)),
            models.FieldCondition(
                key="acl_tags", match=models.MatchAny(any=list(acl_tags))),
        ])
        with self._lock:
            n = self.client.count(self.coll, count_filter=flt, exact=True).count
            if n > 0:
                self.client.delete(
                    collection_name=self.coll,
                    points_selector=models.FilterSelector(filter=flt),
                )
        self._invalidate_count_cache()
        logger.info("deleted document %s: %d points", document_id, n)
        return int(n)

    def _query_sparse(self, query: str) -> models.SparseVector:
        from collections import Counter
        toks = self._morph.tokens(query)
        if not toks:
            return models.SparseVector(indices=[], values=[])
        return _to_sparse_vector({t: float(c) for t, c in Counter(toks).items()})

    def _with_payload_selector(self):
        try:
            return models.PayloadSelectorInclude(include=_PAYLOAD_INCLUDE)
        except Exception:  # noqa: BLE001
            return _PAYLOAD_INCLUDE

    def retrieve(self, query: str, auth: AuthContext, k: int = 10) -> list[ScoredChunk]:
        dense = self._embed.encode([query])[0]
        flt = models.Filter(must=[models.FieldCondition(
            key="acl_tags", match=models.MatchAny(any=list(auth.acl_tags)))])
        pref = k * self._prefetch_mult
        payload_sel = self._with_payload_selector()

        with self._lock:
            if self._hybrid:
                sparse = self._query_sparse(query)
                if sparse.indices:
                    res = self.client.query_points(
                        collection_name=self.coll,
                        prefetch=[
                            models.Prefetch(
                                query=dense, using="dense", filter=flt, limit=pref),
                            models.Prefetch(
                                query=sparse, using="sparse", filter=flt, limit=pref),
                        ],
                        query=models.FusionQuery(fusion=models.Fusion.RRF),
                        limit=k, with_payload=payload_sel,
                    ).points
                else:
                    res = self.client.query_points(
                        collection_name=self.coll, query=dense, using="dense",
                        query_filter=flt, limit=k, with_payload=payload_sel,
                    ).points
            else:
                res = self.client.query_points(
                    collection_name=self.coll, query=dense, using="dense",
                    query_filter=flt, limit=k, with_payload=payload_sel,
                ).points

        out: list[ScoredChunk] = []
        for p in res:
            pl = p.payload or {}
            chunk = chunk_from_payload(pl)
            if chunk is None:
                continue
            out.append(ScoredChunk(chunk=chunk, score=float(p.score or 0.0)))
        return out

    def count_for_owner(self, acl_tags: list[str]) -> int:
        flt = models.Filter(must=[models.FieldCondition(
            key="acl_tags", match=models.MatchAny(any=acl_tags))])
        with self._lock:
            return self.client.count(self.coll, count_filter=flt, exact=True).count
