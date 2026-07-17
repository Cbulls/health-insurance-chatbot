# 창구 직원용 harag 사용 가이드

민원·안내 창구에서 **규정 문서**를 올리고, 출처와 함께 짧게 안내하기 위한 UI·정책입니다.

## 목적과 한계

- **하는 일:** 업로드한 문서에서 근거를 찾아 요약·안내합니다.
- **하지 않는 일:** 개별 사례의 법적 판단·확정 안내. 화면 상단 면책 문구를 그대로 안내하세요.
- 근거가 없으면 지어내지 않고 “모른다”고 답합니다.

## 문서 형식

| 형식 | 지원 | 비고 |
|------|------|------|
| 텍스트 PDF | 예 | |
| Word DOCX | 예 | 권장 Word 형식 |
| 구형 Word DOC | 예(조건부) | LibreOffice 로컬 변환(`ENABLE_DOC_CONVERT`) |
| HWPX | 예 | 한글 **권장** 형식 |
| 바이너리 HWP | 예(제한) | 본문 검색 가능, **표 인식 제한** → HWPX 권고 |
| 스캔(이미지) PDF | 아니오 | OCR 미지원 |

HWP 표 품질·라이브러리 결정은 [`docs/adr/ADR-HWP5.md`](adr/ADR-HWP5.md)를 본다.

## 접속

- **데모:** 브라우저 세션 ID (`X-Owner-Id`) — 본인 문서만 보입니다.
- **기관 JWT:** 왼쪽 패널에 Bearer 토큰 붙여넣기 → 저장(탭 `sessionStorage`). `AUTH_JWT_SECRET`/`AUTH_OIDC_JWKS_URL`이 켜진 서버에서는 토큰이 필요합니다.
- **업로드 전:** HWPX·텍스트 PDF 권장 안내를 표시합니다. 바이너리 HWP는 표 제한 경고가 날 수 있습니다.

## 화면 흐름

1. 문서 업로드 → 상태가 **준비됨**이 될 때까지 대기  
2. 질문 입력 (준비된 문서가 있을 때만 활성화)  
3. 답변 아래 **출처** 칩 클릭 → 문서명·조 경로·발췌 확인  
4. 필요 시 **중지**로 스트리밍 중단  

HWP에서 표 제한 경고가 보이면 안내 가능하나, 표 질의는 HWPX 재업로드를 권합니다.

저장 용량이 한도에 가까우면 상단 경고가 뜹니다. 문서 삭제로 공간을 확보하세요.

## 관련 API

- `POST /v1/documents` — `.pdf` / `.hwpx` / `.docx` / `.hwp` / `.doc`
- `POST /v1/query/stream` — SSE `citations`는 **JSON 배열**(chunk_id, struct_path, snippet 포함)
- `GET /health` — `capacity` (선택)

## 환경 변수(파서)

| 변수 | 기본 | 의미 |
|------|------|------|
| `PARSE_MIN_TABLE_RECOVERY` | `0.5` | HWP 표 복원 경고 임계 |
| `ENABLE_DOC_CONVERT` | `true` | `.doc` 변환 허용 |
| `LIBREOFFICE_BIN` | `soffice` | LibreOffice 실행 파일 |
| `DOC_CONVERT_TIMEOUT_SEC` | `120` | 변환 타임아웃 |
