"""
인프로세스 수집(인제스트) 서비스 — MVP.

설계 원본은 메시지 큐 + 별도 indexing-worker로 읽기/쓰기를 물리 격리한다(NFR-2).
MVP는 단일 프로세스에서 FastAPI BackgroundTasks로 비동기 처리한다:
  업로드 → 즉시 202 → 백그라운드로 파싱·청킹·임베딩·Qdrant 적재.

상태는 인메모리 dict(단일 프로세스 MVP). Phase 2: 큐 + PostgreSQL 등록부로 이전.
"""
from __future__ import annotations

import logging
import os
import threading
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from pathlib import Path

from harag.chunking.chunker import ChunkingContext
from harag.contracts.boundaries import ContractViolation, verify_boundary2
from harag.retrieval.qdrant_store import CapacityExceededError

logger = logging.getLogger("harag.ingest")


@dataclass
class DocRecord:
    document_id: str
    filename: str
    owner: str
    status: str = "processing"   # processing | ready | failed
    n_chunks: int = 0
    error: str | None = None


def _owner_tag(owner: str) -> str:
    return f"owner:{owner}"


class InProcessIngest:
    def __init__(self, parser, chunker, embedder, store, max_workers: int = 2):
        self._parser = parser
        self._chunker = chunker
        self._embedder = embedder
        self._store = store
        self._records: dict[str, DocRecord] = {}
        # 라우트(이벤트 루프 스레드)와 process(인제스트 스레드풀)가 _records를
        # 동시 접근한다. register의 검사-후-등록을 원자화하고 상태 갱신을 보호한다.
        self._lock = threading.Lock()
        # 파싱·임베딩은 CPU가 무겁다. asyncio.to_thread(쿼리 오프로드)와 같은
        # 기본 스레드풀을 쓰면 대량 업로드가 질의 응답을 굶기므로 전용 풀로 격리.
        self._executor = ThreadPoolExecutor(
            max_workers=max_workers, thread_name_prefix="harag-ingest")

    def submit(self, document_id: str, spool_path: str, filename: str,
               owner: str) -> None:
        """스풀된 임시 파일을 전용 스레드풀에서 처리(완료 후 파일 삭제)."""
        self._executor.submit(
            self.process_file, document_id, spool_path, filename, owner)

    def process_file(self, document_id: str, spool_path: str, filename: str,
                     owner: str) -> None:
        """임시 파일 경로 기반 처리 — bytes를 큐에 잡아두지 않아 메모리 절약."""
        key = self._key(document_id, owner)
        try:
            raw = Path(spool_path).read_bytes()
        except OSError:
            logger.exception("ingest spool read failed: %s", document_id)
            self._fail(key, "internal_error: spool_read_failed")
            return
        finally:
            try:
                os.unlink(spool_path)
            except OSError:
                pass
        self.process(document_id, raw, filename, owner)

    def register(self, document_id: str, filename: str, owner: str) -> str:
        """멱등 등록. 이미 처리됨/처리중이면 duplicate, 신규면 accepted."""
        key = self._key(document_id, owner)
        with self._lock:
            rec = self._records.get(key)
            if rec is not None and rec.status in ("ready", "processing"):
                return "duplicate"
            self._records[key] = DocRecord(
                document_id=document_id, filename=filename, owner=owner)
            return "accepted"

    def process(self, document_id: str, raw: bytes, filename: str, owner: str) -> None:
        """백그라운드 처리(파싱→청킹→임베딩→적재)."""
        key = self._key(document_id, owner)
        try:
            ir = self._parser.parse(raw, document_id=document_id, filename=filename)
            if ir.parse_status.value == "failed" or not ir.blocks:
                self._fail(key, "parse_failed (텍스트 추출 불가 — 스캔/암호 PDF일 수 있음)")
                return

            ctx = ChunkingContext(
                acl_tags=[_owner_tag(owner)],
                source_document=filename,
                embedding_model_id=self._embedder.model_id,
            )
            chunks = self._chunker.chunk(ir, ctx)
            if not chunks:
                self._fail(key, "no_chunks (본문이 비어 있음)")
                return

            # 용량 사전 검사 — 임베딩(비용이 드는 외부 API) 호출 전에 차단.
            # 무료 사양 Qdrant 디스크 예산을 넘길 문서는 여기서 거부한다.
            checker = getattr(self._store, "ensure_capacity_for", None)
            if callable(checker):
                checker(len(chunks))  # 초과 시 CapacityExceededError → 아래 except

            # 경계2: 인용 계보 검증(위조/노이즈 오염 차단)
            try:
                verify_boundary2(ir, chunks)
            except ContractViolation as e:
                logger.warning("boundary2 violation on %s: %s", document_id, e)

            embedded = self._embedder.embed(chunks)
            n = self._store.index(embedded)

            with self._lock:
                rec = self._records.get(key)
                if rec is not None:
                    rec.status = "ready"
                    rec.n_chunks = n
            logger.info("ingested %s: %d chunks", document_id, n)
        except CapacityExceededError as e:
            # index()의 최후 방어선에서 걸린 경우(동시 인제스트 경쟁 등)
            logger.warning("capacity exceeded at index for %s: %s", document_id, e)
            self._fail(key, "capacity_exceeded (저장 공간 한도 — 기존 문서를 삭제하세요)")
        except Exception as e:  # noqa: BLE001 — 실패는 상태로 노출(500 누설 방지)
            logger.exception("ingest failed: %s", document_id)
            self._fail(key, f"internal_error: {type(e).__name__}")

    def status(self, document_id: str, owner: str) -> DocRecord | None:
        with self._lock:
            return self._records.get(self._key(document_id, owner))

    def list_for_owner(self, owner: str) -> list[DocRecord]:
        with self._lock:
            return [r for r in self._records.values() if r.owner == owner]

    def _fail(self, key: str, msg: str) -> None:
        with self._lock:
            rec = self._records.get(key)
            if rec is not None:
                rec.status = "failed"
                rec.error = msg

    @staticmethod
    def _key(document_id: str, owner: str) -> str:
        return f"{owner}/{document_id}"
