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

- 계약 테스트 79개 초록(불변식)
- 동작 테스트 47개 초록: 파서·청커·임베더(쓰기) + 검색·생성·파이프라인(읽기) + 워커·LLM클라이언트·관측
- 두 경로 end-to-end 연결됨(가짜 어댑터 기반). 남은 것: 실제 HWP 디코더·모델·IdP·배포


## 시작

```bash
make install        # 의존성
make test-contract  # 계약 테스트(초록 확인)
make test-behavior  # 동작 테스트(현재 47 초록)
make ci             # CI 차단 게이트 전체(푸시 전 로컬 검증)
make up             # 로컬 스택(Qdrant/Postgres/MinIO)
```

## CI 파이프라인

`.github/workflows/`에 두 워크플로:

- **`ci.yml` (차단 게이트, 모든 푸시·PR):** 린트 → 계약 테스트(79) → 동작 테스트(47) → 통합(Qdrant) → 컨테이너 빌드. 빠르고 외부 의존 없는 것만. 깨지면 머지 차단.
- **`quality-gates.yml` (비차단, 야간 배치):** 품질 게이트(골드셋 평가)·성능 게이트(부하 테스트)·PoC 게이트(표 복원율). 골드셋·GPU·표본이 필요해 PR 차단이 아니라 야간 실행. 현재는 측정 방법론 검증까지 활성, 실제 리소스 확보 후 전체 활성화.

계약 테스트는 스크립트형이라 `scripts/run_contract_tests.py`가 실패를 종료코드로 강제(CI가 초록 오인 방지).

## 다음 작업

1. **국면 A PoC** — 골든 HWP 표본의 표 복원율 측정. Go/No-Go 판정. (`fixtures/golden/`)
2. 파서 TDD — P-01~P-08 빨강을 초록으로.
3. 청커·인덱싱 → 게이트웨이 → 관측·CI.

자세한 순서는 `docs/code_design_spec.md` §8.
