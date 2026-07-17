"""
파이프라인 회복탄력성 TDD — LLM 장애·스트리밍 경로.

  - LLM 영구 실패(rate limit 소진·타임아웃)는 500이 아니라 abstain으로 강등
  - 비용 상한 초과는 llm_cost_limit abstain
  - 스트리밍: LLM이 지원하면 토큰을 도착하는 대로 전달(실 스트리밍)
  - 스트리밍 도중 실패: 토큰 전이면 abstain, 후면 error 이벤트(중간 절단 고지)
"""
from __future__ import annotations

from datetime import datetime

import pytest

from harag.api.pipeline import QueryPipelineImpl
from harag.contracts.boundaries import AuthContext, Chunk, ScoredChunk
from harag.generation.generator import AnswerGenerator
from harag.generation.llm_client import LLMError, CostLimitError
from harag.schemas.chunk import ChunkMetadata, ChunkType

AUTH = AuthContext(user_id="u1", acl_tags=frozenset({"owner:u1"}))


def make_scored(chunk_id: str = "c1", score: float = 0.9) -> ScoredChunk:
    meta = ChunkMetadata(
        chunk_id=chunk_id, document_id="d1", source_block_ids=["b1"],
        chunk_type=ChunkType.body, security_level="normal",
        acl_tags=["owner:u1"], source_document="doc.pdf",
        citation_label="doc.pdf p.1", parsing_version="v1",
        chunking_version="v1", embedding_model_id="m1",
        content_hash="h", created_at=datetime(2026, 1, 1),
    )
    return ScoredChunk(chunk=Chunk(meta=meta, text="본문 내용"), score=score)


class FixedRetriever:
    def __init__(self, results):
        self._results = results

    def retrieve(self, query, auth, k):
        return list(self._results)


class FailingLLM:
    """complete 호출 시 지정된 예외를 던지는 LLM 대역."""
    def __init__(self, exc: Exception):
        self._exc = exc

    def complete(self, query, context_texts, context_ids):
        raise self._exc


class StreamingLLM:
    """스트리밍 지원 LLM 대역. tokens를 순서대로 yield, 지정 시점에 예외."""
    def __init__(self, tokens, fail_after: int | None = None,
                 exc: Exception | None = None):
        self._tokens = tokens
        self._fail_after = fail_after
        self._exc = exc

    def supports_streaming(self):
        return True

    def complete(self, query, context_texts, context_ids):
        return "".join(self._tokens), []

    def complete_stream(self, query, context_texts, context_ids):
        for i, tok in enumerate(self._tokens):
            if self._fail_after is not None and i >= self._fail_after:
                raise self._exc or LLMError("stream broken")
            yield tok


def make_pipeline(llm, results=None):
    results = results if results is not None else [make_scored()]
    return QueryPipelineImpl(
        retriever=FixedRetriever(results),
        generator=AnswerGenerator(llm=llm, min_score=0.5),
    )


async def test_PR01_llm_error_becomes_abstain_not_500():
    """LLM 영구 실패는 예외 전파(→500)가 아니라 graceful abstain."""
    p = make_pipeline(FailingLLM(LLMError("rate limit: 재시도 소진")))
    result = await p.answer("질문", AUTH, None)
    assert result.abstained is True
    assert result.abstain_reason == "llm_unavailable"
    assert result.answer is None


async def test_PR02_cost_limit_becomes_abstain():
    """비용 상한 초과는 llm_cost_limit abstain."""
    p = make_pipeline(FailingLLM(CostLimitError("예상 비용 초과")))
    result = await p.answer("질문", AUTH, None)
    assert result.abstained is True
    assert result.abstain_reason == "llm_cost_limit"


async def test_PR03_stream_tokens_arrive_incrementally():
    """스트리밍 LLM이면 토큰이 그대로 token 이벤트로 흐른다."""
    # 인용 마커 포함 — OutputGuard가 비기권 답에 인용 필수(A6)
    p = make_pipeline(StreamingLLM(["안녕", "하세요 [문서 1]."]))
    events = [e async for e in p.answer_stream("질문", AUTH, None)]
    kinds = [e.kind for e in events]
    assert kinds == ["token", "token", "citations", "done"]
    assert [e.data for e in events[:2]] == ["안녕", "하세요 [문서 1]."]


async def test_PR04_stream_failure_before_first_token_abstains():
    """첫 토큰 전 실패는 abstain(사용자에게 지어낸 부분 노출 없음)."""
    p = make_pipeline(StreamingLLM(["안녕"], fail_after=0))
    events = [e async for e in p.answer_stream("질문", AUTH, None)]
    assert events[0].kind == "abstain"
    assert events[0].data == "llm_unavailable"


async def test_PR05_stream_failure_mid_stream_emits_error():
    """토큰이 이미 나간 뒤 실패는 error 이벤트(절단 고지, abstain으로 못 되돌림)."""
    p = make_pipeline(StreamingLLM(["안녕", "하세요"], fail_after=1))
    events = [e async for e in p.answer_stream("질문", AUTH, None)]
    assert events[0].kind == "token"
    assert events[-1].kind == "error"
    assert events[-1].data == "llm_stream_interrupted"


async def test_PR06_stream_abstains_before_llm_on_empty_context():
    """컨텍스트가 비면 LLM을 부르지 않고 abstain(스트리밍 경로에도 pre-guard)."""
    p = make_pipeline(StreamingLLM(["안녕"]), results=[])
    events = [e async for e in p.answer_stream("질문", AUTH, None)]
    assert [e.kind for e in events] == ["abstain"]
    assert events[0].data == "empty_context"


async def test_PR07_nonstreaming_llm_falls_back_to_chunked_stream():
    """스트리밍 미지원 LLM은 완성 후 조각 전송으로 폴백(기존 동작 유지)."""
    class PlainLLM:
        def complete(self, query, context_texts, context_ids):
            body = ("가" * 30) + " [문서 1]."
            return body, [context_ids[0]]

    p = make_pipeline(PlainLLM())
    events = [e async for e in p.answer_stream("질문", AUTH, None)]
    kinds = [e.kind for e in events]
    assert kinds[-2:] == ["citations", "done"]
    assert all(k == "token" for k in kinds[:-2])
    assert "".join(e.data for e in events[:-2]) == ("가" * 30) + " [문서 1]."
