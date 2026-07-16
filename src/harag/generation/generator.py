"""
generator.py — 답변 생성기.

환각 방어의 세 가드는 LLM '호출 전후'의 게이트라, LLM 없이도 구현된다.
  - 생성 전 가드: 빈/저점수 컨텍스트면 LLM을 부르지 않고 abstain.
  - 생성 후 가드 1: 답의 인용 ID를 받은 컨텍스트와 대조, 위조면 차단.
  - 생성 후 가드 2: 인용 없이 '정보 없음'류 답변이면 abstain으로 정규화
    (LLM이 모른다는 말을 '답변'으로 내보내 abstention 지표를 깨는 것 방지).

LLM은 주입 가능한 인터페이스(LLMClient)로 둔다.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Iterator, Protocol

# '문서에 근거 없음'을 답변 텍스트로 표현하는 전형 패턴(한국어)
_NO_EVIDENCE_RE = re.compile(
    r"(정보를 찾을 수 없|정보가 (없|나와\s*있지 않|포함되어 있지 않)"
    r"|근거가 없|언급되어 있지 않|알 수 없습니다|모릅니다|모른다고?\b)")


def _looks_like_no_evidence(answer: str) -> bool:
    return bool(_NO_EVIDENCE_RE.search(answer))


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

    def precheck(self, context) -> GenerationResult | None:
        """생성 전 가드(abstention). 통과하면 None, 아니면 abstain 결과."""
        if not context:
            return GenerationResult(answer=None, abstained=True,
                                    abstain_reason="empty_context")
        if max(sc.score for sc in context) < self._min_score:
            return GenerationResult(answer=None, abstained=True,
                                    abstain_reason="low_score")
        return None

    def stream_tokens(self, query, context) -> Iterator[str] | None:
        """스트리밍 생성 진입점 — LLM이 스트리밍을 지원할 때만 iterator 반환.

        전제: precheck 통과. 미지원(local LLM 등)이면 None → 호출자가
        논스트리밍 generate()로 폴백한다."""
        llm = self._llm
        if not callable(getattr(llm, "complete_stream", None)):
            return None
        supports = getattr(llm, "supports_streaming", None)
        if callable(supports) and not supports():
            return None
        ctx_ids = [sc.chunk.meta.chunk_id for sc in context]
        ctx_texts = [sc.chunk.text for sc in context]
        return llm.complete_stream(query, ctx_texts, ctx_ids)

    def generate(self, query, context) -> GenerationResult:
        # ── 생성 전 가드: abstention ──
        pre = self.precheck(context)
        if pre is not None:
            return pre

        # ── LLM 호출 ──
        ctx_ids = [sc.chunk.meta.chunk_id for sc in context]
        ctx_texts = [sc.chunk.text for sc in context]
        answer, cited = self._llm.complete(query, ctx_texts, ctx_ids)

        # ── 생성 후 가드 1: 인용 검증(위조 인용 차단) ──
        available = set(ctx_ids)
        fabricated = [c for c in cited if c not in available]
        if fabricated:
            return GenerationResult(answer=None, abstained=True,
                                    abstain_reason=f"fabricated_citation:{fabricated}")

        # ── 생성 후 가드 2: '정보 없음' 답변 → abstain 정규화 ──
        # 마커를 붙였더라도 본문이 '문서에 정보가 없다'면 인용은 형식적 —
        # 모른다는 사실의 근거가 그 문서에 있는 게 아니므로 abstain이 맞다.
        if answer and _looks_like_no_evidence(answer):
            return GenerationResult(answer=None, abstained=True,
                                    abstain_reason="no_evidence_in_documents")

        return GenerationResult(answer=answer, cited_chunk_ids=cited)
