"""DOCX 디코더 — python-docx → RawBlock (StructuringParser 입력)."""
from __future__ import annotations

import io
import logging
import re

from harag.parsing.parser import DecodeError, RawBlock

logger = logging.getLogger("harag.parsing")

_HEADING_RE = re.compile(
    r"^\s*(제\s*\d+\s*(?:조|장|절|항|호)|Article\s+\d+|Section\s+\d+)",
    re.I,
)


class DocxDecoder:
    """HwpDecoder Protocol 호환 — source_format='docx'."""

    def decode(self, raw: bytes, source_format: str) -> list[RawBlock]:
        if source_format != "docx":
            raise DecodeError(f"unsupported format: {source_format}")
        try:
            from docx import Document
            from docx.oxml.ns import qn
        except ImportError as e:
            raise DecodeError("python-docx not installed") from e

        try:
            doc = Document(io.BytesIO(raw))
        except Exception as e:  # noqa: BLE001
            raise DecodeError(f"invalid DOCX: {e}") from e

        blocks: list[RawBlock] = []
        # 본문을 body 순서대로 — 단락과 표를 문서 순서 유지
        body = doc.element.body
        for child in body:
            tag = child.tag
            if tag == qn("w:p"):
                from docx.text.paragraph import Paragraph
                p = Paragraph(child, doc)
                text = (p.text or "").strip()
                if not text:
                    continue
                style = (p.style.name if p.style is not None else "") or ""
                if style.lower().startswith("heading") or _HEADING_RE.match(text):
                    blocks.append(RawBlock(kind="heading", text=text))
                else:
                    blocks.append(RawBlock(kind="paragraph", text=text))
            elif tag == qn("w:tbl"):
                from docx.table import Table
                tbl = Table(child, doc)
                cells: list[dict] = []
                n_cols = 0
                for ri, row in enumerate(tbl.rows):
                    for ci, cell in enumerate(row.cells):
                        cells.append({
                            "row": ri, "col": ci,
                            "text": (cell.text or "").strip(),
                            "row_span": 1, "col_span": 1,
                        })
                    n_cols = max(n_cols, len(row.cells))
                if cells:
                    blocks.append(RawBlock(
                        kind="table", cells=cells,
                        n_rows=len(tbl.rows), n_cols=n_cols, text=""))

        # 헤더/푸터(노이즈 후보)
        try:
            for section in doc.sections:
                for part_name, part in (
                    ("header", section.header),
                    ("footer", section.footer),
                ):
                    if part is None:
                        continue
                    for p in part.paragraphs:
                        t = (p.text or "").strip()
                        if t:
                            blocks.append(RawBlock(kind="header_footer", text=t))
        except Exception:  # noqa: BLE001
            logger.debug("docx header/footer skip", exc_info=True)

        if not blocks:
            raise DecodeError("DOCX yielded no text blocks")
        return blocks
