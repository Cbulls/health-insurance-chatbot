"""
파서 TDD — 구조화 로직(원시 디코드 → 검증된 IR).

HWP 바이너리 디코딩은 HwpDecoder 어댑터 뒤로 분리(라이브러리·표본 의존).
이 테스트는 파서의 '구조화 책임'을 검증한다 — 표본 없이 가능:
  - 디코더가 준 원시 블록을 IR 블록으로 변환
  - 머리말/꼬리말을 is_noise=true로
  - 표를 셀 좌표로 보존(평탄화 금지)
  - 조·항·호를 struct_path로
  - order_index 전순서 부여
  - 디코드 실패 → parse_status=failed (예외 아님)
"""
import pytest
from harag.parsing.parser import StructuringParser, RawBlock, DecodeError


# ── 가짜 디코더(라이브러리 대역) — 원시 블록 리스트를 돌려준다 ──
class FakeDecoder:
    def __init__(self, raw_blocks=None, raise_error=None):
        self._blocks = raw_blocks or []
        self._error = raise_error

    def decode(self, raw: bytes, source_format: str):
        if self._error:
            raise self._error
        return self._blocks


def _parser(raw_blocks=None, raise_error=None):
    return StructuringParser(decoder=FakeDecoder(raw_blocks, raise_error))


def test_P01_produces_valid_ir():
    """원시 블록 → 유효한 DocumentIR(ok 상태)."""
    raw = [RawBlock(kind="heading", text="제1조 목적", level=1),
           RawBlock(kind="paragraph", text="이 규정은...")]
    ir = _parser(raw).parse(b"x", "hwp5",
                            document_id="d1", source_system="s", department="dep",
                            security_level="internal", original_path="/x.hwp")
    assert ir.parse_status.value == "ok"
    assert len(ir.blocks) == 2


def test_P05_clause_heading_to_struct_path():
    """조·항·호 heading이 struct_path에 계층으로."""
    raw = [RawBlock(kind="heading", text="제24조의2 여비", level=1),
           RawBlock(kind="paragraph", text="...")]
    ir = _parser(raw).parse(b"x", "hwp5", document_id="d", source_system="s",
                            department="d", security_level="internal", original_path="/x")
    headings = [b for b in ir.blocks if b.block_type.value == "heading"]
    assert any("제24조의2" in b.struct_path for b in headings), "조항이 struct_path에 없음"


def test_P06_header_footer_flagged_noise():
    """머리말/꼬리말은 is_noise=true."""
    raw = [RawBlock(kind="header_footer", text="- 1 -"),
           RawBlock(kind="paragraph", text="본문")]
    ir = _parser(raw).parse(b"x", "hwp5", document_id="d", source_system="s",
                            department="d", security_level="internal", original_path="/x")
    hf = [b for b in ir.blocks if b.block_type.value == "header_footer"]
    assert hf and all(b.is_noise for b in hf), "머리말이 노이즈로 분리 안 됨"


def test_P04_table_preserved_as_cells():
    """표는 셀 좌표로 보존(병합 포함). 문자열 평탄화 아님."""
    raw = [RawBlock(kind="table", cells=[
        {"row": 0, "col": 0, "text": "항목", "row_span": 1, "col_span": 2},
        {"row": 1, "col": 0, "text": "교통비"},
        {"row": 1, "col": 1, "text": "5만원"},
    ], n_rows=2, n_cols=2)]
    ir = _parser(raw).parse(b"x", "hwp5", document_id="d", source_system="s",
                            department="d", security_level="internal", original_path="/x")
    tables = [b for b in ir.blocks if b.block_type.value == "table"]
    assert tables, "표 블록 없음"
    t = tables[0].table_content
    assert t.n_rows == 2 and t.n_cols == 2
    assert any(c.col_span == 2 for c in t.cells), "병합셀 좌표 보존 안 됨"


def test_P07_order_index_total_order():
    """order_index가 0,1,2... 전순서로 부여."""
    raw = [RawBlock(kind="paragraph", text=f"p{i}") for i in range(5)]
    ir = _parser(raw).parse(b"x", "hwp5", document_id="d", source_system="s",
                            department="d", security_level="internal", original_path="/x")
    orders = [b.order_index for b in ir.blocks]
    assert orders == sorted(orders) and len(set(orders)) == len(orders), "전순서 위반"


def test_P08_decode_failure_returns_failed_not_crash():
    """디코드 실패(암호·손상) → parse_status=failed, 예외 아님."""
    ir = _parser(raise_error=DecodeError("encrypted")).parse(
        b"x", "hwp5", document_id="d", source_system="s",
        department="d", security_level="internal", original_path="/x")
    assert ir.parse_status.value == "failed", "실패가 failed 상태로 처리 안 됨"


if __name__ == "__main__":
    import sys
    sys.exit(pytest.main([__file__, "-v"]))
