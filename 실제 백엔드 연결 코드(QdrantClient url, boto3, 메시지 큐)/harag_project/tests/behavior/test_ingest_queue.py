"""
인덱싱 큐 어댑터 TDD — 읽기/쓰기 격리(NFR-2).

업로드 → 큐 등록(즉시 반환) → 워커가 비동기 소비. API가 인덱싱을 안 기다림.
IngestQueue 프로토콜(deps.py) 구현. 운영은 메시지 큐(SQS/RabbitMQ 등), 검증은 인메모리.

어댑터 책임:
  - enqueue: 작업 등록, 즉시 반환(블로킹 안 함)
  - 워커가 dequeue로 소비
  - 멱등: 같은 document_id 중복 등록 방지(content 기반)
  - 처리 실패 작업 격리(dead-letter)
"""
import pytest

from harag.storage.ingest_queue import InMemoryIngestQueue


@pytest.mark.asyncio
async def test_Q01_enqueue_returns_immediately():
    """enqueue는 즉시 수락 반환(블로킹 안 함)."""
    q = InMemoryIngestQueue()
    accepted = await q.enqueue("doc1", b"data", "규정.hwp", "uploader1")
    assert accepted is True


@pytest.mark.asyncio
async def test_Q02_worker_consumes_enqueued_job():
    """워커가 등록된 작업을 소비."""
    q = InMemoryIngestQueue()
    await q.enqueue("doc1", b"data", "규정.hwp", "uploader1")
    job = await q.dequeue()
    assert job is not None
    assert job.document_id == "doc1"
    assert job.raw == b"data"


@pytest.mark.asyncio
async def test_Q03_idempotent_duplicate_rejected():
    """같은 document_id 중복 등록 → 거부(멱등)."""
    q = InMemoryIngestQueue()
    first = await q.enqueue("doc1", b"data", "규정.hwp", "u1")
    dup = await q.enqueue("doc1", b"data", "규정.hwp", "u1")
    assert first is True
    assert dup is False, "중복 등록이 거부되지 않음"


@pytest.mark.asyncio
async def test_Q04_fifo_order():
    """선입선출 — 등록 순서대로 소비."""
    q = InMemoryIngestQueue()
    await q.enqueue("doc1", b"a", "a.hwp", "u")
    await q.enqueue("doc2", b"b", "b.hwp", "u")
    j1 = await q.dequeue()
    j2 = await q.dequeue()
    assert j1.document_id == "doc1" and j2.document_id == "doc2"


@pytest.mark.asyncio
async def test_Q05_empty_queue_returns_none():
    """빈 큐 dequeue → None(블로킹/크래시 아님)."""
    q = InMemoryIngestQueue()
    assert await q.dequeue() is None


@pytest.mark.asyncio
async def test_Q06_failed_job_to_dead_letter():
    """처리 실패 작업을 dead-letter로 격리."""
    q = InMemoryIngestQueue()
    await q.enqueue("doc1", b"data", "규정.hwp", "u")
    job = await q.dequeue()
    q.mark_failed(job, reason="parse_error")
    assert len(q.dead_letters()) == 1
    assert q.dead_letters()[0].document_id == "doc1"


if __name__ == "__main__":
    import sys
    sys.exit(pytest.main([__file__, "-v", "--asyncio-mode=auto"]))
