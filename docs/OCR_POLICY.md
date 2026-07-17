# OCR 도입 정책 (go / no-go)

## 현재 제품

스캔(이미지) PDF는 `scan_pdf_no_ocr`로 **제품상 거부**한다. 엔진은 없다.

## 결정 규칙

표본 문서에서 스캔 비율을 측정한 뒤:

| 스캔 비율 | 결정 |
|-----------|------|
| &lt; `OCR_SCAN_RATIO_THRESHOLD` (기본 0.15) | OCR 미도입. 텍스트 PDF/변환 SOP 유지 |
| ≥ 임계 | OCR 어댑터 1개 도입 + 골드셋 scan strata 추가 후에만 merge |

코드: `harag.parsing.ocr_policy.decide_ocr(scan_ratio, threshold)`.

## 운영 SOP (OCR 미도입 시)

1. 스캐너에서 OCR 텍스트 레이어 PDF로 저장
2. 또는 기관 문서변환기로 텍스트 PDF/HWPX 변환 후 업로드
3. 창구 UI의 `scan_pdf_no_ocr` 안내를 따른다
