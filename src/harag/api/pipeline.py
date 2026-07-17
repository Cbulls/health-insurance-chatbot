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
import logging
import threading
from typing import AsyncIterator

from harag.contracts.boundaries import AuthContext, ScoredChunk
from harag.api.deps import AnswerResult, StreamEvent
from harag.api.middleware import current_trace_id
from harag.generation.llm_client import LLMError, CostLimitError
from harag.observability.tracing import QueryTrace
from harag.retrieval.context_compact import dedupe_scored_chunks

logger = logging.getLogger("harag.api")


class _InflightGauge:
    """동시 질의 수 — RR-04 under_load 신호."""

    def __init__(self) -> None:
        self._n = 0
        self._lock = threading.Lock()

    def __enter__(self) -> int:
        with self._lock:
            self._n += 1
            return self._n

    def __exit__(self, *exc) -> None:
        with self._lock:
            self._n = max(0, self._n - 1)

    @property
    def value(self) -> int:
        with self._lock:
            return self._n

    def _reset_for_tests(self) -> None:
        with self._lock:
            self._n = 0


_INFLIGHT = _InflightGauge()


class QueryPipelineImpl:
    def __init__(self, retriever, generator, reranker=None, rewriter=None,
                 top_k: int = 20, under_load_inflight: int = 4,
                 context_dedupe: bool = True):
        self._retriever = retriever
        self._generator = generator
        self._reranker = reranker
        self._rewriter = rewriter
        self._top_k = top_k
        self._under_load_inflight = max(1, under_load_inflight)
        self._context_dedupe = context_dedupe

    async def answer(self, query: str, auth: AuthContext,
                     conversation_id: str | None) -> AnswerResult:
        # 검색·리랭크·재작성·생성은 모두 동기(httpx·Qdrant·CPU)라 이벤트 루프를
        # 막는다. 워커 스레드로 오프로드해 다른 요청(헬스체크 등)을 굶기지 않는다.
        return await asyncio.to_thread(
            self._answer_sync, query, auth, conversation_id)

    def _retrieve_sync(self, query: str, auth: AuthContext,
                       conversation_id: str | None) -> list[ScoredChunk]:
        """재작성 → 검색 → 리랭크 → (선택) 중복 제거."""
        tid = current_trace_id() or "local"
        tr = QueryTrace(tid, query)

        search_query = query
        with tr.stage("rewrite"):
            if self._rewriter is not None:
                search_query = self._rewriter.rewrite_for_search(
                    query, conversation_id)

        with tr.stage("retrieval"):
            results: list[ScoredChunk] = self._retriever.retrieve(
                search_query, auth=auth, k=self._top_k)

        under_load = _INFLIGHT.value >= self._under_load_inflight
        with tr.stage("rerank"):
            if self._reranker is not None and results:
                results = self._reranker.rerank(
                    search_query, results, under_load=under_load)

        if self._context_dedupe and results:
            before = len(results)
            results = dedupe_scored_chunks(results)
            if len(results) < before:
                logger.info(
                    "context dedupe: %d → %d (trace=%s)",
                    before, len(results), tid)

        tr.record_retrieval(
            [(s.chunk.meta.chunk_id, s.score) for s in results])
        snap = tr.snapshot()
        logger.info(
            "query stages trace_id=%s under_load=%s stages=%s top_scores=%s",
            tid, under_load,
            {k: round(v, 4) for k, v in snap["stages"].items()},
            [(cid, round(sc, 4)) for cid, sc in snap["retrieved"][:5]],
        )
        return results

    def _record_turn(self, query: str, conversation_id: str | None) -> None:
        if self._rewriter is not None and conversation_id is not None:
            self._rewriter.record_turn(conversation_id, query)

    def _answer_sync(self, query: str, auth: AuthContext,
                     conversation_id: str | None) -> AnswerResult:
        with _INFLIGHT:
            results = self._retrieve_sync(query, auth, conversation_id)
            return self._generate_sync(query, results, conversation_id)

    def _generate_sync(self, query: str, results: list[ScoredChunk],
                       conversation_id: str | None) -> AnswerResult:
        # LLM 장애(rate limit 소진·비용 상한·타임아웃)는 500이 아니라
        # abstain으로 강등한다(graceful degradation — 검색까지는 성공했으므로).
        try:
            gen = self._generator.generate(query=query, context=results)
        except CostLimitError as e:
            logger.warning("LLM cost limit: %s", e)
            return AnswerResult(answer=None, abstained=True,
                                abstain_reason="llm_cost_limit")
        except LLMError as e:
            logger.warning("LLM unavailable: %s", e)
            return AnswerResult(answer=None, abstained=True,
                                abstain_reason="llm_unavailable")

        self._record_turn(query, conversation_id)

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
        def _retrieve_counted():
            with _INFLIGHT:
                return self._retrieve_sync(query, auth, conversation_id)

        results = await asyncio.to_thread(_retrieve_counted)
        pre = self._generator.precheck(results)
        if pre is not None:
            yield StreamEvent(kind="abstain", data=pre.abstain_reason or "no_answer")
            return

        token_iter = self._generator.stream_tokens(query, results)
        if token_iter is None:
            # LLM이 스트리밍 미지원(local 폴백 등) — 완성 후 조각 전송(체감 스트리밍).
            result = await asyncio.to_thread(
                self._generate_sync, query, results, conversation_id)
            if result.abstained or result.answer is None:
                yield StreamEvent(kind="abstain",
                                  data=result.abstain_reason or "no_answer")
                return
            for piece in _chunks_of(result.answer):
                yield StreamEvent(kind="token", data=piece)
            labels = _unique_labels(result.context_chunks)
            yield StreamEvent(kind="citations", data="; ".join(labels))
            yield StreamEvent(kind="done", data="")
            return

        # 실 스트리밍: LLM 토큰을 도착하는 대로 전달(TTFT = 검색+첫 토큰).
        # 토큰 대기는 블로킹 I/O라 next()를 워커 스레드로 오프로드한다.
        # 답변 전문을 모아 종료 후 [문서 N] 마커를 파싱한다(구조화 인용).
        emitted = False
        pieces: list[str] = []
        it = iter(token_iter)
        try:
            while True:
                piece = await asyncio.to_thread(next, it, _STREAM_DONE)
                if piece is _STREAM_DONE:
                    break
                emitted = True
                pieces.append(piece)
                yield StreamEvent(kind="token", data=piece)
        except CostLimitError as e:
            logger.warning("LLM cost limit (stream): %s", e)
            yield StreamEvent(kind="abstain", data="llm_cost_limit")
            return
        except LLMError as e:
            logger.warning("LLM stream failed (emitted=%s): %s", emitted, e)
            if emitted:
                yield StreamEvent(kind="error", data="llm_stream_interrupted")
            else:
                yield StreamEvent(kind="abstain", data="llm_unavailable")
            return

        if not emitted:
            yield StreamEvent(kind="abstain", data="empty_answer")
            return

        self._record_turn(query, conversation_id)
        # 답변이 실제로 인용한 청크만 출처로 노출. 마커가 없으면(모델 미준수)
        # 검색 결과 전체로 폴백 — 출처를 아예 숨기는 것보다 낫다.
        from harag.generation.citations import extract_cited_ids
        cited = set(extract_cited_ids(
            "".join(pieces), [r.chunk.meta.chunk_id for r in results]))
        cited_chunks = [r for r in results if r.chunk.meta.chunk_id in cited] \
            if cited else results
        labels = _unique_labels(cited_chunks)
        yield StreamEvent(kind="citations", data="; ".join(labels))
        yield StreamEvent(kind="done", data="")


_STREAM_DONE = object()  # 스트림 종료 센티널(next 기본값)


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
