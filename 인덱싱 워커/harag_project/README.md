# harag — 한글 행정문서 RAG 챗봇

한글 행정문서(HWP 포함)를 파싱·청킹해 검색하고, LLM으로 질의응답하는 RAG 시스템.

## 설계 문서

- **`docs/MASTER_DESIGN.md` — 전체 조망 (여기서 시작)**
- `docs/design_report_v4.md` — 요구사항·계약·리스크 ("무엇을 왜")
- `docs/code_design_spec.md` — 기술 스택·인프라 ("무엇으로 어떻게")
- `docs/api_design_spec.md` — API 게이트웨이 계약·보안 경계
- `docs/tdd_test_spec.md` — 테스트 명세(요구사항 → 테스트 매핑)

## 구조

```
src/harag/
  schemas/      스키마 계약 (ir, chunk) — B-1, 불변식
  contracts/    컴포넌트 경계·ACL — B-2, B-4
  parsing/      포맷별 파서 (PoC 후 구현)
  chunking/     구조 기반 청킹
  embedding/    self-host 임베딩/리랭커 서버
  retrieval/    하이브리드 검색 (Qdrant) — 구현됨
  generation/   생성 + 안전 가드 — 구현됨
  indexing/     버전 전환·GC (B-3) — versioning 구현됨
  api/          FastAPI 게이트웨이
  eval/         평가 하니스·용량 계산기
  observability/ 트레이싱·메트릭
  config/       설정(환경변수 외부화)
tests/
  contract/     계약 테스트(항상 초록, 스크립트형)
  behavior/     동작 테스트(TDD, 빨강→초록)
  integration/  Qdrant 등 외부 의존
```

## 현재 상태 (구현 진행도)

- 초록: 스키마·경계·ACL·버전전환 계약(64) + 검색기(4) + 생성기(5)
- 빨강(구현 대기): 파서(5) — **다음 작업: 국면 A PoC**

## 시작

```bash
make install        # 의존성
make test-contract  # 계약 테스트(초록 확인)
make test-behavior  # 동작 테스트(현재 9초록/5빨강)
make up             # 로컬 스택(Qdrant/Postgres/MinIO)
```

## 다음 작업

1. **국면 A PoC** — 골든 HWP 표본의 표 복원율 측정. Go/No-Go 판정. (`fixtures/golden/`)
2. 파서 TDD — P-01~P-08 빨강을 초록으로.
3. 청커·인덱싱 → 게이트웨이 → 관측·CI.

자세한 순서는 `docs/code_design_spec.md` §8.
