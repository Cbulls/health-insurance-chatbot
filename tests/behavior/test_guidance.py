"""상태 기반 안내 — 마스킹·규칙 매칭·가드."""
from __future__ import annotations

from harag.guidance.engine import run_rules
from harag.guidance.guard import DISCLAIMER, guard_text
from harag.guidance.service import GuidanceService


def test_assess_does_not_return_raw_pii():
    svc = GuidanceService()
    raw = "머리가 아파요 010-1234-5678"
    out = svc.assess(raw)
    assert "010-1234-5678" not in out.masked_preview
    assert "[PII]" in out.masked_preview
    assert out.pii_kinds.get("phone", 0) >= 1
    assert not out.abstained
    assert any(s["code"] == "headache" for s in out.symptoms)


def test_headache_and_leg_cramp_match():
    h = run_rules("갑자기 머리가 지끈거려요")
    assert not h.abstained
    assert h.symptoms[0].code == "headache"
    assert any(c.code == "outpatient" for c in h.coverages)

    leg = run_rules("서비스직인데 다리에 쥐가 나요", occupation="service")
    assert any(s.code == "leg_cramp" for s in leg.symptoms)
    assert any(c.code == "injury" for c in leg.coverages)
    assert any(a.confidence <= 0.3 for a in leg.areas)


def test_unsupported_symptom_abstains():
    r = run_rules("우주선이 보고 싶어요")
    assert r.abstained
    assert r.abstain_reason == "unsupported_symptom"
    g = GuidanceService().guide("우주선이 보고 싶어요")
    assert g.abstained and g.raw_stored is False


def test_diagnosis_phrase_blocked():
    text, flags = guard_text(
        "당신은 갑상선 질환입니다. 실손 통원으로 해결됩니다.",
        ["실손 통원"])
    assert "diagnosis_blocked" in flags
    assert "질환입니다" not in text
    assert DISCLAIMER in text


def test_rule_deviation_stripped():
    text, flags = guard_text(
        "실손 입원과 실손 통원을 함께 안내합니다.",
        ["실손 통원"],
        catalog_labels=["실손 통원", "실손 입원", "실손 검사", "상해 관련 실손"])
    assert "rule_deviation" in flags
    assert "실손 입원" not in text
    assert "실손 통원" in text


def test_guide_template_has_disclaimer_and_no_company():
    g = GuidanceService().guide(
        "허리가 아파요", occupation="office", concern="outpatient")
    assert not g.abstained
    assert g.raw_stored is False
    assert g.disclaimer == DISCLAIMER
    assert DISCLAIMER not in g.explanation
    assert "삼성" not in g.explanation
    labels = {c["label"] for c in g.coverages}
    assert "실손 통원" in labels


def test_symptom_codes_are_authoritative():
    r = run_rules("", symptom_codes=["headache"])
    assert not r.abstained
    assert r.symptoms[0].code == "headache"
    g = GuidanceService().guide("", symptom_codes=["back_pain"])
    assert not g.abstained
    assert "요통" in g.explanation
    assert g.profile.get("masked_preview") == ""


def test_guide_llm_does_not_see_raw_sentence():
    class CaptureLLM:
        def __init__(self):
            self.blobs: list[str] = []

        def complete(self, q, ctx, cites):
            self.blobs.append(q + "\n" + "\n".join(ctx or []))
            return "실손 통원만 안내합니다.", None

    llm = CaptureLLM()
    unique = "UNIQUE_PHRASE_XYZ"
    g = GuidanceService(llm=llm).guide(
        f"{unique} 머리가 아파요 010-9999-8888",
        symptom_codes=["headache"])
    blob = "\n".join(llm.blobs)
    assert unique not in blob
    assert "010-9999-8888" not in blob
    assert unique not in g.explanation
    assert "두통" in blob or "실손 통원" in blob


def test_conversation_store_clear():
    from harag.retrieval.rewriter import ConversationStore
    store = ConversationStore()
    store.append("c1", "머리가 아파요")
    assert store.get("c1") == ["머리가 아파요"]
    assert store.clear("c1") is True
    assert store.get("c1") == []
    assert store.clear("c1") is False
