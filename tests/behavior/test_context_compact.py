"""생성 전 컨텍스트 중복 제거."""
from datetime import datetime, timezone

from harag.schemas.chunk import ChunkMetadata, ChunkType
from harag.contracts.boundaries import Chunk, ScoredChunk
from harag.retrieval.context_compact import dedupe_scored_chunks


def _sc(cid, text, score, content_hash=None):
    meta = ChunkMetadata(
        chunk_id=cid, document_id="d1", source_block_ids=["b1"], struct_path="제1조",
        chunk_type=ChunkType.body, security_level="internal", acl_tags=["dept:x"],
        source_document="규정.hwp", citation_label="규정 제1조", parsing_version="p",
        chunking_version="c", embedding_model_id="m",
        content_hash=content_hash or ("h-" + cid),
        created_at=datetime.now(timezone.utc))
    return ScoredChunk(chunk=Chunk(meta=meta, text=text), score=score)


def test_dedupe_same_hash():
    a = _sc("c1", "출장비 한도는 5만원", 0.9, content_hash="same")
    b = _sc("c2", "출장비 한도는 5만원 (복제)", 0.8, content_hash="same")
    out = dedupe_scored_chunks([a, b])
    assert [s.chunk.meta.chunk_id for s in out] == ["c1"]


def test_dedupe_near_duplicate_jaccard():
    a = _sc("c1", "국내 출장비 한도는 하루 오만원이다", 0.9)
    b = _sc("c2", "국내 출장비 한도는 하루 오만원이다 추가", 0.8)
    out = dedupe_scored_chunks([a, b], jaccard_threshold=0.8)
    assert len(out) == 1


def test_keeps_distinct():
    a = _sc("c1", "출장비 한도", 0.9)
    b = _sc("c2", "연차 휴가 일수", 0.8)
    out = dedupe_scored_chunks([a, b])
    assert len(out) == 2
