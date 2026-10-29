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


def test_solicit_phrase_blocked():
    text, flags = guard_text(
        "실손 통원을 가입하세요. 가입을 추천합니다.",
        ["실손 통원"])
    assert "solicit_blocked" in flags
    assert "가입하세요" not in text
    assert "가입을 추천" not in text
    ok, ok_flags = guard_text(
        "이 상황에 맞춰 비교 추천합니다. 실손 통원을 살펴보시면 됩니다.",
        ["실손 통원"])
    assert "solicit_blocked" not in ok_flags
    assert "비교 추천합니다" in ok


def test_diagnosis_phrase_blocked():
    text, flags = guard_text(
        "당신은 갑상선 질환입니다. 실손 통원으로 해결됩니다.",
        ["실손 통원"])
    assert "diagnosis_blocked" in flags
    assert "질환입니다" not in text
    assert "[진단 단정 삭제]" not in text
    assert DISCLAIMER in text


def test_disclaimer_keeps_negative_hwakjin():
    """면책의 「확진이 아닙니다」는 가드가 깨뜨리지 않는다."""
    text, flags = guard_text(
        "실손 통원을 살펴보시면 됩니다.\n\n" + DISCLAIMER,
        ["실손 통원"])
    assert "diagnosis_blocked" not in flags
    assert "확진이 아닙니다" in text
    assert "뿐 이 아닙니다" not in text
    blocked, bflags = guard_text(
        "확진됩니다. 실손 통원으로 해결됩니다.",
        ["실손 통원"])
    assert "diagnosis_blocked" in bflags
    assert "확진됩니다" not in blocked


def test_why_fallback_honorific_and_two_types():
    g = GuidanceService().guide(
        "", symptom_codes=["cough", "night_sweat"], concern="inpatient")
    assert not g.abstained
    assert "습니다" in g.explanation_why
    assert "자주 느끼는" in g.explanation_why
    assert "놓치기 쉬운" in g.explanation_why
    assert "## 공공데이터 해석" in g.explanation_why
    assert "## 주의·참고" in g.explanation_why
    assert "## 비교할 상품 2개" in g.explanation_why
    assert "confidence" not in g.explanation_why
    assert "확진이 아닙니다" in g.explanation_why
    assert "뿐 이 아닙니다" not in g.explanation_why
    assert "질병관리청" in g.explanation_why
    assert "건강보험심사평가원" in g.explanation_why
    assert "참고용 안내입니다" not in g.explanation_why
    assert "(R" not in g.explanation_why
    assert "R05" not in g.explanation_why
    cough = next(s for s in g.profile["symptoms"] if s["code"] == "cough")
    ov = cough.get("portal_overview", {}) or {}
    assert ov.get("text")
    assert "참고용 안내" not in (ov.get("text") or "")
    assert g.explanation_caution == ""
    # stats 미주입 시 비움; Hira 연동 계약은 test_hira_stats
    assert g.ref_stats == []
    assert "[문서" not in g.explanation_why
    assert "[진단 단정 삭제]" not in g.explanation


def test_portal_overviews_have_no_filler():
    from harag.guidance.lexicon import load_kb

    for code, spec in (load_kb().get("symptoms") or {}).items():
        text = ((spec.get("portal_overview") or {}).get("text") or "")
        assert "참고용 안내" not in text, code
        assert "참고 정보입니다" not in text, code


def test_why_fallback_mentions_matched_products():
    # pipeline+auth만으로는 빈 products — 폴백에 상품을 직접 주입하려면
    # guide 경로의 _explain을 products와 함께 호출하는 대신 서비스 결과 후처리 없이
    # _why_fallback 단위로 검증한다.
    from harag.guidance.engine import run_rules
    from harag.guidance.service import _why_fallback

    rules = run_rules("", symptom_codes=["cough"])
    text = _why_fallback(rules, products=[{
        "name": "테스트건강보험",
        "insurer": "테스트생명",
        "matched_coverages": ["실손 통원"],
    }])
    assert "테스트건강보험" in text
    assert "실손 통원" in text
    assert "## 비교할 상품 2개" in text
    assert "비교하기 좋습니다" in text


def test_why_strips_doc_markers():
    class MarkerLLM:
        def complete(self, q, ctx, cites):
            return (
                "## 공공데이터 해석\n"
                "자주 느끼는 증상과 놓치기 쉬운 불편을 함께 반영했습니다.\n"
                "## 주의·참고\n"
                "확진이 아닙니다.\n"
                "## 비교할 상품 2개\n"
                "실손 통원은 증상과 관련이 있습니다 [문서 1].\n",
                None)

    g = GuidanceService(llm=MarkerLLM()).guide(
        "", symptom_codes=["cough"])
    assert "[문서" not in g.explanation_why
    assert "## 공공데이터 해석" in g.explanation_why
    assert "관련이 있습니다" in g.explanation_why


