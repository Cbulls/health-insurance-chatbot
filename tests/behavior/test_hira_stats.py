"""심평원 참고 통계 — 캐시 폴백, KCD만 전송, guide ref_stats 계약."""
from __future__ import annotations

import json

from harag.guidance.guard import guard_text
from harag.guidance.hira import HiraStats
from harag.guidance.service import GuidanceService


def test_guide_fills_ref_stats_from_hira():
    """guide는 ref_stats를 채우고, 브리프는 요약만·원시 건수 반복은 하지 않는다."""
    g = GuidanceService(stats=HiraStats()).guide("", symptom_codes=["headache"])
    assert g.ref_stats
    row = g.ref_stats[0]
    assert row["kcd"] == "R51"
    assert row.get("total_count") is not None
    assert row.get("source")
    assert "cached" in row
    assert "당신 연령대" not in g.explanation
    assert "유병률" not in g.explanation
    assert "## 주의·참고" in (g.explanation_why or "")
    assert "건강보험심사평가원" in (g.explanation_why or "")
    assert "공공 참고 카드" in (g.explanation_why or "")
    assert "질병관리청" in (g.explanation_why or "")
    assert "(R51)" not in (g.explanation_why or "")
    assert "참고용 안내입니다" not in (g.explanation_why or "")
    assert "confidence" not in (g.explanation_why or "")
    # 원시 전건수 나열은 카드로 미룸
    assert str(row["total_count"]) not in (g.explanation_why or "")


def test_hira_lookup_age_sex_reflected_in_guide():
    stats = HiraStats()
    from harag.guidance.engine import run_rules
    rules = run_rules("", symptom_codes=["headache"])
    rows = stats.lookup(rules, age_band="20-29", sex="female")
    assert rows
    assert rows[0]["kcd"] == "R51"
    assert rows[0].get("age_sex_count")
    assert rows[0].get("note") or rows[0].get("cached") is True
    aged = GuidanceService(stats=stats).guide(
        "", symptom_codes=["headache"], age_band="20-29", sex="female")
    assert aged.ref_stats
    assert aged.ref_stats[0].get("age_sex_count") == rows[0]["age_sex_count"]
    assert "20대" in aged.explanation_why
    assert "유병률" not in aged.explanation_why
    assert str(rows[0]["age_sex_count"]) not in aged.explanation_why


def test_guard_blocks_unsourced_risk_claims():
    text, flags = guard_text(
        "유병률이 높고 위험합니다. 실손 통원을 살펴보시면 됩니다.",
        ["실손 통원"])
    assert "risk_claim_blocked" in flags
    assert "유병률" not in text
    assert "위험합니다" not in text


def test_guide_fills_cluster_stats_from_hira():
    g = GuidanceService(stats=HiraStats()).guide(
        "",
        symptom_codes=["fatigue", "headache", "numbness"],
        age_band="20-29",
        sex="male",
    )
    assert g.symptom_clusters
    assert g.cluster_stats
    kcds = {row["kcd"] for row in g.cluster_stats}
    assert kcds & {"R53", "R20", "R51"}
    assert "유병률" not in (g.explanation_why or "")
    assert "전조 증상" not in (g.explanation_why or "")


def test_live_request_sends_kcd_only(monkeypatch):
    captured: list[tuple[str, dict]] = []

    class FakeResp:
        text = (
            "<response><body><items>"
            "<item><ptntCnt>12</ptntCnt><hsptlzYn>외래</hsptlzYn></item>"
            "</items></body></response>"
        )

        def raise_for_status(self):
            return None

    class FakeClient:
        def __init__(self, timeout=None):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

        def get(self, url, params=None):
            captured.append((url, dict(params or {})))
            return FakeResp()

    monkeypatch.setattr("harag.guidance.hira.httpx.Client", FakeClient)
    live = HiraStats(service_key="dummy-key")._try_live("R51")
    assert live
    assert captured
    for url, params in captured:
        blob = url + json.dumps(params, ensure_ascii=False)
        assert params.get("sickCd") == "R51"
        assert "머리" not in blob
        assert "아파" not in blob
        assert "두통" not in params.get("searchText", "")
