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


def _is_embedding_failure(exc: BaseException) -> bool:
    """Jina 잔액·429 등 임베딩 API 실패 (스트림을 error로 떨어뜨리지 않음)."""
    from harag.embedding.api_embedder import EmbeddingRateLimitError
    if isinstance(exc, EmbeddingRateLimitError):
        return True
    name = type(exc).__name__
    if "Embedding" in name and ("Rate" in name or "Error" in name):
        return True
    msg = str(exc).lower()
    return (
        "insufficient" in msg
        or "insufficient account balance" in msg
        or "embedding" in msg and ("429" in msg or "403" in msg)
    )


MATCH_CONTEXT_CHUNK_ID = "match-context"
_MATCH_CONTEXT_SCORE = 1.0


def scored_match_context(match_context: str) -> ScoredChunk | None:
    """결과 채팅용 합성 청크. 비어 있으면 None."""
    text = (match_context or "").strip()
    if not text:
        return None
    from datetime import datetime, timezone

    from harag.contracts.boundaries import Chunk
    from harag.schemas.chunk import ChunkMetadata, ChunkType

    body = (
        "매칭 안내\n"
        "아래는 안내 단계에서 확정된 증상·상품 매칭 사실이다. "
        "장점·왜·추천 이유·연결 질문에는 이 사실로 증상·담보 연결을 설명하라.\n\n"
        + text[:4000]
    )
    meta = ChunkMetadata(
        chunk_id=MATCH_CONTEXT_CHUNK_ID,
        document_id="match-context",
        source_block_ids=["match-context"],
        struct_path="매칭안내",
        chunk_type=ChunkType.body,
        security_level="internal",
        acl_tags=["scope:library", "lib:knowledge"],
        source_document="매칭 안내",
        citation_label="매칭 안내",
        parsing_version="match-1",
        chunking_version="match-1",
        embedding_model_id="none",
        content_hash="match-context",
        created_at=datetime.now(timezone.utc),
    )
    return ScoredChunk(
        chunk=Chunk(meta=meta, text=body), score=_MATCH_CONTEXT_SCORE)


def prepend_match_context(
    results: list[ScoredChunk], match_context: str | None,
) -> list[ScoredChunk]:
    sc = scored_match_context(match_context or "")
    if sc is None:
        return results
    # 동일 id 중복 방지
    rest = [
        r for r in results
        if getattr(r.chunk.meta, "chunk_id", "") != MATCH_CONTEXT_CHUNK_ID
    ]
    return [sc] + rest


class _MatchModePrompt:
    """match_context 있을 때 LLM 시스템 지시 일시 보강."""

    def __init__(self, generator, enabled: bool):
        self._generator = generator
        self._enabled = enabled
        self._llm = None
        self._prev = None

    def __enter__(self):
        if not self._enabled:
            return self
        llm = getattr(self._generator, "_llm", None)
        if llm is None or not hasattr(llm, "_system_instruction"):
            return self
        from harag.generation.llm_client import ExternalLLMClient
        addendum = getattr(
            ExternalLLMClient, "MATCH_CONTEXT_ADDENDUM", "") or ""
        if not addendum:
            return self
        self._llm = llm
        self._prev = llm._system_instruction
        if addendum not in (self._prev or ""):
            llm._system_instruction = (self._prev or "") + addendum
        return self

    def __exit__(self, *exc):
        if self._llm is not None and self._prev is not None:
            self._llm._system_instruction = self._prev
        return False


class _WebModePrompt:
    """공식 웹 청크가 있을 때 시스템 지시 보강."""

    def __init__(self, generator, enabled: bool):
        self._generator = generator
        self._enabled = enabled
        self._llm = None
        self._prev = None

    def __enter__(self):
        if not self._enabled:
            return self
        llm = getattr(self._generator, "_llm", None)
        if llm is None or not hasattr(llm, "_system_instruction"):
            return self
        from harag.generation.llm_client import ExternalLLMClient
        addendum = getattr(ExternalLLMClient, "WEB_CONTEXT_ADDENDUM", "") or ""
        if not addendum:
            return self
        self._llm = llm
        self._prev = llm._system_instruction
        if addendum not in (self._prev or ""):
            llm._system_instruction = (self._prev or "") + addendum
        return self

    def __exit__(self, *exc):
        if self._llm is not None and self._prev is not None:
            self._llm._system_instruction = self._prev
        return False


