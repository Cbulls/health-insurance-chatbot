"""상품 태그 nature 분리·입력 연결."""
from __future__ import annotations

from harag.guidance.product_match import (
    attach_input_links,
    build_input_link_lines,
    enrich_product_presentation,
    normalize_matched_tags,
    soften_product_summary,
)
from harag.guidance.coverage_priority import build_cost_pattern
from harag.guidance.coverage_priority import build_coverage_priority
from harag.guidance.areas import match_area_specs
from harag.guidance.service import GuidanceService


def test_lump_sum_not_labeled_as_silson():
    products = normalize_matched_tags([{
        "name": "간편건강",
        "cost_nature": "lump_sum",
        "coverage_codes": ["outpatient", "inpatient"],
        "matched_coverages": ["실손 통원", "실손 입원"],
        "summary": "정액 수술비·입원일당 특약",
    }])
    labels = products[0]["matched_coverages"]
    assert labels
    assert all("실손" not in lab for lab in labels)
    assert any("정액" in lab for lab in labels)


def test_indemnity_keeps_silson_labels():
    products = normalize_matched_tags([{
        "name": "실손A",
        "cost_nature": "indemnity",
        "coverage_codes": ["outpatient"],
        "matched_coverages": ["실손 통원"],
        "summary": "통원의료비",
    }])
    assert "실손 통원" in products[0]["matched_coverages"]


def test_input_links_skip_missing_and_similar():
    priority = build_coverage_priority(concerns=["outpatient"], occupation=None)
    product = {
        "name": "정액상품",
        "cost_nature": "lump_sum",
        "coverage_codes": ["inpatient", "outpatient"],
        "summary": "수술비·입원일당",
        "matched_coverages": [],
    }
    product = normalize_matched_tags([product])[0]
    links = build_input_link_lines(product, priority)
    texts = " ".join(L["text"] for L in links)
    assert "요약서에서 확인되지 않음" not in texts
    assert "축 유사" not in texts
    assert "실손 직접 보전" not in texts
    assert all(L["status"] == "match" for L in links)


def test_attach_input_links_on_guide_products_shape():
    priority = {"items": [{
        "rank": 1, "axis": "inpatient", "nature": "indemnity",
        "title": "실손 입원", "reason": "x", "check_points": [],
    }]}
    # 정액 vs 실손 요구 → mismatch는 더 이상 라인으로 내지 않음
    products = attach_input_links([{
        "name": "A",
        "cost_nature": "lump_sum",
        "coverage_codes": ["inpatient"],
        "summary": "입원일당",
        "matched_coverages": ["정액 입원일당"],
    }], priority)
    assert "input_links" in products[0]
    assert products[0]["input_links"] == []

    match_products = attach_input_links([{
        "name": "B",
        "cost_nature": "indemnity",
        "coverage_codes": ["inpatient"],
        "summary": "실손 입원",
        "matched_coverages": ["실손 입원"],
    }], priority)
    assert match_products[0]["input_links"]
    assert match_products[0]["input_links"][0]["status"] == "match"


def test_skin_and_leg_itch_map_to_area():
    hits = match_area_specs(["skin", "leg_itch", "headache"])
    ids = {h[0] for h in hits}  # (id, spec, linked, score)
    assert "skin_derm" in ids
    assert "head_neuro" in ids


def test_guide_skin_has_area_card():
    g = GuidanceService().guide(
        "",
        symptom_codes=["skin", "leg_itch", "headache"],
        concerns=["inpatient"],
        occupation="student",
        age_band="20-29",
        sex="male",
    )
    assert not g.abstained
    titles = {c["title"] for c in (g.area_cards or [])}
    assert any("피부" in t for t in titles)
    assert any("머리" in t or "신경" in t for t in titles)
    assert g.coverage_priority.get("items")


def test_soften_summary_strips_premium_fail_and_chronic_pitch():
    raw = (
        "유병력자·고령층 친화 건강보험입니다. "
        "해당 상품명의 공개 월납 예시는 찾기 어려워 상담이 필요합니다. "
        "갱신형입니다."
    )
    soft = soften_product_summary(raw)
    assert "유병력자" not in soft
    assert "찾기 어려워" not in soft
    assert "갱신형" in soft


def test_enrich_presentation_has_watch_and_short():
    products = enrich_product_presentation([{
        "name": "A",
        "cost_nature": "lump_sum",
        "coverage_codes": ["inpatient"],
        "summary": (
            "간편고지형 정액 상품입니다. 갱신형·해약환급금 미지급형이며 "
            "면책·감액이 있을 수 있습니다."
        ),
        "matched_coverages": [],
    }], {"items": [{
        "rank": 1, "axis": "inpatient", "nature": "indemnity",
        "title": "실손 입원", "reason": "x", "check_points": [],
    }]})
    p = products[0]
    assert p.get("short_summary")
    assert p.get("watch_points")
    assert p.get("premium_note")
    assert "input_links" in p
    # lump vs indemnity 요구 → 유사/미확인 칩 없음
    assert p.get("input_links") == []
    assert "유병력" not in (p.get("summary") or "")


def test_cost_pattern_from_area_severity():
    panel = build_cost_pattern([{
        "title": "머리·신경 관련",
        "severity": {
            "missing": False,
            "label": "대부분 외래 중심",
            "inpatient_share": 0.04,
            "source": "HIRA",
        },
    }], {})
    assert panel["care_pattern"] == "outpatient_heavy"
    assert panel["lines"]
    assert any("외래" in (ln.get("text") or "") for ln in panel["lines"])


def test_guide_has_cost_pattern():
    g = GuidanceService().guide(
        "",
        symptom_codes=["headache"],
        concerns=["inpatient"],
        age_band="20-29",
        sex="male",
    )
    assert g.cost_pattern.get("title")
    assert "lines" in g.cost_pattern


def test_kb_symptoms_have_area_catalog_mapping():
    """UI에 쓰일 증상 코드는 areas_catalog에 최소 1개 연결."""
    from harag.guidance.lexicon import load_kb
    from harag.guidance.areas import load_areas_catalog

    kb = load_kb()
    catalog = load_areas_catalog(kb)
    covered: set[str] = set()
    for spec in catalog.values():
        for c in spec.get("symptom_codes") or []:
            covered.add(str(c))
    missing = []
    for code, spec in (kb.get("symptoms") or {}).items():
        if not isinstance(spec, dict):
            continue
        if code not in covered:
            missing.append(code)
    assert not missing, f"areas_catalog 미매핑: {missing}"
