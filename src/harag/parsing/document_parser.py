"""확장자별 파서 레지스트리 — PDF / HWPX / DOCX / HWP5 / DOC(변환)."""
from __future__ import annotations

import logging
from typing import Callable

from harag.config.settings import get_settings
from harag.parsing.convert_service import ConvertError, ConvertService
from harag.parsing.docx_decoder import DocxDecoder
from harag.parsing.hwp5_decoder import Hwp5Decoder
from harag.parsing.hwpx_decoder import HwpxDecoder
from harag.parsing.parser import StructuringParser
from harag.parsing.pdf_parser import PdfParser
from harag.schemas.ir import (
    DocumentIR, ExtractionPath, ParseQuality, ParseStatus, SourceFormat,
)

logger = logging.getLogger("harag.parsing")

# suffix → SourceFormat / handler key
_STRUCT_FORMATS = {
    ".hwpx": "hwpx",
    ".docx": "docx",
    ".hwp": "hwp5",
}


class DocumentParser:
    """suffix → 핸들러 맵. last_fail_code / last_warning 계약 유지."""

    def __init__(self, convert: ConvertService | None = None):
        self._pdf = PdfParser()
        self._hwpx = StructuringParser(HwpxDecoder())
        self._docx_dec = DocxDecoder()
        self._docx = StructuringParser(self._docx_dec)
        self._hwp5_dec = Hwp5Decoder()
        self._hwp5 = StructuringParser(self._hwp5_dec)
        self._convert = convert
        self.last_fail_code: str | None = None
        self.last_warning: str | None = None

        self._handlers: dict[str, Callable[..., DocumentIR]] = {
            ".pdf": self._parse_pdf,
            ".hwpx": self._parse_hwpx,
            ".docx": self._parse_docx,
            ".hwp": self._parse_hwp5,
            ".doc": self._parse_doc_via_convert,
        }

    def parse(self, raw: bytes, *, document_id: str, filename: str,
              source_system: str = "user-upload", department: str = "self",
              security_level: str = "internal") -> DocumentIR:
        self.last_fail_code = None
        self.last_warning = None
        name = (filename or "").lower()
        suffix = ""
        for s in (".hwpx", ".docx", ".hwp", ".doc", ".pdf"):
            if name.endswith(s):
                suffix = s
                break
        handler = self._handlers.get(suffix, self._parse_pdf)
        return handler(
            raw, document_id=document_id, filename=filename,
            source_system=source_system, department=department,
            security_level=security_level,
        )

    def _kw(self, document_id, filename, source_system, department, security_level):
        return dict(
            document_id=document_id,
            source_system=source_system,
            department=department,
            security_level=security_level,
            original_path=filename,
        )

    def _parse_pdf(self, raw, *, document_id, filename, source_system,
                   department, security_level) -> DocumentIR:
        ir = self._pdf.parse(
            raw, document_id=document_id, filename=filename,
            source_system=source_system, department=department,
            security_level=security_level,
        )
        self.last_fail_code = getattr(self._pdf, "last_fail_code", None)
        return ir

    def _parse_hwpx(self, raw, *, document_id, filename, source_system,
                    department, security_level) -> DocumentIR:
        ir = self._hwpx.parse(
            raw, "hwpx", **self._kw(
                document_id, filename, source_system, department, security_level))
        if ir.parse_status.value == "failed" or not ir.blocks:
            self.last_fail_code = "hwpx_parse_failed"
        return ir

    def _parse_docx(self, raw, *, document_id, filename, source_system,
                    department, security_level,
                    extraction_path: ExtractionPath = ExtractionPath.native,
                    ) -> DocumentIR:
        ir = self._docx.parse(
            raw, "docx", **self._kw(
                document_id, filename, source_system, department,
                security_level))
        if extraction_path != ExtractionPath.native and ir.blocks:
            ir = ir.model_copy(update={"extraction_path": extraction_path})
        if ir.parse_status.value == "failed" or not ir.blocks:
            self.last_fail_code = "docx_parse_failed"
        return ir

    def _parse_hwp5(self, raw, *, document_id, filename, source_system,
                    department, security_level) -> DocumentIR:
        settings = get_settings()
        ir = self._hwp5.parse(
            raw, "hwp5", **self._kw(
                document_id, filename, source_system, department, security_level))
        if ir.parse_status.value == "failed" or not ir.blocks:
            self.last_fail_code = "hwp5_parse_failed"
            return ir

        # B3: 표 미복원 → partial + 경고(검색은 가능)
        min_rec = settings.parse_min_table_recovery
        n_tables = self._hwp5_dec.tables_recovered
        table_avg = ir.parse_quality.table_recovery_avg
        if n_tables == 0:
            table_avg = 0.0
            ir = ir.model_copy(update={
                "parse_status": ParseStatus.partial,
                "parse_quality": ParseQuality(
                    char_count=ir.parse_quality.char_count,
                    table_recovery_avg=table_avg,
                    broken_char_ratio=ir.parse_quality.broken_char_ratio,
                    order_confidence=ir.parse_quality.order_confidence,
                ),
            })
        if table_avg < min_rec:
            self.last_warning = "hwp5_table_limited"
        return ir

    def _parse_doc_via_convert(self, raw, *, document_id, filename,
                               source_system, department,
                               security_level) -> DocumentIR:
        settings = get_settings()
        conv = self._convert or ConvertService(
            libreoffice_bin=settings.libreoffice_bin,
            timeout_sec=settings.doc_convert_timeout_sec,
            enabled=settings.enable_doc_convert,
        )
        try:
            docx_bytes = conv.doc_to_docx(raw)
        except ConvertError as e:
            logger.warning("doc convert failed: %s", e)
            self.last_fail_code = "doc_convert_failed"
            # 빈 failed IR
            from datetime import datetime, timezone
            from harag.schemas.ir import SourceMetadata
            return DocumentIR(
                document_id=document_id,
                source_format=SourceFormat.docx,
                extraction_path=ExtractionPath.pdf_via,
                parser_version="doc-convert-0.1",
                ingested_at=datetime.now(timezone.utc),
                parse_status=ParseStatus.failed,
                source_metadata=SourceMetadata(
                    source_system=source_system, department=department,
                    security_level=security_level, original_path=filename,
                ),
                blocks=[],
                parse_quality=ParseQuality(
                    char_count=0, table_recovery_avg=0.0,
                    broken_char_ratio=0.0, order_confidence=0.0),
            )
        return self._parse_docx(
            docx_bytes, document_id=document_id, filename=filename,
            source_system=source_system, department=department,
            security_level=security_level,
            extraction_path=ExtractionPath.pdf_via,
        )