def _has_web_chunks(results: list[ScoredChunk]) -> bool:
    from harag.retrieval.official_web import WEB_DOCUMENT_ID
    for r in results or []:
        did = str(getattr(r.chunk.meta, "document_id", "") or "")
        cid = str(getattr(r.chunk.meta, "chunk_id", "") or "")
        if did == WEB_DOCUMENT_ID or cid.startswith("web-"):
            return True
    return False


class QueryPipelineImpl:
    def __init__(self, retriever, generator, reranker=None, rewriter=None,
                 top_k: int = 20, under_load_inflight: int = 4,
                 context_dedupe: bool = True,
                 context_expand: bool = True,
                 context_max_tokens: int = 3500,
                 metadata=None,
                 web_browser=None,
                 intent_llm=None,
                 min_score: float = 0.15):
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
        self._web_browser = web_browser
        self._intent_llm = intent_llm
        self._min_score = float(min_score)
        self._store = retriever  # empty_context 진단용(count_for_owner)

    async def answer(self, query: str, auth: AuthContext,
                     conversation_id: str | None,
                     document_id: str | None = None,
                     document_ids: list[str] | None = None,
                     match_context: str | None = None) -> AnswerResult:
        return await asyncio.to_thread(
            self._answer_sync, query, auth, conversation_id,
            document_id, document_ids, match_context)

    def _doc_ids(self, document_id: str | None,
                 document_ids: list[str] | None) -> list[str]:
        out: list[str] = []
        seen: set[str] = set()
        for d in list(document_ids or []) + (
                [document_id] if document_id else []):
            s = (d or "").strip()
            if not s or s in seen:
                continue
            seen.add(s)
            out.append(s)
        return out

    def _faq_short_circuit(self, intent, docs: list[str]) -> AnswerResult | None:
        """문서 미선택 FAQ/보험료 → 큐레이션 문장으로 즉시 응답."""
        if docs:
            return None
        return self._faq_curated_answer(intent)

    def _faq_curated_answer(self, intent) -> AnswerResult | None:
        """문서 유무와 무관하게 FAQ/보험료 큐레이션 답 (임베딩 폴백용)."""
        from harag.query.context_chunks import (
            scored_faq_chunk, scored_premium_chunk,
        )
        from harag.query.faq import format_faq_answer, premium_template_answer

        if intent.label == "premium_quote":
            chunk = scored_premium_chunk()
            return AnswerResult(
                answer=premium_template_answer(),
                context_chunks=[chunk],
                abstained=False,
                abstain_reason=None,
            )
        if intent.faq is not None:
            chunk = scored_faq_chunk(intent.faq)
            return AnswerResult(
                answer=format_faq_answer(intent.faq),
                context_chunks=[chunk],
                abstained=False,
                abstain_reason=None,
            )
        return None

    def _embedding_unavailable_result(self, intent) -> AnswerResult:
        """임베딩 실패 시 FAQ 폴백 또는 embedding_unavailable abstain."""
        fb = self._faq_curated_answer(intent)
        if fb is not None:
            logger.warning(
                "embedding unavailable — FAQ/premium fallback intent=%s",
                getattr(intent, "label", None),
            )
            return fb
        return AnswerResult(
            answer=None,
            abstained=True,
            abstain_reason="embedding_unavailable",
            context_chunks=[],
        )

    def _inject_web_if_needed(
        self,
        query: str,
        intent,
        docs: list[str],
        results: list[ScoredChunk],
    ) -> list[ScoredChunk]:
        if self._web_browser is None or not docs:
            return results
        cfg = getattr(self._web_browser, "config", None)
        if cfg is not None and not getattr(cfg, "enabled", True):
            return results

        want = False
        if intent.label in ("premium_quote", "common_faq"):
            want = True
        else:
            top = max((r.score for r in results), default=0.0)
            if top < self._min_score:
                want = True
        if not want:
            return results

        from harag.query.context_chunks import resolve_selected_products
        from harag.retrieval.official_web import (
            prepend_web_chunks, resolve_allowlist_for_products,
        )

        products = resolve_selected_products(docs, self._metadata)
        entries = resolve_allowlist_for_products(products)
        if not entries:
            return results
        try:
            web = self._web_browser.browse(query, entries)
        except Exception:  # noqa: BLE001
            logger.exception("official web browse failed")
            web = []
        return prepend_web_chunks(results, web)

    def _build_faq_premium_context(
        self, intent, docs: list[str],
    ) -> list[ScoredChunk]:
        from harag.query.context_chunks import (
            resolve_selected_products,
            scored_faq_chunk,
            scored_premium_chunk,
        )

        products = resolve_selected_products(docs, self._metadata)
        p0 = products[0] if products else {}
        if intent.label == "premium_quote":
            return [scored_premium_chunk(
                display_name=p0.get("display_name", ""),
                insurer=p0.get("insurer", ""),
                summary=p0.get("summary", ""),
                cost_nature=p0.get("cost_nature", ""),
                source_url=p0.get("source_url", ""),
            )]
        out: list[ScoredChunk] = []
        if intent.faq is not None:
            out.append(scored_faq_chunk(intent.faq))
        return out

    def _assemble_results(
        self,
        query: str,
        auth: AuthContext,
        conversation_id: str | None,
        document_id: str | None,
        document_ids: list[str] | None,
        match_context: str | None,
    ) -> tuple[list[ScoredChunk], "QueryTrace", object, AnswerResult | None]:
        """intent → FAQ short-circuit 또는 검색/FAQ 컨텍스트 + 웹.

        Returns: results, trace, intent, short_circuit_or_None
        """
        from harag.query.intent import classify_intent

        docs = self._doc_ids(document_id, document_ids)
        intent = classify_intent(query, llm=self._intent_llm)
        short = self._faq_short_circuit(intent, docs)
        if short is not None:
            tid = current_trace_id() or "local"
            tr = QueryTrace(tid, query, mask=_query_mask_fn())
            return [], tr, intent, short

        # 면책·대기 등은 상품 조항이 핵심 → 문서 선택 시 RAG 유지 + FAQ 보강
        faq_id = getattr(intent.faq, "id", None) if intent.faq else None
        skip_rag = intent.label == "premium_quote" or (
            intent.label == "common_faq"
            and faq_id != "waiting_period_general"
        )
        if skip_rag:
            tid = current_trace_id() or "local"
            tr = QueryTrace(tid, query, mask=_query_mask_fn())
            results = self._build_faq_premium_context(intent, docs)
            results = self._inject_web_if_needed(query, intent, docs, results)
            results = prepend_match_context(results, match_context)
            return results, tr, intent, None

        try:
            results, tr = self._retrieve_sync(
                query, auth, conversation_id,
                document_id=document_id, document_ids=document_ids)
        except Exception as exc:  # noqa: BLE001 — 임베딩 실패만 FAQ/abstain으로
            if not _is_embedding_failure(exc):
                raise
            logger.warning("retrieve embedding failed: %s", exc)
            tid = current_trace_id() or "local"
            tr = QueryTrace(tid, query, mask=_query_mask_fn())
            return [], tr, intent, self._embedding_unavailable_result(intent)

        if intent.faq is not None:
            from harag.query.context_chunks import scored_faq_chunk
            faq_sc = scored_faq_chunk(intent.faq)
            results = [faq_sc] + [
                r for r in results
                if r.chunk.meta.chunk_id != faq_sc.chunk.meta.chunk_id
            ]
        results = self._inject_web_if_needed(query, intent, docs, results)
        results = prepend_match_context(results, match_context)
        return results, tr, intent, None

    def _retrieve_sync(self, query: str, auth: AuthContext,
                       conversation_id: str | None,
                       document_id: str | None = None,
                       document_ids: list[str] | None = None,
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
            try:
                results: list[ScoredChunk] = self._retriever.retrieve(
                    search_query, auth=auth, k=self._top_k,
                    document_id=document_id,
                    document_ids=document_ids)
            except TypeError:
                try:
                    results = self._retriever.retrieve(
                        search_query, auth=auth, k=self._top_k,
                        document_id=document_id)
                except TypeError:
                    results = self._retriever.retrieve(
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
                     conversation_id: str | None,
                     document_id: str | None = None,
                     document_ids: list[str] | None = None,
                     match_context: str | None = None) -> AnswerResult:
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
            results, tr, _intent, short = self._assemble_results(
                query, auth, conversation_id,
                document_id, document_ids, match_context)
            if short is not None:
                self._record_turn(query, conversation_id)
                est = max(len(query) // 2, 1)
                if short.answer:
                    est += len(short.answer) // 2
                event = (
                    "abstain" if short.abstained or not short.answer else "query"
                )
                self._audit(event, auth, tr, short, token_estimate=est)
                return short
            match_on = bool((match_context or "").strip())
            web_on = _has_web_chunks(results)
            with _MatchModePrompt(self._generator, match_on), \
                    _WebModePrompt(self._generator, web_on):
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
            if gen.abstained and gen.abstain_reason == "empty_context":
                logger.warning(
                    "abstain empty_context (sync) hits=%d — "
                    "index empty or still seeding; RAG withheld",
                    len(results),
                )
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
                            conversation_id: str | None,
                            document_id: str | None = None,
                            document_ids: list[str] | None = None,
                            match_context: str | None = None,
                            ) -> AsyncIterator[StreamEvent]:
        hard = _scan_query_hard(query)
        if hard is not None:
            tid = current_trace_id() or "local"
            tr = QueryTrace(tid, query, mask=_query_mask_fn())
            fake = AnswerResult(answer=None, abstained=True,
                                abstain_reason="injection_blocked")
            self._audit("injection", auth, tr, fake)
            yield StreamEvent(kind="abstain", data="injection_blocked")
            return

        def _assemble_counted():
            with _INFLIGHT:
                return self._assemble_results(
                    query, auth, conversation_id,
                    document_id, document_ids, match_context)

        results, tr, _intent, short = await asyncio.to_thread(_assemble_counted)
        if short is not None:
            self._record_turn(query, conversation_id)
            est = max(len(query) // 2, 1)
            if short.answer:
                est += len(short.answer) // 2
            if short.abstained or not short.answer:
                self._audit("abstain", auth, tr, short, token_estimate=est)
                yield StreamEvent(
                    kind="abstain",
                    data=short.abstain_reason or "no_answer")
                return
            self._audit("query", auth, tr, short, token_estimate=est)
            from harag.api.citations_fmt import (
                citations_sse_payload, sources_sse_payload)
            ctx = short.context_chunks or []
            yield StreamEvent(kind="sources", data=sources_sse_payload(ctx))
            for piece in _chunks_of(short.answer or ""):
                yield StreamEvent(kind="token", data=piece)
            yield StreamEvent(
                kind="citations", data=citations_sse_payload(ctx))
            yield StreamEvent(kind="done", data="")
            return

        pre = self._generator.precheck(results)
        if pre is not None:
            reason = pre.abstain_reason or "no_answer"
            if reason == "empty_context":
                n_pts = None
                count_fn = getattr(self._store, "count_for_owner", None)
                if callable(count_fn):
                    try:
                        n_pts = int(count_fn(list(auth.acl_tags) or []) or 0)
                    except Exception:  # noqa: BLE001
                        n_pts = None
                logger.warning(
                    "abstain empty_context trace_id=%s acl_hits=%s "
                    "(0 → index empty or still seeding; RAG withheld)",
                    getattr(tr, "trace_id", None) or current_trace_id(),
                    n_pts,
                )
            fake = AnswerResult(answer=None, abstained=True,
                                abstain_reason=reason,
                                context_chunks=[])
            self._audit("abstain", auth, tr, fake, token_estimate=len(query) // 2)
            yield StreamEvent(kind="abstain", data=reason)
            return

        match_on = bool((match_context or "").strip())
        web_on = _has_web_chunks(results)
        with _MatchModePrompt(self._generator, match_on), \
                _WebModePrompt(self._generator, web_on):
            token_iter = self._generator.stream_tokens(query, results)
        if token_iter is None:
            with _MatchModePrompt(self._generator, match_on), \
                    _WebModePrompt(self._generator, web_on):
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
            from harag.api.citations_fmt import (
                citations_sse_payload, sources_sse_payload)
            yield StreamEvent(kind="sources", data=sources_sse_payload(results))
            for piece in _chunks_of(result.answer):
                yield StreamEvent(kind="token", data=piece)
            yield StreamEvent(
                kind="citations",
                data=citations_sse_payload(result.context_chunks))
            yield StreamEvent(kind="done", data="")
            return

        from harag.api.citations_fmt import (
            citations_sse_payload, sources_sse_payload)
        yield StreamEvent(kind="sources", data=sources_sse_payload(results))

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
