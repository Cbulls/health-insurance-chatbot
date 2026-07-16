"""
Qdrant 저장소 어댑터(MVP) — 인덱싱 + dense 검색을 통합 구현.

CTO 리뷰 반영(원 qdrant_store.py 대비 수정):
  - 원문/상태를 프로세스 인메모리 dict에 두던 것을 제거 → 청크 원문·메타를 Qdrant
    payload에 저장하고 검색 시 payload에서 복원(재시작 내구성·수평확장 가능).
  - ACL을 조직 태그 전체가 아니라 MVP owner 태그(acl_tags MatchAny)로 pre-filter.
  - 포인트 id를 chunk_id 기반 결정적 UUID로 → 재인덱싱 시 멱등 upsert.
  - 쿼리 임베딩을 적재와 '동일 모델'로 통일(더미 규칙 제거).

Phase 2: 하이브리드(dense+sparse)+RRF, 버전 태깅/원자적 전환/GC, 결정적 sparse 해시.

무료 사양(Qdrant Cloud free: 1GiB RAM, 4GiB disk, 0.5 vCPU) 대응:
  - 원격 모드 컬렉션은 벡터·HNSW·payload를 디스크에 두고(int8 양자화본만 RAM),
    세그먼트 수를 최소화해 1GiB RAM·0.5 vCPU에서 안정 동작하게 한다.
  - 디스크 예산(disk_budget_mb)으로 max_points를 역산해, 한도를 넘길 인덱싱은
    CapacityExceededError로 거부한다(fail-closed — 클러스터가 꽉 차기 전에 차단).
"""
from __future__ import annotations

import logging
import threading
import uuid
from contextlib import nullcontext

from qdrant_client import QdrantClient, models

from harag.contracts.boundaries import (
    Chunk, EmbeddedChunk, ScoredChunk, AuthContext,
)
from harag.schemas.chunk import ChunkMetadata

logger = logging.getLogger("harag.qdrant")

_NAMESPACE = uuid.UUID("6ba7b810-9dad-11d1-80b4-00c04fd430c8")  # 청크 id → 결정적 point id

# Qdrant 공식 용량 산정: 벡터 크기 × 1.5 (인덱스·보조 구조 오버헤드)
_VECTOR_OVERHEAD = 1.5
_WARN_THRESHOLD = 0.8  # 공식 모니터링의 디스크 80% 경고선과 동일


class CapacityExceededError(Exception):
    """디스크 예산 초과 — 인덱싱 거부(무료 사양 한도 보호)."""


