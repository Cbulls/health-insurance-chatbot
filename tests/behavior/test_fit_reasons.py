"""적합 이유 3축(증상·걱정·조건) fallback·파싱."""
from __future__ import annotations

from harag.guidance.engine import CoverageHit, RuleResult
from harag.guidance.lexicon import SymptomHit
from harag.guidance.fit_reasons import (
    ASPECTS,
    attach_fit_reasons,
    fit_reasons_fallback,
    _parse_fit_reasons_json,
)
from harag.guidance.product_match import build_input_link_lines
from harag.guidance.coverage_priority import build_coverage_priority


def _rules(**kwargs) -> RuleResult:
    return RuleResult(
        symptoms=kwargs.get("symptoms") or [
            SymptomHit(
                code="headache", label="두통", type="usual", alias="두통"),
            SymptomHit(
                code="fatigue", label="피로", type="usual", alias="피로"),
        ],
        areas=[],
        coverages=kwargs.get("coverages") or [
            CoverageHit(
                code="outpatient", label="실손 통원",
                summary="통원", score=1.0),
        ],
        occupation=kwargs.get("occupation", "student"),
        concern=kwargs.get("concern", "outpatient"),
        concerns=kwargs.get("concerns") or ["outpatient"],
        abstained=False,
        abstain_reason=None,
    )


def test_fit_reasons_fallback_has_three_aspects():
    product = {
        "name": "통합간편건강보험",
        "cost_nature": "lump_sum",
        "cost_nature_label": "정액",
        "matched_coverages": ["정액 통원·외래", "정액 입원일당"],
        "summary": "진단비·수술비·입원일당 특약",
    }
    reasons = fit_reasons_fallback(
        product, _rules(), age_band="20-29", sex="male",
        care={"existing_silson": "no"})
    assert len(reasons) >= 3
    aspects = {r["aspect"] for r in reasons}
    assert set(ASPECTS) <= aspects
    assert any("두통" in r["text"] or "피로" in r["text"] for r in reasons)
    assert any("병원비" in r["text"] or "걱정" in r["text"] for r in reasons)


def test_attach_fit_reasons_without_llm():
    products = attach_fit_reasons(
        [{
            "name": "A",
            "matched_coverages": ["정액 통원·외래"],
            "cost_nature": "lump_sum",
            "summary": "건강",
        }],
        _rules(),
        llm=None,
        web_browser=None,
        age_band="20-29",
        sex="female",
    )
    assert len(products[0]["fit_reasons"]) >= 3


def test_parse_fit_reasons_json():
    raw = '''
    {"reasons":[
      {"aspect":"symptom","text":"두통·피로와 연결되는 통원 특약이 안내되어 있습니다."},
      {"aspect":"concern","text":"병원비 걱정에 맞춰 정액 통원 축을 볼 수 있습니다."},
      {"aspect":"condition","text":"학생·20대 남성 기준으로 고지 유형을 비교할 수 있습니다."}
    ]}
    '''
    parsed = _parse_fit_reasons_json(raw)
    assert len(parsed) == 3
    assert {r["aspect"] for r in parsed} == set(ASPECTS)


def test_no_missing_similar_user_copy():
    priority = build_coverage_priority(concerns=["inpatient"], occupation=None)
    links = build_input_link_lines({
        "name": "정액",
        "cost_nature": "lump_sum",
        "coverage_codes": ["outpatient"],
        "summary": "통원",
    }, priority)
    blob = " ".join(L.get("text") or "" for L in links)
    assert "요약서에서 확인되지 않음" not in blob
    assert "축 유사" not in blob
