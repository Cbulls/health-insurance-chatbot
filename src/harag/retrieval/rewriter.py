"""
멀티턴 질의 재작성 — 후속 질의를 독립 검색 가능한 형태로(FR-6, R-09).

"그건 언제까지 정산해?" → "출장비 정산 기한은?" (이전 맥락으로 지시어 해소)

설계 원칙:
  - 재작성 LLM은 어댑터 뒤로. 실패 시 원본 질의 폴백(graceful degradation —
    재작성 실패는 검색이 빗나갈 뿐 위험하지 않다).
  - 지시어·짧은 후속문이 없으면 LLM 재작성을 건너뛴다(P1: 불필요 호출 절감).
  - 이력은 '질의'만 저장. 이전 답변·청크 내용은 저장하지 않는다(권한 안전).
  - 권한은 이력에 캐시하지 않는다. 검색 시점에 매 턴 재평가(B-4).
  - conversation_id별 격리.
"""
from __future__ import annotations

import re
import threading
from collections import OrderedDict
from typing import Protocol

# 지시어·대용 표현 — 있을 때만 LLM rewrite (없으면 원문 유지)
_DEIXIS_RE = re.compile(
    r"(그것|그건|그게|그거|저것|저건|이것|이건|이거|"
    r"거기|여기|저쪽|상기|해당|"
    r"그\s*(규정|조항|내용|금액|한도|기한)|"
    r"앞(서|의)|위\s*(내용|규정)|"
    r"그럼|그래서|그리고\s*그건)",
    re.IGNORECASE,
)


class RewriteLLM(Protocol):
    """질의 + 이전 질의 이력 → 재작성된 독립 질의. 실제론 경량 LLM."""
    def rewrite(self, query: str, history: list[str]) -> str: ...


def needs_rewrite(query: str, history: list[str]) -> bool:
    """이력이 있고 지시어·초단문 후속이면 LLM 재작성 대상."""
    if not history:
        return False
    q = (query or "").strip()
    if not q:
        return False
    if _DEIXIS_RE.search(q):
        return True
    # 매우 짧은 후속("얼마야?", "기한은?") — 맥락 의존 가능성
    if len(q) <= 10:
        return True
    return False


class ConversationStore:
    """대화별 질의 이력. 질의 문자열만 저장(답변·청크 미저장 — 권한 안전).

    운영에선 Redis + TTL로 교체. 여기선 메모리 구현. conversation_id가 무한히
    쌓여 메모리가 새지 않도록 LRU 상한을 두고, 여러 스레드(쿼리 오프로드)에서
    안전하게 접근하도록 락으로 보호한다.
    """
    def __init__(self, max_turns: int = 5, max_conversations: int = 10000):
        self._store: OrderedDict[str, list[str]] = OrderedDict()
        self._max_turns = max_turns
        self._max_conversations = max(1, max_conversations)
        self._lock = threading.Lock()

    def append(self, conversation_id: str, query: str) -> None:
        with self._lock:
            hist = self._store.get(conversation_id)
            if hist is None:
                hist = []
                self._store[conversation_id] = hist
            self._store.move_to_end(conversation_id)  # 최근 사용 표시(LRU)
            hist.append(query)
            # 최근 N턴만 유지(맥락 폭주·비용 방어)
            if len(hist) > self._max_turns:
                del hist[0]
            # 대화 수 상한 초과 시 가장 오래 안 쓴 대화 축출
            while len(self._store) > self._max_conversations:
                self._store.popitem(last=False)

    def get(self, conversation_id: str) -> list[str]:
        with self._lock:
            hist = self._store.get(conversation_id)
            if hist is None:
                return []
            self._store.move_to_end(conversation_id)  # 읽기도 최근 사용으로
            return list(hist)


class QueryRewriter:
    def __init__(self, llm: RewriteLLM, store):
        # store: ConversationStore | RedisConversationStore (append/get 계약)
        self._llm = llm
        self._store = store

    def rewrite_for_search(self, query: str,
                           conversation_id: str | None) -> str:
        # 단발 질의(대화 없음)는 재작성 불필요
        if conversation_id is None:
            return query

        history = self._store.get(conversation_id)
        # 첫 턴(이력 없음) 또는 지시어 없는 독립 질의 → LLM 스킵
        if not needs_rewrite(query, history):
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
