"""
다단·노이즈·헤더상속·Redis 큐 동작 테스트.
"""
from __future__ import annotations

from datetime import datetime, timezone

import pytest

from harag.parsing.pdf_parser import (
    PdfParser, _clause_of, _PAGE_NUM_RE, _Line,
)
from harag.chunking.chunker import StructuralChunker, ChunkingContext
from harag.schemas.ir import (
    DocumentIR, Block, BlockType,
    SourceFormat, ExtractionPath, ParseStatus, SourceMetadata, ParseQuality,
)


def test_page_number_noise_regex():
    assert _PAGE_NUM_RE.match("- 3 -")
    assert _PAGE_NUM_RE.match("3/12")
    assert _PAGE_NUM_RE.match("Page 7")
    assert not _PAGE_NUM_RE.match("제3조 목적")


def test_multicolumn_orders_left_column_before_right():
    # 왼쪽 열 y=100,200 / 오른쪽 열 y=110,210 — 단순 y정렬이면 교차됨
    lines = [
        _Line(100, 112, 50, 200, "LEFT-A"),
        _Line(110, 122, 320, 500, "RIGHT-A"),
        _Line(200, 212, 50, 200, "LEFT-B"),
        _Line(210, 222, 320, 500, "RIGHT-B"),
    ]
    ordered = PdfParser._order_lines_multicolumn(lines, page_width=612)
    texts = [ln.text for ln in ordered]
    assert texts == ["LEFT-A", "LEFT-B", "RIGHT-A", "RIGHT-B"]


def test_single_column_keeps_top_order():
    lines = [
        _Line(200, 212, 50, 400, "B"),
        _Line(100, 112, 50, 400, "A"),
    ]
    ordered = PdfParser._order_lines_multicolumn(lines, page_width=612)
    assert [ln.text for ln in ordered] == ["A", "B"]


def test_header_inheritance_and_parent_chunk_id():
    blocks = [
        Block(block_id="d-b0", block_type=BlockType.paragraph,
              struct_path="제3장", order_index=0, text="제3장 보험금",
              confidence=1.0, page_ref=1),
        Block(block_id="d-b1", block_type=BlockType.paragraph,
              struct_path="제12조", order_index=1, text="제12조 여비 한도",
              confidence=1.0, page_ref=1),
        Block(block_id="d-b2", block_type=BlockType.paragraph,
              struct_path="제13조", order_index=2, text="제13조 정산",
              confidence=1.0, page_ref=2),
    ]
    meta = SourceMetadata(
        source_system="s", department="d", security_level="internal",
        original_path="/x.pdf")
    quality = ParseQuality(
        char_count=10, table_recovery_avg=1.0,
        broken_char_ratio=0.0, order_confidence=1.0)
    ir = DocumentIR(
        document_id="d", source_format=SourceFormat.pdf_text,
        extraction_path=ExtractionPath.native, parser_version="p",
        ingested_at=datetime.now(timezone.utc),
        source_metadata=meta, parse_quality=quality,
        parse_status=ParseStatus.ok, blocks=blocks)
    ctx = ChunkingContext(
        acl_tags=["owner:t"], source_document="약관.pdf",
        embedding_model_id="m")
    chunks = StructuralChunker().chunk(ir, ctx)
    # 제12조는 제3장 하위로 상속
    c12 = next(c for c in chunks if "제12조" in c.meta.struct_path)
    assert "제3장" in c12.meta.struct_path
    assert c12.text.startswith("[제3장 > 제12조]")
    # 제13조는 직전 형제/상위 parent_chunk_id를 가짐
    c13 = next(c for c in chunks if "제13조" in c.meta.struct_path)
    assert c13.meta.parent_chunk_id is not None


def test_redis_queue_idempotent_enqueue():
    redis = pytest.importorskip("redis")
    try:
        client = redis.Redis.from_url("redis://localhost:6379/0",
                                      decode_responses=True)
        client.ping()
    except Exception:
        pytest.skip("Redis not available on localhost:6379")

    import uuid
    from harag.storage.redis_ingest_queue import RedisIngestQueue
    prefix = f"test:{uuid.uuid4().hex}:"
    q = RedisIngestQueue(client, prefix=prefix)
    ok1 = q.enqueue("doc-a", "/tmp/a.pdf", "a.pdf", "owner1")
    ok2 = q.enqueue("doc-a", "/tmp/a.pdf", "a.pdf", "owner1")
    assert ok1 is True
    assert ok2 is False
    job = q.dequeue(timeout_sec=1)
    assert job is not None and job.document_id == "doc-a"
    q.ack_success(job)
    client.delete(f"{prefix}ingest:jobs", f"{prefix}ingest:inflight",
                  f"{prefix}ingest:dead")


def test_clause_of_still_works():
    assert _clause_of("제12조(여비) 국내") is not None
