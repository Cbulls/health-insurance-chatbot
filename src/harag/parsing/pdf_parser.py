"""
PDF 파서 — 원본 PDF 바이트를 검증된 IR(DocumentIR)로 구조화(MVP).

설계:
  - HWP 경로(StructuringParser + HwpDecoder)와 별개로, PDF 전용 파서를 둔다.
  - pdfplumber로 페이지별 텍스트 + 표를 추출한다.
  - 텍스트 문단 → paragraph 블록, 표 → table 블록(셀 좌표 보존).
  - struct_path는 MVP에서 페이지("p{n}")를 프록시로 사용 → 페이지 단위 청킹·인용.
  - 실패(암호·손상·스캔 전용)는 예외가 아니라 ParseStatus.failed로 변환.

Phase 2: 조·항·호 struct_path 복원, 표 recovery_confidence 실측, OCR(스캔 PDF).
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone

import pdfplumber

from harag.schemas.ir import (
    DocumentIR, Block, BlockType, TableCell, TableContent,
    SourceFormat, ExtractionPath, ParseStatus, SourceMetadata, ParseQuality,
)

logger = logging.getLogger("harag.parsing")

PARSER_VERSION = "pdf-mvp-0.1"


class PdfParser:
    """PDF → DocumentIR. Parser Protocol의 PDF 특화 구현."""

    def parse(self, raw: bytes, *, document_id: str, filename: str,
              source_system: str = "user-upload", department: str = "self",
              security_level: str = "internal") -> DocumentIR:
        meta = SourceMetadata(
            source_system=source_system, department=department,
            security_level=security_level, original_path=filename,
        )

        blocks: list[Block] = []
        order = 0
        char_count = 0
        n_tables = 0
        table_conf_sum = 0.0

        try:
            import io
            with pdfplumber.open(io.BytesIO(raw)) as pdf:
                for page_no, page in enumerate(pdf.pages, start=1):
                    struct = f"p{page_no}"

                    # ── 표(가능하면 셀 좌표 보존) ──
                    try:
                        tables = page.extract_tables() or []
                    except Exception:  # noqa: BLE001 — 표 추출 실패는 본문 추출을 막지 않음
                        tables = []
                    for tbl in tables:
                        blk = self._table_block(tbl, document_id, order, struct, page_no)
                        if blk is not None:
                            blocks.append(blk)
                            order += 1
                            n_tables += 1
                            table_conf_sum += blk.table_content.recovery_confidence

                    # ── 본문 텍스트 → 문단 블록 ──
                    text = page.extract_text() or ""
                    for para in self._split_paragraphs(text):
                        char_count += len(para)
                        blocks.append(Block(
                            block_id=f"{document_id}-b{order}",
                            block_type=BlockType.paragraph,
                            struct_path=struct,
                            order_index=order,
                            text=para,
                            confidence=1.0,
                            page_ref=page_no,
                        ))
                        order += 1
        except Exception:  # noqa: BLE001 — 열기 실패(암호·손상) → failed
            logger.exception("pdf parse failed: %s", filename)
            return self._failed(document_id, meta)

        if not blocks:
            # 텍스트가 전혀 없는 스캔 PDF 등 → failed(추측 답변 방지)
            return self._failed(document_id, meta)

        table_rec = (table_conf_sum / n_tables) if n_tables else 1.0
        return DocumentIR(
            document_id=document_id,
            source_format=SourceFormat.pdf_text,
            extraction_path=ExtractionPath.native,
            parser_version=PARSER_VERSION,
            ingested_at=datetime.now(timezone.utc),
            source_metadata=meta,
            parse_status=ParseStatus.ok,
            blocks=blocks,
            parse_quality=ParseQuality(
                char_count=char_count,
                table_recovery_avg=table_rec,
                broken_char_ratio=0.0,
                order_confidence=1.0,
            ),
        )

    @staticmethod
    def _split_paragraphs(text: str) -> list[str]:
        # 빈 줄 기준 문단 분리. 없으면 줄 단위. 공백만인 조각은 제외.
        text = text.replace("\r\n", "\n").replace("\r", "\n")
        parts = [p.strip() for p in text.split("\n\n")]
        parts = [p for p in parts if p]
        if not parts:
            parts = [ln.strip() for ln in text.split("\n") if ln.strip()]
        return parts

    @staticmethod
    def _table_block(tbl: list, document_id: str, order: int,
                     struct: str, page_no: int) -> Block | None:
        rows = [r for r in tbl if r is not None]
        if not rows:
            return None
        n_rows = len(rows)
        n_cols = max((len(r) for r in rows), default=0)
        if n_cols == 0:
            return None
        cells: list[TableCell] = []
        for r_idx, row in enumerate(rows):
            for c_idx, val in enumerate(row):
                cells.append(TableCell(
                    row=r_idx, col=c_idx,
                    text="" if val is None else str(val).strip(),
                ))
        if not cells:
            return None
        try:
            tc = TableContent(
                n_rows=n_rows, n_cols=n_cols,
                header_rows=[0], cells=cells, recovery_confidence=0.5,
            )
            return Block(
                block_id=f"{document_id}-b{order}",
                block_type=BlockType.table,
                struct_path=struct,
                order_index=order,
                table_content=tc,
                confidence=0.5,
                page_ref=page_no,
            )
        except Exception:  # noqa: BLE001 — 표 스키마 위반 시 표는 버리고 본문만
            return None

    @staticmethod
    def _failed(document_id: str, meta: SourceMetadata) -> DocumentIR:
        return DocumentIR(
            document_id=document_id,
            source_format=SourceFormat.pdf_text,
            extraction_path=ExtractionPath.native,
            parser_version=PARSER_VERSION,
            ingested_at=datetime.now(timezone.utc),
            source_metadata=meta,
            parse_status=ParseStatus.failed,
            blocks=[],
            parse_quality=ParseQuality(
                char_count=0, table_recovery_avg=0.0,
                broken_char_ratio=0.0, order_confidence=0.0,
            ),
        )
