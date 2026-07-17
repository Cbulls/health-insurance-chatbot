# 창구 직원용 harag 사용 가이드

민원·안내 창구에서 **규정 문서**를 올리고, 출처와 함께 짧게 안내하기 위한 UI·정책입니다.

제품 백로그·스프린트 우선순위(SSO·공유 문서함·원문 열람 등)는
[`COUNTER_PRODUCT_BACKLOG.md`](COUNTER_PRODUCT_BACKLOG.md)를 본다.

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

1. **기관 SSO (권장):** 왼쪽 **기관 계정으로 로그인** → IdP 인증 후 자동으로 토큰이 저장됩니다.  
   - 파일럿: `AUTH_OIDC_MOCK=true` + `AUTH_JWT_SECRET` 이면 모의 로그인으로 바로 JWT가 발급됩니다.  
   - 운영: `AUTH_OIDC_AUTHORIZE_URL` / `CLIENT_ID` / `TOKEN_URL` / redirect URI 등록 — [`SECRETS_OPS.md`](SECRETS_OPS.md).
2. **데모:** SSO·JWT가 없을 때 브라우저 세션 ID (`X-Owner-Id`) — 본인 문서만 보입니다 (`AUTH_ALLOW_DEMO_OWNER`).
3. **고급 · JWT 붙여넣기:** 개발/장애 대응용. 접기 패널에 Bearer 토큰을 넣고 저장(탭 `sessionStorage`).
4. 토큰 만료(401) 시 사이드바가 열리며 재로그인을 안내합니다.

## 사내 지식 라이브러리 / 공용 규정 / 내 업로드

| 구분 | 누가 보나 | 누가 올리나 | 질의 시 |
|------|-----------|-------------|---------|
| **사내 지식 라이브러리** | 전원 | `admin` / `doc_admin`이 컬렉션에 등록 | **항상 함께 참조** (약관+법령 등) |
| **공용 규정** | 같은 부서(`dept`) | `admin` / `doc_admin` + 부서 클레임 | 부서 직원 검색에 포함 |
| **내 업로드** | 본인만 | 모든 로그인 사용자 | 본인 검색에만 포함 |

**전형적 사용:** 직원이 보험사 약관을 **내 업로드**에 올리고, 개발자/관리자가 **라이브러리**에 `건강보험법령` 컬렉션을 만들어 법령 PDF를 올리면, 직원 질의 시 LLM이 약관과 법령을 같이 보고 답합니다.

업로드 대상 선택: `내 업로드` / `공용 규정` / `라이브러리 · (컬렉션명)`.  
컬렉션 만들기는 관리자만 **컬렉션+** 버튼으로 가능합니다.

## 화면 흐름

1. (권장) 기관 로그인 → 공용 규정 확인 또는 문서 업로드  
2. 상태가 **준비됨**이 될 때까지 대기  
3. 질문 입력 (준비된 문서가 있을 때만 활성화)  
4. 답변 아래 **출처** 칩 클릭 → 문서명·조 경로·발췌 확인  
5. 필요 시 **중지**로 스트리밍 중단  

한도·장애 메시지: 일일 질문 한도 소진(`budget_exhausted`)과 요청 과다(`rate_limit`)는 구분되어 표시됩니다.

HWP에서 표 제한 경고가 보이면 안내 가능하나, 표 질의는 HWPX 재업로드를 권합니다.

저장 용량이 한도에 가까우면 상단 경고가 뜹니다. 문서 삭제로 공간을 확보하세요.

## 관련 API

- `GET /v1/auth/config` · `GET /v1/auth/login` · `GET /v1/auth/me`
- `POST /v1/documents` — form: `file`, 선택 `shared=true`
- `POST /v1/query/stream` — SSE `citations`는 **JSON 배열**(chunk_id, struct_path, snippet 포함)
- `GET /health` — `capacity` (선택)

## 환경 변수(파서)

| 변수 | 기본 | 의미 |
|------|------|------|
| `PARSE_MIN_TABLE_RECOVERY` | `0.5` | HWP 표 복원 경고 임계 |
| `ENABLE_DOC_CONVERT` | `true` | `.doc` 변환 허용 |
| `LIBREOFFICE_BIN` | `soffice` | LibreOffice 실행 파일 |
| `DOC_CONVERT_TIMEOUT_SEC` | `120` | 변환 타임아웃 |
