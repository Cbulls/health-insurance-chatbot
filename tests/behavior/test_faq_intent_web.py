"""FAQ intent 라우팅 + 공식 사이트 allowlist 검색."""
from __future__ import annotations

from harag.api.citations_fmt import citations_from_chunks
from harag.query.faq import (
    format_faq_answer,
    load_faq,
    match_faq,
    premium_template_answer,
)
from harag.query.intent import classify_intent, classify_intent_rules
from harag.retrieval.official_web import (
    AllowlistEntry,
    HeuristicWebPlanner,
    OfficialWebBrowser,
    WebBrowseConfig,
    html_to_text,
    is_host_allowed,
    prepend_web_chunks,
    resolve_allowlist_for_products,
    scored_web_chunk,
)


def test_faq_cards_load_and_match_enroll():
    cards = load_faq()
    assert any(c.id == "how_to_enroll" for c in cards)
    hit = match_faq("이 보험 가입 방법이 궁금해요")
    assert hit is not None
    assert hit.id == "how_to_enroll"
    text = format_faq_answer(hit)
    assert "청약" in text or "가입" in text


def test_intent_premium_and_enroll():
    p = classify_intent_rules("월 보험료 얼마예요?")
    assert p.label == "premium_quote"
    e = classify_intent_rules("어떻게 가입하나요?")
    assert e.label == "common_faq"
    assert e.faq is not None
    # 담보·한도 조항 질의는 상품 사실(RAG)
    assert classify_intent("통원의료비 특약 보장 한도는?").label == "product_fact"


def test_premium_template_has_no_fixed_won_amount():
    ans = premium_template_answer(
        display_name="테스트상품",
        insurer="테스트생명",
        summary="요약",
        source_url="https://www.example.com/p",
    )
    assert "확정할 수 없습니다" in ans
    assert "테스트상품" in ans
    assert "https://www.example.com/p" in ans
    # 템플릿 자체가 월 ○○원을 단정하지 않음
    assert "월 5만원" not in ans
    assert "월 53,000" not in ans


def test_allowlist_rejects_foreign_host():
    products = [{
        "insurer": "KB손해보험",
        "source_url": "https://www.kbinsure.co.kr/x",
        "display_name": "뉴라이프",
    }]
    entries = resolve_allowlist_for_products(products)
    assert entries
    hosts = entries[0].hosts
    assert is_host_allowed("https://www.kbinsure.co.kr/a", hosts)
    assert not is_host_allowed("https://evil.example/phish", hosts)
    assert not is_host_allowed("https://www.google.com/search", hosts)


def test_html_to_text_strips_script():
    html = "<html><script>alert(1)</script><p>가입 안내</p></html>"
    text = html_to_text(html, 500)
    assert "alert" not in text
    assert "가입 안내" in text


def test_web_chunk_citation_exposes_url():
    sc = scored_web_chunk(
        "https://www.kbinsure.co.kr/page",
        "보험료는 나이·성별에 따라 달라집니다.",
    )
    cites = citations_from_chunks([sc])
    assert cites
    assert cites[0].document_id == "web"
    assert "kbinsure.co.kr" in cites[0].source_document
    assert cites[0].chunk_id.startswith("web-")


def test_browser_drops_non_allowlisted_plan_url(monkeypatch):
    """플랜에 외부 URL이 있어도 fetch하지 않는다."""
    fetched: list[str] = []

    class FakeClient:
        def get(self, url):
            fetched.append(url)
            raise AssertionError("should not fetch")

        def post(self, url, data=None):
            class R:
                status_code = 200
                text = ""
                def raise_for_status(self):
                    return None
            return R()

        def close(self):
            return None

    class EvilPlanner:
        def plan(self, **kwargs):
            return {
                "searches": [],
                "urls": ["https://evil.example/steal"],
            }

    browser = OfficialWebBrowser(
        config=WebBrowseConfig(enabled=True, max_fetches=2),
        planner=EvilPlanner(),
        client=FakeClient(),
    )
    entry = AllowlistEntry(
        insurer="KB손해보험",
        hosts=frozenset({"www.kbinsure.co.kr", "kbinsure.co.kr"}),
        homepage="https://www.kbinsure.co.kr",
        source_url="",
        display_name="뉴라이프",
    )

    # homepage fetch만 시도 — FakeClient.get이 실패하면 청크 없음
    chunks = browser.browse("가입 방법", [entry])
    assert all("evil.example" not in u for u in fetched)
    assert chunks == [] or all(
        "evil" not in (c.chunk.meta.source_document or "") for c in chunks
    )


def test_browser_fetches_allowlisted_seed(monkeypatch):
    class Resp:
        def __init__(self, url, body):
            self.url = url
            self._body = body
            self.headers = {"content-type": "text/html; charset=utf-8"}
            self.encoding = "utf-8"
            self.content = body.encode("utf-8")

        def raise_for_status(self):
            return None

    class FakeClient:
        def get(self, url):
            return Resp(
                url,
                "<html><body><p>공식 가입 안내 페이지입니다.</p></body></html>",
            )

        def post(self, url, data=None):
            class R:
                status_code = 200
                text = ""
                def raise_for_status(self):
                    return None
            return R()

        def close(self):
            return None

    browser = OfficialWebBrowser(
        config=WebBrowseConfig(
            enabled=True, max_fetches=1, max_searches=0),
        planner=HeuristicWebPlanner(),
        client=FakeClient(),
    )
    entry = AllowlistEntry(
        insurer="삼성생명",
        hosts=frozenset({"www.samsunglife.com", "samsunglife.com"}),
        homepage="https://www.samsunglife.com",
        source_url="https://www.samsunglife.com/product",
        display_name="New플러스원",
    )
    chunks = browser.browse("보험료 얼마인가요", [entry])
    assert len(chunks) >= 1
    assert chunks[0].chunk.meta.document_id == "web"
    assert "가입 안내" in chunks[0].chunk.text or "공식" in chunks[0].chunk.text


