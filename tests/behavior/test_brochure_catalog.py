"""설명서 카테고리 — 닫힌 코드, 보험료 폐기, 프로필 없어도 카드 유지."""
from __future__ import annotations

from datetime import datetime, timezone

from harag.contracts.boundaries import Chunk, ScoredChunk
from harag.guidance.brochure import (
    extract_brochure_with_llm, sanitize_brochure_payload,
    upsert_catalog_profile,
)
from harag.guidance.service import GuidanceService
from harag.schemas.chunk import ChunkMetadata, ChunkType
from harag.storage.metadata_store import MetadataStore


def _scored(doc_id, title, citation, text, score):
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


def test_sanitize_drops_premium_and_unknown_codes():
    payload = sanitize_brochure_payload({
        "display_name": "임의상품",
        "product_line": "spaceship",
        "doc_kind": "terms",
        "cost_nature": "mixed",
        "premium": 30000,
        "monthly": 12000,
        "coverage_codes": ["outpatient", "magic"],
        "filled_by": "llm",
    }, "a.pdf")
    assert "premium" not in payload
    assert "monthly" not in payload
    assert payload["product_line"] == "other"
    assert payload["doc_kind"] == "terms"
    assert payload["coverage_codes"] == ["outpatient"]


def test_llm_extract_discards_premium_and_bad_codes():
    class LLM:
        def complete(self, q, ctx, ids):
            return (
                '{"display_name":"A","product_line":"health",'
                '"premium":30000,"coverage_codes":["nope"]}',
                [],
            )

    out = extract_brochure_with_llm(LLM(), "상해통원 의료비", "a.pdf")
    assert "premium" not in out
    assert out["product_line"] == "health"
    assert out["coverage_codes"] == []
    assert out["filled_by"] == "llm"


def test_catalog_newlife_upsert():
    meta = MetadataStore(dsn="sqlite:///:memory:")
    assert upsert_catalog_profile(
        meta, "doc1", "library", "뉴라이프건강보험_상품요약서.pdf")
    rec = meta.get_brochure_profile("doc1", "library")
    assert rec.product_line == "health"
    assert rec.cost_nature == "mixed"
    assert rec.doc_kind == "summary"
    assert rec.display_name == "뉴라이프건강보험"
    assert rec.insurer == "KB손해보험"
    assert rec.source_url == "https://www.kbinsure.co.kr/CG802030002.ec"
    assert "입원일당" in rec.summary
    assert "구구구구" not in rec.summary


def test_catalog_samsung_plusone_upsert():
    meta = MetadataStore(dsn="sqlite:///:memory:")
    assert upsert_catalog_profile(
        meta, "doc2", "library", "삼성_New플러스원_건강보험2608_상품요약서.pdf")
    rec = meta.get_brochure_profile("doc2", "library")
    assert rec.product_line == "health"
    assert rec.cost_nature == "lump_sum"
    assert rec.doc_kind == "summary"
    assert rec.display_name == "삼성 New플러스원 건강보험"
    assert rec.insurer == "삼성생명"
    assert rec.source_url == (
        "https://www.samsunglife.com/individual/products/disclosure/sales/"
        "PDO-PRPRI010110M")
    assert "실손" not in rec.summary
    assert set(rec.coverage_codes) == {"outpatient", "inpatient", "injury"}


def test_catalog_meritz_simple_upsert():
    meta = MetadataStore(dsn="sqlite:///:memory:")
    assert upsert_catalog_profile(
        meta, "doc3", "library", "메리츠_통합간편건강보험2607_상품요약서.pdf")
    rec = meta.get_brochure_profile("doc3", "library")
    assert rec.product_line == "health"
    assert rec.cost_nature == "lump_sum"
    assert rec.doc_kind == "summary"
    assert rec.display_name == "메리츠 통합간편건강보험"
    assert rec.insurer == "메리츠화재"
    assert rec.source_url == ""
    assert "실손" not in rec.summary
    assert set(rec.coverage_codes) == {"outpatient", "inpatient", "injury"}


