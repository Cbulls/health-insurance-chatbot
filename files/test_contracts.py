"""
계약 테스트 — 불변식이 '문서상 약속'이 아니라 '실행되는 거부'임을 증명한다.

각 테스트는 두 가지를 확인:
  (1) 올바른 객체는 생성된다(positive).
  (2) self-critique가 지목한 위협 패턴은 생성 자체가 불가능하다(negative).
"""
from datetime import datetime, timezone

from pydantic import ValidationError

from ir_schema import (
    DocumentIR, Block, BlockType, TableContent, TableCell,
    SourceFormat, ExtractionPath, ParseStatus, SourceMetadata, ParseQuality,
)
from chunk_schema import ChunkMetadata, ChunkType

NOW = datetime.now(timezone.utc)
PASS, FAIL = [], []

def expect_ok(name, fn):
    try:
        fn(); PASS.append(name)
    except Exception as e:
        FAIL.append(f"{name} — should have succeeded but raised: {type(e).__name__}: {e}")

def expect_reject(name, fn):
    try:
        fn(); FAIL.append(f"{name} — should have been REJECTED but was accepted")
    except (ValidationError, ValueError):
        PASS.append(name)


def _meta(**over):
    base = dict(
        chunk_id="c1", document_id="d1", source_block_ids=["b1"],
        chunk_type=ChunkType.body, security_level="internal",
        acl_tags=["dept:finance"], source_document="여비규정.hwp",
        citation_label="여비규정 제12조", parsing_version="p1",
        chunking_version="ck1", embedding_model_id="kure-v1",
        content_hash="abc", created_at=NOW,
    )
    base.update(over)
    return ChunkMetadata(**base)


def _table_block(order_index=1, block_id="t1"):
    return Block(
        block_id=block_id, block_type=BlockType.table, order_index=order_index, confidence=0.9,
        table_content=TableContent(
            n_rows=2, n_cols=2, header_rows=[0],
            cells=[
                TableCell(row=0, col=0, text="항목", is_header=True),
                TableCell(row=0, col=1, text="금액", is_header=True),
                TableCell(row=1, col=0, text="교통비"),
                TableCell(row=1, col=1, text="50000"),
            ],
            recovery_confidence=0.95,
        ),
    )

def _doc(blocks):
    return DocumentIR(
        document_id="d1", source_format=SourceFormat.hwp5,
        extraction_path=ExtractionPath.native, parser_version="p1",
        ingested_at=NOW,
        source_metadata=SourceMetadata(source_system="ed", department="fin",
                                       security_level="internal", original_path="/x"),
        parse_quality=ParseQuality(char_count=100, table_recovery_avg=0.95,
                                   broken_char_ratio=0.0, order_confidence=0.9),
        parse_status=ParseStatus.ok, blocks=blocks,
    )


# ════════ IR 스키마 ════════
# I3: 표는 셀 구조여야 한다 — 평탄화된 문자열 표는 거부
expect_ok("IR/table cell-structured table accepted", lambda: _table_block())
expect_reject("IR/I3 string-payload table rejected",
    lambda: Block(block_id="t", block_type=BlockType.table, order_index=1,
                  confidence=0.9, text="항목|금액\n교통비|50000"))  # 문자열 표 = 위반
expect_reject("IR/I3 table without table_content rejected",
    lambda: Block(block_id="t", block_type=BlockType.table, order_index=1, confidence=0.9))

# 표 셀이 경계를 벗어나면 거부(복원 오류 은폐 방지)
expect_reject("IR/table cell out of bounds rejected",
    lambda: TableContent(n_rows=1, n_cols=1, cells=[TableCell(row=5, col=0, text="x")],
                         recovery_confidence=0.5))

# header_footer는 노이즈로 표시되어야 함(검색 오염 차단)
expect_reject("IR/header_footer must be flagged noise",
    lambda: Block(block_id="h", block_type=BlockType.header_footer, order_index=2,
                  confidence=0.9, text="페이지 1", is_noise=False))
expect_ok("IR/header_footer noise accepted",
    lambda: Block(block_id="h", block_type=BlockType.header_footer, order_index=2,
                  confidence=0.9, text="페이지 1", is_noise=True))

# I1: block_id 유일
expect_reject("IR/I1 duplicate block_id rejected",
    lambda: _doc([
        Block(block_id="dup", block_type=BlockType.paragraph, order_index=1, confidence=0.9, text="a"),
        Block(block_id="dup", block_type=BlockType.paragraph, order_index=2, confidence=0.9, text="b"),
    ]))

# I2: order_index 전순서(중복 금지) — 읽기 순서 복원 실패 은폐 방지
expect_reject("IR/I2 duplicate order_index rejected",
    lambda: _doc([
        Block(block_id="b1", block_type=BlockType.paragraph, order_index=1, confidence=0.9, text="a"),
        Block(block_id="b2", block_type=BlockType.paragraph, order_index=1, confidence=0.9, text="b"),
    ]))

expect_ok("IR/well-formed document accepted",
    lambda: _doc([
        Block(block_id="b1", block_type=BlockType.heading, struct_path="여비규정 > 제12조",
              order_index=1, level=2, confidence=0.95, text="제12조 국내출장"),
        _table_block(order_index=2),
    ]))


# ════════ 청크 메타데이터 스키마 ════════
expect_ok("CHUNK/well-formed metadata accepted", lambda: _meta())

# M1: acl_tags fail-closed — 권한 미상 청크는 인덱싱 거부
expect_reject("CHUNK/M1 empty acl_tags rejected", lambda: _meta(acl_tags=[]))
expect_reject("CHUNK/M1 whitespace acl_tag rejected", lambda: _meta(acl_tags=["  "]))

# M2: 근거 없는 청크는 거부 — 인용 검증 불가능성 차단
expect_reject("CHUNK/M2 empty source_block_ids rejected", lambda: _meta(source_block_ids=[]))

# extra 필드 금지(스키마 드리프트 차단)
expect_reject("CHUNK/extra field rejected", lambda: _meta(unexpected="x"))

# 표 청크 정상 경로
expect_ok("CHUNK/table chunk accepted",
    lambda: _meta(chunk_type=ChunkType.table, source_block_ids=["t1"]))


# ════════ 결과 ════════
print(f"\n{'='*60}")
print(f"PASS: {len(PASS)} / {len(PASS)+len(FAIL)}")
for f in FAIL:
    print("  ✗", f)
if not FAIL:
    print("  ✓ 모든 계약 불변식이 강제됨 — 위반 객체는 생성 불가")
print('='*60)
