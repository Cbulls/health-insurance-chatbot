"""
PDF 인제스트 공용 파이프라인 — API 인프로세스·워커가 동일 경로를 공유.

파싱 → (PII) → 청킹 → 임베딩 → Qdrant 적재 → MetadataStore 상태 갱신.
선택: ObjectStore 원본 보존, VersionCoordinator.
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


def _parse_fail_message(code: str) -> str:
    if code == "encrypted_pdf":
        return "encrypted_pdf (암호 PDF — 암호 해제 후 재업로드)"
    if code == "scan_pdf_no_ocr":
        return ("scan_pdf_no_ocr (스캔본은 OCR 미지원 — "
                "텍스트 PDF로 재업로드)")
    if code == "hwpx_parse_failed":
        return "hwpx_parse_failed (HWPX 텍스트 추출 실패)"
    if code == "docx_parse_failed":
        return "docx_parse_failed (Word DOCX 텍스트 추출 실패)"
    if code == "hwp5_parse_failed":
        return ("hwp5_parse_failed (HWP 텍스트 추출 실패 — "
                "가능하면 HWPX로 변환해 주세요)")
    if code == "doc_convert_failed":
        return ("doc_convert_failed (구형 DOC 변환 실패 — "
                "DOCX로 저장 후 재업로드하거나 LibreOffice를 설치하세요)")
    return ("parse_failed (텍스트 추출 불가 — "
            "스캔/암호 PDF이거나 손상된 파일일 수 있음)")


def _parse_warning_message(code: str) -> str:
    if code == "hwp5_table_limited":
        return ("hwp5_table_limited (표 인식 제한 — "
                "검색은 가능하나 HWPX 변환을 권장합니다)")
    return code


def _mask_ir_pii(ir, masker) -> None:
    for b in ir.blocks:
        if b.text:
            masked, _ = masker.mask(b.text)
            b.text = masked
        if b.table_content:
            for cell in b.table_content.cells:
                if cell.text:
                    cell.text, _ = masker.mask(cell.text)


class PdfIngestPipeline:
    def __init__(self, parser, chunker, embedder, store, metadata,
                 status_cache=None,
                 on_failed: OnDocCallback | None = None,
                 on_success: OnDocCallback | None = None,
                 pii_masker=None,
                 object_store=None,
                 version_coord=None):
        self._parser = parser
        self._chunker = chunker
        self._embedder = embedder
        self._store = store
        self._metadata = metadata
        self._cache = status_cache
        self._on_failed = on_failed
        self._on_success = on_success
        self._pii_masker = pii_masker
        self._object_store = object_store
        self._version_coord = version_coord

    def process(self, document_id: str, raw: bytes, filename: str,
                owner: str, acl_tags: list[str] | None = None,
                department: str = "") -> None:
        try:
            if self._object_store is not None:
                try:
                    self._object_store.put_original(document_id, raw)
                    self._metadata.set_object_key(
                        document_id, owner, f"originals/{document_id}")
                except Exception:  # noqa: BLE001
                    logger.exception(
                        "object store put failed for %s — continue ingest",
                        document_id)

            ir = self._parser.parse(
                raw, document_id=document_id, filename=filename)
            if ir.parse_status.value == "failed" or not ir.blocks:
                code = getattr(self._parser, "last_fail_code", None) or ""
                self._fail(document_id, owner, filename,
                           _parse_fail_message(code))
                return

            if self._pii_masker is not None:
                _mask_ir_pii(ir, self._pii_masker)

            tags = list(acl_tags) if acl_tags else [_owner_tag(owner)]
            if _owner_tag(owner) not in tags:
                tags.append(_owner_tag(owner))
            if department:
                dept_tag = f"dept:{department}"
                if dept_tag not in tags:
                    tags.append(dept_tag)

            emb_model = getattr(self._embedder, "_model", None)
            ctx = ChunkingContext(
                acl_tags=tags,
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
            except (ContractViolation, AttributeError, TypeError) as e:
                logger.warning("boundary2 skip/violation on %s: %s",
                               document_id, e)

            embedded = self._embedder.embed(chunks)
            index_fn = self._store.index
            try:
                n = index_fn(
                    embedded,
                    skip_capacity_check=True,
                    replace_document=True,
                )
            except TypeError:
                n = index_fn(embedded)

            chunk_ids = {
                getattr(c.meta, "chunk_id", "") for c in chunks
                if getattr(c, "meta", None)
            }
            chunk_ids.discard("")
            version: int | None = None
            if self._version_coord is not None:
                version = self._version_coord.next_version(document_id, owner)
                self._version_coord.stage_activate(
                    document_id, version, chunk_ids)

            warn_code = getattr(self._parser, "last_warning", None)
            warn_msg = (_parse_warning_message(warn_code)
                        if warn_code else None)
            self._metadata.mark_ready(
                document_id, owner, n, warning=warn_msg, version=version)
            self._cache_set(document_id, owner, filename, "ready", n, warn_msg)
            if self._on_success is not None:
                self._on_success(document_id)
            if warn_msg:
                logger.warning("ingested %s with warning: %s", document_id,
                               warn_msg)
            logger.info("ingested %s: %d chunks v=%s", document_id, n, version)
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
                     owner: str, acl_tags: list[str] | None = None,
                     department: str = "") -> None:
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
        self.process(document_id, raw, filename, owner,
                     acl_tags=acl_tags, department=department)

    def reindex_from_store(self, document_id: str, owner: str,
                           filename: str = "",
                           acl_tags: list[str] | None = None,
                           department: str = "") -> bool:
        """ObjectStore 원본으로 재인덱싱. 원본 없으면 False."""
        if self._object_store is None:
            return False
        try:
            raw = self._object_store.get_original(document_id)
        except KeyError:
            return False
        name = filename
        if not name:
            rec = self._metadata.get_for_owner(document_id, owner)
            name = (rec.filename if rec else "") or f"{document_id}.bin"
        self._metadata.mark_processing(document_id, owner)
        self.process(document_id, raw, name, owner,
                     acl_tags=acl_tags, department=department)
        return True

    def _fail(self, document_id: str, owner: str, filename: str,
              msg: str) -> None:
        self._metadata.mark_failed(document_id, owner, msg)
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
