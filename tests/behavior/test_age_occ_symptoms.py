"""연령 매퍼 · 신규 hidden 증상 · 직군 crosswalk."""
from __future__ import annotations

from harag.guidance.age_bands import hira_age_band, kosis_ages
from harag.guidance.engine import OCCUPATIONS, run_rules
from harag.guidance.occupation_stats import MolKosisOccupationStats
from harag.guidance.service import GuidanceService
from harag.guidance.hira import HiraStats


def test_hira_age_fold():
    assert hira_age_band("0-4") == "0-9"
    assert hira_age_band("5-9") == "0-9"
    assert hira_age_band("70-79") == "70+"
    assert hira_age_band("80+") == "70+"
    assert hira_age_band("30-39") == "30-39"
    assert hira_age_band(None) is None


def test_kosis_ages_preschool():
    ages = kosis_ages("0-4", "default")
    assert ages == ["0세", "1 - 4세"]
    a0611 = kosis_ages("0-4", "a0611")
    assert "0세" in a0611 and "1-4세" in a0611


def test_new_hidden_symptoms_in_rules():
    r = run_rules(
        "",
        symptom_codes=["appetite_loss", "urine_color", "stool_color"],
        occupation="office",
    )
    assert not r.abstained
    codes = {h.code for h in r.symptoms}
    assert codes == {"appetite_loss", "urine_color", "stool_color"}
    cov = {c.code for c in r.coverages}
    assert "outpatient" in cov
    assert "tests" in cov


def test_new_occupations_registered():
    for code in (
        "care_health", "education", "homemaker", "retired",
        "office", "service", "field", "agriculture", "fishery",
        "student", "other",
    ):
        assert code in OCCUPATIONS


def test_agriculture_fishery_crosswalk():
    agri = MolKosisOccupationStats().lookup("x", "agriculture")
    fish = MolKosisOccupationStats().lookup("x", "fishery")
    if not agri.missing:
        assert "농업" in (agri.text or "")
        assert agri.claims_not_incidence is True
    if not fish.missing:
        assert "어업" in (fish.text or "")
        assert fish.claims_not_incidence is True


def test_care_health_crosswalk_approx():
    ev = MolKosisOccupationStats().lookup("x", "care_health")
    # DB may or may not be loaded; if loaded should not be missing
    if not ev.missing:
        assert ev.claims_not_incidence is True
        assert "근사" in (ev.text or "") or "보건" in (ev.text or "")


def test_homemaker_occupation_missing_stats():
    ev = MolKosisOccupationStats().lookup("x", "homemaker")
    assert ev.missing is True


def test_guide_with_new_symptoms_and_age():
    g = GuidanceService(stats=HiraStats()).guide(
        "",
        symptom_codes=["abdominal_pain", "nausea", "appetite_loss"],
        occupation="care_health",
        age_band="0-4",
        sex="male",
    )
    assert not g.abstained
    assert g.profile.get("age_band") == "0-4"
    assert any(c.get("id") == "gi_urine_stool" for c in (g.area_cards or []))
