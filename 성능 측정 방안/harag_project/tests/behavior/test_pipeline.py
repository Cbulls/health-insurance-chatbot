"""
쿼리 파이프라인 TDD — 검색기 + 생성기를 읽기 경로로 조립.

파이프라인의 책임:
  - 권한(auth)을 검색기로 전달 → ACL 적용된 검색
  - 검색 결과를 생성기 컨텍스트로 변환
  - 생성기의 abstention/인용 검증 결과를 AnswerResult로
  - 게이트웨이가 기대하는 QueryPipeline 인터페이스 충족
"""
import pytest
from datetime import datetime, timezone

from harag.schemas.chunk import ChunkMetadata, ChunkType
from harag.contracts.boundaries import Chunk, ScoredChunk, AuthContext
from harag.api.pipeline import QueryPipelineImpl


def _scored(cid, text, score, acl=None):
    meta = ChunkMetadata(
        chunk_id=cid, document_id="d1", source_block_ids=["b1"], struct_path="제1조",
        chunk_type=ChunkType.body, security_level="internal",
        acl_tags=acl or ["dept:finance"], source_document="규정.hwp",
        citation_label="규정 제1조", parsing_version="p", chunking_version="c",
        embedding_model_id="m", content_hash="h", created_at=datetime.now(timezone.utc))
    return ScoredChunk(chunk=Chunk(meta=meta, text=text), score=score)


# ── 가짜 검색기·생성기 ──
class FakeRetriever:
    def __init__(self, results): self._results = results
    def retrieve(self, query, auth, k=10, mode="hybrid"):
        return self._results

class FakeLLM:
    def complete(self, query, context_texts, context_ids):
        return ("국내출장 여비는 1일 5만원입니다.", [context_ids[0]])


def _pipeline(results):
    from harag.generation.generator import AnswerGenerator
    return QueryPipelineImpl(retriever=FakeRetriever(results),
                             generator=AnswerGenerator(llm=FakeLLM(), min_score=0.5))


@pytest.mark.asyncio
async def test_PL01_answer_from_retrieved_context():
    """검색 결과가 있으면 그 근거로 답 생성."""
    results = [_scored("c1", "국내출장 여비는 1일 5만원으로 한다.", 0.9)]
    pl = _pipeline(results)
    r = await pl.answer("출장비 한도?", AuthContext(user_id="u", acl_tags=frozenset({"dept:finance"})), None)
    assert r.answer is not None
    assert not r.abstained
    assert r.context_chunks, "컨텍스트가 결과에 안 실림"


@pytest.mark.asyncio
async def test_PL02_abstain_when_no_results():
    """검색 결과 없으면 abstain(지어내지 않음)."""
    pl = _pipeline([])
    r = await pl.answer("우주여행 출장비?", AuthContext(user_id="u", acl_tags=frozenset({"dept:finance"})), None)
    assert r.answer is None
    assert r.abstained


@pytest.mark.asyncio
async def test_PL03_abstain_when_low_score():
    """검색은 됐지만 점수 낮으면 abstain."""
    pl = _pipeline([_scored("c1", "관련 낮음", 0.1)])
    r = await pl.answer("질문", AuthContext(user_id="u", acl_tags=frozenset({"dept:finance"})), None)
    assert r.abstained


@pytest.mark.asyncio
async def test_PL04_auth_passed_to_retriever():
    """권한이 검색기로 전달되는지(ACL 적용 지점)."""
    captured = {}
    class CapturingRetriever:
        def retrieve(self, query, auth, k=10, mode="hybrid"):
            captured["auth"] = auth
            return [_scored("c1", "내용", 0.9)]
    from harag.generation.generator import AnswerGenerator
    pl = QueryPipelineImpl(retriever=CapturingRetriever(),
                           generator=AnswerGenerator(llm=FakeLLM()))
    auth = AuthContext(user_id="u9", acl_tags=frozenset({"dept:hr"}))
    await pl.answer("q", auth, None)
    assert captured["auth"].user_id == "u9", "권한이 검색기로 전달 안 됨"


if __name__ == "__main__":
    import sys
    sys.exit(pytest.main([__file__, "-v"]))
