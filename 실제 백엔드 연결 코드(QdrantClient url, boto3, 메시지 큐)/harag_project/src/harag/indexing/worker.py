"""
인덱싱 워커 — 쓰기 경로 오케스트레이션(NFR-2: 읽기와 격리된 비동기 경로).

흐름: 업로드 → 파싱 → 품질 게이트 → 청킹 → 임베딩 → 벡터 적재 → 버전 전환.
실패·품질 미달은 격리(quarantine)하고 인덱싱하지 않는다(경계1: 오염 차단).
버전 전환은 기존 versioning.upsert로 — 빈 창 없는 활성화(B-3).
"""
from __future__ import annotations

import time
from dataclasses import dataclass, field
from enum import Enum

from harag.parsing.parser import StructuringParser, HwpDecoder
from harag.chunking.chunker import StructuralChunker, ChunkingContext
from harag.embedding.embedder import HybridEmbedder, EmbeddingModel, Morph
from harag.schemas.ir import ParseStatus, BlockType


class IngestStatus(str, Enum):
    indexed = "indexed"
    quarantined = "quarantined"


@dataclass
class IngestJob:
    document_id: str
    raw: bytes
    filename: str
    source_system: str
    department: str
    security_level: str
    acl_tags: list[str]


@dataclass
class IngestResult:
    document_id: str
    status: IngestStatus
    version: int = 0
    chunk_count: int = 0
    reason: str | None = None


class IndexingWorker:
    """1~4단계를 묶고 버전 전환을 호출하는 오케스트레이터.

    벡터 적재(upsert_vectors)와 버전 전환(stage/activate/gc)을 store에 위임.
    실제 store는 Qdrant 어댑터, 여기선 인터페이스로 받는다(테스트·교체 용이).
    """

    def __init__(self, decoder: HwpDecoder, model: EmbeddingModel, morph: Morph,
                 store, min_table_recovery: float = 0.0, pii_masker=None,
                 incremental: bool = False, object_store=None):
        self._parser = StructuringParser(decoder)
        self._chunker = StructuralChunker()
        self._embedder = HybridEmbedder(model, morph)
        self.store = store
        self._min_table_recovery = min_table_recovery
        self._pii_masker = pii_masker         # None이면 마스킹 생략
        self._incremental = incremental       # True면 변경분만 재임베딩
        self._object_store = object_store     # None이면 원본 보존 생략
        self._versions: dict[str, int] = {}   # doc_id -> 최신 버전(멱등 단조 증가)

    def process(self, job: IngestJob) -> IngestResult:
        # ── 1단계: 파싱 ──
        ir = self._parser.parse(
            job.raw, _format_of(job.filename),
            document_id=job.document_id, source_system=job.source_system,
            department=job.department, security_level=job.security_level,
            original_path=f"/{job.filename}")

        # 원본 보존(NFR-8 재인덱싱 전제) — 파싱 성공 여부와 무관하게 보존.
        # 파싱이 실패해도 원본이 있으면 나중에 다른 디코더로 재시도 가능.
        if self._object_store is not None:
            self._object_store.put_original(job.document_id, job.raw)

        # 경계1: 파싱 실패는 격리(인덱싱 안 함)
        if ir.parse_status == ParseStatus.failed:
            return IngestResult(job.document_id, IngestStatus.quarantined,
                                reason="parse_failed")

        # 품질 게이트: 표 복원율 임계 미달이면 격리(국면 A 임계의 런타임 적용)
        has_table = any(b.block_type == BlockType.table for b in ir.blocks)
        if has_table and ir.parse_quality.table_recovery_avg < self._min_table_recovery:
            return IngestResult(job.document_id, IngestStatus.quarantined,
                                reason=f"low_table_recovery:{ir.parse_quality.table_recovery_avg:.2f}")

        # ── PII 마스킹(적재 전 — 외부 유출 방어, SEC-03) ──
        if self._pii_masker is not None:
            ir = self._mask_pii(ir)

        # ── 2단계: 청킹 ──
        ctx = ChunkingContext(acl_tags=list(job.acl_tags), source_document=job.filename,
                              embedding_model_id="UNSET-embedding-model")
        chunks = self._chunker.chunk(ir, ctx)
        if not chunks:
            return IngestResult(job.document_id, IngestStatus.quarantined,
                                reason="no_chunks")

        # ── 3단계: 임베딩(증분이면 변경분만) ──
        embedded = self._embed_incremental(job.document_id, chunks)

        # ── 4단계: 적재 + 버전 전환(빈 창 없음) ──
        version = self._next_version(job.document_id)
        self.store.upsert_vectors(job.document_id, version, embedded)
        chunk_ids = {e.chunk.meta.chunk_id for e in embedded}
        # versioning.upsert 패턴: stage -> activate (옛 버전은 activate 전까지 온전)
        self.store.stage(job.document_id, version, chunk_ids)
        self.store.activate(job.document_id, version)

        return IngestResult(job.document_id, IngestStatus.indexed,
                            version=version, chunk_count=len(chunks))

    def _embed_incremental(self, doc_id: str, chunks):
        """증분이면 이전 버전 해시와 비교해 변경분만 임베딩, 나머지 재사용.

        정합성: 재사용 + 신규를 합쳐 '전체 청크 집합'을 반환한다.
        버전 전환은 이 전체 집합으로 원자적으로 일어나 빈 창이 없다(B-3).
        """
        # 증분 비활성 또는 store가 해시 조회를 지원 안 하면 전체 임베딩(기존 동작)
        if not self._incremental or not hasattr(self.store, "get_embeddings_by_hash"):
            return self._embedder.embed(chunks)

        hashes = [c.meta.content_hash for c in chunks]
        reusable = self.store.get_embeddings_by_hash(doc_id, hashes)  # hash -> EmbeddedChunk

        to_embed = [c for c in chunks if c.meta.content_hash not in reusable]
        newly = self._embedder.embed(to_embed) if to_embed else []
        new_by_hash = {e.chunk.meta.content_hash: e for e in newly}

        # 청크 순서대로 재사용/신규를 합쳐 전체 집합 구성(정합성)
        result = []
        for c in chunks:
            h = c.meta.content_hash
            result.append(reusable[h] if h in reusable else new_by_hash[h])
        return result

    def _mask_pii(self, ir):
        """IR 블록의 본문·표 셀 텍스트에서 PII 마스킹(적재 전)."""
        for b in ir.blocks:
            if b.text:
                masked, _ = self._pii_masker.mask(b.text)
                b.text = masked
            if b.table_content:
                for cell in b.table_content.cells:
                    if cell.text:
                        cell.text, _ = self._pii_masker.mask(cell.text)
        return ir

    def _next_version(self, doc_id: str) -> int:
        v = self._versions.get(doc_id, 0) + 1
        self._versions[doc_id] = v
        return v


def _format_of(filename: str) -> str:
    ext = filename.rsplit(".", 1)[-1].lower() if "." in filename else ""
    return {"hwp": "hwp5", "hwpx": "hwpx", "pdf": "pdf_text",
            "docx": "docx"}.get(ext, "other")