class QdrantVectorStore:
    def __init__(self, embedding_model, dim: int, collection: str = "harag_pdf_mvp",
                 url: str | None = None, api_key: str | None = None,
                 disk_budget_mb: int = 0, payload_bytes_per_point: int = 2048):
        # url 없으면 인메모리(개발). 운영은 url로 실제 클러스터(Cloud는 api_key 필수).
        self._remote = bool(url)
        self.client = (QdrantClient(url=url, api_key=api_key or None)
                       if url else QdrantClient(":memory:"))
        self.coll = collection
        self._dim = dim
        self._embed = embedding_model
        self._max_points = self._compute_max_points(
            disk_budget_mb, dim, payload_bytes_per_point)
        # 쿼리 오프로드(to_thread)+인제스트 스레드풀로 client가 여러 스레드에서
        # 동시 호출됨. 로컬(:memory:) 클라이언트만 스레드 안전 보장이 없어 직렬화.
        # 원격(HTTP) 클라이언트는 스레드 안전이므로 락 없이 병렬 검색을 허용한다
        # (전역 락은 모든 검색을 직렬화해 동시성 병목이 된다).
        self._lock = threading.Lock() if not url else nullcontext()
        self._ensure_collection()
        if self._max_points > 0:
            logger.info("capacity guard: disk_budget=%dMB dim=%d → max_points=%d",
                        disk_budget_mb, dim, self._max_points)

    @staticmethod
    def _compute_max_points(budget_mb: int, dim: int,
                            payload_bytes: int) -> int:
        """디스크 예산(MB) → 저장 가능한 최대 포인트 수 역산.

        포인트당 비용 = dim × 4바이트 × 1.5(인덱스 오버헤드) + payload 예상치.
        budget_mb <= 0 이면 가드 비활성(0 반환)."""
        if budget_mb <= 0:
            return 0
        per_point = dim * 4 * _VECTOR_OVERHEAD + payload_bytes
        # 예산이 있는데 역산이 0이면 가드가 꺼진 것으로 오인되므로 최소 1 보장
        return max(1, int(budget_mb * 1024 * 1024 / per_point))

    def _ensure_collection(self) -> None:
        if self.client.collection_exists(self.coll):
            self._verify_existing_dim()
            return
        if self._remote:
            # 저자원(무료 사양) 구성: 원본 벡터·HNSW 그래프·payload는 디스크(mmap),
            # RAM에는 int8 양자화본만 상주(4배 압축) → 1GiB RAM에서 안정 동작.
            # 주의: 이 설정은 '컬렉션 생성 시'에만 적용된다. 기존 컬렉션에 적용하려면
            # 삭제 후 재인덱싱이 필요하다.
            self.client.create_collection(
                collection_name=self.coll,
                vectors_config={"dense": models.VectorParams(
                    size=self._dim, distance=models.Distance.COSINE,
                    on_disk=True)},
                on_disk_payload=True,
                hnsw_config=models.HnswConfigDiff(m=16, on_disk=True),
                quantization_config=models.ScalarQuantization(
                    scalar=models.ScalarQuantizationConfig(
                        type=models.ScalarType.INT8, quantile=0.99,
                        always_ram=True)),
                # 0.5 vCPU에서 백그라운드 세그먼트 병합 부하 최소화
                optimizers_config=models.OptimizersConfigDiff(
                    default_segment_number=1),
            )
            logger.info("collection %s created (low-resource: on_disk vectors/"
                        "payload/hnsw + int8 quantization)", self.coll)
        else:
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

    def _verify_existing_dim(self) -> None:
        """기존 컬렉션의 dense 차원이 설정과 다르면 즉시 실패(fail-fast).

        불일치를 방치하면 upsert 시점에야 암호 같은 오류로 실패한다.
        임베딩 모델/차원을 바꿨다면 컬렉션을 지우고 재인덱싱해야 한다."""
        try:
            info = self.client.get_collection(self.coll)
            params = info.config.params.vectors
            dense = params.get("dense") if isinstance(params, dict) else params
            existing = getattr(dense, "size", None)
        except Exception:  # noqa: BLE001 — 스키마 조회 실패 시 검증 생략(구버전 호환)
            return
        if existing is not None and existing != self._dim:
            raise RuntimeError(
                f"컬렉션 '{self.coll}'의 벡터 차원({existing})이 설정된 "
                f"EMBEDDING_DIM({self._dim})과 다릅니다. 임베딩 모델을 바꿨다면 "
                f"컬렉션을 삭제 후 재인덱싱하거나, EMBEDDING_DIM을 {existing}으로 "
                f"되돌리세요.")

    # ── 용량 가드 ──
    def count_total(self) -> int:
        with self._lock:
            return self.client.count(self.coll, exact=True).count

    def ensure_capacity_for(self, n_new: int) -> None:
        """n_new 포인트를 추가해도 예산 안인지 검사. 초과면 fail-closed.

        임베딩(비용이 드는 외부 API) 호출 '전'에 부르면 낭비도 막는다."""
        if self._max_points <= 0:
            return
        current = self.count_total()
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
        """현재 사용량 요약(관측용). 가드 비활성/조회 실패 시 None."""
        if self._max_points <= 0:
            return None
        try:
            current = self.count_total()
        except Exception:  # noqa: BLE001 — 관측 실패가 헬스체크를 죽이면 안 됨
            return None
        return {
            "points": current,
            "max_points": self._max_points,
            "used_pct": round(current * 100 / self._max_points, 1),
        }

    # ── 적재 ──
    def index(self, embedded: list[EmbeddedChunk]) -> int:
        # 최후 방어선 — 인제스트가 사전 검사를 안 거쳐도 예산을 넘기지 않는다.
        self.ensure_capacity_for(len(embedded))
        points = []
        for ec in embedded:
            meta = ec.chunk.meta
            points.append(models.PointStruct(
                # chunk_id만 쓰면 같은 PDF를 다른 owner가 올릴 때 포인트가
                # 덮어써져 ACL이 바뀐다. owner 태그를 id에 넣어 격리·멱등을 유지.
                id=str(uuid.uuid5(
                    _NAMESPACE,
                    f"{meta.chunk_id}|{','.join(sorted(meta.acl_tags))}")),
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
