"""
수집(인제스트) 서비스.

  - REDIS_URL 있음: 스풀 경로를 Redis Streams에 넣고 워커가 처리.
  - REDIS_URL 없음: 전용 ThreadPoolExecutor로 인프로세스 처리(현행 MVP).

문서 상태는 MetadataStore가 진실원천, Redis DocStatusCache는 읽기 가속.
"""
from __future__ import annotations

import logging
import os
import threading
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass

from harag.indexing.pdf_pipeline import PdfIngestPipeline
from harag.storage.metadata_store import MetadataStore

logger = logging.getLogger("harag.ingest")


def _owner_tag(owner: str) -> str:
    return f"owner:{owner}"


@dataclass
class DocRecord:
    document_id: str
    filename: str
    owner: str
    status: str = "processing"   # processing | ready | failed
    n_chunks: int = 0
    error: str | None = None
    scope: str = "personal"
    department: str = ""
    collection_id: str = ""


def _from_meta(rec) -> DocRecord:
    reason = (rec.status_reason or "").strip()
    return DocRecord(
        document_id=rec.document_id,
        filename=rec.filename,
        owner=rec.uploaded_by,
        status=rec.status,
        n_chunks=rec.n_chunks,
        error=reason or None,
        scope=getattr(rec, "scope", None) or "personal",
        department=getattr(rec, "department", None) or "",
        collection_id=getattr(rec, "collection_id", None) or "",
    )


class InProcessIngest:
    def __init__(self, parser, chunker, embedder, store,
                 metadata: MetadataStore | None = None,
                 max_workers: int = 2,
                 queue=None,
                 status_cache=None,
                 pii_masker=None,
                 object_store=None,
                 version_coord=None):
        self._metadata = metadata or MetadataStore(dsn="sqlite:///:memory:")
        self._cache = status_cache
        self._queue = queue  # RedisIngestQueue | None
        on_failed = None
        if queue is not None:
            on_failed = queue.on_failed
        self._pipeline = PdfIngestPipeline(
            parser, chunker, embedder, store, self._metadata,
            status_cache=status_cache, on_failed=on_failed,
            pii_masker=pii_masker, object_store=object_store,
            version_coord=version_coord)
        self._lock = threading.Lock()
        self._executor = ThreadPoolExecutor(
            max_workers=max_workers, thread_name_prefix="harag-ingest")

    @property
    def uses_queue(self) -> bool:
        return self._queue is not None

    @property
    def pipeline(self) -> PdfIngestPipeline:
        return self._pipeline

    def submit(self, document_id: str, spool_path: str, filename: str,
               owner: str, acl_tags: list[str] | None = None,
               department: str = "") -> None:
        """스풀된 임시 파일을 큐 또는 전용 스레드풀로 위임."""
        tags = list(acl_tags) if acl_tags else [_owner_tag(owner)]
        dept = department or "self"
        if self._queue is not None:
            ok = self._queue.enqueue(
                document_id, spool_path, filename, owner,
                department=dept, acl_tags=tags)
            if not ok:
                # failed→재등록 후 in-flight 잔존이 흔한 원인 → 해제 후 1회 재시도
                self._queue.clear_inflight(document_id)
                ok = self._queue.enqueue(
                    document_id, spool_path, filename, owner,
                    department=dept, acl_tags=tags)
            if not ok:
                rec = self._metadata.get_for_owner(document_id, owner)
                # 이미 ready인 중복만 스풀 삭제. processing인데 enqueue 실패는
                # Redis 장애일 수 있어 스풀을 보존한다.
                if rec is not None and rec.status == "ready":
                    logger.info(
                        "queue duplicate %s status=ready — spool drop",
                        document_id)
                    try:
                        os.unlink(spool_path)
                    except OSError:
                        pass
                    return
                logger.warning(
                    "enqueue rejected for %s — spool kept at %s",
                    document_id, spool_path)
            return
        self._executor.submit(
            self._pipeline.process_file, document_id, spool_path,
            filename, owner, tags, department)

    def process_file(self, document_id: str, spool_path: str, filename: str,
                     owner: str, acl_tags: list[str] | None = None,
                     department: str = "") -> None:
        self._pipeline.process_file(
            document_id, spool_path, filename, owner,
            acl_tags=acl_tags, department=department)

    def process(self, document_id: str, raw: bytes, filename: str,
                owner: str, acl_tags: list[str] | None = None,
                department: str = "") -> None:
        self._pipeline.process(
            document_id, raw, filename, owner,
            acl_tags=acl_tags, department=department)

    def register(self, document_id: str, filename: str, owner: str,
                 department: str = "", scope: str = "personal",
                 collection_id: str = "") -> str:
        with self._lock:
            result = self._metadata.register_for_owner(
                document_id, filename, owner,
                department=department, scope=scope,
                collection_id=collection_id)
        if result == "accepted" and self._cache is not None:
            self._cache.set(document_id, owner, {
                "document_id": document_id, "filename": filename,
                "owner": owner, "status": "processing",
                "n_chunks": 0, "error": None,
            })
        return result

    def status(self, document_id: str, owner: str) -> DocRecord | None:
        if self._cache is not None:
            cached = self._cache.get(document_id, owner)
            if cached is not None:
                return DocRecord(
                    document_id=cached.get("document_id", document_id),
                    filename=cached.get("filename", ""),
                    owner=cached.get("owner", owner),
                    status=cached.get("status", "processing"),
                    n_chunks=int(cached.get("n_chunks") or 0),
                    error=cached.get("error"),
                )
        rec = self._metadata.get_for_owner(document_id, owner)
        if rec is None:
            return None
        doc = _from_meta(rec)
        if self._cache is not None:
            self._cache.set(document_id, owner, {
                "document_id": doc.document_id, "filename": doc.filename,
                "owner": doc.owner, "status": doc.status,
                "n_chunks": doc.n_chunks, "error": doc.error,
            })
        return doc

    def list_for_owner(self, owner: str) -> list[DocRecord]:
        return [_from_meta(r) for r in self._metadata.list_for_owner(owner)]

    def delete(self, document_id: str, owner: str,
               *, uploaded_by: str | None = None) -> str:
        """uploaded_by가 있으면 해당 업로더 행을 삭제(공유 문서 관리자 삭제)."""
        row_owner = uploaded_by or owner
        with self._lock:
            rec = self._metadata.get_for_owner(document_id, row_owner)
            if rec is None:
                return "not_found"
            if rec.status == "processing":
                return "busy"
        deleter = getattr(self._pipeline._store, "delete_document", None)
        if callable(deleter):
            deleter(document_id, [_owner_tag(row_owner)])
        if uploaded_by and uploaded_by != owner:
            self._metadata.delete_document_row(document_id, uploaded_by)
        else:
            self._metadata.delete_for_owner(document_id, owner)
        if self._cache is not None:
            self._cache.invalidate(document_id, row_owner)
        if self._queue is not None:
            self._queue.clear_inflight(document_id)
        logger.info("deleted document record %s for owner %s",
                    document_id, row_owner)
        return "deleted"
