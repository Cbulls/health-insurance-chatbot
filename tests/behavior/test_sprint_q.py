"""스프린트 Q — 인용 필수·질의 정규화·임베딩 캐시·정답 게이트."""
from __future__ import annotations

from harag.embedding.query_cache import CachedEmbeddingModel
from harag.eval.harness import (
    GoldQuery, QueryType, SystemOutput, QualitySLO, evaluate, ci_gate,
    answer_correct,
)
from harag.retrieval.query_normalize import expand_query_for_search
from harag.security.output_guard import OutputGuard


def test_missing_citation_abstains():
    g = OutputGuard().validate(
        "통원 한도는 20만원이다.", [], ["c1"])
    assert not g.ok
    assert g.abstain_reason == "missing_citation"


def test_valid_citation_passes():
    g = OutputGuard().validate(
        "통원 한도는 20만원이다 [문서 1].", ["c1"], ["c1"])
    assert g.ok
    assert g.cited_chunk_ids == ["c1"]


def test_query_synonym_expand():
    out = expand_query_for_search("본인부담은 얼마인가요?")
    assert "본인부담" in out
    assert "copayment" in out.lower() or "본인부담금" in out


def test_query_embed_cache_hits():
    calls = {"n": 0}

    class FakeEmb:
        model_id = "m"
        dim = 4

        def encode(self, texts):
            calls["n"] += 1
            return [[0.1, 0.2, 0.3, 0.4] for _ in texts]

    cached = CachedEmbeddingModel(FakeEmb(), max_entries=8, ttl_sec=60.0)
    a = cached.encode(["동일 질의"])
    b = cached.encode(["동일 질의"])
    assert a == b
    assert calls["n"] == 1
    cached.encode(["다른 질의"])
    assert calls["n"] == 2
    # 배치는 캐시 안 함
    cached.encode(["a", "b"])
    assert calls["n"] == 3


def test_answer_correct_keywords_and_forbidden():
    gold = GoldQuery(
        "q1", "한도?", QueryType.body, {"c1"},
        "1일 5만원", "d1",
        forbidden_claims=["구 고시 금액"],
    )
    ok = SystemOutput(["c1"], "한도는 1일 5만원입니다", ["c1"])
    bad = SystemOutput(["c1"], "구 고시 금액은 3만원", ["c1"])
    assert answer_correct(gold, ok) is True
    assert answer_correct(gold, bad) is False


def test_evaluate_answer_acc_and_optional_gate():
    gold = [
        GoldQuery("q1", "한도?", QueryType.body, {"c1"}, "1일 5만원", "d1"),
        GoldQuery("q2", "우주?", QueryType.absent, set(), None, "d1"),
    ]
    wrong = {
        "q1": SystemOutput(["c1"], "전혀 다른 답변", ["c1"]),
        "q2": SystemOutput(["x"], None),
    }
    reports = evaluate(gold, wrong, k=5)
    assert reports[QueryType.body].answer_acc == 0.0
    # 기본 SLO는 answer_acc 미적용
    slo = QualitySLO(0.0, 0.0, 0.9, 0.0)
    ok, _ = ci_gate(reports, slo)
    assert ok
    # 훅 켜면 차단
    slo2 = QualitySLO(0.0, 0.0, 0.9, 0.0, min_answer_acc=0.8)
    ok2, viol = ci_gate(reports, slo2)
    assert not ok2
    assert any("answer_acc" in v for v in viol)


def test_llm_rerank_keeps_longer_tables():
    from harag.llm.local_rerank import LLMCrossEncoder
    table = "| a | b |\n| --- | --- |\n" + ("| xxxxxx | yyyyyy |\n" * 80)
    assert len(table) > 600
    clipped = LLMCrossEncoder._clip_doc(table)
    assert len(clipped) > 600
    assert len(clipped) <= 1800
    body = "짧은 본문 " * 50
    assert len(LLMCrossEncoder._clip_doc(body)) <= 600
