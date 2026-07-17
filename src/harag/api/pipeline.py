"""
쿼리 파이프라인 — 읽기 경로 오케스트레이션(검색 → 생성).

질의 audit·메트릭·PII 마스킹된 로그·일일 예산 카운트를 포함.
"""
from __future__ import annotations

import asyncio
import json
import logging
import threading
from typing import AsyncIterator

from harag.contracts.boundaries import AuthContext, ScoredChunk
from harag.api.deps import AnswerResult, StreamEvent
from harag.api.middleware import current_trace_id
from harag.generation.llm_client import LLMError, CostLimitError
from harag.observability.tracing import QueryTrace

logger = logging.getLogger("harag.api")


class _InflightGauge:
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


def _query_mask_fn():
    from harag.config.settings import get_settings
    if not get_settings().pii_mask_enabled:
        return None
    from harag.security.pii import PiiMasker
    masker = PiiMasker()

    def _mask(q: str) -> str:
        masked, _ = masker.mask(q)
        return masked

    return _mask


class QueryPipelineImpl:
    def __init__(self, retriever, generator, reranker=None, rewriter=None,
                 top_k: int = 20, under_load_inflight: int = 4,
                 context_dedupe: bool = True,
                 metadata=None):
        self._retriever = retriever
        self._generator = generator
        self._reranker = reranker
        self._rewriter = rewriter
        self._top_k = top_k
        self._under_load_inflight = max(1, under_load_inflight)
        self._context_dedupe = context_dedupe
        self._metadata = metadata

    async def answer(self, query: str, auth: AuthContext,
                     conversation_id: str | None) -> AnswerResult:
        return await asyncio.to_thread(
            self._answer_sync, query, auth, conversation_id)

    def _retrieve_sync(self, query: str, auth: AuthContext,
                       conversation_id: str | None
                       ) -> tuple[list[ScoredChunk], QueryTrace]:
        tid = current_trace_id() or "local"
        tr = QueryTrace(tid, query, mask=_query_mask_fn())

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
            from harag.retrieval.context_compact import dedupe_scored_chunks
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
        return results, tr

    def _record_turn(self, query: str, conversation_id: str | None) -> None:
        if self._rewriter is not None and conversation_id is not None:
            self._rewriter.record_turn(conversation_id, query)

    def _audit(self, event: str, auth: AuthContext, tr: QueryTrace,
               result: AnswerResult | None, token_estimate: int = 0) -> None:
        from harag.observability import metrics_export
        from harag.api import daily_budget

        snap = tr.snapshot()
        cited = []
        doc_ids = []
        abstained = True
        reason = ""
        if result is not None:
            abstained = result.abstained or result.answer is None
            reason = result.abstain_reason or ""
            for sc in result.context_chunks or []:
                cited.append(sc.chunk.meta.chunk_id)
                src = getattr(sc.chunk.meta, "source_document", "") or ""
                if src:
                    doc_ids.append(src)

        top = snap["retrieved"][0][1] if snap["retrieved"] else None
        metrics_export.record_query(
            abstained=abstained,
            top_score=top,
            stages=snap.get("stages") or {},
            egress_tokens=token_estimate,
        )
        daily_budget.record_question(auth.user_id)
        if token_estimate:
            daily_budget.record_tokens(auth.user_id, token_estimate)

        if self._metadata is None:
            return
        detail = json.dumps({
            "query_redacted": snap.get("query", ""),
            "citation_chunk_ids": cited,
            "doc_ids": list(dict.fromkeys(doc_ids)),
            "abstain_reason": reason,
            "token_estimate": token_estimate,
            "latency_ms": int(sum(snap.get("stages", {}).values()) * 1000),
            "top_chunk_ids": [c for c, _ in snap.get("retrieved", [])[:8]],
        }, ensure_ascii=False)
        try:
            self._metadata.log_audit(
                event=event, user_id=auth.user_id,
                detail=detail[:4000], trace_id=tr.trace_id)
        except Exception:  # noqa: BLE001
            logger.exception("query audit failed")

    def _answer_sync(self, query: str, auth: AuthContext,
                     conversation_id: str | None) -> AnswerResult:
        with _INFLIGHT:
            results, tr = self._retrieve_sync(query, auth, conversation_id)
            result = self._generate_sync(query, results, conversation_id)
            est = max(len(query) // 2, 1)
            if result.answer:
                est += len(result.answer) // 2
            event = "abstain" if result.abstained or not result.answer else "query"
            if result.abstain_reason in ("llm_unavailable", "llm_cost_limit"):
                event = "error"
            self._audit(event, auth, tr, result, token_estimate=est)
            return result

    def _generate_sync(self, query: str, results: list[ScoredChunk],
                       conversation_id: str | None) -> AnswerResult:
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
        def _retrieve_counted():
            with _INFLIGHT:
                return self._retrieve_sync(query, auth, conversation_id)

        results, tr = await asyncio.to_thread(_retrieve_counted)
        pre = self._generator.precheck(results)
        if pre is not None:
            fake = AnswerResult(answer=None, abstained=True,
                                abstain_reason=pre.abstain_reason or "no_answer",
                                context_chunks=[])
            self._audit("abstain", auth, tr, fake, token_estimate=len(query) // 2)
            yield StreamEvent(kind="abstain", data=pre.abstain_reason or "no_answer")
            return

        token_iter = self._generator.stream_tokens(query, results)
        if token_iter is None:
            result = await asyncio.to_thread(
                self._generate_sync, query, results, conversation_id)
            est = max(len(query) // 2, 1)
            if result.answer:
                est += len(result.answer) // 2
            event = "abstain" if result.abstained or not result.answer else "query"
            self._audit(event, auth, tr, result, token_estimate=est)
            if result.abstained or result.answer is None:
                yield StreamEvent(kind="abstain",
                                  data=result.abstain_reason or "no_answer")
                return
            for piece in _chunks_of(result.answer):
                yield StreamEvent(kind="token", data=piece)
            from harag.api.citations_fmt import citations_sse_payload
            yield StreamEvent(
                kind="citations",
                data=citations_sse_payload(result.context_chunks))
            yield StreamEvent(kind="done", data="")
            return

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
            fake = AnswerResult(answer=None, abstained=True,
                                abstain_reason="llm_cost_limit")
            self._audit("error", auth, tr, fake)
            yield StreamEvent(kind="abstain", data="llm_cost_limit")
            return
        except LLMError as e:
            logger.warning("LLM stream failed (emitted=%s): %s", emitted, e)
            fake = AnswerResult(answer=None, abstained=True,
                                abstain_reason="llm_unavailable")
            self._audit("error", auth, tr, fake)
            if emitted:
                yield StreamEvent(kind="error", data="llm_stream_interrupted")
            else:
                yield StreamEvent(kind="abstain", data="llm_unavailable")
            return

        if not emitted:
            fake = AnswerResult(answer=None, abstained=True,
                                abstain_reason="empty_answer")
            self._audit("abstain", auth, tr, fake)
            yield StreamEvent(kind="abstain", data="empty_answer")
            return

        self._record_turn(query, conversation_id)
        from harag.generation.citations import extract_cited_ids
        cited = set(extract_cited_ids(
            "".join(pieces), [r.chunk.meta.chunk_id for r in results]))
        cited_chunks = [r for r in results if r.chunk.meta.chunk_id in cited] \
            if cited else results
        answer_text = "".join(pieces)
        result = AnswerResult(
            answer=answer_text, context_chunks=cited_chunks,
            abstained=False, abstain_reason=None)
        est = max((len(query) + len(answer_text)) // 2, 1)
        self._audit("query", auth, tr, result, token_estimate=est)
        from harag.api.citations_fmt import citations_sse_payload
        yield StreamEvent(
            kind="citations", data=citations_sse_payload(cited_chunks))
        yield StreamEvent(kind="done", data="")


_STREAM_DONE = object()


def _chunks_of(text: str, size: int = 24) -> list[str]:
    return [text[i:i + size] for i in range(0, len(text), size)] or [text]
