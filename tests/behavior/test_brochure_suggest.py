"""추천 질문 facts + LLM 윤문 · 품질 골든."""
from __future__ import annotations

from harag.guidance.brochure_suggest import (
    KIND_PRODUCT_TYPE,
    SUGGEST_QUESTIONS,
    all_catalog_profiles,
    answer_from_catalog,
    build_suggest_facts,
    compose_suggest_answer,
    match_suggest_question,
    polish_suggest_answer,
    try_brochure_suggest_answer,
)
from harag.guidance.fit_reasons import strip_doc_markers, _parse_fit_reasons_json
from harag.security.output_guard import soft_no_evidence_answer


def _meritz_profile():
    for fname, prof in all_catalog_profiles():
        if "메리츠" in fname or "메리츠" in (prof.get("display_name") or ""):
            return fname, prof
    raise AssertionError("meritz catalog missing")


def test_strip_doc_markers_from_fit_reasons():
    raw = (
        '{"reasons":['
        '{"aspect":"symptom","text":"두통과 연결됩니다 [문서 1]."},'
        '{"aspect":"concern","text":"병원비 걱정을 봅니다 [문서 1, 2]."},'
        '{"aspect":"condition","text":"20대 남성 조건을 참고합니다."}'
        "]}"
    )
    parsed = _parse_fit_reasons_json(raw)
    assert len(parsed) == 3
    blob = " ".join(r["text"] for r in parsed)
    assert "[문서" not in blob


def test_eleven_catalog_times_three_quality_gates():
    profiles = all_catalog_profiles()
    assert len(profiles) >= 11
    soft = soft_no_evidence_answer("통원 담보 한도는?")
    for fname, prof in profiles:
        for q in SUGGEST_QUESTIONS:
            m = match_suggest_question(q)
            assert m is not None
            ans = answer_from_catalog(prof, m.kind)
            assert ans and len(ans) > 20, fname
            assert "근거가 없" not in ans
            assert "찾지 못했습니다" not in ans
            assert "은(는)" not in ans
            assert "[문서" not in ans
            assert ans != soft


def test_meritz_product_type_golden():
    _fname, prof = _meritz_profile()
    ans = answer_from_catalog(prof, KIND_PRODUCT_TYPE)
    assert "정액" in ans
    assert "건강" in ans
    assert "은(는)" not in ans
    assert "메리츠화재 메리츠" not in ans
    # 메타 문장만 인용하지 않음
    assert "「메리츠화재 무배당 통합간편건강보험(연만기형)2607 상품요약서입니다」" not in ans
    assert not ans.strip().endswith("상품요약서입니다.")


def test_facts_skip_meta_summary_sentence():
    _fname, prof = _meritz_profile()
    facts = build_suggest_facts(prof, KIND_PRODUCT_TYPE)
    assert "상품요약서입니다" not in (facts.get("summary_core") or "")
    assert facts["nature_short"] == "정액형"
    assert "메리츠화재 메리츠" not in facts["head"]


def test_polish_uses_llm_and_fallback_on_fail():
    _fname, prof = _meritz_profile()
    facts = build_suggest_facts(prof, KIND_PRODUCT_TYPE)
    q = SUGGEST_QUESTIONS[0]

    class GoodLLM:
        def complete(self, prompt, ctx, cites):
            return (
                "메리츠 통합간편건강보험은 정액형 건강보험입니다. "
                "간편심사형으로 진단·수술·입원일당 특약을 안내합니다.",
                [],
            )

    polished = polish_suggest_answer(facts, q, GoodLLM())
    assert polished
    assert "정액" in polished
    assert "은(는)" not in polished

    class BadLLM:
        def complete(self, prompt, ctx, cites):
            raise RuntimeError("down")

    composed = compose_suggest_answer(
        prof, KIND_PRODUCT_TYPE, q, llm=BadLLM())
    assert "정액" in composed
    assert "은(는)" not in composed
    assert "찾지 못했습니다" not in composed


def test_polish_rejects_solicit():
    facts = build_suggest_facts(_meritz_profile()[1], KIND_PRODUCT_TYPE)

    class SolicitLLM:
        def complete(self, prompt, ctx, cites):
            return ("이 상품에 가입하세요. 정액 건강입니다.", [])

    assert polish_suggest_answer(
        facts, SUGGEST_QUESTIONS[0], SolicitLLM()) is None


def test_try_brochure_suggest_with_filename_as_id():
    profiles = all_catalog_profiles()
    fname, _ = profiles[0]
    q = SUGGEST_QUESTIONS[0]
    hit = try_brochure_suggest_answer(q, [fname], metadata=None, llm=None)
    assert hit is not None
    ans, did = hit
    assert did == fname
    assert "은(는)" not in ans
    assert "찾지 못했습니다" not in ans


def test_pipeline_suggest_short_circuits_without_retrieve():
    from harag.api.pipeline import QueryPipelineImpl
    from harag.contracts.boundaries import AuthContext
    from harag.generation.generator import AnswerGenerator

    class BoomRetriever:
        def retrieve(self, *a, **k):
            raise AssertionError("brochure suggest must skip retrieve")

    class NiceLLM:
        def complete(self, prompt, ctx, cites):
            return (
                "이 상품은 정액형 건강보험입니다. "
                "요약서에 진단·수술·입원일당이 안내됩니다.",
                [],
            )

    fname, _ = all_catalog_profiles()[0]
    pipe = QueryPipelineImpl(
        retriever=BoomRetriever(),
        generator=AnswerGenerator(min_score=0.15),
        web_browser=None,
        suggest_llm=NiceLLM(),
        context_max_tokens=3500,
    )
    auth = AuthContext(user_id="t", acl_tags=frozenset({"owner:t"}))
    _results, _tr, _intent, short = pipe._assemble_results(
        SUGGEST_QUESTIONS[0],
        auth, None,
        document_id=fname,
        document_ids=[fname],
        match_context=None,
    )
    assert short is not None
    assert short.answer
    assert not short.abstained
    assert "찾지 못했습니다" not in short.answer
    assert "은(는)" not in short.answer
    assert "정액" in short.answer or "건강" in short.answer
