"""결과 채팅 match_context 주입 + soft-abstain 가드."""
from __future__ import annotations

from datetime import datetime, timezone

from harag.api.pipeline import (
    MATCH_CONTEXT_CHUNK_ID,
    prepend_match_context,
    scored_match_context,
)
from harag.contracts.boundaries import Chunk, ScoredChunk
from harag.generation.generator import AnswerGenerator
from harag.schemas.chunk import ChunkMetadata, ChunkType
from harag.security.output_guard import OutputGuard


def _scored(cid="c1", text="본문", score=0.9):
    meta = ChunkMetadata(
        chunk_id=cid, document_id="d1", source_block_ids=["b1"],
        struct_path="제1조", chunk_type=ChunkType.body,
        security_level="internal", acl_tags=["owner:t"],
        source_document="약관.pdf", citation_label="약관 제1조",
        parsing_version="p", chunking_version="c", embedding_model_id="m",
        content_hash="h", created_at=datetime.now(timezone.utc))
    return ScoredChunk(chunk=Chunk(meta=meta, text=text), score=score)


def test_scored_match_context_empty_is_none():
    assert scored_match_context("") is None
    assert scored_match_context("   ") is None


def test_prepend_match_context_puts_guide_first():
    pdf = _scored("pdf-1", "통원 한도 20만원", 0.4)
    out = prepend_match_context(
        [pdf],
        "【상품】삼성 New플러스원\n매칭 담보: 통원의료비",
    )
    assert len(out) == 2
    assert out[0].chunk.meta.chunk_id == MATCH_CONTEXT_CHUNK_ID
    assert out[0].score >= 1.0
    assert "매칭 안내" in out[0].chunk.text
    assert "삼성" in out[0].chunk.text
    assert out[1].chunk.meta.chunk_id == "pdf-1"


def test_prepend_alone_satisfies_precheck():
    """PDF 히트가 없어도 match_context면 empty_context가 아니다."""
    ctx = prepend_match_context([], "【왜】증상과 통원 담보가 연결됩니다.")
    gen = AnswerGenerator(min_score=0.15)
    assert gen.precheck(ctx) is None


def test_soft_abstain_기재되어_있지_않_normalized():
    guard = OutputGuard()
    text = (
        "제공된 문서에는 삼성 보험의 장점에 대한 내용이 기재되어 있지 않아 "
        "안내해 드리기 어렵습니다."
    )
    out = guard.validate(text, [], ["c1"])
    assert not out.ok
    assert out.abstain_reason == "no_evidence_in_documents"


def test_soft_abstain_관련_내용이_있지_않_normalized():
    guard = OutputGuard()
    text = "문서에 관련 내용이 장점으로 있지 않아 안내가 어렵습니다."
    out = guard.validate(text, ["c1"], ["c1"])
    assert not out.ok
    assert out.abstain_reason == "no_evidence_in_documents"
