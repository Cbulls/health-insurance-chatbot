"""
generator.py — 답변 생성기.

환각 방어 + SEC-02 출력 가드(OutputGuard)를 LLM 호출 전후에 적용.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Iterator, Protocol

from harag.generation.llm_client import CostLimitError
from harag.security.output_guard import OutputGuard

logger = logging.getLogger("harag.generation")

# CostLimit 축소 시 유지할 합성·FAQ·웹 청크
_PROTECTED_CHUNK_PREFIXES = ("web-", "faq-", "premium-")
_PROTECTED_CHUNK_IDS = frozenset({
    "match-context", "faq-context", "premium-context",
})


@dataclass
class GenerationResult:
    answer: str | None
    cited_chunk_ids: list[str] = field(default_factory=list)
    abstained: bool = False
    abstain_reason: str | None = None


class LLMClient(Protocol):
    def complete(self, query: str, context_texts: list[str],
                 context_ids: list[str]) -> tuple[str, list[str]]: ...


class _StubLLM:
    def complete(self, query, context_texts, context_ids):
        return (f"[stub] {context_texts[0][:30]}...", [context_ids[0]])


def _is_protected_chunk(chunk_id: str) -> bool:
    cid = chunk_id or ""
    if cid in _PROTECTED_CHUNK_IDS:
        return True
    return any(cid.startswith(p) for p in _PROTECTED_CHUNK_PREFIXES)


def shrink_context_for_cost(context: list, *, drop_n: int = 3) -> list:
    """점수 낮은 비보호(PDF) 청크부터 제거. 비어 있지 않게 최소 1개 유지."""
    if not context or len(context) <= 1:
        return list(context or [])
    droppable = [
        (i, sc) for i, sc in enumerate(context)
        if not _is_protected_chunk(sc.chunk.meta.chunk_id)
    ]
    if not droppable:
        # 보호만 있으면 점수 낮은 것부터(마지막 수단)
        droppable = list(enumerate(context))
    droppable.sort(key=lambda t: t[1].score)
    drop_idx = {i for i, _ in droppable[: max(1, drop_n)]}
    # 전부 지우지 않도록
    if len(drop_idx) >= len(context):
        drop_idx = set(list(drop_idx)[: len(context) - 1])
    return [sc for i, sc in enumerate(context) if i not in drop_idx]


class AnswerGenerator:
    def __init__(self, llm: LLMClient | None = None, min_score: float = 0.5,
                 output_guard: OutputGuard | None = None):
        self._llm = llm or _StubLLM()
        self._min_score = min_score
        self._guard = output_guard or OutputGuard()

    def precheck(self, context) -> GenerationResult | None:
        if not context:
            return GenerationResult(answer=None, abstained=True,
                                    abstain_reason="empty_context")
        if max(sc.score for sc in context) < self._min_score:
            return GenerationResult(answer=None, abstained=True,
                                    abstain_reason="low_score")
        return None

    def stream_tokens(self, query, context) -> Iterator[str] | None:
        llm = self._llm
        if not callable(getattr(llm, "complete_stream", None)):
            return None
        supports = getattr(llm, "supports_streaming", None)
        if callable(supports) and not supports():
            return None
        ctx = list(context)
        try:
            ctx_ids = [sc.chunk.meta.chunk_id for sc in ctx]
            ctx_texts = [sc.chunk.text for sc in ctx]
            return llm.complete_stream(query, ctx_texts, ctx_ids)
        except CostLimitError:
            shrunk = shrink_context_for_cost(ctx)
            if len(shrunk) >= len(ctx):
                raise
            logger.warning(
                "CostLimit on stream — retry with %d→%d chunks",
                len(ctx), len(shrunk),
            )
            ctx_ids = [sc.chunk.meta.chunk_id for sc in shrunk]
            ctx_texts = [sc.chunk.text for sc in shrunk]
            return llm.complete_stream(query, ctx_texts, ctx_ids)

    def last_canary(self) -> str | None:
        return getattr(self._llm, "last_canary", None)

    def validate_output(self, answer: str | None, cited: list[str],
                        available_ids: list[str],
                        canary: str | None = None,
                        query: str = "") -> GenerationResult:
        """스트림/동기 공통 출력 가드."""
        canary = canary if canary is not None else self.last_canary()
        g = self._guard.validate(
            answer, cited, available_ids, canary=canary, query=query)
        if not g.ok:
            return GenerationResult(
                answer=None, abstained=True, abstain_reason=g.abstain_reason)
        return GenerationResult(answer=g.answer, cited_chunk_ids=g.cited_chunk_ids)

    def generate(self, query, context) -> GenerationResult:
        pre = self.precheck(context)
        if pre is not None:
            return pre

        ctx = list(context)
        ctx_ids = [sc.chunk.meta.chunk_id for sc in ctx]
        ctx_texts = [sc.chunk.text for sc in ctx]
        try:
            answer, cited = self._llm.complete(query, ctx_texts, ctx_ids)
        except CostLimitError:
            shrunk = shrink_context_for_cost(ctx)
            if len(shrunk) >= len(ctx):
                raise
            logger.warning(
                "CostLimit on generate — retry with %d→%d chunks",
                len(ctx), len(shrunk),
            )
            ctx = shrunk
            ctx_ids = [sc.chunk.meta.chunk_id for sc in ctx]
            ctx_texts = [sc.chunk.text for sc in ctx]
            answer, cited = self._llm.complete(query, ctx_texts, ctx_ids)
        return self.validate_output(answer, cited, ctx_ids, query=query)
