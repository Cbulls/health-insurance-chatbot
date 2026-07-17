"""
OCR 도입 go/no-go — 실문서 스캔 비율 표본으로 결정.

스캔 비율 < OCR_SCAN_RATIO_THRESHOLD(기본 0.15) → 제품 거부 유지 + 변환 SOP
≥ 임계 → OCR 어댑터 도입 권고
"""
from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class OcrDecision:
    introduce_ocr: bool
    scan_ratio: float
    threshold: float
    reason: str


def decide_ocr(scan_ratio: float, threshold: float = 0.15) -> OcrDecision:
    ratio = max(0.0, min(1.0, float(scan_ratio)))
    thr = float(threshold)
    if ratio >= thr:
        return OcrDecision(
            introduce_ocr=True,
            scan_ratio=ratio,
            threshold=thr,
            reason=("scan_ratio>=threshold — introduce OCR adapter "
                    "and add scan strata to goldset before merge"),
        )
    return OcrDecision(
        introduce_ocr=False,
        scan_ratio=ratio,
        threshold=thr,
        reason=("scan_ratio<threshold — keep scan_pdf_no_ocr product ban; "
                "require text PDF / conversion SOP"),
    )
