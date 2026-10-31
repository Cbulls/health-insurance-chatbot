"""KOSIS · 건보공단 캐시 조회 — guide()는 HTTP 없이 동작."""
from __future__ import annotations

from harag.guidance.hira import HiraStats
from harag.guidance.kosis_nhis import (
    SOURCE,
    TBL_A001,
    TBL_A002,
    TBL_A043,
    TBL_A047,
    TBL_A054,
    TBL_N034,
    build_kosis_bundle,
    care_context_for,
    load_table_cache,
    surgery_context_for,
)
from harag.guidance.service import GuidanceService


def test_kosis_cache_files_present():
    assert load_table_cache(TBL_A043)
    assert load_table_cache(TBL_A001)
    assert load_table_cache(TBL_A002)
    assert load_table_cache(TBL_A047)
    assert load_table_cache(TBL_A054)
    assert load_table_cache(TBL_N034)


def test_care_context_age_sex_from_cache():
    ctx = care_context_for("20-29", "male")
    assert ctx["missing"] is False
    assert SOURCE.split("·")[0].strip() in ctx["source"] or "KOSIS" in ctx["source"]
    assert "확률" not in (ctx.get("label") or "")
    assert "유병" not in (ctx.get("note") or "") or "유병·확진이 아니라" in (ctx.get("note") or "")


def test_surgery_nhi_vs_medical_aid_separated():
    nhi = surgery_context_for(["musculoskeletal_motion"], "male",
                              scheme="health_insurance")
    aid = surgery_context_for(["musculoskeletal_motion"], "male",
                              scheme="medical_aid")
    assert nhi["missing"] is False
    assert aid["missing"] is False
    assert nhi["scheme"] == "health_insurance"
    assert aid["scheme"] == "medical_aid"
    assert aid.get("scheme_label") == "의료급여"


def test_guide_attaches_kosis_without_live_http(monkeypatch):
    import harag.guidance.kosis_nhis as kn

    def boom(*a, **k):
        raise AssertionError("guide must not call KOSIS OpenAPI")

    monkeypatch.setattr(kn, "fetch_openapi", boom)
    g = GuidanceService(stats=HiraStats()).guide(
        "",
        symptom_codes=["fatigue", "headache", "leg_cramp"],
        occupation="field",
        age_band="20-29",
        sex="male",
    )
    assert g.kosis
    care = (g.kosis.get("care_context") or {})
    assert care.get("missing") is False
    assert any(
        (c.get("care_context") or {}).get("missing") is False
        for c in g.area_cards
    )
    ms = next(c for c in g.area_cards if c["id"] == "musculoskeletal_motion")
    assert (ms.get("surgery_context") or {}).get("missing") is False
    occ = ms.get("occupation_factor") or {}
    assert occ.get("missing") is False
    assert occ.get("claims_not_incidence") is True
    bridge = g.insurance_bridge or {}
    aid = bridge.get("kosis_surgery_medical_aid") or {}
    assert aid.get("scheme") == "medical_aid"
    cost = bridge.get("kosis_cost_bracket") or g.kosis.get("cost_bracket") or {}
    assert cost.get("missing") is False
    blob = " ".join(bridge.get("paragraphs") or [])
    assert "확률" not in blob


def test_bundle_source_attribution():
    b = build_kosis_bundle(["musculoskeletal_motion"], "40-49", "female")
    assert "KOSIS" in (b.get("source") or "")
    assert "국민건강보험공단" in (b.get("source") or "")
    assert (b.get("cost_bracket") or {}).get("tbl_id") == TBL_A054
    assert (b.get("care_context") or {}).get("inpatient")
