"""
파서 — 원시 디코드 결과를 검증된 IR로 구조화.

설계: HWP 바이너리 디코딩(라이브러리·표본 의존)은 HwpDecoder 어댑터 뒤로 분리한다.
StructuringParser는 디코더가 준 RawBlock 목록을 받아 구조화 책임만 진다:
  노이즈 분리 · 표 셀 좌표 보존 · struct_path · order_index 전순서 · 실패 처리.
이렇게 하면 디코더(어려운 부분)와 구조화(검증 가능한 부분)를 독립 개발·테스트한다.

국면 A PoC GO 판정 후, 실제 디코더(pyhwp/hwp5 등)를 이 인터페이스 뒤에 끼운다.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Protocol

from harag.schemas.ir import (
    DocumentIR, Block, BlockType, TableCell, TableContent,
    SourceFormat, ExtractionPath, ParseStatus, SourceMetadata, ParseQuality,
)


class DecodeError(Exception):
    """디코더가 원본을 못 읽을 때(암호·손상). 파서가 잡아 failed로 변환."""


@dataclass
class RawBlock:
    """디코더가 내놓는 원시 블록(라이브러리 중립 형태)."""
    kind: str                       # heading/paragraph/table/header_footer/...
    text: str = ""
    level: int = 0                  # heading 깊이
    cells: list[dict] = field(default_factory=list)  # table용
    n_rows: int = 0
    n_cols: int = 0


class HwpDecoder(Protocol):
    """원본 바이트 → RawBlock 목록. 구현은 PoC 후 실제 라이브러리로."""
    def decode(self, raw: bytes, source_format: str) -> list[RawBlock]: ...


# 조·항·호 패턴(struct_path 구성용)
_CLAUSE = re.compile(r"(제\s*\d+\s*조(?:의\s*\d+)?|제\s*\d+\s*항|제\s*\d+\s*호)")

_KIND_TO_BLOCKTYPE = {
    "heading": BlockType.heading,
    "paragraph": BlockType.paragraph,
    "table": BlockType.table,
    "list": BlockType.list,
    "footnote": BlockType.footnote,
    "caption": BlockType.caption,
    "header_footer": BlockType.header_footer,
    "figure": BlockType.figure,
}


class StructuringParser:
    def __init__(self, decoder: HwpDecoder):
        self._decoder = decoder

    def parse(self, raw: bytes, source_format: str, *, document_id: str,
              source_system: str, department: str, security_level: str,
              original_path: str) -> DocumentIR:
        meta = SourceMetadata(source_system=source_system, department=department,
                              security_level=security_level, original_path=original_path)
        fmt = SourceFormat(source_format) if source_format in SourceFormat._value2member_map_ \
            else SourceFormat.other

        # ── 디코드(실패는 예외 아니라 failed 상태로) ──
        try:
            raw_blocks = self._decoder.decode(raw, source_format)
        except DecodeError:
            return DocumentIR(
                document_id=document_id, source_format=fmt,
                extraction_path=ExtractionPath.native, parser_version="struct-0.1",
                ingested_at=datetime.now(timezone.utc),
                parse_status=ParseStatus.failed, source_metadata=meta,
                blocks=[],
                parse_quality=ParseQuality(char_count=0, table_recovery_avg=0.0,
                                           broken_char_ratio=0.0, order_confidence=0.0))

        # ── 구조화 ──
        blocks: list[Block] = []
        current_clause = ""          # 직전 조항 경로(하위 블록이 상속)
        char_count = 0
        for idx, rb in enumerate(raw_blocks):
            btype = _KIND_TO_BLOCKTYPE.get(rb.kind, BlockType.paragraph)

            # 조항 heading이면 struct_path 갱신
            struct_path = ""
            if btype == BlockType.heading:
                m = _CLAUSE.search(rb.text)
                if m:
                    current_clause = m.group(1).replace(" ", "")
                struct_path = current_clause
            else:
                struct_path = current_clause

            # 표는 셀 좌표로 보존
            table_content = None
            if btype == BlockType.table:
                cells = [TableCell(row=c["row"], col=c["col"], text=c.get("text", ""),
                                   row_span=c.get("row_span", 1), col_span=c.get("col_span", 1))
                         for c in rb.cells]
                table_content = TableContent(n_rows=rb.n_rows, n_cols=rb.n_cols,
                                             cells=cells, recovery_confidence=1.0)

            char_count += len(rb.text)
            block_kwargs = dict(
                block_id=f"{document_id}-b{idx}",
                block_type=btype,
                struct_path=struct_path,
                order_index=idx,                       # 전순서
                is_noise=(btype == BlockType.header_footer),  # 노이즈 분리
                confidence=1.0,                        # 디코더 신뢰도(실제 디코더가 채움)
            )
            if btype == BlockType.table:
                # 표는 셀 구조만(평탄화 금지 — text 금지)
                block_kwargs["table_content"] = table_content
            else:
                block_kwargs["text"] = rb.text
            blocks.append(Block(**block_kwargs))

        status = ParseStatus.ok if blocks else ParseStatus.partial
        # 표 복원율: table 블록 신뢰도 평균(없으면 1.0). 실제 디코더가 채울 값.
        table_blocks = [b for b in blocks if b.block_type == BlockType.table]
        table_rec = (sum(b.confidence for b in table_blocks) / len(table_blocks)
                     if table_blocks else 1.0)
        return DocumentIR(
            document_id=document_id, source_format=fmt,
            extraction_path=ExtractionPath.native, parser_version="struct-0.1",
            ingested_at=datetime.now(timezone.utc),
            parse_status=status, source_metadata=meta, blocks=blocks,
            parse_quality=ParseQuality(
                char_count=char_count, table_recovery_avg=table_rec,
                broken_char_ratio=0.0, order_confidence=1.0))
