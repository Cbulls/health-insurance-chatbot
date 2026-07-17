"""
정확도 개선 동작 테스트 — 조항 감지·표 직렬화·문장 폴백·구조화 인용.

대상:
  - pdf_parser._clause_of: 조·항 헤더 감지(한국어 규정 + 영문 약관)
  - chunker 표 직렬화: 행 단위 '헤더: 값' + 섹션 프리픽스
  - chunker 문장 폴백: max_tokens 초과 시 문장 경계 분할 + 1문장 오버랩
  - citations.extract_cited_ids: [문서 N] 마커 → chunk_id(위조 번호 무시)
  - injection.build_safe_messages: system/user 역할 분리 + 문서 번호
  - http_transport: system 역할 전송 + 마커 → citations 매핑
"""
from __future__ import annotations

import json
from datetime import datetime, timezone

import httpx
import pytest

from harag.parsing.pdf_parser import _clause_of
from harag.chunking.chunker import StructuralChunker, ChunkingContext
from harag.generation.citations import extract_cited_ids
from harag.security.injection import build_safe_messages
from harag.schemas.ir import (
    DocumentIR, Block, BlockType, TableCell, TableContent,
    SourceFormat, ExtractionPath, ParseStatus, SourceMetadata, ParseQuality,
)


# ── 조·항 헤더 감지 ──
@pytest.mark.parametrize("line,expected", [
    ("제12조(여비) 국내 출장은...", "제12조"),
    ("제24조의2 특례", "제24조의2"),
    ("제 3 장 보험금의 지급", "제 3 장"),
    ("Article 5 (Benefit Limits) Annual...", "Article 5"),
    ("Section 2. Definitions", "Section 2"),
])
def test_clause_header_detected(line, expected):
    got = _clause_of(line)
    assert got is not None
    assert got.replace(" ", "") == expected.replace(" ", "")


@pytest.mark.parametrize("line", [
    "이 규정은 제12조에 따라 시행한다.",   # 줄 중간 언급은 헤더 아님
    "보험금 청구 절차 안내",
    "Premiums are due monthly.",
])
def test_non_header_not_detected(line):
    assert _clause_of(line) is None


# ── IR 헬퍼 ──
def _ir(blocks):
    return DocumentIR(
        document_id="d1", source_format=SourceFormat.pdf_text,
        extraction_path=ExtractionPath.native, parser_version="p",
        ingested_at=datetime.now(timezone.utc),
        source_metadata=SourceMetadata(
            source_system="s", department="d", security_level="internal",
            original_path="/x.pdf"),
        parse_quality=ParseQuality(
            char_count=100, table_recovery_avg=1.0,
            broken_char_ratio=0.0, order_confidence=1.0),
        parse_status=ParseStatus.ok, blocks=blocks,
    )


def _ctx(**kw):
    defaults = dict(acl_tags=["owner:t"], source_document="약관.pdf",
                    embedding_model_id="m")
    defaults.update(kw)
    return ChunkingContext(**defaults)


def _table_block(order=0, struct="제5조"):
    cells = [
        TableCell(row=0, col=0, text="항목", is_header=True),
        TableCell(row=0, col=1, text="연간 한도", is_header=True),
        TableCell(row=1, col=0, text="치과"),
        TableCell(row=1, col=1, text="100만원"),
        TableCell(row=2, col=0, text="통원"),
        TableCell(row=2, col=1, text="회당 20만원"),
    ]
    return Block(
        block_id=f"d1-b{order}", block_type=BlockType.table,
        struct_path=struct, order_index=order,
        table_content=TableContent(
            n_rows=3, n_cols=2, header_rows=[0], cells=cells,
            recovery_confidence=0.5),
        confidence=0.5, page_ref=3,
    )


