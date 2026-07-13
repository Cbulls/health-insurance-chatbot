"""
외부 LLM 클라이언트 TDD — LLMClient 인터페이스 구현 + 방어 로직.

실제 HTTP 호출(transport)은 주입식으로 분리. 클라이언트의 책임은 '방어':
  - rate limit(429) 시 백오프 재시도
  - 비용 상한 초과 예상 시 호출 차단(토큰 폭주 방어, v4)
  - 타임아웃 시 명확한 예외(NFR-4)
  - 영구 실패 시 LLMError로 — 상위(파이프라인)가 graceful degradation
  - 프로바이더 응답에서 answer·cited_ids 파싱
"""
import pytest

from harag.generation.llm_client import (
    ExternalLLMClient, LLMError, RateLimitError, CostLimitError, LLMTimeout
)


# ── 가짜 transport(HTTP 대역) ──
class FakeTransport:
    def __init__(self, responses=None, raise_seq=None):
        self._responses = responses or []
        self._raise_seq = list(raise_seq or [])
        self.calls = 0

    def post(self, payload):
        self.calls += 1
        if self._raise_seq:
            exc = self._raise_seq.pop(0)
            if exc is not None:
                raise exc
        return self._responses.pop(0) if self._responses else {
            "answer": "기본 답변", "citations": ["c1"], "tokens": 100}


def _client(transport, **kw):
    defaults = dict(model="test-model", max_cost_per_query_usd=0.50,
                    cost_per_1k_tokens=0.01, max_retries=3)
    defaults.update(kw)  # 테스트가 넘긴 값이 우선
    return ExternalLLMClient(transport=transport, **defaults)


def test_L01_normal_response_parsed():
    """정상 응답에서 answer·cited_ids 파싱."""
    t = FakeTransport(responses=[{"answer": "5만원입니다", "citations": ["c1", "c2"], "tokens": 50}])
    answer, cited = _client(t).complete("질문", ["근거"], ["c1", "c2"])
    assert answer == "5만원입니다"
    assert cited == ["c1", "c2"]


def test_L02_retries_on_rate_limit_then_succeeds():
    """429(rate limit) → 백오프 후 재시도 → 성공."""
    t = FakeTransport(
        responses=[{"answer": "성공", "citations": ["c1"], "tokens": 50}],
        raise_seq=[RateLimitError(), None])  # 첫 호출 429, 둘째 성공
    answer, _ = _client(t).complete("q", ["ctx"], ["c1"])
    assert answer == "성공"
    assert t.calls == 2, "재시도가 일어나지 않음"


def test_L03_gives_up_after_max_retries():
    """rate limit이 계속되면 max_retries 후 포기(LLMError)."""
    t = FakeTransport(raise_seq=[RateLimitError()] * 5)
    with pytest.raises(LLMError):
        _client(t).complete("q", ["ctx"], ["c1"])


def test_L04_cost_limit_blocks_before_call():
    """예상 비용이 상한 초과면 호출 자체를 차단(폭주 방어)."""
    t = FakeTransport()
    # 매우 긴 컨텍스트 → 예상 토큰이 상한 비용 초과
    huge_context = ["가" * 200000]  # 큰 입력
    with pytest.raises(CostLimitError):
        _client(t).complete("q", huge_context, ["c1"])
    assert t.calls == 0, "비용 상한 초과인데 호출됨"


def test_L05_timeout_raises():
    """타임아웃은 LLMTimeout으로."""
    t = FakeTransport(raise_seq=[LLMTimeout()])
    with pytest.raises(LLMError):  # LLMTimeout은 LLMError의 하위
        _client(t, max_retries=0).complete("q", ["ctx"], ["c1"])


def test_L06_integrates_with_generator_guards():
    """생성기에 끼웠을 때 안전 가드가 여전히 작동(위조 인용 차단)."""
    from harag.generation.generator import AnswerGenerator
    from harag.contracts.boundaries import Chunk, ScoredChunk
    from harag.schemas.chunk import ChunkMetadata, ChunkType
    from datetime import datetime, timezone

    # LLM이 컨텍스트에 없는 인용을 반환 → 생성기가 차단해야
    t = FakeTransport(responses=[{"answer": "답", "citations": ["ghost"], "tokens": 30}])
    meta = ChunkMetadata(chunk_id="real1", document_id="d", source_block_ids=["b"],
        struct_path="제1조", chunk_type=ChunkType.body, security_level="internal",
        acl_tags=["dept:fin"], source_document="x.hwp", citation_label="x 제1조",
        parsing_version="p", chunking_version="c", embedding_model_id="m",
        content_hash="h", created_at=datetime.now(timezone.utc))
    ctx = [ScoredChunk(chunk=Chunk(meta=meta, text="근거"), score=0.9)]
    gen = AnswerGenerator(llm=_client(t))
    result = gen.generate(query="q", context=ctx)
    assert result.answer is None and "fabricated" in (result.abstain_reason or ""), \
        "외부 LLM 위조 인용이 생성기 가드를 우회함"


if __name__ == "__main__":
    import sys
    sys.exit(pytest.main([__file__, "-v"]))
