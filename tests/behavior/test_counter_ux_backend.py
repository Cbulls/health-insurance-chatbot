"""창구 UX 백엔드: 인용 스키마, HWPX, 확장자, 스캔 오류."""
from __future__ import annotations

import io
import zipfile
from unittest.mock import MagicMock

import pytest

from harag.api.citations_fmt import citations_from_chunks, citations_sse_payload
from harag.contracts.boundaries import Chunk, ScoredChunk
from harag.parsing.document_parser import DocumentParser
from harag.parsing.hwpx_decoder import HwpxDecoder
from harag.parsing.pdf_parser import PdfParser
from harag.schemas.chunk import ChunkMetadata, ChunkType
from harag.indexing.pdf_pipeline import _parse_fail_message
from datetime import datetime, timezone


def _chunk(cid: str, label: str, text: str, path: str = "제1조") -> ScoredChunk:
    meta = ChunkMetadata(
        chunk_id=cid,
        document_id="d1",
        source_block_ids=["b1"],
        struct_path=path,
        chunk_type=ChunkType.body,
        page_ref=3,
        security_level="internal",
        acl_tags=["owner:u"],
        source_document="규정.pdf",
        citation_label=label,
        parsing_version="t",
        chunking_version="t",
        embedding_model_id="t",
        content_hash="abc",
        created_at=datetime.now(timezone.utc),
    )
    return ScoredChunk(chunk=Chunk(text=text, meta=meta), score=0.9)


def test_citations_include_chunk_id_and_snippet():
    cites = citations_from_chunks([_chunk("c1", "규정.pdf p.3", "출장비 한도는 10만원이다.")])
    assert len(cites) == 1
    assert cites[0].chunk_id == "c1"
    assert cites[0].struct_path == "제1조"
    assert "출장비" in cites[0].snippet
    payload = citations_sse_payload([_chunk("c1", "L", "hello world")])
    assert isinstance(payload, list)
    assert payload[0]["chunk_id"] == "c1"


def _minimal_hwpx_bytes() -> bytes:
    section = """<?xml version="1.0" encoding="UTF-8"?>
    <hs:sec xmlns:hs="http://www.hancom.co.kr/hwpml/2011/section"
            xmlns:hp="http://www.hancom.co.kr/hwpml/2011/paragraph">
      <hp:p><hp:run><hp:t>제12조 출장비</hp:t></hp:run></hp:p>
      <hp:p><hp:run><hp:t>국내 출장 한도는 1일 10만원이다.</hp:t></hp:run></hp:p>
      <hp:tbl>
        <hp:tr>
          <hp:tc><hp:p><hp:run><hp:t>항목</hp:t></hp:run></hp:p></hp:tc>
          <hp:tc><hp:p><hp:run><hp:t>한도</hp:t></hp:run></hp:p></hp:tc>
        </hp:tr>
        <hp:tr>
          <hp:tc><hp:p><hp:run><hp:t>출장</hp:t></hp:run></hp:p></hp:tc>
          <hp:tc><hp:p><hp:run><hp:t>10만원</hp:t></hp:run></hp:p></hp:tc>
        </hp:tr>
      </hp:tbl>
    </hs:sec>
    """
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr("Contents/section0.xml", section)
        zf.writestr("mimetype", "application/hwp+zip")
    return buf.getvalue()


def test_hwpx_decoder_extracts_paragraphs_and_table():
    blocks = HwpxDecoder().decode(_minimal_hwpx_bytes(), "hwpx")
    kinds = {b.kind for b in blocks}
    assert "paragraph" in kinds or "heading" in kinds
    assert "table" in kinds
    texts = " ".join(b.text for b in blocks if b.text)
    assert "출장" in texts or any(
        c.get("text") == "출장" for b in blocks if b.kind == "table" for c in b.cells
    )


def test_document_parser_hwpx():
    p = DocumentParser()
    ir = p.parse(
        _minimal_hwpx_bytes(),
        document_id="doc-hwpx",
        filename="출장.hwpx",
    )
    assert ir.parse_status.value in ("ok", "partial")
    assert ir.blocks


def test_parse_fail_messages():
    assert "OCR" in _parse_fail_message("scan_pdf_no_ocr")
    assert "암호" in _parse_fail_message("encrypted_pdf")


def test_pdf_empty_text_sets_scan_code():
    """텍스트 없는 최소 PDF → scan_pdf_no_ocr."""
    # 빈 페이지에 가까운 최소 PDF (헤더만)
    # pdfplumber로 열리지만 텍스트 없음
    pytest.importorskip("pdfplumber")
    # 1-page blank-ish PDF bytes (minimal valid PDF)
    raw = b"""%PDF-1.1
1 0 obj<< /Type /Catalog /Pages 2 0 R >>endobj
2 0 obj<< /Type /Pages /Kids [3 0 R] /Count 1 >>endobj
3 0 obj<< /Type /Page /Parent 2 0 R /MediaBox [0 0 300 144] /Contents 4 0 R >>endobj
4 0 obj<< /Length 0 >>stream
endstream
endobj
xref
0 5
0000000000 65535 f 
0000000009 00000 n 
0000000058 00000 n 
0000000115 00000 n 
0000000214 00000 n 
trailer<< /Size 5 /Root 1 0 R >>
startxref
291
%%EOF
"""
    parser = PdfParser()
    ir = parser.parse(raw, document_id="blank", filename="blank.pdf")
    assert ir.parse_status.value == "failed"
    assert parser.last_fail_code == "scan_pdf_no_ocr"


def test_suffix_formats():
    from harag.api.routes_ingest import _suffix_of, _ALLOWED_SUFFIXES
    assert _suffix_of("a.hwp") == ".hwp"
    assert _suffix_of("a.hwpx") == ".hwpx"
    assert _suffix_of("a.pdf") == ".pdf"
    assert _suffix_of("a.docx") == ".docx"
    assert ".hwp" in _ALLOWED_SUFFIXES
    assert ".docx" in _ALLOWED_SUFFIXES