# ── 표 직렬화: 헤더-값 관계 보존 ──
def test_table_serialized_as_markdown():
    chunks = StructuralChunker().chunk(_ir([_table_block()]), _ctx())
    assert len(chunks) == 1
    text = chunks[0].text
    assert "| 항목 | 연간 한도 |" in text
    assert "| --- | --- |" in text
    assert "| 치과 | 100만원 |" in text
    assert "| 통원 | 회당 20만원 |" in text
    # 섹션 프리픽스 — 표만 검색돼도 소속 조항을 안다
    assert text.startswith("[제5조]")


def test_table_without_clause_uses_page_proxy_no_prefix():
    chunks = StructuralChunker().chunk(
        _ir([_table_block(struct="p3")]), _ctx())
    assert not chunks[0].text.startswith("[p3]")


# ── 문장 경계 폴백 + 오버랩 ──
def test_oversize_body_split_at_sentence_boundary_with_overlap():
    sentences = [f"문장 {i}번의 내용은 이러하다." for i in range(20)]
    text = " ".join(sentences)
    block = Block(block_id="d1-b0", block_type=BlockType.paragraph,
                  struct_path="제1조", order_index=0, text=text,
                  confidence=1.0, page_ref=1)
    ctx = _ctx(max_tokens=60, overlap_tokens=20)
    chunks = StructuralChunker().chunk(_ir([block]), ctx)

    assert len(chunks) > 1, "상한 초과인데 분할되지 않음"
    for c in chunks:
        # 프리픽스([제1조])를 감안해 여유를 둔다
        body = c.text.split("\n", 1)[-1] if c.text.startswith("[") else c.text
        assert len(body) // 2 <= 60 + 40, "조각이 상한을 크게 초과"
        assert c.meta.struct_path == "제1조"
    # 토큰 오버랩: 인접 조각이 공통 숫자(문장 번호)를 가진다
    import re
    for prev, nxt in zip(chunks, chunks[1:]):
        prev_nums = set(re.findall(r"문장 (\d+)번", prev.text))
        next_nums = set(re.findall(r"문장 (\d+)번", nxt.text))
        assert prev_nums & next_nums, f"오버랩 없음: {prev_nums} vs {next_nums}"


# ── [문서 N] 마커 파싱 ──
def test_extract_cited_ids_maps_markers():
    ids = ["c-a", "c-b", "c-c"]
    ans = "통원 한도는 회당 20만원이다 [문서 2]. 면책은 성형이다 [문서 3]."
    assert extract_cited_ids(ans, ids) == ["c-b", "c-c"]


def test_extract_cited_ids_ignores_out_of_range_and_dedups():
    ids = ["c-a", "c-b"]
    ans = "근거 [문서 1] 그리고 [문서 9]. 다시 [문서 1]. 묶음 [문서 1, 2]."
    assert extract_cited_ids(ans, ids) == ["c-a", "c-b"]


def test_extract_cited_ids_no_markers():
    assert extract_cited_ids("마커 없는 답변", ["c-a"]) == []


# ── system/user 역할 분리 ──
def test_build_safe_messages_separates_roles_and_numbers_docs():
    from harag.security.injection import InjectionPolicy
    system, user = build_safe_messages(
        system_instruction="문서에만 근거해 답하라.",
        query="치과 한도는?",
        context_texts=["첫 문서", "둘째 문서"],
        policy=InjectionPolicy(datamark_enabled=False, canary_enabled=False))
    assert "문서에만 근거해" in system
    assert "[문서 1] 첫 문서" in user and "[문서 2] 둘째 문서" in user
    assert "치과 한도는?" in user
    assert "첫 문서" not in system  # 문서 본문은 system에 넣지 않는다
    assert "HARAG_CTX_OPEN_" in user


