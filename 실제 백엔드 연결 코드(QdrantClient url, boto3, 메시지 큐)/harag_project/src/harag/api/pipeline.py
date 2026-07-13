"""
쿼리 파이프라인 — 읽기 경로 오케스트레이션(검색 → 생성).

게이트웨이의 QueryPipeline 인터페이스를 구현한다. 책임:
  - 권한(auth)을 검색기로 전달 → 검색 시점 ACL(B-4)
  - 검색 결과(ScoredChunk)를 생성기 컨텍스트로 전달
  - 생성기의 abstention·인용 검증 결과를 AnswerResult로 변환
라우트는 이 파이프라인만 호출하고, 검색·생성 내부는 모른다(얇은 라우트).
"""
from __future__ import annotations

from typing import AsyncIterator

from harag.contracts.boundaries import AuthContext, ScoredChunk
from harag.api.deps import AnswerResult, StreamEvent


class QueryPipelineImpl:
    def __init__(self, retriever, generator, reranker=None, rewriter=None,
                 top_k: int = 40):
        self._retriever = retriever
        self._generator = generator
        self._reranker = reranker      # None이면 리랭킹 생략
        self._rewriter = rewriter      # None이면 멀티턴 재작성 생략
        self._top_k = top_k

    async def answer(self, query: str, auth: AuthContext,
                     conversation_id: str | None) -> AnswerResult:
        # 0) 멀티턴 재작성 — 후속 질의를 독립 검색 가능 형태로(있을 때만)
        #    권한은 재작성과 무관하게 아래 검색 시점에 재평가됨(B-4)
        search_query = query
        if self._rewriter is not None:
            search_query = self._rewriter.rewrite_for_search(query, conversation_id)

        # 1) 검색(권한 적용) — auth가 검색기로 흘러 ACL pre-filter
        results: list[ScoredChunk] = self._retriever.retrieve(
            search_query, auth=auth, k=self._top_k)

        # 2) 리랭킹 — 정밀 재순위로 top-N 선별(있을 때만)
        if self._reranker is not None and results:
            results = self._reranker.rerank(search_query, results)

        # 3) 생성(안전 가드는 생성기 내부)
        gen = self._generator.generate(query=query, context=results)

        # 4) 턴 기록(다음 턴 재작성용 — 질의만, 권한 안전)
        if self._rewriter is not None and conversation_id is not None:
            self._rewriter.record_turn(conversation_id, query)

        # 5) AnswerResult로 변환. 인용된 청크만 컨텍스트로 노출(출처 표시용).
        cited = set(gen.cited_chunk_ids)
        context_chunks = [r for r in results if r.chunk.meta.chunk_id in cited] \
            if cited else results
        return AnswerResult(
            answer=gen.answer,
            context_chunks=context_chunks if gen.answer else [],
            abstained=gen.abstained,
            abstain_reason=gen.abstain_reason,
        )

    async def answer_stream(self, query: str, auth: AuthContext,
                            conversation_id: str | None) -> AsyncIterator[StreamEvent]:
        # abstention은 스트리밍 전에 결정(지어낸 토큰 노출 방지)
        result = await self.answer(query, auth, conversation_id)
        if result.abstained or result.answer is None:
            yield StreamEvent(kind="abstain", data=result.abstain_reason or "no_answer")
            return
        # 실제 스트리밍 LLM이면 토큰 단위. 여기선 전체 답을 한 이벤트로.
        yield StreamEvent(kind="token", data=result.answer)
        labels = [c.chunk.meta.citation_label for c in result.context_chunks]
        yield StreamEvent(kind="citations", data="; ".join(labels))
        yield StreamEvent(kind="done", data="")