def test_rule_deviation_stripped():
    text, flags = guard_text(
        "실손 입원과 실손 통원을 함께 안내합니다.",
        ["실손 통원"],
        catalog_labels=["실손 통원", "실손 입원", "실손 검사", "상해 관련 실손"])
    assert "rule_deviation" in flags
    assert "실손 입원" not in text
    assert "실손 통원" in text


def test_guide_template_has_overlap_not_age_band():
    g = GuidanceService().guide(
        "허리가 아파요", occupation="office", concern="outpatient",
        age_band="40-49")
    # 상황 브리프에는 선택 연령 라벨을 쓰되, 「당신 연령대」식 추정 문구는 쓰지 않는다.
    assert "당신 연령대" not in g.explanation
    assert "40대" in g.explanation_why


def test_guide_template_has_disclaimer_and_no_company():
    g = GuidanceService().guide(
        "허리가 아파요", occupation="office", concern="outpatient")
    assert not g.abstained
    assert g.raw_stored is False
    assert g.disclaimer == DISCLAIMER
    assert DISCLAIMER not in g.explanation
    assert DISCLAIMER not in g.explanation_why
    assert DISCLAIMER not in g.explanation_caution
    assert "삼성" not in g.explanation
    assert g.explanation_why
    assert "요통" in g.explanation_why or "실손" in g.explanation_why
    assert g.explanation_caution == ""
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
    assert g.explanation_why
    assert g.explanation_caution == ""


def test_guide_survives_tuple_retrieve():
    class Pipe:
        def _retrieve_sync(self, q, auth, cid):
            return ([], object())

    g = GuidanceService(pipeline=Pipe()).guide(
        "", symptom_codes=["headache"], auth=object())
    assert not g.abstained
    assert any(c["code"] == "outpatient" for c in g.coverages)
    assert g.citations == []
    assert g.products == []


def test_area_overlap_counts_and_usual_type():
    r = run_rules("", symptom_codes=["headache", "back_pain"])
    assert not r.abstained
    assert r.areas
    assert all(a.selected == 2 for a in r.areas)
    assert all(a.matched >= 1 for a in r.areas)
    matched = [a.matched for a in r.areas]
    assert matched == sorted(matched, reverse=True)
    assert all(s.type == "usual" for s in r.symptoms)


def test_multi_concerns_and_none():
    both = run_rules(
        "", symptom_codes=["headache"], concerns=["outpatient", "tests"])
    assert set(both.concerns) == {"outpatient", "tests"}
    none = run_rules(
        "", symptom_codes=["headache"], concerns=["none", "outpatient"])
    assert none.concerns == []


def test_field_occupation_is_known():
    r = run_rules("", symptom_codes=["leg_cramp"], occupation="field")
    assert r.occupation == "field"
    assert any(c.code == "injury" for c in r.coverages)


def test_new_usual_and_hidden_codes():
    cough = run_rules("", symptom_codes=["cough"])
    assert not cough.abstained
    assert cough.symptoms[0].type == "usual"
    cramp = run_rules("", symptom_codes=["leg_cramp"])
    assert any(s.code == "leg_cramp" and s.type == "hidden" for s in cramp.symptoms)
    itch = run_rules("다리 가려움이 있어요")
    assert any(s.code == "leg_itch" and s.type == "hidden" for s in itch.symptoms)
    numb = run_rules("", symptom_codes=["cough", "numbness"])
    assert {s.code for s in numb.symptoms} == {"cough", "numbness"}
    assert any(s.type == "hidden" for s in numb.symptoms)
    g = GuidanceService().guide("", symptom_codes=["leg_itch", "thirst"])
    blob = g.explanation + " ".join(a["label"] for a in g.areas)
    assert "당뇨" not in blob
    assert "고혈압" not in blob
    assert "혈압" not in blob
    assert "혈당" not in blob


def test_assess_includes_ref_kcd():
    out = GuidanceService().assess("머리가 아파요")
    headache = next(s for s in out.symptoms if s["code"] == "headache")
    assert headache["ref_conditions"][0]["kcd"] == "R51"
    assert "진단" not in headache["ref_conditions"][0]["name"]


def test_hidden_ref_kcd_is_not_endocrine():
    out = GuidanceService().assess("다리 가려움 자주 목마름")
    codes = {
        r["kcd"]
        for s in out.symptoms
        for r in s.get("ref_conditions") or []
    }
    assert "L29" in codes
    assert "K11.7" in codes
    assert "E11" not in codes
    assert "I10" not in codes


