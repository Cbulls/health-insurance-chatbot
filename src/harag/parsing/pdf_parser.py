"""
PDF 파서 — 원본 PDF 바이트를 검증된 IR(DocumentIR)로 구조화.

설계:
  - pdfplumber로 페이지별 텍스트 + 표를 추출한다.
  - 다단: 줄 bbox의 x좌표를 클러스터링해 열 왼쪽→오른쪽, 열 안 위→아래 순으로
    읽기 순서를 교정한다.
  - 노이즈: 페이지 상·하단 반복 텍스트·페이지 번호를 header_footer(is_noise)로 마킹.
  - struct_path: 조·항 헤더 감지 시 조 단위, 없으면 페이지("p{n}") 프록시.
  - 표 영역(bbox) 안의 텍스트는 본문에서 제외(표-본문 중복 제거).
  - 실패(암호·손상·스캔 전용)는 ParseStatus.failed로 변환.
"""
from __future__ import annotations

import logging
import re
from collections import Counter
from datetime import datetime, timezone
from typing import NamedTuple

import pdfplumber

from harag.schemas.ir import (
    DocumentIR, Block, BlockType, TableCell, TableContent,
    SourceFormat, ExtractionPath, ParseStatus, SourceMetadata, ParseQuality,
)

logger = logging.getLogger("harag.parsing")

PARSER_VERSION = "pdf-mvp-0.3"

# 조·항 헤더 패턴 — 행정문서/약관의 "제n조(제목)", "제n조의m", "제n장",
# 영문 약관의 "Article n (Title)" / "Section n". 줄 시작에서만 매치.
_CLAUSE_RE = re.compile(
    r"^\s*(제\s*\d+\s*(?:조(?:의\s*\d+)?|장|절|관)"
    r"|Article\s+\d+|Section\s+\d+)",
    re.IGNORECASE,
)

# 단독 페이지 번호: "- 3 -", "3", "3/12", "Page 3"
_PAGE_NUM_RE = re.compile(
    r"^\s*(?:[-–—]\s*)?(?:page\s*)?\d+(?:\s*/\s*\d+)?(?:\s*[-–—])?\s*$",
    re.IGNORECASE,
)

_HEADER_FOOTER_BAND = 0.08   # 페이지 높이의 상·하 8%
_COLUMN_GAP_RATIO = 0.12     # 페이지 너비의 12% 이상 x-gap이면 열 경계


def _clause_of(line: str) -> str | None:
    """줄이 조·항 헤더로 시작하면 정규화된 조 라벨을 반환."""
    m = _CLAUSE_RE.match(line)
    if m is None:
        return None
    return re.sub(r"\s+", " ", m.group(1)).strip()


class _Line(NamedTuple):
    top: float
    bottom: float
    x0: float
    x1: float
    text: str


