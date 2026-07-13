"""
청커 TDD — IR → 구조 기반 청크.

청커의 책임(표본 없이 검증 가능):
  - 조항 단위 유지(토큰 상한 내면 한 청크로, 찢지 않음)
  - 표는 독립 청크(chunk_type=table)
  - 노이즈 블록(머리말)은 청크화 안 함
  - source_block_ids로 IR 역추적(계보)
  - struct_path를 IR에서 전파
  - acl_tags 등 메타 채움(인덱싱 fail-closed 통과)
"""
import pytest
from datetime import datetime, timezone

from harag.schemas.ir import (
    DocumentIR, Block, BlockType, TableCell, TableContent,
    SourceFormat, ExtractionPath, ParseStatus, SourceMetadata, ParseQuality,
)
from harag.chunking.chunker import StructuralChunker, ChunkingContext


def _ir(blocks):
    return DocumentIR(
        document_id="doc1", source_format=SourceFormat.hwp5,
        extraction_path=ExtractionPath.native, parser_version="p1",
        ingested_at=datetime.now(timezone.utc), parse_status=ParseStatus.ok,
        source_metadata=SourceMetadata(source_system="s", department="dep",
                                       security_level="internal", original_path="/x.hwp"),
        blocks=blocks,
        parse_quality=ParseQuality(char_count=100, table_recovery_avg=1.0,
                                   broken_char_ratio=0.0, order_confidence=1.0))


def _ctx():
    return ChunkingContext(acl_tags=["dept:finance"], source_document="여비규정.hwp",
                           embedding_model_id="kure-v1", max_tokens=512)


def _blk(i, kind, text="", struct="", noise=False, table=None):
    kw = dict(block_id=f"doc1-b{i}", block_type=kind, struct_path=struct,
              order_index=i, confidence=1.0, is_noise=noise)
    if kind == BlockType.table:
        kw["table_content"] = table
    else:
        kw["text"] = text
    return Block(**kw)


def test_C01_clause_kept_as_single_chunk():
    """짧은 조항은 한 청크로 유지(찢지 않음)."""
    blocks = [_blk(0, BlockType.heading, "제1조 목적", struct="제1조"),
              _blk(1, BlockType.paragraph, "이 규정은 여비를 정한다.", struct="제1조")]
    chunks = StructuralChunker().chunk(_ir(blocks), _ctx())
    assert len(chunks) == 1, "짧은 조항이 불필요하게 분할됨"
    assert "제1조" in chunks[0].text and "여비를 정한다" in chunks[0].text


def test_C03_table_is_independent_chunk():
    """표는 본문과 분리된 독립 청크."""
    tc = TableContent(n_rows=2, n_cols=2, recovery_confidence=1.0, cells=[
        TableCell(row=0, col=0, text="항목"), TableCell(row=0, col=1, text="금액"),
        TableCell(row=1, col=0, text="교통비"), TableCell(row=1, col=1, text="5만원")])
    blocks = [_blk(0, BlockType.paragraph, "다음 표와 같다.", struct="제2조"),
              _blk(1, BlockType.table, struct="제2조", table=tc)]
    chunks = StructuralChunker().chunk(_ir(blocks), _ctx())
    table_chunks = [c for c in chunks if c.meta.chunk_type.value == "table"]
    assert len(table_chunks) == 1, "표가 독립 청크로 분리 안 됨"


def test_C07_noise_blocks_excluded():
    """노이즈(머리말)는 청크화하지 않음."""
    blocks = [_blk(0, BlockType.header_footer, "- 1 -", noise=True),
              _blk(1, BlockType.paragraph, "본문 내용", struct="제1조")]
    chunks = StructuralChunker().chunk(_ir(blocks), _ctx())
    joined = " ".join(c.text for c in chunks)
    assert "- 1 -" not in joined, "머리말이 청크에 포함됨"


def test_C08_lineage_source_block_ids():
    """청크가 자기 출처 IR 블록 ID를 보존(계보)."""
    blocks = [_blk(0, BlockType.heading, "제1조 목적", struct="제1조"),
              _blk(1, BlockType.paragraph, "본문", struct="제1조")]
    chunks = StructuralChunker().chunk(_ir(blocks), _ctx())
    all_ids = set()
    for c in chunks:
        all_ids.update(c.meta.source_block_ids)
    assert "doc1-b0" in all_ids and "doc1-b1" in all_ids, "source_block_ids 계보 끊김"


def test_C04_struct_path_propagated():
    """struct_path가 IR에서 청크로 전파."""
    blocks = [_blk(0, BlockType.heading, "제24조의2 여비", struct="제24조의2"),
              _blk(1, BlockType.paragraph, "본문", struct="제24조의2")]
    chunks = StructuralChunker().chunk(_ir(blocks), _ctx())
    assert any("제24조의2" in c.meta.struct_path for c in chunks), "struct_path 전파 안 됨"


def test_C_meta_failclosed_acl():
    """acl_tags가 채워져 인덱싱 fail-closed를 통과."""
    blocks = [_blk(0, BlockType.paragraph, "본문", struct="제1조")]
    chunks = StructuralChunker().chunk(_ir(blocks), _ctx())
    assert all(c.meta.acl_tags for c in chunks), "acl_tags 비어 인덱싱 거부될 것"


if __name__ == "__main__":
    import sys
    sys.exit(pytest.main([__file__, "-v"]))
