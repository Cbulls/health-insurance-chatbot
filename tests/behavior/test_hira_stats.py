"""심평원 참고 통계 — 캐시 폴백, KCD만 전송."""
from __future__ import annotations

import json

from harag.guidance.hira import HiraStats
from harag.guidance.service import GuidanceService


def test_guide_skips_ref_stats_even_with_hira():
    """결과 UX에서 참고 통계를 쓰지 않으므로 guide는 ref_stats를 비운다."""
    g = GuidanceService(stats=HiraStats()).guide("", symptom_codes=["headache"])
    assert g.ref_stats == []
    assert "당신 연령대" not in g.explanation
    assert "유병률" not in g.explanation


def test_hira_lookup_still_works_directly():
    stats = HiraStats()
    from harag.guidance.engine import run_rules
    rules = run_rules("", symptom_codes=["headache"])
    rows = stats.lookup(rules, age_band="20-29", sex="female")
    assert rows
    assert rows[0]["kcd"] == "R51"
    assert rows[0].get("age_sex_count")
    aged = GuidanceService(stats=stats).guide(
        "", symptom_codes=["headache"], age_band="20-29", sex="female")
    assert aged.ref_stats == []
    assert "20대" not in aged.explanation_why


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
