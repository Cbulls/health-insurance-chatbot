"""생성 전 컨텍스트 중복 제거·이웃 확장·토큰 패킹."""
from datetime import datetime, timezone

from harag.schemas.chunk import ChunkMetadata, ChunkType
from harag.contracts.boundaries import Chunk, ScoredChunk
from harag.retrieval.context_compact import (
    dedupe_scored_chunks, expand_neighbors, pack_by_token_budget, prepare_context,
)


def _sc(cid, text, score, content_hash=None, struct_path="제1조",
        parent_chunk_id=None):
    meta = ChunkMetadata(
        chunk_id=cid, document_id="d1", source_block_ids=["b1"],
        struct_path=struct_path, parent_chunk_id=parent_chunk_id,
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


def test_expand_parent_and_struct_neighbors():
    parent = _sc("p1", "제12조 총칙", 0.5, struct_path="제12조")
    child = _sc("c1", "본인부담 규정", 0.9, struct_path="제12조 > 제1항",
                parent_chunk_id="p1")
    sib = _sc("c2", "면제 요건", 0.4, struct_path="제12조 > 제2항")
    other = _sc("x1", "다른 장", 0.3, struct_path="제3조")
    pool = [child, parent, sib, other]
    out = expand_neighbors([child], pool, max_extra_per_hit=2)
    ids = {s.chunk.meta.chunk_id for s in out}
    assert "c1" in ids and "p1" in ids and "c2" in ids
    assert "x1" not in ids


def test_pack_respects_token_budget():
    chunks = [
        _sc("c1", "가" * 200, 0.9),
        _sc("c2", "나" * 200, 0.8),
        _sc("c3", "다" * 200, 0.7),
    ]
    # 각 ~100 토큰 → 예산 150이면 상위 1~2개만
    out = pack_by_token_budget(chunks, max_tokens=150)
    assert 1 <= len(out) <= 2
    assert out[0].chunk.meta.chunk_id == "c1"


def test_prepare_context_pipeline():
    parent = _sc("p1", "부모 조항 본문입니다.", 0.5, struct_path="제1조")
    hit = _sc("c1", "자식 항 본문입니다.", 0.9, struct_path="제1조 > 제1항",
              parent_chunk_id="p1")
    out = prepare_context([hit, parent], max_tokens=5000, expand=True)
    ids = [s.chunk.meta.chunk_id for s in out]
    assert "c1" in ids and "p1" in ids