# ── 전송 계층: system 역할 + 마커 → citations ──
def test_transport_sends_system_role_and_maps_citations():
    from harag.llm.http_transport import OpenAIChatTransport

    seen = {}

    def handler(request):
        body = json.loads(request.read())
        seen["messages"] = body["messages"]
        content = "치과 한도는 100만원이다 [문서 2]."
        return httpx.Response(200, json={
            "choices": [{"message": {"content": content}}]})

    t = OpenAIChatTransport(api_base="https://fake.local/v1", api_key="k")
    t._client = httpx.Client(base_url="https://fake.local/v1",
                             transport=httpx.MockTransport(handler))
    resp = t.post({"model": "m", "system": "지시", "prompt": "질문",
                   "context_ids": ["c-a", "c-b"]})

    roles = [m["role"] for m in seen["messages"]]
    assert roles == ["system", "user"]
    assert resp["citations"] == ["c-b"]


def test_transport_without_system_uses_single_user_message():
    """재작성·리랭크 경로({model, prompt}만)의 하위 호환."""
    from harag.llm.http_transport import OpenAIChatTransport

    seen = {}

    def handler(request):
        seen["messages"] = json.loads(request.read())["messages"]
        return httpx.Response(200, json={
            "choices": [{"message": {"content": "ok"}}]})

    t = OpenAIChatTransport(api_base="https://fake.local/v1", api_key="k")
    t._client = httpx.Client(base_url="https://fake.local/v1",
                             transport=httpx.MockTransport(handler))
    resp = t.post({"model": "m", "prompt": "재작성 질의"})
    assert [m["role"] for m in seen["messages"]] == ["user"]
    assert resp["citations"] == []


# ── 생성 후 가드 2: '정보 없음' 답변의 abstain 정규화 ──
def _scored(cid="c1", text="본문", score=0.9):
    from harag.contracts.boundaries import Chunk, ScoredChunk
    from harag.schemas.chunk import ChunkMetadata, ChunkType
    meta = ChunkMetadata(
        chunk_id=cid, document_id="d1", source_block_ids=["b1"],
        struct_path="제1조", chunk_type=ChunkType.body,
        security_level="internal", acl_tags=["owner:t"],
        source_document="약관.pdf", citation_label="약관 제1조",
        parsing_version="p", chunking_version="c", embedding_model_id="m",
        content_hash="h", created_at=datetime.now(timezone.utc))
    return ScoredChunk(chunk=Chunk(meta=meta, text=text), score=score)


def test_no_evidence_answer_normalized_to_abstain():
    from harag.generation.generator import AnswerGenerator

    class NoEvidenceLLM:
        def complete(self, query, context_texts, context_ids):
            return "제공된 문서에는 관련 정보가 포함되어 있지 않아 알 수 없습니다.", []

    gen = AnswerGenerator(llm=NoEvidenceLLM(), min_score=0.1)
    out = gen.generate("보험료는?", [_scored()])
    assert out.abstained and out.answer is None
    assert out.abstain_reason == "no_evidence_in_documents"


def test_grounded_answer_not_normalized():
    from harag.generation.generator import AnswerGenerator

    class GroundedLLM:
        def complete(self, query, context_texts, context_ids):
            return "통원 한도는 회당 20만원이다 [문서 1].", [context_ids[0]]

    gen = AnswerGenerator(llm=GroundedLLM(), min_score=0.1)
    out = gen.generate("통원 한도는?", [_scored()])
    assert not out.abstained
    assert out.cited_chunk_ids == ["c1"]


# ── LLM 리랭커: 점수 파싱 + 실패 시 어휘 폴백 ──
def test_llm_reranker_parses_scores_and_falls_back():
    from harag.llm.local_rerank import LLMCrossEncoder

    class GoodTransport:
        def post(self, payload):
            return {"answer": "[8, 1, 5]"}

    class BadTransport:
        def post(self, payload):
            raise RuntimeError("down")

    good = LLMCrossEncoder(GoodTransport(), "m")
    assert good.score_pairs("q", ["a", "b", "c"]) == [0.8, 0.1, 0.5]

    bad = LLMCrossEncoder(BadTransport(), "m")
    scores = bad.score_pairs("출장비 한도", ["출장비 한도 규정", "무관한 내용"])
    assert scores[0] > scores[1], "폴백(어휘 겹침)이 동작하지 않음"
