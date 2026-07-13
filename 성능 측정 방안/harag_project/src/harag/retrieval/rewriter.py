"""
멀티턴 질의 재작성 — 후속 질의를 독립 검색 가능한 형태로(FR-6, R-09).

"그건 언제까지 정산해?" → "출장비 정산 기한은?" (이전 맥락으로 지시어 해소)

설계 원칙:
  - 재작성 LLM은 어댑터 뒤로. 실패 시 원본 질의 폴백(graceful degradation —
    재작성 실패는 검색이 빗나갈 뿐 위험하지 않다).
  - 이력은 '질의'만 저장. 이전 답변·청크 내용은 저장하지 않는다(권한 안전:
    이전 턴의 민감 내용이 재작성으로 현재 질의에 새지 않게).
  - 권한은 이력에 캐시하지 않는다. 검색 시점에 매 턴 재평가(B-4).
  - conversation_id별 격리.

저장소는 인터페이스로 — 로컬은 메모리, 운영은 Redis 등(TTL로 자동 만료).
"""
from __future__ import annotations

from typing import Protocol


class RewriteLLM(Protocol):
    """질의 + 이전 질의 이력 → 재작성된 독립 질의. 실제론 경량 LLM."""
    def rewrite(self, query: str, history: list[str]) -> str: ...


class ConversationStore:
    """대화별 질의 이력. 질의 문자열만 저장(답변·청크 미저장 — 권한 안전).

    운영에선 Redis + TTL로 교체. 여기선 메모리 구현.
    """
    def __init__(self, max_turns: int = 5):
        self._store: dict[str, list[str]] = {}
        self._max_turns = max_turns

    def append(self, conversation_id: str, query: str) -> None:
        hist = self._store.setdefault(conversation_id, [])
        hist.append(query)
        # 최근 N턴만 유지(맥락 폭주·비용 방어)
        if len(hist) > self._max_turns:
            del hist[0]

    def get(self, conversation_id: str) -> list[str]:
        return list(self._store.get(conversation_id, []))


class QueryRewriter:
    def __init__(self, llm: RewriteLLM, store: ConversationStore):
        self._llm = llm
        self._store = store

    def rewrite_for_search(self, query: str,
                           conversation_id: str | None) -> str:
        # 단발 질의(대화 없음)는 재작성 불필요
        if conversation_id is None:
            return query

        history = self._store.get(conversation_id)
        # 첫 턴(이력 없음)은 재작성 불필요
        if not history:
            return query

        # 재작성 시도 — 실패하면 원본 폴백(graceful degradation)
        try:
            rewritten = self._llm.rewrite(query, history)
            return rewritten or query
        except Exception:
            return query

    def record_turn(self, conversation_id: str, query: str) -> None:
        """턴 종료 시 질의를 이력에 기록(다음 턴 재작성용)."""
        self._store.append(conversation_id, query)
