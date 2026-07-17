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


def _scan_query_hard(query: str):
    """질의 hard 인젝션이면 InjectionVerdict, 아니면 None."""
    from harag.security.injection import InjectionScanner, policy_from_settings
    from harag.observability import metrics_export
    pol = policy_from_settings()
    if not pol.enabled or not pol.scan_query:
        return None
    v = InjectionScanner(hard_refuse_score=pol.hard_refuse_score).verdict(
        query, source="query", policy=pol)
    if v.is_soft or v.is_hard:
        metrics_export.record_injection(
            soft=v.is_soft and not v.is_hard, hard=v.is_hard)
    if v.is_hard:
        return v
    return None


class QueryPipelineImpl:
    def __init__(self, retriever, generator, reranker=None, rewriter=None,
                 top_k: int = 20, under_load_inflight: int = 4,
                 context_dedupe: bool = True,
                 context_expand: bool = True,
                 context_max_tokens: int = 3500,
                 metadata=None):
        self._retriever = retriever
        self._generator = generator
        self._reranker = reranker
        self._rewriter = rewriter
        self._top_k = top_k
        self._under_load_inflight = max(1, under_load_inflight)
        self._context_dedupe = context_dedupe
        self._context_expand = context_expand
        self._context_max_tokens = max(500, context_max_tokens)
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
            # A4: 단일턴·재작성 후 보험 동의어 확장(검색 전용)
            from harag.retrieval.query_normalize import expand_query_for_search
            search_query = expand_query_for_search(search_query)

        with tr.stage("retrieval"):
            results: list[ScoredChunk] = self._retriever.retrieve(
                search_query, auth=auth, k=self._top_k)

        under_load = _INFLIGHT.value >= self._under_load_inflight
        with tr.stage("rerank"):
            if self._reranker is not None and results:
                results = self._reranker.rerank(
                    search_query, results, under_load=under_load)

        if self._context_dedupe and results:
            from harag.retrieval.context_compact import prepare_context
            before = len(results)
            results = prepare_context(
                results,
                max_tokens=self._context_max_tokens,
                expand=self._context_expand,
            )
            if len(results) != before:
                logger.info(
                    "context pack: %d → %d (trace=%s)",
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
        hard = _scan_query_hard(query)
        if hard is not None:
            tid = current_trace_id() or "local"
            tr = QueryTrace(tid, query, mask=_query_mask_fn())
            result = AnswerResult(
                answer=None, abstained=True,
                abstain_reason="injection_blocked")
            self._audit("injection", auth, tr, result, token_estimate=0)
            if self._metadata is not None:
                try:
                    self._metadata.log_audit(
                        event="injection_query",
                        user_id=auth.user_id,
                        detail=json.dumps({
                            "level": "hard",
                            "score": hard.risk.score,
                            "matched": hard.risk.matched[:8],
                        }, ensure_ascii=False)[:2000],
                        trace_id=tid,
                    )
                except Exception:  # noqa: BLE001
                    pass
            return result

        with _INFLIGHT:
            results, tr = self._retrieve_sync(query, auth, conversation_id)
            result = self._generate_sync(query, results, conversation_id)
            est = max(len(query) // 2, 1)
            if result.answer:
                est += len(result.answer) // 2
            event = "abstain" if result.abstained or not result.answer else "query"
            if result.abstain_reason in ("llm_unavailable", "llm_cost_limit"):
                event = "error"
            if result.abstain_reason == "injection_canary_leak":
                event = "injection"
                from harag.observability import metrics_export
                metrics_export.record_injection(canary_leak=True)
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

        # A6: 마커 없으면 전 청크를 citation으로 노출하지 않음
        cited = set(gen.cited_chunk_ids)
        context_chunks = [
            r for r in results if r.chunk.meta.chunk_id in cited]
        return AnswerResult(
            answer=gen.answer,
            context_chunks=context_chunks if gen.answer else [],
            abstained=gen.abstained,
            abstain_reason=gen.abstain_reason,
        )

    async def answer_stream(self, query: str, auth: AuthContext,
                            conversation_id: str | None) -> AsyncIterator[StreamEvent]:
        hard = _scan_query_hard(query)
        if hard is not None:
            tid = current_trace_id() or "local"
            tr = QueryTrace(tid, query, mask=_query_mask_fn())
            fake = AnswerResult(answer=None, abstained=True,
                                abstain_reason="injection_blocked")
            self._audit("injection", auth, tr, fake)
            yield StreamEvent(kind="abstain", data="injection_blocked")
            return

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
            if result.abstain_reason == "injection_canary_leak":
                event = "injection"
                from harag.observability import metrics_export
                metrics_export.record_injection(canary_leak=True)
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
        try:
            async for piece in _bridge_sync_token_iter(token_iter):
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
        answer_text = "".join(pieces)
        ctx_ids = [r.chunk.meta.chunk_id for r in results]
        cited_list = extract_cited_ids(answer_text, ctx_ids)
        # 스트림 종료 후 OutputGuard (canary·위조 인용·no_evidence·인용 필수)
        guarded = self._generator.validate_output(
            answer_text, cited_list, ctx_ids)
        if guarded.abstained or guarded.answer is None:
            reason = guarded.abstain_reason or "no_answer"
            if reason == "injection_canary_leak":
                from harag.observability import metrics_export
                metrics_export.record_injection(canary_leak=True)
            fake = AnswerResult(answer=None, abstained=True,
                                abstain_reason=reason)
            self._audit(
                "injection" if reason == "injection_canary_leak" else "abstain",
                auth, tr, fake)
            yield StreamEvent(kind="revoke", data=reason)
            yield StreamEvent(kind="abstain", data=reason)
            return

        cited = set(guarded.cited_chunk_ids)
        cited_chunks = [
            r for r in results if r.chunk.meta.chunk_id in cited]
        result = AnswerResult(
            answer=guarded.answer, context_chunks=cited_chunks,
            abstained=False, abstain_reason=None)
        est = max((len(query) + len(answer_text)) // 2, 1)
        self._audit("query", auth, tr, result, token_estimate=est)
        from harag.api.citations_fmt import citations_sse_payload
        yield StreamEvent(
            kind="citations", data=citations_sse_payload(cited_chunks))
        yield StreamEvent(kind="done", data="")


_STREAM_DONE = object()


async def _bridge_sync_token_iter(sync_iter) -> AsyncIterator[str]:
    """동기 토큰 이터레이터 → asyncio.Queue 브리지(토큰마다 to_thread 제거)."""
    loop = asyncio.get_running_loop()
    q: asyncio.Queue = asyncio.Queue(maxsize=64)

    def _producer() -> None:
        try:
            for piece in sync_iter:
                fut = asyncio.run_coroutine_threadsafe(q.put(piece), loop)
                fut.result(timeout=120)
        except BaseException as exc:  # noqa: BLE001 — 스트림으로 전달
            fut = asyncio.run_coroutine_threadsafe(q.put(exc), loop)
            try:
                fut.result(timeout=5)
            except Exception:  # noqa: BLE001
                pass
        finally:
            try:
                asyncio.run_coroutine_threadsafe(
                    q.put(_STREAM_DONE), loop).result(timeout=5)
            except Exception:  # noqa: BLE001
                pass

    threading.Thread(target=_producer, daemon=True, name="harag-stream").start()
    while True:
        item = await q.get()
        if item is _STREAM_DONE:
            break
        if isinstance(item, BaseException):
            raise item
        yield item


def _chunks_of(text: str, size: int = 24) -> list[str]:
    return [text[i:i + size] for i in range(0, len(text), size)] or [text]
