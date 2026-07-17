#!/usr/bin/env python3
"""포맷별 Go/No-Go 간단 게이트 — 표본 파일 경로를 인자로 받는다.

예:
  python scripts/poc_format_gate.py sample.docx sample.hwp
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from harag.config.settings import get_settings
from harag.parsing.document_parser import DocumentParser


def main(paths: list[str]) -> int:
    settings = get_settings()
    parser = DocumentParser()
    rc = 0
    for p in paths:
        path = Path(p)
        if not path.is_file():
            print(f"NO_GO missing {p}")
            rc = 1
            continue
        raw = path.read_bytes()
        ir = parser.parse(
            raw, document_id=path.stem[:32], filename=path.name)
        chars = ir.parse_quality.char_count
        tables = ir.parse_quality.table_recovery_avg
        status = ir.parse_status.value
        warn = parser.last_warning or ""
        fail = parser.last_fail_code or ""
        go = status != "failed" and bool(ir.blocks) and chars > 0
        label = "GO" if go else "NO_GO"
        if warn:
            label = "CONDITIONAL"
        print(
            f"{label} {path.name}: status={status} chars={chars} "
            f"table_avg={tables:.2f} warn={warn!r} fail={fail!r} "
            f"blocks={len(ir.blocks)} min_table={settings.parse_min_table_recovery}"
        )
        if not go:
            rc = 1
    return rc


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
