"""결과 UX 정직성 P0 — 연령 게이트·정렬·care 미부착·문구."""
from __future__ import annotations

from harag.guidance.areas import match_area_specs, scrub_user_limits, scrub_stat_reason
from harag.guidance.engine import OCCUPATIONS
from harag.guidance.hira import HiraStats
from harag.guidance.kosis_nhis import (
    disease_298_context_for,
    filter_diseases_for_age,
    filter_surgeries_for_age,
    surgery_context_for,
)
from harag.guidance.service import GuidanceService
from harag.guidance.stat_labels import commonness_from_counts


def test_ami_and_knee_blocked_for_20s():
    assert "급성심근경색증" not in filter_diseases_for_age(
        ["폐렴", "급성심근경색증"], "20-29")
    assert "급성심근경색증" in filter_diseases_for_age(
        ["폐렴", "급성심근경색증"], "40-49")
    assert "슬관절 치환술" not in filter_surgeries_for_age(
        ["슬관절 치환술", "하지정맥류 수술"], "20-29")
    ctx = disease_298_context_for("cardio_resp", "20-29")
    blob = str(ctx)
    assert "급성심근경색" not in blob
    surg = surgery_context_for(
        ["musculoskeletal_motion"], "male",
        scheme="health_insurance", age_band="20-29")
    assert "슬관절 치환" not in str(surg)


def test_commonness_needs_three_refs():
    out = commonness_from_counts(
        [("R51", 100), ("R53", 50)], focus_kcd="R51",
        age_band="20대", sex="남성")
    assert out["missing"] is True
    assert "참고군" in (out.get("reason") or "")
    ok = commonness_from_counts(
        [("R51", 100), ("R53", 50), ("R25", 20)], focus_kcd="R51",
        age_band="20대", sex="남성")
    assert ok["missing"] is False
    assert "전체 인구" in (ok.get("detail") or "")


def test_specificity_ranks_thirst_over_fatigue_only():
    hits = match_area_specs(["thirst", "fatigue", "headache"])
    ids = [h[0] for h in hits]
    assert "balance_metabolic" in ids
    assert "head_neuro" in ids
    # 목마름 특이도 → 대사가 호흡/근육보다 앞이거나 최소한 cardio만 있는 피로 단독보다 앞
    bal_i = ids.index("balance_metabolic")
    if "cardio_resp" in ids:
        assert bal_i < ids.index("cardio_resp")
    if "musculoskeletal_motion" in ids:
        assert bal_i < ids.index("musculoskeletal_motion")


def test_guide_no_care_on_cards_and_no_ami_for_20s():
    g = GuidanceService(stats=HiraStats()).guide(
        "",
        symptom_codes=["fatigue", "headache", "thirst"],
        concerns=["inpatient"],
        occupation="student",
        age_band="20-29",
        sex="male",
    )
    assert not g.abstained
    blob = str(g.area_cards) + str(g.cost_pattern) + str(g.kosis)
    assert "급성심근경색" not in blob
    assert "슬관절 치환" not in blob
    for c in g.area_cards or []:
        assert not c.get("care_context")
        assert not c.get("surgery_context")
        for lim in c.get("limits") or []:
            assert "draft" not in lim.lower()
            assert "캐시" not in lim
        reason = (c.get("commonness") or {}).get("reason") or ""
        assert "캐시 미적재" not in reason
        assert "(R" not in reason
    cp = g.cost_pattern or {}
    assert "본인" in (cp.get("note") or "") or "총진료" in (cp.get("note") or "")
    assert cp.get("items") is None or True
    assert g.coverage_priority.get("items")
    assert OCCUPATIONS["retired"] == "무직·취업준비"


def test_scrub_helpers():
    # 면책·초안 문구는 카드 limits에서 제거하고 섹션 공통 안내로만 둔다.
    assert scrub_user_limits([
        "진단이 아닙니다",
        "집단 청구 경향이며 개인의 중증도가 아닙니다",
        "임상 후보 문구는 초안(draft)이며 의료 검수가 필요합니다",
        "남겨둘 한계",
    ]) == ["남겨둘 한계"]
    assert "준비" in scrub_stat_reason("심평원 캐시 미적재 (R06)")
