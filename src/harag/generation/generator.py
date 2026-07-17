"""
generator.py — 답변 생성기.

환각 방어 + SEC-02 출력 가드(OutputGuard)를 LLM 호출 전후에 적용.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Iterator, Protocol

from harag.security.output_guard import OutputGuard


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
        ctx_ids = [sc.chunk.meta.chunk_id for sc in context]
        ctx_texts = [sc.chunk.text for sc in context]
        return llm.complete_stream(query, ctx_texts, ctx_ids)

    def last_canary(self) -> str | None:
        return getattr(self._llm, "last_canary", None)

    def validate_output(self, answer: str | None, cited: list[str],
                        available_ids: list[str],
                        canary: str | None = None) -> GenerationResult:
        """스트림/동기 공통 출력 가드."""
        canary = canary if canary is not None else self.last_canary()
        g = self._guard.validate(answer, cited, available_ids, canary=canary)
        if not g.ok:
            return GenerationResult(
                answer=None, abstained=True, abstain_reason=g.abstain_reason)
        return GenerationResult(answer=g.answer, cited_chunk_ids=g.cited_chunk_ids)

    def generate(self, query, context) -> GenerationResult:
        pre = self.precheck(context)
        if pre is not None:
            return pre

        ctx_ids = [sc.chunk.meta.chunk_id for sc in context]
        ctx_texts = [sc.chunk.text for sc in context]
        answer, cited = self._llm.complete(query, ctx_texts, ctx_ids)
        return self.validate_output(answer, cited, ctx_ids)
