"""유사 매칭 사다리 · 100% 비보장 고지 · 거절 없음."""
from __future__ import annotations

from harag.guidance.engine import CoverageHit
from harag.guidance.service import (
    PRODUCT_MATCH_DISCLAIMER,
    GuidanceService,
    _assign_match_tier,
    _build_product_match_meta,
    _product_fit_score,
    _select_compare_products,
)


def _cov(*codes_labels: tuple[str, str]) -> list[CoverageHit]:
    return [
        CoverageHit(code=c, label=lab, summary=lab, score=1.0)
        for c, lab in codes_labels
    ]


def test_l0_mixed_beats_lump_for_outpatient():
    coverages = _cov(("outpatient", "실손 통원"))
    mixed = {
        "document_id": "m1",
        "name": "뉴라이프",
        "insurer": "KB",
        "product_line": "health",
        "cost_nature": "mixed",
        "coverage_codes": ["outpatient", "inpatient"],
        "matched_coverages": ["실손 통원"],
        "summary": "실손 특약",
        "score": 0.3,
    }
    lump = {
        "document_id": "h1",
        "name": "간편건강",
        "insurer": "메리츠",
        "product_line": "health",
        "cost_nature": "lump_sum",
        "coverage_codes": ["outpatient", "inpatient"],
        "matched_coverages": [],
        "summary": "정액 진단비",
        "score": 0.9,
        "_library_fill": True,
    }
    assert _assign_match_tier(mixed, coverages, ["outpatient"]) == "L0"
    assert _assign_match_tier(lump, coverages, ["outpatient"]) == "L2"
    picked = _select_compare_products(
        [lump, mixed], coverages, concerns=["outpatient"])
    assert len(picked) == 2
    assert picked[0]["document_id"] == "m1"
    assert picked[0]["match_quality"] == "direct"
    assert picked[1]["match_quality"] == "similar"
    assert "병원비" in (picked[0].get("selection_reason") or "") or \
        "실손" in (picked[0].get("selection_reason") or "")


def test_lump_only_pool_still_fills_two_similar():
    coverages = _cov(("outpatient", "실손 통원"))
    a = {
        "document_id": "a",
        "name": "A건강",
        "insurer": "한화",
        "product_line": "health",
        "cost_nature": "lump_sum",
        "coverage_codes": ["outpatient", "inpatient"],
        "summary": "정액",
        "score": 0.5,
        "_library_fill": True,
    }
    b = {
        "document_id": "b",
        "name": "B건강",
        "insurer": "삼성",
        "product_line": "health",
        "cost_nature": "lump_sum",
        "coverage_codes": ["outpatient"],
        "summary": "정액2",
        "score": 0.4,
        "_library_fill": True,
    }
    picked = _select_compare_products(
        [a, b], coverages, concerns=["outpatient"])
    assert len(picked) == 2
    assert all(p.get("match_tier") in ("L2", "L3", "L4") for p in picked)
    assert all(p.get("match_quality") == "similar" for p in picked)
    meta = _build_product_match_meta(picked, ["outpatient"])
    assert "100%" in meta["disclaimer"]
    assert meta["similar_note"]
    assert "병원비" in meta["similar_note"]


def test_outpatient_does_not_force_nature_diversity():
    """병원비 걱정 시 실손 두 개를 골라도 됨 (정액 강제 교차 금지)."""
    coverages = _cov(("outpatient", "실손 통원"))
    m1 = {
        "document_id": "m1",
        "name": "혼합1",
        "insurer": "KB",
        "product_line": "health",
        "cost_nature": "mixed",
        "coverage_codes": ["outpatient"],
        "matched_coverages": ["실손 통원"],
        "summary": "실손",
        "score": 0.8,
    }
    m2 = {
        "document_id": "m2",
        "name": "혼합2",
        "insurer": "DB",
        "product_line": "health",
        "cost_nature": "indemnity",
        "coverage_codes": ["outpatient"],
        "matched_coverages": ["실손 통원"],
        "summary": "실손2",
        "score": 0.7,
    }
    lump = {
        "document_id": "l1",
        "name": "정액",
        "insurer": "메리츠",
        "product_line": "health",
        "cost_nature": "lump_sum",
        "coverage_codes": ["outpatient"],
        "summary": "정액",
        "score": 0.9,
        "_library_fill": True,
    }
    picked = _select_compare_products(
        [m1, m2, lump], coverages, concerns=["outpatient"])
    natures = {p.get("cost_nature") for p in picked}
    assert "lump_sum" not in natures
    assert len(picked) == 2


def test_care_slot_boosts_indemnity_when_no_silson():
    coverages = _cov(("outpatient", "실손 통원"))
    mixed = {
        "document_id": "m1",
        "product_line": "health",
        "cost_nature": "mixed",
        "coverage_codes": ["outpatient"],
        "matched_coverages": ["실손 통원"],
        "summary": "실손",
        "score": 0.2,
    }
    lump = {
        "document_id": "l1",
        "product_line": "health",
        "cost_nature": "lump_sum",
        "coverage_codes": ["outpatient"],
        "summary": "정액",
        "score": 0.9,
        "_library_fill": True,
    }
    care = {"existing_silson": "no", "care_intensity": "frequent",
            "cost_pain_type": "outpatient"}
    ms = _product_fit_score(
        mixed, coverages, None, ["outpatient"], care=care)
    ls = _product_fit_score(
        lump, coverages, None, ["outpatient"], care=care)
    assert ms > ls


def test_guide_disclaimer_always_has_accuracy_note():
    g = GuidanceService().guide(
        "",
        symptom_codes=["headache", "fatigue"],
        concerns=["outpatient"],
        occupation="student",
        age_band="20-29",
        sex="male",
        existing_silson="no",
        care_intensity="sometimes",
        cost_pain_type="outpatient",
    )
    assert not g.abstained
    assert "100%" in (g.disclaimer or "")
    assert PRODUCT_MATCH_DISCLAIMER in (g.disclaimer or "") or \
        "100%" in (g.product_match_meta or {}).get("disclaimer", "")
    assert g.profile.get("existing_silson") == "no"
    assert g.profile.get("care_intensity") == "sometimes"