def test_conversation_store_clear():
    from harag.retrieval.rewriter import ConversationStore
    store = ConversationStore()
    store.append("c1", "머리가 아파요")
    assert store.get("c1") == ["머리가 아파요"]
    assert store.clear("c1") is True
    assert store.get("c1") == []
    assert store.clear("c1") is False


def _scored(doc_id, title, citation, text, score):
    from datetime import datetime, timezone

    from harag.contracts.boundaries import Chunk, ScoredChunk
    from harag.schemas.chunk import ChunkMetadata, ChunkType

    meta = ChunkMetadata(
        chunk_id=f"c-{doc_id}-{score}",
        document_id=doc_id,
        source_block_ids=["b1"],
        chunk_type=ChunkType.body,
        security_level="internal",
        acl_tags=["dept:x"],
        source_document=title,
        citation_label=citation,
        parsing_version="p",
        chunking_version="c",
        embedding_model_id="m",
        content_hash=f"h-{doc_id}-{score}",
        created_at=datetime.now(timezone.utc),
    )
    return ScoredChunk(chunk=Chunk(meta=meta, text=text), score=score)


def test_product_cards_from_retrieve_and_no_raw_in_query():
    queries = []

    class Pipe:
        def _retrieve_sync(self, q, auth, cid):
            queries.append(q)
            return ([
                _scored("d1", "실손A.pdf", "실손A p.3",
                        "실손 통원 외래 진료비를 안내합니다.", 0.9),
                _scored("d1", "실손A.pdf", "실손A p.4",
                        "낮은 점수 청크", 0.1),
                _scored("d2", "실손B.pdf", "실손B p.1",
                        "실손 검사 특약을 설명합니다.", 0.8),
                _scored("d3", "실손C.pdf", "실손C p.2",
                        "실손 입원 한도를 안내합니다.", 0.7),
                _scored("d4", "실손D.pdf", "실손D p.1",
                        "네 번째 문서는 잘립니다.", 0.2),
            ], object())

    unique = "UNIQUE_RAW_XYZ"
    g = GuidanceService(pipeline=Pipe()).guide(
        f"{unique} 머리가 아파요 010-1111-2222",
        symptom_codes=["headache"],
        occupation="office",
        age_band="30-39",
        sex="male",
        auth=object(),
    )
    assert len(g.products) == 2
    titles = [p["title"] for p in g.products]
    assert titles == ["실손A.pdf", "실손B.pdf"]
    assert g.products[0]["citation_label"] == "실손A p.3"
    assert g.products[0]["snippet"] == ""
    assert "실손 통원" in g.products[0]["matched_coverages"]
    assert queries
    q = queries[0]
    assert unique not in q
    assert "010-1111-2222" not in q
    assert "머리가 아파요" not in q
    assert "실손 통원" in q
    assert "사무직" in q
    assert "30대" in q
    assert "남성" in q
    assert q.endswith("보장")
    assert "당신 성별" not in g.explanation
    assert "가입하세요" not in g.explanation
    assert g.profile["age_label"] == "30대"
    assert g.profile["sex_label"] == "남성"


def test_products_empty_when_no_upload():
    g = GuidanceService().guide("", symptom_codes=["headache"])
    assert g.products == []

    class Pipe:
        def _retrieve_sync(self, q, auth, cid):
            return ([], object())

    empty = GuidanceService(pipeline=Pipe()).guide(
        "", symptom_codes=["headache"], auth=object())
    assert empty.products == []


class _LibRow:
    def __init__(self, document_id, filename, status="ready"):
        self.document_id = document_id
        self.filename = filename
        self.status = status


class _LibMeta:
    def __init__(self, rows):
        self._rows = rows

    def list_library_documents(self, collection_id=None):
        return list(self._rows)


def test_library_ready_fills_when_retrieve_empty():
    class Pipe:
        _metadata = _LibMeta([
            _LibRow("lib1", "뉴라이프건강보험_상품요약서.pdf"),
            _LibRow("lib2", "아직처리중.pdf", status="processing"),
        ])

        def _retrieve_sync(self, q, auth, cid):
            return ([], object())

    g = GuidanceService(pipeline=Pipe()).guide(
        "", symptom_codes=["headache"], auth=object())
    assert len(g.products) == 1
    card = g.products[0]
    assert card["document_id"] == "lib1"
    assert card["title"] == "뉴라이프건강보험_상품요약서.pdf"
    assert card["scope"] == "library"
    assert card["matched_coverages"]
    assert card["snippet"] == ""


