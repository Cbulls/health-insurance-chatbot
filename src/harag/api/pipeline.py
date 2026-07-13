"""
쿼리 파이프라인 — 읽기 경로 오케스트레이션(검색 → 생성).

책임:
  - 권한(auth)을 검색기로 전달 → 검색 시점 ACL(MVP: owner 격리)
  - 검색 결과(ScoredChunk)를 생성기 컨텍스트로 전달
  - 생성기의 abstention·인용 검증 결과를 AnswerResult로 변환
라우트는 이 파이프라인만 호출하고, 검색·생성 내부는 모른다(얇은 라우트).
"""
from __future__ import annotations

import asyncio
from typing import AsyncIterator

from harag.contracts.boundaries import AuthContext, ScoredChunk
from harag.api.deps import AnswerResult, StreamEvent


class QueryPipelineImpl:
    def __init__(self, retriever, generator, reranker=None, rewriter=None,
                 top_k: int = 20):
        self._retriever = retriever
        self._generator = generator
        self._reranker = reranker      # None이면 리랭킹 생략(Phase 2)
        self._rewriter = rewriter      # None이면 멀티턴 재작성 생략(Phase 2)
        self._top_k = top_k

    async def answer(self, query: str, auth: AuthContext,
                     conversation_id: str | None) -> AnswerResult:
        # 검색·리랭크·재작성·생성은 모두 동기(httpx·Qdrant·CPU)라 이벤트 루프를
        # 막는다. 워커 스레드로 오프로드해 다른 요청(헬스체크 등)을 굶기지 않는다.
        return await asyncio.to_thread(
            self._answer_sync, query, auth, conversation_id)

    def _answer_sync(self, query: str, auth: AuthContext,
                     conversation_id: str | None) -> AnswerResult:
        search_query = query
        if self._rewriter is not None:
            search_query = self._rewriter.rewrite_for_search(query, conversation_id)

        results: list[ScoredChunk] = self._retriever.retrieve(
            search_query, auth=auth, k=self._top_k)

        if self._reranker is not None and results:
            results = self._reranker.rerank(search_query, results)

        gen = self._generator.generate(query=query, context=results)

        if self._rewriter is not None and conversation_id is not None:
            self._rewriter.record_turn(conversation_id, query)

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
        # MVP: 전체 답을 어절 단위로 흘려 스트리밍 체감 제공(실 스트리밍 LLM은 Phase 2)
        for piece in _chunks_of(result.answer):
            yield StreamEvent(kind="token", data=piece)
        labels = _unique_labels(result.context_chunks)
        yield StreamEvent(kind="citations", data="; ".join(labels))
        yield StreamEvent(kind="done", data="")


def _chunks_of(text: str, size: int = 24) -> list[str]:
    return [text[i:i + size] for i in range(0, len(text), size)] or [text]


def _unique_labels(chunks: list[ScoredChunk]) -> list[str]:
    seen, out = set(), []
    for c in chunks:
        lbl = c.chunk.meta.citation_label
        if lbl not in seen:
            seen.add(lbl)
            out.append(lbl)
    return out
