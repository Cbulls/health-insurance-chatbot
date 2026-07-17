# ADR: HWP5(바이너리 .hwp) 파싱 전략

## 상태

Accepted (2026-07)

## 맥락

행정문서는 여전히 `.hwp`가 많다. HWPX(ZIP+XML)는 이미 라이브이나, 바이너리 HWP5는 OLE+레코드 구조라 표 복원이 SPOF다.

## 결정

1. **HWPX 우선 정책**을 유지한다. UI·오류 메시지에서 HWPX 변환을 권고한다.
2. **HWP5 B1**은 `olefile`로 `PrvText`(UTF-16) 및 BodyText zlib 최선 추출 → `RawBlock` → 기존 `StructuringParser`.
3. **표(B3)** 미복원 시 인제스트를 실패시키지 않고 `parse_status=partial` + `hwp5_table_limited` 경고로 **ready**(검색 가능)한다. 임계는 `PARSE_MIN_TABLE_RECOVERY`.
4. LibreOffice로 HWP→PDF 우회는 한글/표 품질이 불안정하여 **채택하지 않는다**.
5. 상용 한컴 SDK는 라이선스·배포 부담이 커지면 재검토(본 ADR 비범위).
6. **정책 동결(2026-07 고도화)**: 2주 표 PoC가 `PARSE_MIN_TABLE_RECOVERY` 게이트를 통과하지 못하면 **기관 SOP는 HWP→HWPX 필수**로 동결한다. 바이너리 HWP는 텍스트-only + UI 경고를 유지하고, SDK 평가는 별도 예산 항목으로 분리한다(무한 파싱 투자 방지).

## 결과

- 의존성: `olefile`(필수), `pyhwp`는 환경 호환 이슈로 1차 경로에서 제외.
- 위험: 표·수식·각주 누락. 창구 안내는 가능하나 표 질의 정확도는 HWPX 대비 낮다.
- 운영: 창구 UI에 HWPX 권장 카피·업로드 전 안내. 게이트: `scripts/poc_format_gate.py`.

## 관련

- [`src/harag/parsing/hwp5_decoder.py`](../../src/harag/parsing/hwp5_decoder.py)
- [`docs/COUNTER_UX.md`](../COUNTER_UX.md)
