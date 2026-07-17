"""
HWP5(바이너리 .hwp) 디코더 — B1 텍스트 중심.

표 셀 정밀 복원(B3)은 라이브러리 한계로 제한적이다.
텍스트·조항 휴리스틱은 olefile PrvText(+ 가능 시 BodyText)로 추출한다.
"""
from __future__ import annotations

import io
import logging
import re
import zlib

from harag.parsing.parser import DecodeError, RawBlock

logger = logging.getLogger("harag.parsing")

_HEADING_RE = re.compile(
    r"^\s*(제\s*\d+\s*(?:조|장|절|항|호)|Article\s+\d+|Section\s+\d+)",
    re.I,
)
_PARA_SPLIT = re.compile(r"\r\n|\n|\r|\x0b")


class Hwp5Decoder:
    """HwpDecoder Protocol — source_format='hwp5'.

    tables_recovered: decode 후 표 RawBlock 수(DocumentParser 경고용).
    """

    def __init__(self) -> None:
        self.tables_recovered: int = 0
        self.text_source: str = ""

    def decode(self, raw: bytes, source_format: str) -> list[RawBlock]:
        if source_format != "hwp5":
            raise DecodeError(f"unsupported format: {source_format}")
        self.tables_recovered = 0
        self.text_source = ""

        # 1) pyhwp 시도(설치·호환 시)
        blocks = self._try_pyhwp(raw)
        if blocks:
            return blocks

        # 2) olefile PrvText / BodyText 폴백
        blocks = self._ole_text_blocks(raw)
        if not blocks:
            raise DecodeError(
                "HWP5 text extract failed — convert to HWPX if possible")
        return blocks

    def _try_pyhwp(self, raw: bytes) -> list[RawBlock] | None:
        try:
            # pyhwp는 파일 경로 위주라 임시 파일 없이 스킵하는 배포가 많음.
            # 선택 의존 — 실패하면 None.
            import olefile  # noqa: F401
        except ImportError:
            return None
        # pyhwp 풀 파싱은 환경마다 깨짐 — 안정 경로인 ole로 통일
        return None

    def _ole_text_blocks(self, raw: bytes) -> list[RawBlock]:
        try:
            import olefile
        except ImportError as e:
            raise DecodeError("olefile not installed") from e

        try:
            ole = olefile.OleFileIO(io.BytesIO(raw))
        except Exception as e:  # noqa: BLE001
            raise DecodeError(f"not a valid HWP OLE: {e}") from e

        if not ole.exists("FileHeader"):
            ole.close()
            raise DecodeError("missing HWP FileHeader")

        text = ""
        # PrvText: UTF-16LE 미리보기(가장 안정)
        if ole.exists("PrvText"):
            try:
                data = ole.openstream("PrvText").read()
                text = data.decode("utf-16-le", errors="ignore").strip()
                self.text_source = "PrvText"
            except Exception:  # noqa: BLE001
                logger.debug("PrvText read failed", exc_info=True)

        # BodyText 섹션 zlib 시도(추가 본문)
        if len(text) < 40:
            body = self._bodytext_plain(ole)
            if body:
                text = (text + "\n" + body).strip() if text else body
                self.text_source = self.text_source or "BodyText"

        ole.close()
        if not text.strip():
            return []

        blocks: list[RawBlock] = []
        for line in _PARA_SPLIT.split(text):
            line = line.strip()
            if not line:
                continue
            kind = "heading" if _HEADING_RE.match(line) else "paragraph"
            blocks.append(RawBlock(kind=kind, text=line))
        # 표는 B1에서 복원하지 않음 → tables_recovered=0
        self.tables_recovered = 0
        return blocks

    @staticmethod
    def _bodytext_plain(ole) -> str:
        """BodyText/Section* 스트림에서 zlib 해제 후 UTF-16 조각 추출(최선 노력)."""
        chunks: list[str] = []
        try:
            for entry in ole.listdir():
                path = "/".join(entry)
                if not path.upper().startswith("BODYTEXT"):
                    continue
                try:
                    data = ole.openstream(entry).read()
                except Exception:  # noqa: BLE001
                    continue
                # 일부 레코드는 zlib
                for i in range(0, min(len(data), 64)):
                    if data[i:i + 2] == b"\x78\x9c" or data[i:i + 2] == b"\x78\x01":
                        try:
                            dec = zlib.decompress(data[i:])
                            chunks.append(
                                dec.decode("utf-16-le", errors="ignore"))
                            break
                        except Exception:  # noqa: BLE001
                            continue
                # 원본이 UTF-16처럼 보이면
                if not chunks and len(data) > 4:
                    try:
                        t = data.decode("utf-16-le", errors="ignore")
                        if sum(1 for c in t if c.isalpha() or "\uac00" <= c <= "\ud7a3") > 10:
                            chunks.append(t)
                    except Exception:  # noqa: BLE001
                        pass
        except Exception:  # noqa: BLE001
            logger.debug("BodyText scan failed", exc_info=True)
        return "\n".join(c.strip() for c in chunks if c.strip())