def test_catalog_hanwha_311_upsert():
    meta = MetadataStore(dsn="sqlite:///:memory:")
    assert upsert_catalog_profile(
        meta, "doc4", "library", "한화_311간편건강보험2604_상품요약서.pdf")
    rec = meta.get_brochure_profile("doc4", "library")
    assert rec.product_line == "health"
    assert rec.cost_nature == "lump_sum"
    assert rec.doc_kind == "summary"
    assert rec.display_name == "한화 311 간편건강보험"
    assert rec.insurer == "한화손해보험"
    assert rec.source_url == ""
    assert "실손" not in rec.summary
    assert set(rec.coverage_codes) == {"outpatient", "inpatient", "injury"}


def test_catalog_abl_wooriwon_upsert():
    meta = MetadataStore(dsn="sqlite:///:memory:")
    assert upsert_catalog_profile(
        meta, "doc5", "library",
        "ABL_우리WON건강N더보장종합보험2601_상품요약서.pdf")
    rec = meta.get_brochure_profile("doc5", "library")
    assert rec.product_line == "health"
    assert rec.cost_nature == "lump_sum"
    assert rec.doc_kind == "summary"
    assert rec.display_name == "우리WON건강N더보장종합보험"
    assert rec.insurer == "ABL생명"
    assert rec.source_url == ""
    assert "실손" not in rec.summary
    assert set(rec.coverage_codes) == {"outpatient", "inpatient", "injury"}


def test_catalog_heungkuk_3n5n_upsert():
    meta = MetadataStore(dsn="sqlite:///:memory:")
    assert upsert_catalog_profile(
        meta, "doc6", "library",
        "흥국_3N5N플러스간편건강보험_해약환급금미지급형V2_상품요약서.pdf")
    rec = meta.get_brochure_profile("doc6", "library")
    assert rec.product_line == "health"
    assert rec.cost_nature == "lump_sum"
    assert rec.doc_kind == "summary"
    assert rec.display_name == "흥국 3.N.5.N플러스간편건강보험"
    assert rec.insurer == "흥국생명"
    assert rec.source_url == ""
    assert "실손" not in rec.summary
    assert set(rec.coverage_codes) == {"outpatient", "inpatient", "injury"}


def test_sanitize_drops_non_http_url():
    payload = sanitize_brochure_payload({
        "display_name": "A",
        "product_line": "health",
        "source_url": "javascript:alert(1)",
        "filled_by": "catalog",
    }, "a.pdf")
    assert payload["source_url"] == ""


def test_llm_extract_does_not_keep_url():
    class LLM:
        def complete(self, q, ctx, ids):
            return (
                '{"display_name":"A","product_line":"health",'
                '"insurer":"KB손해보험",'
                '"source_url":"https://evil.example/x"}',
                [],
            )

    out = extract_brochure_with_llm(LLM(), "상해통원", "a.pdf")
    assert out["insurer"] == "KB손해보험"
    assert out["source_url"] == ""


def test_products_keep_filename_without_profile():
    class Pipe:
        def _retrieve_sync(self, q, auth, cid):
            return ([
                _scored("d1", "실손A.pdf", "실손A p.3",
                        "실손 통원 외래 진료비를 안내합니다.", 0.9),
            ], object())

    g = GuidanceService(pipeline=Pipe()).guide(
        "", symptom_codes=["headache"], auth=object())
    assert g.products
    assert g.products[0]["title"] == "실손A.pdf"
    assert not g.products[0].get("product_line")


def test_products_attach_catalog_profile():
    meta = MetadataStore(dsn="sqlite:///:memory:")
    upsert_catalog_profile(
        meta, "d1", "library", "뉴라이프건강보험_상품요약서.pdf")

    class Pipe:
        _metadata = meta

        def _retrieve_sync(self, q, auth, cid):
            return ([
                _scored("d1", "뉴라이프건강보험_상품요약서.pdf", "요약서 p.5",
                        "실손 통원 외래 진료비를 안내합니다.", 0.8),
            ], object())

    g = GuidanceService(pipeline=Pipe()).guide(
        "", symptom_codes=["headache"], auth=object())
    assert g.products[0]["name"] == "뉴라이프건강보험"
    assert g.products[0]["product_line"] == "health"
    assert g.products[0]["cost_nature_label"]
    assert g.products[0]["title"] == "뉴라이프건강보험_상품요약서.pdf"
    assert g.products[0]["insurer"] == "KB손해보험"
    assert g.products[0]["source_url"] == "https://www.kbinsure.co.kr/CG802030002.ec"
    assert g.products[0]["snippet"] == ""
    assert "구구구구" not in (g.products[0].get("summary") or "")
