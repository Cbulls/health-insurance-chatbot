"""구형 Word(.doc) → DOCX 로컬 변환 (LibreOffice headless).

외부 SaaS로 문서를 보내지 않는다. 동시 변환은 세마포어로 1개로 제한.
"""
from __future__ import annotations

import logging
import os
import shutil
import subprocess
import tempfile
import threading
from pathlib import Path

logger = logging.getLogger("harag.parsing")


class ConvertError(Exception):
    """변환 실패."""


class ConvertService:
    def __init__(self, *,
                 libreoffice_bin: str = "soffice",
                 timeout_sec: int = 120,
                 enabled: bool = True):
        self._bin = libreoffice_bin or "soffice"
        self._timeout = max(10, timeout_sec)
        self._enabled = enabled
        self._lock = threading.Semaphore(1)

    def available(self) -> bool:
        if not self._enabled:
            return False
        return shutil.which(self._bin) is not None

    def doc_to_docx(self, raw: bytes) -> bytes:
        if not self._enabled:
            raise ConvertError("DOC convert disabled (ENABLE_DOC_CONVERT=false)")
        if not self.available():
            raise ConvertError(
                f"LibreOffice not found ({self._bin}) — install or set LIBREOFFICE_BIN")

        with self._lock:
            return self._convert_locked(raw)

    def _convert_locked(self, raw: bytes) -> bytes:
        tmp = tempfile.mkdtemp(prefix="harag_doc_")
        try:
            src = Path(tmp) / "input.doc"
            src.write_bytes(raw)
            out_dir = Path(tmp) / "out"
            out_dir.mkdir()
            # 매크로 비활성·헤드리스
            cmd = [
                self._bin,
                "--headless",
                "--norestore",
                "--nolockcheck",
                "--nodefault",
                "--nofirststartwizard",
                f"-env:UserInstallation=file://{tmp}/lo_profile",
                "--convert-to", "docx",
                "--outdir", str(out_dir),
                str(src),
            ]
            try:
                proc = subprocess.run(
                    cmd,
                    capture_output=True,
                    timeout=self._timeout,
                    check=False,
                    env={**os.environ, "HOME": tmp},
                )
            except subprocess.TimeoutExpired as e:
                raise ConvertError("DOC convert timed out") from e
            if proc.returncode != 0:
                err = (proc.stderr or proc.stdout or b"").decode(
                    "utf-8", errors="replace")[:300]
                raise ConvertError(f"LibreOffice failed: {err}")
            produced = list(out_dir.glob("*.docx"))
            if not produced:
                raise ConvertError("DOC convert produced no DOCX")
            return produced[0].read_bytes()
        finally:
            shutil.rmtree(tmp, ignore_errors=True)
