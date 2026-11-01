"""병원비·일반 증상에는 암/종신 상품이 선정되지 않아야 한다."""
from __future__ import annotations

from harag.guidance.engine import CoverageHit
from harag.guidance.service import (
    _product_fit_score,
    _select_compare_products,
)


def _cov(*codes_labels: tuple[str, str]) -> list[CoverageHit]:
    return [
        CoverageHit(code=c, label=lab, summary=lab, score=1.0)
        for c, lab in codes_labels
    ]


def test_cancer_demoted_for_outpatient_headache():
    coverages = _cov(
        ("outpatient", "실손 통원"),
        ("tests", "실손 검사"),
    )
    cancer = {
        "document_id": "c1",
        "name": "시그니처H암보험",
        "product_line": "cancer",
        "cost_nature": "lump_sum",
        "coverage_codes": ["tests"],
        "matched_coverages": [],
        "summary": "암 진단·치료·통원",
        "score": 0.9,
        "_library_fill": True,
    }
    health = {
        "document_id": "h1",
        "name": "통합간편건강보험",
        "product_line": "health",
        "cost_nature": "lump_sum",
        "coverage_codes": ["outpatient", "inpatient", "injury"],
        "matched_coverages": ["실손 통원"],
        "summary": "질병·상해 진단비·수술비",
        "score": 0.5,
        "_library_fill": True,
    }
    mixed = {
        "document_id": "m1",
        "name": "뉴라이프건강보험",
        "product_line": "health",
        "cost_nature": "mixed",
        "coverage_codes": ["outpatient", "inpatient", "tests", "injury"],
        "matched_coverages": ["실손 통원", "실손 검사"],
        "summary": "실손 특약과 정액",
        "score": 0.4,
        "_library_fill": False,
    }
    cs = _product_fit_score(
        cancer, coverages, None, ["outpatient"], specialty_ok=False)
    hs = _product_fit_score(
        health, coverages, None, ["outpatient"], specialty_ok=False)
    ms = _product_fit_score(
        mixed, coverages, None, ["outpatient"], specialty_ok=False)
    assert cs < hs
    assert cs < ms
    assert ms >= hs

    picked = _select_compare_products(
        [cancer, health, mixed], coverages,
        concerns=["outpatient"])
    lines = {p.get("product_line") for p in picked}
    assert "cancer" not in lines
    assert len(picked) == 2
    assert all(p.get("selection_reason") for p in picked)
    assert all(p.get("match_tier") for p in picked)
    assert picked[0].get("cost_nature") == "mixed"
    assert any("병원비" in (p.get("selection_reason") or "")
               or "실손" in (p.get("selection_reason") or "")
               or "통원" in (p.get("selection_reason") or "")
               for p in picked)


def test_cancer_allowed_when_specialty_signal():
    coverages = _cov(("tests", "암 진단비"))
    cancer = {
        "document_id": "c1",
        "name": "시그니처H암보험",
        "product_line": "cancer",
        "cost_nature": "lump_sum",
        "coverage_codes": ["tests"],
        "matched_coverages": ["암 진단비"],
        "summary": "암 진단",
        "score": 0.8,
    }
    health = {
        "document_id": "h1",
        "name": "건강보험",
        "product_line": "health",
        "cost_nature": "lump_sum",
        "coverage_codes": ["outpatient"],
        "matched_coverages": [],
        "summary": "건강",
        "score": 0.3,
        "_library_fill": True,
    }
    picked = _select_compare_products(
        [cancer, health], coverages, concerns=["tests"])
    assert any(p.get("product_line") == "cancer" for p in picked)
