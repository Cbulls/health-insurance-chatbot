"""
PDF 파서 — 원본 PDF 바이트를 검증된 IR(DocumentIR)로 구조화(MVP).

설계:
  - HWP 경로(StructuringParser + HwpDecoder)와 별개로, PDF 전용 파서를 둔다.
  - pdfplumber로 페이지별 텍스트 + 표를 추출한다.
  - 텍스트 문단 → paragraph 블록, 표 → table 블록(셀 좌표 보존).
  - struct_path: 조·항 헤더("제n조", "Article n")를 감지하면 조 단위,
    없으면 페이지("p{n}") 프록시 → 구조 단위 청킹·인용.
  - 표 영역(bbox) 안의 텍스트는 본문에서 제외(표-본문 중복 제거).
  - 본문과 표를 세로 좌표 순으로 병합해 읽기 순서를 보존 → 표가 자기 조항의
    struct_path를 물려받는다.
  - 실패(암호·손상·스캔 전용)는 예외가 아니라 ParseStatus.failed로 변환.

Phase 2: 항·호 하위 계층, 표 recovery_confidence 실측, OCR(스캔 PDF).
"""
from __future__ import annotations

import logging
import re
from datetime import datetime, timezone

import pdfplumber

from harag.schemas.ir import (
    DocumentIR, Block, BlockType, TableCell, TableContent,
    SourceFormat, ExtractionPath, ParseStatus, SourceMetadata, ParseQuality,
)

logger = logging.getLogger("harag.parsing")

PARSER_VERSION = "pdf-mvp-0.2"

# 조·항 헤더 패턴 — 행정문서/약관의 "제n조(제목)", "제n조의m", "제n장",
# 영문 약관의 "Article n (Title)" / "Section n". 줄 시작에서만 매치.
_CLAUSE_RE = re.compile(
    r"^\s*(제\s*\d+\s*(?:조(?:의\s*\d+)?|장|절|관)"
    r"|Article\s+\d+|Section\s+\d+)",
    re.IGNORECASE,
)


def _clause_of(line: str) -> str | None:
    """줄이 조·항 헤더로 시작하면 정규화된 조 라벨을 반환."""
    m = _CLAUSE_RE.match(line)
    if m is None:
        return None
    return re.sub(r"\s+", " ", m.group(1)).strip()


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
        current_clause = ""  # 페이지를 넘어 이어지는 조항 컨텍스트

        try:
            import io
            with pdfplumber.open(io.BytesIO(raw)) as pdf:
                for page_no, page in enumerate(pdf.pages, start=1):
                    items = self._page_items(page)

                    para_buf: list[str] = []
                    last_bottom: float | None = None

                    def flush_para():
                        nonlocal order, char_count
                        text = "\n".join(para_buf).strip()
                        para_buf.clear()
                        if not text:
                            return
                        char_count += len(text)
                        blocks.append(Block(
                            block_id=f"{document_id}-b{order}",
                            block_type=BlockType.paragraph,
                            struct_path=current_clause or f"p{page_no}",
                            order_index=order,
                            text=text,
                            confidence=1.0,
                            page_ref=page_no,
                        ))
                        order += 1

                    for top, bottom, kind, payload in items:
                        if kind == "table":
                            flush_para()
                            blk = self._table_block(
                                payload, document_id, order,
                                current_clause or f"p{page_no}", page_no)
                            if blk is not None:
                                blocks.append(blk)
                                order += 1
                                n_tables += 1
                                table_conf_sum += \
                                    blk.table_content.recovery_confidence
                            last_bottom = bottom
                            continue

                        line = payload
                        clause = _clause_of(line)
                        # 새 조항 헤더 → 문단 경계 + struct_path 갱신
                        if clause is not None:
                            flush_para()
                            current_clause = clause
                        # 세로 간격이 줄 높이보다 크면 빈 줄(문단 경계)로 간주
                        elif (last_bottom is not None
                              and top - last_bottom > (bottom - top) * 0.6):
                            flush_para()
                        para_buf.append(line)
                        last_bottom = bottom
                    flush_para()
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
    def _page_items(page) -> list[tuple[float, float, str, object]]:
        """페이지를 (top, bottom, kind, payload) 목록으로 — 세로 좌표 순.

        kind="line"  payload=텍스트 줄(표 영역 밖만 — 중복 제거)
        kind="table" payload=추출된 표(list[list])
        표 추출·좌표 조회가 실패하면 페이지 전체 텍스트로 폴백한다.
        """
        try:
            found = page.find_tables() or []
        except Exception:  # noqa: BLE001 — 표 추출 실패는 본문 추출을 막지 않음
            found = []

        items: list[tuple[float, float, str, object]] = []
        try:
            filtered = page
            for t in found:
                filtered = filtered.outside_bbox(t.bbox)
            for ln in filtered.extract_text_lines() or []:
                text = (ln.get("text") or "").strip()
                if text:
                    items.append((float(ln["top"]), float(ln["bottom"]),
                                  "line", text))
            for t in found:
                items.append((float(t.bbox[1]), float(t.bbox[3]),
                              "table", t.extract()))
        except Exception:  # noqa: BLE001 — 좌표 기반 추출 실패 → 평문 폴백
            items = []
            text = page.extract_text() or ""
            y = 0.0
            for para in PdfParser._split_paragraphs(text):
                for line in para.split("\n"):
                    if line.strip():
                        items.append((y, y + 1.0, "line", line.strip()))
                        y += 2.0
                y += 2.0  # 문단 사이 간격 > 줄 높이 → 문단 경계 유지
            for t in found:
                try:
                    items.append((float(t.bbox[1]), float(t.bbox[3]),
                                  "table", t.extract()))
                except Exception:  # noqa: BLE001
                    continue

        items.sort(key=lambda it: it[0])
        return items

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
        rows = [r for r in (tbl or []) if r is not None]
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
                    is_header=(r_idx == 0),
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
