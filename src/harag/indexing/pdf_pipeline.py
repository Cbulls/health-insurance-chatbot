"""
PDF 인제스트 공용 파이프라인 — API 인프로세스·워커가 동일 경로를 공유.

파싱 → 청킹 → 임베딩 → Qdrant 적재 → MetadataStore 상태 갱신.
"""
from __future__ import annotations

import logging
import os
from pathlib import Path
from typing import Callable

from harag.chunking.chunker import ChunkingContext
from harag.contracts.boundaries import ContractViolation, verify_boundary2
from harag.retrieval.qdrant_store import CapacityExceededError

logger = logging.getLogger("harag.ingest")

OnDocCallback = Callable[[str], None]


def _owner_tag(owner: str) -> str:
    return f"owner:{owner}"


class PdfIngestPipeline:
    def __init__(self, parser, chunker, embedder, store, metadata,
                 status_cache=None,
                 on_failed: OnDocCallback | None = None,
                 on_success: OnDocCallback | None = None):
        self._parser = parser
        self._chunker = chunker
        self._embedder = embedder
        self._store = store
        self._metadata = metadata
        self._cache = status_cache
        self._on_failed = on_failed
        self._on_success = on_success

    def process(self, document_id: str, raw: bytes, filename: str,
                owner: str) -> None:
        try:
            ir = self._parser.parse(
                raw, document_id=document_id, filename=filename)
            if ir.parse_status.value == "failed" or not ir.blocks:
                self._fail(
                    document_id, owner, filename,
                    "parse_failed (텍스트 추출 불가 — 스캔/암호 PDF일 수 있음)")
                return

            emb_model = getattr(self._embedder, "_model", None)
            ctx = ChunkingContext(
                acl_tags=[_owner_tag(owner)],
                source_document=filename,
                embedding_model_id=self._embedder.model_id,
                embedding_model=emb_model,
            )
            chunks = self._chunker.chunk(ir, ctx)
            if not chunks:
                self._fail(document_id, owner, filename,
                           "no_chunks (본문이 비어 있음)")
                return

            checker = getattr(self._store, "ensure_capacity_for", None)
            if callable(checker):
                checker(len(chunks))

            try:
                verify_boundary2(ir, chunks)
            except ContractViolation as e:
                logger.warning("boundary2 violation on %s: %s", document_id, e)

            embedded = self._embedder.embed(chunks)
            n = self._store.index(embedded)
            self._metadata.mark_ready(document_id, owner, n)
            self._cache_set(document_id, owner, filename, "ready", n, None)
            if self._on_success is not None:
                self._on_success(document_id)
            logger.info("ingested %s: %d chunks", document_id, n)
        except CapacityExceededError as e:
            logger.warning("capacity exceeded at index for %s: %s",
                           document_id, e)
            self._fail(
                document_id, owner, filename,
                "capacity_exceeded (저장 공간 한도 — 기존 문서를 삭제하세요)")
        except Exception as e:  # noqa: BLE001
            logger.exception("ingest failed: %s", document_id)
            self._fail(document_id, owner, filename,
                       f"internal_error: {type(e).__name__}")

    def process_file(self, document_id: str, spool_path: str, filename: str,
                     owner: str) -> None:
        try:
            raw = Path(spool_path).read_bytes()
        except OSError:
            logger.exception("ingest spool read failed: %s", document_id)
            self._fail(document_id, owner, filename,
                       "internal_error: spool_read_failed")
            return
        finally:
            try:
                os.unlink(spool_path)
            except OSError:
                pass
        self.process(document_id, raw, filename, owner)

    def _fail(self, document_id: str, owner: str, filename: str,
              msg: str) -> None:
        self._metadata.mark_failed(document_id, owner, msg)
        # ready/failed 전이 시 filename 유지(빈 문자열로 덮어쓰지 않음)
        keep_name = filename
        if not keep_name and self._cache is not None:
            prev = self._cache.get(document_id, owner)
            if prev:
                keep_name = prev.get("filename") or ""
        self._cache_set(document_id, owner, keep_name, "failed", 0, msg)
        if self._on_failed is not None:
            self._on_failed(document_id)

    def _cache_set(self, document_id, owner, filename, status, n_chunks, error):
        if self._cache is None:
            return
        self._cache.set(document_id, owner, {
            "document_id": document_id,
            "filename": filename,
            "owner": owner,
            "status": status,
            "n_chunks": n_chunks,
            "error": error,
        })
