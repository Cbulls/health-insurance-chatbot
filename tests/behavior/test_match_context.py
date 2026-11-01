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
    from harag.security.output_guard import SOFT_NO_EVIDENCE_ANSWER

    guard = OutputGuard()
    text = (
        "제공된 문서에는 삼성 보험의 장점에 대한 내용이 기재되어 있지 않아 "
        "안내해 드리기 어렵습니다."
    )
    out = guard.validate(text, [], ["c1"])
    assert out.ok
    assert out.answer == SOFT_NO_EVIDENCE_ANSWER
    assert out.cited_chunk_ids == ["c1"]
    assert out.abstain_reason is None


def test_soft_abstain_관련_내용이_있지_않_normalized():
    from harag.security.output_guard import SOFT_NO_EVIDENCE_ANSWER

    guard = OutputGuard()
    text = "문서에 관련 내용이 장점으로 있지 않아 안내가 어렵습니다."
    out = guard.validate(text, ["c1"], ["c1"])
    assert out.ok
    assert out.answer == SOFT_NO_EVIDENCE_ANSWER
    assert "c1" in out.cited_chunk_ids
    assert out.abstain_reason is None


def test_pack_keeping_priority_keeps_match_under_budget():
    from harag.retrieval.context_compact import (
        approx_tokens,
        pack_keeping_priority,
    )

    match = prepend_match_context([], "매칭 " + ("요약 " * 200))[0]
    big = "가" * 4000
    pdfs = [
        _scored(f"pdf-{i}", big, score=0.9 - i * 0.01) for i in range(5)
    ]
    max_tok = 2000
    out = pack_keeping_priority(
        [match] + pdfs,
        priority_ids={MATCH_CONTEXT_CHUNK_ID},
        max_tokens=max_tok,
    )
    assert out[0].chunk.meta.chunk_id == MATCH_CONTEXT_CHUNK_ID
    match_cost = approx_tokens(match.chunk.text)
    pdf_total = sum(
        approx_tokens(sc.chunk.text) for sc in out
        if sc.chunk.meta.chunk_id != MATCH_CONTEXT_CHUNK_ID
    )
    if match_cost < max_tok:
        assert pdf_total <= max_tok - match_cost
        total = match_cost + pdf_total
        assert total <= max_tok
    else:
        assert pdf_total == 0


def test_pipeline_match_explain_skips_retrieve():
    from harag.api.pipeline import QueryPipelineImpl
    from harag.contracts.boundaries import AuthContext
    from harag.generation.generator import AnswerGenerator

    class BoomRetriever:
        def retrieve(self, *a, **k):
            raise AssertionError("match-explain must skip retrieve")

    pipe = QueryPipelineImpl(
        retriever=BoomRetriever(),
        generator=AnswerGenerator(min_score=0.15),
        web_browser=None,
        context_max_tokens=3500,
    )
    auth = AuthContext(user_id="t", acl_tags=frozenset({"owner:t"}))
    results, _tr, _intent, short = pipe._assemble_results(
        "추천된 상품의 매칭 이유를 알려주세요.",
        auth, None,
        document_id="doc-1",
        document_ids=["doc-1"],
        match_context="【왜】두통과 통원 담보가 연결됩니다.\n【상품】메리츠",
    )
    assert short is None
    assert results
    assert all(
        r.chunk.meta.chunk_id == MATCH_CONTEXT_CHUNK_ID for r in results
    )


def test_generate_retries_once_on_cost_limit():
    from harag.generation.generator import AnswerGenerator
    from harag.generation.llm_client import CostLimitError

    calls = {"n": 0}

    class FlakyLLM:
        def complete(self, query, context_texts, context_ids):
            calls["n"] += 1
            if calls["n"] == 1:
                raise CostLimitError("too big")
            return ("축소 후 답변입니다.", [context_ids[0]])

    gen = AnswerGenerator(llm=FlakyLLM(), min_score=0.1)
    ctx = [
        _scored("match-context", "매칭 안내 본문", 1.0),
        _scored("pdf-low", "낮음 " * 50, 0.2),
        _scored("pdf-high", "높음 " * 50, 0.8),
    ]
    out = gen.generate("매칭 이유?", ctx)
    assert calls["n"] == 2
    assert not out.abstained
    assert out.answer and "축소" in out.answer