class PdfParser:
    """PDF → DocumentIR. Parser Protocol의 PDF 특화 구현."""

    def __init__(self) -> None:
        self.last_fail_code: str | None = None

    def parse(self, raw: bytes, *, document_id: str, filename: str,
              source_system: str = "user-upload", department: str = "self",
              security_level: str = "internal") -> DocumentIR:
        self.last_fail_code = None
        meta = SourceMetadata(
            source_system=source_system, department=department,
            security_level=security_level, original_path=filename,
        )

        blocks: list[Block] = []
        order = 0
        char_count = 0
        n_tables = 0
        table_conf_sum = 0.0
        current_clause = ""
        # 페이지별 상·하단 밴드 텍스트 → 2페이지 이상 반복되면 노이즈
        band_counter: Counter[str] = Counter()
        n_pages_seen = 0

        try:
            import io
            with pdfplumber.open(io.BytesIO(raw)) as pdf:
                # 1차: 밴드 후보 수집(반복 헤더/푸터 탐지)
                page_band_texts: list[list[str]] = []
                for page in pdf.pages:
                    bands = self._band_texts(page)
                    page_band_texts.append(bands)
                    for t in bands:
                        band_counter[t] += 1
                n_pages = max(1, len(pdf.pages))
                n_pages_seen = len(pdf.pages)
                noise_texts = {
                    t for t, c in band_counter.items()
                    if c >= 2 or (n_pages == 1 and _PAGE_NUM_RE.match(t))
                }

                for page_no, page in enumerate(pdf.pages, start=1):
                    items = self._page_items(page)
                    para_buf: list[str] = []
                    last_bottom: float | None = None
                    page_height = float(page.height or 792)

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

                        line = payload if isinstance(payload, str) else payload.text
                        x0 = payload.x0 if isinstance(payload, _Line) else 0.0
                        # 노이즈: 페이지 번호 또는 반복 헤더/푸터
                        if self._is_noise_line(line, top, bottom, page_height,
                                               noise_texts):
                            flush_para()
                            blocks.append(Block(
                                block_id=f"{document_id}-b{order}",
                                block_type=BlockType.header_footer,
                                struct_path=f"p{page_no}",
                                order_index=order,
                                text=line,
                                confidence=1.0,
                                is_noise=True,
                                page_ref=page_no,
                            ))
                            order += 1
                            last_bottom = bottom
                            continue

                        clause = _clause_of(line)
                        if clause is not None:
                            flush_para()
                            current_clause = clause
                        elif (last_bottom is not None
                              and top - last_bottom > (bottom - top) * 0.6):
                            flush_para()
                        para_buf.append(line)
                        last_bottom = bottom
                        _ = x0  # 열 정렬은 _page_items에서 이미 반영
                    flush_para()
        except Exception as e:  # noqa: BLE001 — 열기 실패(암호·손상) → failed
            logger.exception("pdf parse failed: %s", filename)
            msg = str(e).lower()
            ename = type(e).__name__.lower()
            if ("password" in msg or "encrypt" in msg
                    or "password" in ename):
                self.last_fail_code = "encrypted_pdf"
            else:
                self.last_fail_code = "parse_failed"
            return self._failed(document_id, meta)

        if not blocks or all(b.is_noise for b in blocks):
            # 페이지는 있는데 텍스트가 없으면 스캔본으로 본다
            if n_pages_seen > 0:
                self.last_fail_code = "scan_pdf_no_ocr"
            else:
                self.last_fail_code = "parse_failed"
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
    def _band_texts(page) -> list[str]:
        """페이지 상·하 밴드의 짧은 텍스트 줄(헤더/푸터 후보)."""
        h = float(page.height or 792)
        top_cut = h * _HEADER_FOOTER_BAND
        bot_cut = h * (1.0 - _HEADER_FOOTER_BAND)
        out: list[str] = []
        try:
            for ln in page.extract_text_lines() or []:
                text = (ln.get("text") or "").strip()
                if not text or len(text) > 80:
                    continue
                top = float(ln.get("top", 0))
                bot = float(ln.get("bottom", top))
                if bot <= top_cut or top >= bot_cut:
                    out.append(text)
        except Exception:  # noqa: BLE001
            pass
        return out

    @staticmethod
    def _is_noise_line(text: str, top: float, bottom: float,
                       page_height: float, noise_texts: set[str]) -> bool:
        if _PAGE_NUM_RE.match(text):
            return True
        if text in noise_texts:
            return True
        # 밴드 안 단독 짧은 줄이면서 반복 후보였던 경우
        top_cut = page_height * _HEADER_FOOTER_BAND
        bot_cut = page_height * (1.0 - _HEADER_FOOTER_BAND)
        in_band = bottom <= top_cut or top >= bot_cut
        if in_band and len(text) <= 40 and (
                _PAGE_NUM_RE.match(text) or text in noise_texts):
            return True
        return False

    @classmethod
    def _page_items(cls, page) -> list[tuple[float, float, str, object]]:
        """페이지를 (top, bottom, kind, payload) 목록으로 — 다단 교정 순.

        kind="line"  payload=_Line 또는 str
        kind="table" payload=추출된 표(list[list])
        """
        try:
            found = page.find_tables() or []
        except Exception:  # noqa: BLE001
            found = []

        page_width = float(page.width or 612)
        lines: list[_Line] = []
        tables: list[tuple[float, float, object]] = []

        try:
            filtered = page
            for t in found:
                filtered = filtered.outside_bbox(t.bbox)
            for ln in filtered.extract_text_lines() or []:
                text = (ln.get("text") or "").strip()
                if not text:
                    continue
                lines.append(_Line(
                    top=float(ln["top"]),
                    bottom=float(ln["bottom"]),
                    x0=float(ln.get("x0", 0)),
                    x1=float(ln.get("x1", ln.get("x0", 0))),
                    text=text,
                ))
            for t in found:
                tables.append((float(t.bbox[1]), float(t.bbox[3]), t.extract()))
        except Exception:  # noqa: BLE001 — 평문 폴백
            lines = []
            text = page.extract_text() or ""
            y = 0.0
            for para in cls._split_paragraphs(text):
                for line in para.split("\n"):
                    if line.strip():
                        lines.append(_Line(y, y + 1.0, 0.0, page_width,
                                           line.strip()))
                        y += 2.0
                y += 2.0
            for t in found:
                try:
                    tables.append((float(t.bbox[1]), float(t.bbox[3]),
                                   t.extract()))
                except Exception:  # noqa: BLE001
                    continue

        ordered_lines = cls._order_lines_multicolumn(lines, page_width)
        # 표와 본문을 읽기 순서로 합치기: 표 top이 현재 줄 이상이면 앞에 삽입
        result: list[tuple[float, float, str, object]] = []
        table_items = sorted(
            [(t, b, "table", p) for t, b, p in tables], key=lambda x: x[0])
        ti = 0
        for ln in ordered_lines:
            while ti < len(table_items) and table_items[ti][0] <= ln.top:
                result.append(table_items[ti])
                ti += 1
            result.append((ln.top, ln.bottom, "line", ln))
        while ti < len(table_items):
            result.append(table_items[ti])
            ti += 1
        return result

    @staticmethod
    def _order_lines_multicolumn(lines: list[_Line],
                                 page_width: float) -> list[_Line]:
        """x-gap으로 열을 나누고 열 왼쪽→오른쪽, 열 안 위→아래 순으로 정렬.

        열이 1개면 y(top) 정렬만 한다.
        """
        if not lines:
            return []
        # 왼쪽 가장자리(x0) 기준 정렬 후 큰 gap으로 열 분리
        by_x = sorted(lines, key=lambda ln: ln.x0)
        gap_thresh = page_width * _COLUMN_GAP_RATIO
        columns: list[list[_Line]] = [[by_x[0]]]
        for prev, cur in zip(by_x, by_x[1:]):
            # 같은 줄대(세로 겹침)에서만 gap을 열 경계로 본다
            if cur.x0 - prev.x1 >= gap_thresh:
                columns.append([cur])
            else:
                # 열 배정: 기존 열 중심과 가장 가까운 열에 넣거나 새 열
                col_centers = [
                    sum(ln.x0 for ln in col) / len(col) for col in columns
                ]
                best = min(range(len(columns)),
                           key=lambda i: abs(col_centers[i] - cur.x0))
                # 최근접 열과의 거리도 gap 이상이면 새 열
                if abs(col_centers[best] - cur.x0) >= gap_thresh:
                    columns.append([cur])
                else:
                    columns[best].append(cur)

        if len(columns) <= 1:
            return sorted(lines, key=lambda ln: (ln.top, ln.x0))

        # 열을 왼쪽→오른쪽(중심 x)으로 정렬한 뒤 각 열 안 top 정렬
        columns.sort(key=lambda col: sum(ln.x0 for ln in col) / len(col))
        ordered: list[_Line] = []
        for col in columns:
            ordered.extend(sorted(col, key=lambda ln: (ln.top, ln.x0)))
        return ordered

    @staticmethod
    def _split_paragraphs(text: str) -> list[str]:
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
        except Exception:  # noqa: BLE001
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
