# harag — 프로젝트 인벤토리

코드 작성 직전 설계부터 핵심 로직 구현·CI까지의 전체 산출물 목록.
시작점은 `docs/MASTER_DESIGN.md`(전체 조망), 구현 상세는 `docs/IMPLEMENTATION_REPORT.md`.

## 한눈에 보는 상태

| 영역 | 상태 | 비고 |
|---|---|---|
| 설계 문서 | 완료 | 마스터 + 4개 상세(요구사항·기술·API·테스트) |
| 데이터 계약 | 완료·검증 | 스키마·경계·ACL — 불변식 79개 |
| 쓰기 경로 로직 | 완료·검증 | 파싱→청킹→임베딩→적재→버전전환 |
| 읽기 경로 로직 | 완료·검증 | 검색→생성→응답(게이트웨이 연결) |
| 외부 LLM 방어 | 완료·검증 | 백오프·비용상한·타임아웃·degradation |
| 관측 | 완료·검증 | trace·메트릭·감사로그 |
| CI 파이프라인 | 완료 | 차단 게이트 + 야간 품질 게이트 |
| **실제 어댑터** | **미구현** | HWP 디코더·임베딩/LLM 모델·IdP(아래 §부족한 점) |
| 배포·부하 실측 | 미구현 | 클라우드 리소스 필요 |

테스트: 계약 79개 + 동작 47개 + 통합(Qdrant) 전부 초록.
소스 ~2,500줄(테스트 제외).

## 디렉터리

```
docs/                    설계 문서 5종(MASTER_DESIGN이 진입점)
src/harag/
  schemas/               IR·청크 스키마 계약(불변식)
  contracts/             컴포넌트 경계·ACL 계약
  parsing/               파서(구조화 로직 완성, 디코더는 어댑터)
  chunking/              구조 기반 청커
  embedding/             하이브리드 임베더(dense+sparse)
  retrieval/             하이브리드 검색(ACL pre-filter)
  generation/            생성기(안전 가드) + 외부 LLM 클라이언트
  indexing/              버전 전환 + 인덱싱 워커
  api/                   게이트웨이(인증·라우팅·파이프라인)
  eval/                  평가·용량·품질·성능·PoC 하니스
  observability/         trace·메트릭
  config/                설정(환경변수 외부화)
tests/
  contract/              불변식 계약(79, 항상 초록)
  behavior/              동작 TDD(47)
  integration/           Qdrant 통합
docker/                  Dockerfile(앱/추론) + compose
.github/workflows/       CI(차단) + 품질 게이트(야간)
scripts/                 계약 테스트 런너
```

## 빠른 시작

```bash
make install     # 의존성
make ci          # 차단 게이트 전체(계약+동작+통합)
make up          # 로컬 스택(Qdrant/Postgres/MinIO)
```

## 핵심 설계 원칙(이 코드를 읽기 전에)

1. **막을 수 있는 건 코드로 증명한다.** 불변식은 주석이 아니라 Pydantic validator로 — 위반 객체는 생성 자체가 불가능.
2. **막을 수 없는 건 경계 밖으로 명시한다.** 추론 누수·채점자 신뢰도 등은 §부족한 점 / docs 참조.
3. **외부 의존성은 어댑터 뒤로.** 디코더·모델·LLM·transport를 주입식으로 분리 → 표본·GPU·네트워크 없이 로직 검증. 실제 어댑터만 끼우면 동작.
