"""
관측 TDD — 단계별 trace + 메트릭 + 감사 로그(NFR-6, OBS-01~04).

책임:
  - 단계별 trace를 하나의 trace_id로 묶음(OBS-02)
  - 검색 청크 ID·점수 기록(OBS-01, 사후 재현)
  - abstention율·검색 점수 분포 메트릭(OBS-03)
  - 외부로 나간 트래픽 감사 로그(OBS-04, v4 네트워크 경계)
"""
import pytest

from harag.observability.tracing import QueryTrace, MetricsCollector


def test_OBS01_records_retrieved_chunks_and_scores():
    """검색 청크 ID·점수 기록(사후 재현)."""
    tr = QueryTrace(trace_id="t1", query="출장비?")
    tr.record_retrieval([("c1", 0.9), ("c2", 0.7)])
    snap = tr.snapshot()
    assert snap["retrieved"] == [("c1", 0.9), ("c2", 0.7)]


def test_OBS02_stages_under_single_trace():
    """단계별 trace가 하나의 trace_id로 묶임."""
    tr = QueryTrace(trace_id="t1", query="q")
    with tr.stage("retrieval"):
        pass
    with tr.stage("rerank"):
        pass
    with tr.stage("generation"):
        pass
    snap = tr.snapshot()
    assert set(snap["stages"].keys()) == {"retrieval", "rerank", "generation"}
    assert all(v >= 0 for v in snap["stages"].values()), "단계 지연 음수"
    assert snap["trace_id"] == "t1"


def test_OBS03_abstention_rate_metric():
    """abstention율 메트릭 집계."""
    m = MetricsCollector()
    m.record_query(abstained=True)
    m.record_query(abstained=False)
    m.record_query(abstained=True)
    assert abs(m.abstention_rate() - 2/3) < 1e-9


def test_OBS03_score_distribution():
    """검색 점수 분포 집계(p50 등)."""
    m = MetricsCollector()
    for s in [0.5, 0.6, 0.7, 0.8, 0.9]:
        m.record_top_score(s)
    assert 0.6 <= m.score_p50() <= 0.8


def test_OBS04_egress_audit_log():
    """외부 API로 나간 트래픽 감사 로그(어떤 trace가 외부로)."""
    tr = QueryTrace(trace_id="t1", query="q")
    tr.record_egress(provider="ext-llm", token_estimate=120)
    snap = tr.snapshot()
    assert snap["egress"] and snap["egress"][0]["provider"] == "ext-llm"


def test_OBS_no_pii_in_trace():
    """trace에 원문 질의 전체를 남기되, 민감정보 마스킹 훅이 적용 가능."""
    tr = QueryTrace(trace_id="t1", query="주민번호 901010-1234567 조회",
                    mask=lambda s: "***")
    snap = tr.snapshot()
    assert snap["query"] == "***", "마스킹 훅 미적용"


if __name__ == "__main__":
    import sys
    sys.exit(pytest.main([__file__, "-v"]))
