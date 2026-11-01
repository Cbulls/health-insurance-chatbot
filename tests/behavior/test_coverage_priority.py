"""보장 점검 순위 규칙표 — 스키마·매칭·가이드 연결."""
from __future__ import annotations

from harag.guidance.coverage_priority import (
    build_coverage_priority,
    derive_care_pattern,
    load_priority_rules,
    validate_priority_rules,
)
from harag.guidance.service import GuidanceService


def test_rules_file_validates():
    data = load_priority_rules()
    assert data.get("version") == 1
    assert validate_priority_rules(data) == []


def test_unknown_axis_rejected():
    data = load_priority_rules()
    bad = dict(data)
    bad["rules"] = list(data["rules"]) + [{
        "id": "bad_axis",
        "enabled": True,
        "when": {"concerns_any": ["inpatient"]},
        "emit": {
            "axis": "not_a_real_axis",
            "nature": "indemnity",
            "priority_weight": 1,
            "reason": "x",
            "check_points": [],
            "requires_stats": False,
        },
    }]
    errs = validate_priority_rules(bad)
    assert any("unknown axis" in e for e in errs)


def test_inpatient_concern_ranks_indemnity_first():
    out = build_coverage_priority(
        concerns=["inpatient"],
        occupation=None,
        area_cards=[],
    )
    assert out["items"]
    assert out["items"][0]["axis"] == "inpatient"
    assert out["items"][0]["nature"] == "indemnity"
    assert out["items"][0]["title"] == "실손 입원"
    axes = [i["axis"] for i in out["items"]]
    assert "surgery" in axes
    assert len(out["items"]) <= 3
    assert any("직업 정보" in n for n in out["missing_notes"])
    blob = " ".join(i["reason"] for i in out["items"])
    assert "가입하세요" not in blob
    assert "유병률" not in blob


def test_student_skips_injury_rule():
    out = build_coverage_priority(
        concerns=["outpatient"],
        occupation="student",
        area_cards=[],
    )
    assert all(i["axis"] != "injury" for i in out["items"])
    assert any("직군 상해" in n for n in out["missing_notes"])


def test_field_adds_injury():
    out = build_coverage_priority(
        concerns=["outpatient"],
        occupation="field",
        area_cards=[],
    )
    assert any(i["axis"] == "injury" for i in out["items"])


def test_care_pattern_outpatient_heavy():
    cards = [{
        "severity": {
            "missing": False,
            "inpatient_share": 0.05,
            "label": "대부분 외래 중심",
        }
    }]
    care = derive_care_pattern(cards)
    assert care["care_pattern"] == "outpatient_heavy"
    out = build_coverage_priority(
        concerns=["inpatient"],
        occupation="office",
        area_cards=cards,
    )
    # 입원 걱정 + 외래 패턴 → 통원도 후보에 포함 가능
    titles = [i["title"] for i in out["items"]]
    assert "실손 입원" in titles


def test_guide_includes_coverage_priority():
    g = GuidanceService().guide(
        "",
        symptom_codes=["headache"],
        concerns=["inpatient"],
        occupation="student",
        age_band="20-29",
        sex="male",
    )
    assert not g.abstained
    cp = g.coverage_priority or {}
    assert cp.get("items")
    assert cp["items"][0]["axis"] == "inpatient"
    assert cp.get("selection_basis")
