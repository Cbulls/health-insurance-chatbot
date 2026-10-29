"""Jina OCR(v1) — PDF 페이지 이미지를 마크다운/텍스트로 전사해 DocumentIR 생성."""
from __future__ import annotations

import base64
import io
import logging
import time
from datetime import datetime, timezone

import httpx

from harag.parsing.pdf_parser import PdfParser
from harag.schemas.ir import (
    Block, BlockType, DocumentIR, ExtractionPath, ParseQuality, ParseStatus,
    SourceFormat, SourceMetadata,
)

logger = logging.getLogger("harag.parsing.jina_ocr")

PARSER_VERSION = "jina-ocr-0.1"


class JinaOcrPdfParser:
    """페이지 렌더 → jina-ocr-v1 chat completions → DocumentIR.

    페이지 실패 시 해당 페이지만 pdfplumber 텍스트로 폴백한다.
    """

    def __init__(
        self,
        *,
        api_base: str,
        api_key: str,
        model: str = "jina-ocr-v1",
        rpm: float = 80.0,
        scale: float = 2.0,
        max_retries: int = 6,
        timeout: float = 120.0,
        sleep=time.sleep,
    ):
        self._base = (api_base or "https://api.jina.ai/v1").rstrip("/")
        self._key = api_key
        self._model = model or "jina-ocr-v1"
        self._min_interval = 60.0 / max(1.0, float(rpm))
        self._scale = max(1.0, float(scale))
        self._max_retries = max(0, int(max_retries))
        self._sleep = sleep
        self._client = httpx.Client(
            base_url=self._base,
            headers={"Authorization": f"Bearer {self._key}"},
            timeout=timeout,
        )
        self._fallback = PdfParser()
        self.last_fail_code: str | None = None
        self._last_call = 0.0

    def parse(self, raw: bytes, *, document_id: str, filename: str,
              source_system: str = "user-upload", department: str = "self",
              security_level: str = "internal") -> DocumentIR:
        self.last_fail_code = None
        meta = SourceMetadata(
            source_system=source_system, department=department,
            security_level=security_level, original_path=filename,
        )
        try:
            import pymupdf
        except ImportError:
            logger.warning("pymupdf missing — falling back to pdfplumber")
            ir = self._fallback.parse(
                raw, document_id=document_id, filename=filename,
                source_system=source_system, department=department,
                security_level=security_level)
            self.last_fail_code = self._fallback.last_fail_code
            return ir

        try:
            doc = pymupdf.open(stream=raw, filetype="pdf")
        except Exception:  # noqa: BLE001
            logger.exception("jina ocr open failed %s", filename)
            self.last_fail_code = "parse_failed"
            return self._failed(document_id, meta)

        page_texts: list[tuple[int, str, str]] = []  # page, text, source
        force_plumber = False
        try:
            for i in range(doc.page_count):
                page_no = i + 1
                try:
                    if force_plumber:
                        raise RuntimeError("jina_ocr_disabled")
                    png = self._render_png(doc.load_page(i), pymupdf)
                    text = self._ocr_page(png)
                    src = "jina_ocr"
                    if not (text or "").strip():
                        text = self._plumber_page(raw, i) or ""
                        src = "pdfplumber_fallback"
                except Exception as exc:  # noqa: BLE001
                    msg = str(exc).lower()
                    if "403" in msg or "insufficient" in msg or "balance" in msg:
                        force_plumber = True
                        logger.warning(
                            "jina ocr balance/403 — plumber for remaining pages "
                            "of %s (from p%d)", filename, page_no)
                    else:
                        logger.exception(
                            "jina ocr page failed %s p%d — plumber fallback",
                            filename, page_no)
                    text = self._plumber_page(raw, i) or ""
                    src = "pdfplumber_fallback"
                if (text or "").strip():
                    page_texts.append((page_no, text.strip(), src))
        finally:
            doc.close()

        if not page_texts:
            self.last_fail_code = "scan_pdf_no_ocr"
            return self._failed(document_id, meta)

        blocks: list[Block] = []
        char_count = 0
        for page_no, text, _src in page_texts:
            # 페이지를 단락 단위로 나눔(빈 줄 기준)
            paras = [p.strip() for p in text.split("\n\n") if p.strip()]
            if not paras:
                paras = [text]
            for para in paras:
                char_count += len(para)
                blocks.append(Block(
                    block_id=f"{document_id}-b{len(blocks)}",
                    block_type=BlockType.paragraph,
                    struct_path=f"p{page_no}",
                    order_index=len(blocks),
                    text=para,
                    confidence=0.95,
                    page_ref=page_no,
                ))

        used_ocr = any(s == "jina_ocr" for _, _, s in page_texts)
        return DocumentIR(
            document_id=document_id,
            source_format=SourceFormat.pdf_scan if used_ocr else SourceFormat.pdf_text,
            extraction_path=ExtractionPath.multimodal if used_ocr else ExtractionPath.native,
            parser_version=PARSER_VERSION,
            ingested_at=datetime.now(timezone.utc),
            parse_status=ParseStatus.ok,
            source_metadata=meta,
            blocks=blocks,
            parse_quality=ParseQuality(
                char_count=char_count,
                table_recovery_avg=0.5,
                broken_char_ratio=0.0,
                order_confidence=0.9,
            ),
        )

    def _render_png(self, page, pymupdf_mod=None) -> bytes:
        mod = pymupdf_mod
        if mod is None:
            import pymupdf as mod
        mat = mod.Matrix(self._scale, self._scale)
        pix = page.get_pixmap(matrix=mat, alpha=False)
        return pix.tobytes("png")

    def _pace(self) -> None:
        now = time.monotonic()
        wait = self._min_interval - (now - self._last_call)
        if wait > 0:
            self._sleep(wait)
        self._last_call = time.monotonic()

    def _ocr_page(self, png: bytes) -> str:
        b64 = base64.b64encode(png).decode("ascii")
        data_url = f"data:image/png;base64,{b64}"
        body = {
            "model": self._model,
            "messages": [{
                "role": "user",
                "content": [
                    {
                        "type": "text",
                        "text": (
                            "Transcribe this document page to clean Markdown. "
                            "Preserve headings, lists and tables. "
                            "Reply with the page content only."
                        ),
                    },
                    {"type": "image_url", "image_url": {"url": data_url}},
                ],
            }],
            "temperature": 0,
        }
        last_err: Exception | None = None
        for attempt in range(self._max_retries + 1):
            self._pace()
            resp = self._client.post("/chat/completions", json=body)
            if resp.status_code == 200:
                data = resp.json()
                choices = data.get("choices") or []
                if not choices:
                    return ""
                msg = choices[0].get("message") or {}
                content = msg.get("content") or ""
                if isinstance(content, list):
                    parts = []
                    for part in content:
                        if isinstance(part, dict) and part.get("type") == "text":
                            parts.append(part.get("text") or "")
                        elif isinstance(part, str):
                            parts.append(part)
                    return "\n".join(parts).strip()
                return str(content).strip()
            if resp.status_code == 403:
                detail = ""
                try:
                    detail = str(resp.json())
                except Exception:  # noqa: BLE001
                    detail = resp.text[:200]
                raise httpx.HTTPStatusError(
                    f"403 jina ocr insufficient balance: {detail}",
                    request=resp.request, response=resp)
            if resp.status_code == 429 or resp.status_code >= 500:
                last_err = httpx.HTTPStatusError(
                    f"{resp.status_code} jina ocr",
                    request=resp.request, response=resp)
                wait = 65.0 if resp.status_code == 429 else min(
                    1.5 * (2 ** attempt), 16.0)
                logger.warning(
                    "jina ocr %s, retry %d/%d in %.1fs",
                    resp.status_code, attempt + 1, self._max_retries, wait)
                if attempt < self._max_retries:
                    self._sleep(wait)
                    continue
                raise last_err
            resp.raise_for_status()
        raise RuntimeError("jina ocr exhausted") from last_err

    def _plumber_page(self, raw: bytes, page_index: int) -> str:
        try:
            import pdfplumber
            with pdfplumber.open(io.BytesIO(raw)) as pdf:
                if page_index >= len(pdf.pages):
                    return ""
                return (pdf.pages[page_index].extract_text() or "").strip()
        except Exception:  # noqa: BLE001
            return ""

    def _failed(self, document_id: str, meta: SourceMetadata) -> DocumentIR:
        return DocumentIR(
            document_id=document_id,
            source_format=SourceFormat.pdf_scan,
            extraction_path=ExtractionPath.multimodal,
            parser_version=PARSER_VERSION,
            ingested_at=datetime.now(timezone.utc),
            parse_status=ParseStatus.failed,
            source_metadata=meta,
            blocks=[],
            parse_quality=ParseQuality(
                char_count=0, table_recovery_avg=0.0,
                broken_char_ratio=0.0, order_confidence=0.0),
        )
