"""
generator.py — 답변 생성기.

설계 핵심: 환각 방어의 두 가드는 LLM '호출 전후'의 게이트라, LLM 없이도 구현된다.
  - 생성 전 가드(G-01,G-02): 빈/저점수 컨텍스트면 LLM을 부르지 않고 abstain.
  - 생성 후 가드(G-03,G-04): 답의 인용 ID를 받은 컨텍스트와 대조, 위조면 차단.

LLM은 주입 가능한 인터페이스(LLMClient)로 둔다. 지금은 안전 가드만 TDD로 완성하고,
실제 LLM(외부 API)은 나중에 이 인터페이스 뒤에 끼운다.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Protocol


@dataclass
class GenerationResult:
    answer: str | None
    cited_chunk_ids: list[str] = field(default_factory=list)
    abstained: bool = False
    abstain_reason: str | None = None


class LLMClient(Protocol):
    """주입 가능한 LLM. 컨텍스트 텍스트 + 질의 -> (답변, 인용 청크 id 목록)."""
    def complete(self, query: str, context_texts: list[str],
                 context_ids: list[str]) -> tuple[str, list[str]]: ...


class _StubLLM:
    """LLM 미연결 시 기본 stub. 컨텍스트 첫 청크를 인용하는 정직한 더미."""
    def complete(self, query, context_texts, context_ids):
        return (f"[stub] {context_texts[0][:30]}...", [context_ids[0]])


class AnswerGenerator:
    def __init__(self, llm: LLMClient | None = None, min_score: float = 0.5):
        self._llm = llm or _StubLLM()
        self._min_score = min_score

    def generate(self, query, context) -> GenerationResult:
        # ── 생성 전 가드(G-01, G-02): abstention ──
        if not context:
            return GenerationResult(answer=None, abstained=True,
                                    abstain_reason="empty_context")
        if max(sc.score for sc in context) < self._min_score:
            return GenerationResult(answer=None, abstained=True,
                                    abstain_reason="low_score")

        # ── LLM 호출 ──
        ctx_ids = [sc.chunk.meta.chunk_id for sc in context]
        ctx_texts = [sc.chunk.text for sc in context]
        answer, cited = self._llm.complete(query, ctx_texts, ctx_ids)

        # ── 생성 후 가드(G-03, G-04): 인용 검증 ──
        available = set(ctx_ids)
        fabricated = [c for c in cited if c not in available]
        if fabricated:
            # 위조 인용 발견 -> 답 차단(여기선 abstain으로; 운영에선 재생성 1회 후 abstain)
            return GenerationResult(answer=None, abstained=True,
                                    abstain_reason=f"fabricated_citation:{fabricated}")

        return GenerationResult(answer=answer, cited_chunk_ids=cited)