def test_prepend_web_chunks_puts_web_first():
    web = scored_web_chunk("https://www.kbinsure.co.kr/a", "웹본문")
    from datetime import datetime, timezone
    from harag.contracts.boundaries import Chunk, ScoredChunk
    from harag.schemas.chunk import ChunkMetadata, ChunkType

    meta = ChunkMetadata(
        chunk_id="pdf-1", document_id="d1", source_block_ids=["b"],
        struct_path="제1조", chunk_type=ChunkType.body,
        security_level="internal", acl_tags=["owner:t"],
        source_document="약관.pdf", citation_label="약관",
        parsing_version="p", chunking_version="c", embedding_model_id="m",
        content_hash="h", created_at=datetime.now(timezone.utc),
    )
    pdf = ScoredChunk(chunk=Chunk(meta=meta, text="PDF"), score=0.4)
    out = prepend_web_chunks([pdf], [web])
    assert out[0].chunk.meta.chunk_id.startswith("web-")
    assert out[1].chunk.meta.chunk_id == "pdf-1"


def test_soft_abstain_skipped_when_web_context():
    from harag.security.output_guard import OutputGuard, SOFT_NO_EVIDENCE_ANSWER

    guard = OutputGuard()
    text = "문서에서 관련 정보를 찾을 수 없습니다."
    out = guard.validate(text, [], ["web-abc", "pdf-1"])
    assert out.ok
    assert out.answer == text
    assert out.answer != SOFT_NO_EVIDENCE_ANSWER


def test_pipeline_faq_short_circuit_no_docs():
    from harag.api.pipeline import QueryPipelineImpl
    from harag.contracts.boundaries import AuthContext
    from harag.generation.generator import AnswerGenerator

    class BoomRetriever:
        def retrieve(self, *a, **k):
            raise AssertionError("FAQ short-circuit must skip retrieve")

    pipe = QueryPipelineImpl(
        retriever=BoomRetriever(),
        generator=AnswerGenerator(min_score=0.15),
        web_browser=None,
    )
    auth = AuthContext(user_id="t", acl_tags=frozenset({"owner:t"}))
    result = pipe._answer_sync(
        "실손과 정액의 차이가 뭔가요?", auth, None,
        document_id=None, document_ids=None, match_context=None,
    )
    assert not result.abstained
    assert result.answer
    assert "실손" in result.answer or "정액" in result.answer
    assert result.context_chunks
    assert result.context_chunks[0].chunk.meta.chunk_id == "faq-context"


def test_embedding_failure_faq_fallback():
    """RAG 경로에서 임베딩 403이어도 가입 FAQ는 큐레이션 답으로 폴백."""
    from harag.api.pipeline import QueryPipelineImpl
    from harag.contracts.boundaries import AuthContext
    from harag.embedding.api_embedder import EmbeddingRateLimitError
    from harag.generation.generator import AnswerGenerator

    class FailEmbedRetriever:
        def retrieve(self, *a, **k):
            raise EmbeddingRateLimitError(
                "403 insufficient balance: top up jina")

    pipe = QueryPipelineImpl(
        retriever=FailEmbedRetriever(),
        generator=AnswerGenerator(min_score=0.15),
        web_browser=None,
    )
    auth = AuthContext(user_id="t", acl_tags=frozenset({"owner:t"}))
    # waiting_period + docs → RAG 경로 → 임베딩 실패 → FAQ 폴백
    result = pipe._answer_sync(
        "면책기간은 어떻게 되나요?",
        auth, None,
        document_id="doc-1",
        document_ids=["doc-1"],
        match_context=None,
    )
    assert not result.abstained
    assert result.answer
    assert "책임개시" in result.answer or "면책" in result.answer


def test_embedding_failure_product_fact_abstains():
    from harag.api.pipeline import QueryPipelineImpl
    from harag.contracts.boundaries import AuthContext
    from harag.embedding.api_embedder import EmbeddingRateLimitError
    from harag.generation.generator import AnswerGenerator

    class FailEmbedRetriever:
        def retrieve(self, *a, **k):
            raise EmbeddingRateLimitError("403 insufficient balance")

    pipe = QueryPipelineImpl(
        retriever=FailEmbedRetriever(),
        generator=AnswerGenerator(min_score=0.15),
        web_browser=None,
    )
    auth = AuthContext(user_id="t", acl_tags=frozenset({"owner:t"}))
    result = pipe._answer_sync(
        "통원의료비 특약 보장 한도는?",
        auth, None,
        document_id="doc-1",
        document_ids=["doc-1"],
        match_context=None,
    )
    assert result.abstained
    assert result.abstain_reason == "embedding_unavailable"
    assert result.answer is None
