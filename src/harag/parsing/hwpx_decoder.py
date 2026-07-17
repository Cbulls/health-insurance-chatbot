"""
HWPX 디코더 — ZIP+XML을 RawBlock 목록으로 (stdlib만 사용).

바이너리 .hwp는 지원하지 않는다. 기관에는 HWPX 변환을 요청한다.
"""
from __future__ import annotations

import io
import logging
import re
import zipfile
import xml.etree.ElementTree as ET

from harag.parsing.parser import DecodeError, RawBlock

logger = logging.getLogger("harag.parsing")

_HEADING_RE = re.compile(
    r"^\s*(제\s*\d+\s*(?:조|장|절|항|호)|Article\s+\d+|Section\s+\d+)",
    re.I,
)


def _local(tag: str) -> str:
    return tag.rsplit("}", 1)[-1] if tag else ""


def _text_of(el: ET.Element) -> str:
    parts: list[str] = []
    if el.text:
        parts.append(el.text)
    for child in el:
        parts.append(_text_of(child))
        if child.tail:
            parts.append(child.tail)
    return "".join(parts)


class HwpxDecoder:
    """HwpDecoder Protocol 구현 — Contents/section*.xml 중심."""

    def decode(self, raw: bytes, source_format: str) -> list[RawBlock]:
        if source_format == "hwp5":
            raise DecodeError("binary HWP not supported — use HWPX")
        if source_format not in ("hwpx",):
            raise DecodeError(f"unsupported format: {source_format}")
        try:
            zf = zipfile.ZipFile(io.BytesIO(raw))
        except zipfile.BadZipFile as e:
            raise DecodeError("not a valid HWPX (zip)") from e

        section_names = sorted(
            n for n in zf.namelist()
            if "section" in n.lower() and n.lower().endswith(".xml")
        )
        if not section_names:
            section_names = sorted(
                n for n in zf.namelist()
                if n.lower().endswith(".xml") and "preview" not in n.lower()
            )
        if not section_names:
            raise DecodeError("HWPX has no section XML")

        blocks: list[RawBlock] = []
        for name in section_names:
            try:
                data = zf.read(name)
            except KeyError:
                continue
            try:
                root = ET.fromstring(data)
            except ET.ParseError:
                logger.warning("hwpx xml parse failed: %s", name)
                continue
            blocks.extend(self._walk_section(root))

        if not blocks:
            raise DecodeError("HWPX yielded no text blocks")
        return blocks

    def _walk_section(self, root: ET.Element) -> list[RawBlock]:
        out: list[RawBlock] = []

        def dfs(node: ET.Element) -> None:
            tag = _local(node.tag)
            if tag in ("tbl", "table"):
                cells, n_rows, n_cols = self._table_cells(node)
                if cells:
                    out.append(RawBlock(
                        kind="table", cells=cells,
                        n_rows=n_rows, n_cols=n_cols, text=""))
                return
            if tag == "p":
                text = _text_of(node).strip()
                if text:
                    kind = ("heading" if _HEADING_RE.match(text)
                            else "paragraph")
                    out.append(RawBlock(kind=kind, text=text))
                return
            for child in list(node):
                dfs(child)

        dfs(root)
        return out

    def _table_cells(self, tbl: ET.Element) -> tuple[list[dict], int, int]:
        rows = [el for el in tbl.iter() if _local(el.tag) == "tr"]
        # 중첩 표 tr 포함될 수 있음 — 문서 MVP는 평탄 수집
        cells: list[dict] = []
        n_cols = 0
        for ri, row in enumerate(rows):
            ci = 0
            for cell in row:
                if _local(cell.tag) not in ("tc", "td", "th"):
                    continue
                text = _text_of(cell).strip()
                cells.append({
                    "row": ri, "col": ci, "text": text,
                    "row_span": 1, "col_span": 1,
                })
                ci += 1
            n_cols = max(n_cols, ci)
        return cells, len(rows), n_cols
