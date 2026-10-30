"""SSE sources 페이로드에 쪽수(page)가 실린다."""
from __future__ import annotations

from datetime import datetime, timezone

from harag.api.citations_fmt import sources_sse_payload
from harag.contracts.boundaries import Chunk, ScoredChunk
from harag.schemas.chunk import ChunkMetadata, ChunkType


def _scored(cid: str, page, *, doc="d1") -> ScoredChunk:
    meta = ChunkMetadata(
        chunk_id=cid,
        document_id=doc,
        source_block_ids=["b1"],
        struct_path="p1",
        chunk_type=ChunkType.body,
        security_level="internal",
        acl_tags=["owner:t"],
        source_document="약관.pdf",
        citation_label="약관",
        parsing_version="p",
        chunking_version="c",
        embedding_model_id="m",
        content_hash=cid,
        created_at=datetime.now(timezone.utc),
        page_ref=page,
    )
    return ScoredChunk(chunk=Chunk(meta=meta, text="본문"), score=0.9)


def test_sources_sse_payload_includes_page():
    rows = sources_sse_payload([
        _scored("c1", 24),
        _scored("c2", [31, 32]),
        _scored("c3", None),
    ])
    assert rows[0]["n"] == 1
    assert rows[0]["page"] == 24
    assert rows[0]["document_id"] == "d1"
    assert rows[1]["page"] == 31  # 리스트면 첫 쪽
    assert rows[2]["page"] is None


def test_sources_sse_payload_empty():
    assert sources_sse_payload([]) == []
    assert sources_sse_payload(None) == []
