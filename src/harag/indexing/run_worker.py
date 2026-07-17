"""
PDF 인덱싱 워커 — Redis Streams를 소비해 PdfIngestPipeline으로 처리.

기동:
  REDIS_URL=redis://localhost:6379/0 python -m harag.indexing.run_worker

docker compose의 worker 서비스가 동일 엔트리포인트를 사용한다.
REDIS_URL이 비어 있으면 즉시 종료(인프로세스 인제스트 모드에서는 불필요).
"""
from __future__ import annotations

import logging
import os
import signal
import sys
import time
from pathlib import Path

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("harag.worker")

_STOP = False


def _handle_signal(signum, frame):  # noqa: ARG001
    global _STOP
    _STOP = True
    logger.info("shutdown signal received")


def build_pipeline():
    from harag.config.settings import get_settings
    from harag.embedding.api_embedder import build_embedding_model, build_morph
    from harag.embedding.embedder import HybridEmbedder
    from harag.retrieval.qdrant_store import QdrantVectorStore
    from harag.parsing.pdf_parser import PdfParser
    from harag.chunking.chunker import StructuralChunker
    from harag.storage.metadata_store import MetadataStore
    from harag.storage.redis_client import get_redis
    from harag.storage.redis_cache import DocStatusCache
    from harag.storage.redis_ingest_queue import RedisIngestQueue
    from harag.indexing.pdf_pipeline import PdfIngestPipeline

    settings = get_settings()
    if not settings.redis_url:
        logger.error("REDIS_URL이 비어 있습니다 — 워커를 종료합니다")
        sys.exit(1)

    if not os.environ.get("HARAG_SPOOL_DIR"):
        logger.error(
            "REDIS_URL이 설정됐는데 HARAG_SPOOL_DIR이 없습니다 — "
            "API와 워커가 같은 스풀 경로를 봐야 합니다")
        sys.exit(1)

    r = get_redis(settings.redis_url)
    if r is None:
        logger.error("Redis 연결 실패 — 워커를 종료합니다")
        sys.exit(1)

    db_url = settings.database_url
    if db_url.startswith("sqlite:///./") or db_url.startswith("sqlite:///"):
        raw_path = db_url.removeprefix("sqlite:///")
        if raw_path and raw_path != ":memory:":
            Path(raw_path).parent.mkdir(parents=True, exist_ok=True)

    embedding_model = build_embedding_model(settings)
    morph = build_morph()
    embedder = HybridEmbedder(embedding_model, morph)
    store = QdrantVectorStore(
        embedding_model=embedding_model, dim=embedding_model.dim,
        collection=settings.qdrant_collection,
        url=settings.qdrant_url_or_none,
        api_key=settings.qdrant_api_key or None,
        disk_budget_mb=settings.qdrant_disk_budget_mb,
        payload_bytes_per_point=settings.qdrant_payload_bytes_per_point,
        morph=morph,
    )
    metadata = MetadataStore(dsn=db_url)
    prefix = settings.redis_key_prefix
    cache = DocStatusCache(r, prefix=prefix)
    queue = RedisIngestQueue(
        r,
        prefix=prefix,
        stream_maxlen=settings.redis_stream_maxlen,
        visibility_sec=settings.ingest_visibility_sec,
        max_attempts=settings.ingest_max_attempts,
    )
    pipeline = PdfIngestPipeline(
        PdfParser(), StructuralChunker(), embedder, store, metadata,
        status_cache=cache,
        on_failed=queue.on_failed,
    )
    return queue, pipeline


def main() -> int:
    signal.signal(signal.SIGINT, _handle_signal)
    signal.signal(signal.SIGTERM, _handle_signal)

    queue, pipeline = build_pipeline()
    logger.info("worker started — waiting on Redis Streams ingest queue")
    while not _STOP:
        try:
            job = queue.dequeue(timeout_sec=5)
        except Exception:  # noqa: BLE001
            logger.exception("dequeue failed — backoff")
            time.sleep(2)
            continue
        if job is None:
            continue
        try:
            pipeline.process_file(
                job.document_id, job.spool_path, job.filename,
                job.uploaded_by)
            # 논리 성공·실패 모두 메시지 완료(XACK + in-flight 해제)
            queue.ack_success(job)
        except Exception as e:  # noqa: BLE001
            logger.exception("job crashed: %s", job.document_id)
            queue.nack_retry_or_dead(job, str(e))
    logger.info("worker stopped")
    return 0


if __name__ == "__main__":
    sys.exit(main())
