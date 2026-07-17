"""
멀티턴 질의 재작성 TDD — 후속 질의를 독립 검색 가능한 형태로(FR-6, R-09).

문제: "그건 언제까지 정산해?"는 그 자체로 검색 불가(지시어 "그건").
이전 대화를 참조해 "출장비 정산 기한은?" 같은 독립 질의로 재작성.

설계:
  - 재작성 LLM은 어댑터 뒤로(실패해도 원본 폴백 — graceful degradation)
  - 첫 턴(이력 없음)은 재작성 불필요 → 원본 그대로
  - 대화 이력은 conversation_id로 관리, 권한은 매 턴 재평가(이력에 캐시 안 함)
  - 재작성은 이전 '질의'를 참조(이전 답변 내용 무단 주입 방지 — 권한 안전)
"""
import pytest

from harag.retrieval.rewriter import QueryRewriter, ConversationStore


class FakeRewriteLLM:
    """이전 질의 맥락으로 지시어를 푸는 재작성 LLM 대역."""
    def __init__(self, raise_error=False):
        self._raise = raise_error
    def rewrite(self, query, history):
        if self._raise:
            raise RuntimeError("LLM 실패")
        # "그건/그거"를 직전 질의의 핵심어로 치환하는 간단 대역
        if history and ("그건" in query or "그거" in query or "언제" in query):
            prev = history[-1]
            return query.replace("그건", "출장비").replace("그거", "출장비") + f" (맥락: {prev})"
        return query


def test_RW01_first_turn_no_rewrite():
    """첫 턴(이력 없음)은 재작성 없이 원본."""
    rw = QueryRewriter(llm=FakeRewriteLLM(), store=ConversationStore())
    out = rw.rewrite_for_search("출장비 한도가 얼마야?", conversation_id="conv1")
    assert out == "출장비 한도가 얼마야?"


def test_RW02_followup_rewritten_with_history():
    """후속 질의는 이전 질의 맥락으로 재작성."""
    store = ConversationStore()
    rw = QueryRewriter(llm=FakeRewriteLLM(), store=store)
    # 1턴
    rw.rewrite_for_search("출장비 한도가 얼마야?", conversation_id="conv1")
    rw.record_turn("conv1", "출장비 한도가 얼마야?")
    # 2턴: 지시어 포함
    out = rw.rewrite_for_search("그건 언제까지 정산해?", conversation_id="conv1")
    assert "그건" not in out, "지시어가 안 풀림"
    assert "출장비" in out, "이전 맥락이 반영 안 됨"


def test_RW03_rewrite_failure_falls_back_to_original():
    """재작성 LLM 실패 시 원본 질의로 폴백(graceful degradation)."""
    store = ConversationStore()
    rw = QueryRewriter(llm=FakeRewriteLLM(raise_error=True), store=store)
    rw.record_turn("conv1", "이전 질의")
    out = rw.rewrite_for_search("그건 뭐야?", conversation_id="conv1")
    assert out == "그건 뭐야?", "실패 시 원본 폴백 안 됨"


def test_RW04_history_isolated_by_conversation():
    """대화별 이력 격리 — 다른 대화의 맥락이 새지 않음."""
    store = ConversationStore()
    rw = QueryRewriter(llm=FakeRewriteLLM(), store=store)
    rw.record_turn("convA", "휴가 규정 질의")
    rw.record_turn("convB", "출장비 질의")
    # convB의 후속은 convA 맥락을 안 봄
    out = rw.rewrite_for_search("그건 언제까지 정산해?", conversation_id="convB")
    assert "휴가" not in out, "다른 대화 맥락 누수"


def test_RW05_no_conversation_id_no_rewrite():
    """conversation_id 없으면(단발 질의) 재작성 안 함."""
    rw = QueryRewriter(llm=FakeRewriteLLM(), store=ConversationStore())
    out = rw.rewrite_for_search("그건 언제까지 정산해?", conversation_id=None)
    assert out == "그건 언제까지 정산해?"


def test_RW06_only_queries_in_history_not_answers():
    """이력에는 질의만 — 이전 답변 내용은 재작성에 무단 주입 안 함(권한 안전)."""
    store = ConversationStore()
    store.append("conv1", "출장비 한도?")
    history = store.get("conv1")
    # 이력은 질의 문자열 목록(답변·청크 내용 없음)
    assert history == ["출장비 한도?"]


def test_RW07_skip_llm_when_no_deixis():
    """이력이 있어도 지시어 없는 독립 질의는 LLM 재작성 스킵."""
    store = ConversationStore()
    calls = []

    class CountingLLM:
        def rewrite(self, query, history):
            calls.append(query)
            return "재작성됨"

    rw = QueryRewriter(llm=CountingLLM(), store=store)
    rw.record_turn("conv1", "출장비 한도가 얼마야?")
    out = rw.rewrite_for_search(
        "연차 휴가 일수는 며칠인가요?", conversation_id="conv1")
    assert out == "연차 휴가 일수는 며칠인가요?"
    assert calls == []


if __name__ == "__main__":
    import sys
    sys.exit(pytest.main([__file__, "-v"]))