def test_library_ready_caps_at_two_products():
    class Pipe:
        _metadata = _LibMeta([
            _LibRow("lib1", "a.pdf"),
            _LibRow("lib2", "b.pdf"),
            _LibRow("lib3", "c.pdf"),
        ])

        def _retrieve_sync(self, q, auth, cid):
            return ([], object())

    g = GuidanceService(pipeline=Pipe()).guide(
        "", symptom_codes=["headache"], concerns=["inpatient", "tests"],
        occupation="field", auth=object())
    assert len(g.products) <= 2
    assert len(g.products) == 2
    assert "## 비교할 상품 2개" in g.explanation_why


def test_retrieve_hit_title_is_filename_and_outranks_library():
    class Pipe:
        _metadata = _LibMeta([
            _LibRow("other", "다른설명서.pdf"),
        ])

        def _retrieve_sync(self, q, auth, cid):
            return ([
                _scored("d1", "뉴라이프건강보험_상품요약서.pdf", "요약서 p.5",
                        "실손 통원 외래 진료비를 안내합니다.", 0.8),
            ], object())

    g = GuidanceService(pipeline=Pipe()).guide(
        "", symptom_codes=["headache"], auth=object())
    assert g.products[0]["title"] == "뉴라이프건강보험_상품요약서.pdf"
    assert g.products[0]["document_id"] == "d1"
    ids = [p["document_id"] for p in g.products]
    assert "other" in ids
    assert ids[0] == "d1"


def test_product_card_matches_brochure_aliases():
    class Pipe:
        def _retrieve_sync(self, q, auth, cid):
            return ([
                _scored("d1", "뉴라이프건강보험_상품요약서.pdf", "요약서 p.5",
                        "상해통원의료비 갱신형 질병입원의료비 일반상해의료비", 0.8),
            ], object())

    g = GuidanceService(pipeline=Pipe()).guide(
        "", symptom_codes=["headache", "leg_cramp"], auth=object())
    assert g.products
    matched = g.products[0]["matched_coverages"]
    assert "실손 통원" in matched
    assert "상해 관련 실손" in matched


def test_unspecified_sex_not_in_query():
    queries = []

    class Pipe:
        def _retrieve_sync(self, q, auth, cid):
            queries.append(q)
            return ([], object())

    GuidanceService(pipeline=Pipe()).guide(
        "", symptom_codes=["headache"], sex="unspecified", auth=object())
    assert queries
    assert "남성" not in queries[0]
    assert "여성" not in queries[0]
    assert "응답 안 함" not in queries[0]


def test_symptom_cluster_fatigue_headache_numbness():
    from harag.guidance.clusters import match_symptom_clusters

    miss = match_symptom_clusters(["fatigue", "headache"])
    assert miss == []
    hits = match_symptom_clusters(["fatigue", "headache", "numbness"])
    assert len(hits) == 1
    assert hits[0]["id"] == "fatigue_headache_numbness"
    fams = hits[0]["related_families"]
    assert fams
    assert all(f.get("example_kcds") for f in fams)
    assert "전조" not in (hits[0].get("label") or "")


def test_guide_returns_symptom_clusters_and_fallback_copy():
    g = GuidanceService().guide(
        "",
        symptom_codes=["fatigue", "headache", "numbness"],
        occupation="field",
        age_band="20-29",
        sex="male",
    )
    assert not g.abstained
    assert g.symptom_clusters
    assert g.symptom_clusters[0]["id"] == "fatigue_headache_numbness"
    why = g.explanation_why or ""
    assert "관련 가능 영역" in why
    assert "질병관리청" in why
    assert "확진이 아닙니다" in why
    assert "직업별 질환 통계는 아직" in why
    assert "유병률" not in why
    assert "전조 증상" not in why
    assert "(R" not in why
    assert "참고용 안내입니다" not in why


def test_guard_blocks_precursor_and_risk_wording():
    text, flags = guard_text(
        "당뇨 전조입니다. 위험성이 높습니다. 실손 통원을 살펴보시면 됩니다.",
        ["실손 통원"])
    assert "risk_claim_blocked" in flags
    assert "전조입니다" not in text
    assert "위험성" not in text


def test_guard_strips_kcd_paren_and_filler():
    text, flags = guard_text(
        "권태 및 피로(R53)를 참고합니다. 참고용 안내입니다. 실손 통원을 살펴보시면 됩니다.",
        ["실손 통원"])
    assert "kcd_code_stripped" in flags
    assert "filler_ref_stripped" in flags
    assert "(R53)" not in text
    assert "참고용 안내입니다" not in text
    assert "권태 및 피로" in text
