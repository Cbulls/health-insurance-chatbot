# harag — 서비스화 로드맵 (Production Readiness)

현재 코드는 **검증된 로직**이다. **돌아가는 서비스**가 되려면 무엇이 더 필요한지,
코드베이스 실측 + 실제 모델·라이브러리 조사를 바탕으로 정리한다.

핵심 인식: 지금 모든 외부 의존성이 어댑터 뒤에서 가짜로 검증돼 있다.
서비스화 = **그 어댑터에 실물을 끼우고, 끼운 것이 실제로 품질을 내는지 검증**하는 일이다.

---

## 0. 우선순위 한눈에

| 순위 | 작업 | 막힌 이유 | 위험도 |
|---|---|---|---|
| 1 | HWP 디코더 연결 + PoC | 실제 표본·라이브러리 필요 | **치명(SPOF)** |
| 2 | 임베딩·리랭커 모델 연결 | GPU 필요 | 높음 |
| 3 | LLM API 연결 | API 키·네트워크 | 중간 |
| 4 | 저장소 어댑터(Qdrant·PG·오브젝트) | 인프라 프로비저닝 | 중간 |
| 5 | 골드셋 구축 + 품질 게이트 활성화 | 도메인 라벨링 | 높음 |
| 6 | 배포·운영(K8s·시크릿·모니터링) | 클라우드 환경 | 중간 |
| 7 | 부하 테스트 + SLO 동결 | 배포 환경 | 중간 |

---

## 1. HWP 디코더 연결 + PoC (최우선·SPOF)

### 조사 결과 — 상황이 명확해졌다
- **HWP(구형 바이너리):** OLE 복합 파일 + zlib 압축 + 레코드 구조. 표 등 복잡한 컨트롤 객체 파싱이 어렵다. `pyhwp`/`olefile`로 텍스트는 되지만 **표 셀 구조 복원이 약점**(우리 설계가 SPOF로 지목한 그 지점).
- **HWPX(신형 XML):** ZIP + XML이라 Python 표준 라이브러리(zipfile, ElementTree)만으로 표까지 추출 가능. **HWP보다 훨씬 기계 처리 친화적.**
- 한컴이 2025년 공식 파싱 가이드(tech.hancom.com)를 연재 중 — 참고 자료가 생겼다.

### 전략적 함의 (설계 갱신 제안)
- **가능하면 HWPX를 우선 경로로.** 기관에 HWPX 변환을 요청할 수 있으면 파싱 난이도·리스크가 급감한다.
- HWP만 있는 경우를 위해 디코더를 단계적으로: ① 텍스트·구조(olefile+pyhwp) → ② 표 셀 복원(레코드 직접 파싱 또는 상용 변환).
- **표 복원율이 임계 미달이면**, 우리가 ADR로 열어둔 "파싱 언어 분리" 또는 상용 솔루션(한컴 SDK 등) 검토.

### 할 일
1. 실제 행정문서 표본 수집(층화: 포맷·연도·표 복잡도별, 최악 케이스 포함)
2. `HwpDecoder` 어댑터 구현(`parsing/parser.py`의 Protocol 뒤)
3. `eval/poc.py`에 표본 투입 → 표 복원율 측정 → **GO/CONDITIONAL/NO_GO 판정**
4. 임계(`parse_min_table_recovery`)를 측정 전에 동결(굿하트 방어)

> 이 단계 통과 전까지 시스템 정확도는 GO도 NO-GO도 아니다.

---

## 2. 임베딩·리랭커 모델 연결 (GPU)

### 조사 결과 — 우리 설계 가정이 맞았다
- **KURE-v1**(고려대 NLP&AI 연구실): BGE-M3 기반, **한국어 검색 특화**. `sentence_transformers`로 바로 로드. MTEB-ko-retrieval 리더보드로 검증 가능.
- **BGE-M3**: 다국어, 한국어 우수, dense+sparse+multi-vector를 한 모델로(우리 하이브리드 설계에 적합).
- 둘 다 self-host 가능 — v4의 "임베딩은 self-host" 결정과 일치.

### 할 일
1. GPU 추론 서버 구현(`embedding/` — 현재 어댑터 인터페이스만)
   - `EmbeddingModel.encode`, `CrossEncoder.score_pairs`를 실제 모델로
   - 서빙: TEI(Text Embeddings Inference) 또는 vLLM, `docker/Dockerfile.inference`에 탑재
2. 형태소 분석기 실연결: `Morph.tokens`를 Kiwi(kiwipiepy)로
3. **본인 코퍼스로 모델 벤치마크** — KURE vs BGE-M3 vs 기타, recall@k 비교 후 `embedding_model_id` 확정
4. `settings.py`의 `UNSET-embedding-model` → 실제 모델 ID

---

## 3. LLM API 연결

현재 `ExternalLLMClient`는 방어 로직(백오프·비용·타임아웃) 완성, `Transport`만 가짜.

### 할 일
1. `Transport.post`를 실제 프로바이더 호출로(httpx로 OpenAI 호환/국내 LLM API)
2. API 키를 **시크릿 매니저**로 주입(코드·이미지에 넣지 않음)
3. 데이터 주권: 행정문서 → 국내 리전·학습 비사용 약관 확인(법무)
4. `settings.py`의 `UNSET-llm-*` 확정

---

## 4. 저장소 어댑터 (인프라)

현재 전부 인메모리 가짜. 실제 연결 필요.

| 저장소 | 현재 | 할 일 |
|---|---|---|
| Qdrant | 인메모리 테스트만 | 실제 클러스터, named vector(dense+sparse) 컬렉션, ACL payload 인덱스 |
| PostgreSQL | 미연결 | 문서 등록부·버전 이력·감사 로그 스키마 + 마이그레이션 |
| 오브젝트 스토리지 | 미연결 | S3 호환 버킷, 원본·IR·청크 보존(재인덱싱 대비) |
| 큐 | `IngestQueue` 인터페이스만 | 메시지 큐(읽기/쓰기 격리) + 워커 소비 루프 |

특히 `IndexingWorker`의 `store`에 **증분 인덱싱용 `get_embeddings_by_hash`**를 Qdrant 어댑터가 실제 구현해야 한다.

---

## 5. 골드셋 구축 + 품질 게이트 활성화

지금 `quality-gates.yml`은 스켈레톤(골드셋 없음). **이게 없으면 "정확한지"를 영영 모른다.**

### 할 일
1. 골드셋 구축: 유형별 층화(body/table/multi_clause/absent/conflicting)
   - 질의 출처 3갈래: 실사용 로그 + LLM 생성 + **적대적 수작업**(자기 회피 편향 차단)
   - 라벨러 간 일치도(IAA) 측정, 모호 질의 제외
2. `eval/harness.py`에 골드셋 투입 → 유형별 recall@k·faithfulness·abstention 측정
3. `eval/quality.py`의 신뢰구간 게이트로 SLO 판정 → CI 차단으로 승격
4. LLM-judge 채점자 신뢰도: 사람 표본과 κ 교차검증 루프 운영

---

## 6. 배포·운영

### 할 일
- **시크릿 관리:** JWT 시크릿·LLM 키·DB 자격증명을 시크릿 매니저로(현재 환경변수 평문)
- **K8s 매니페스트:** Deployment/Service/HPA, GPU 노드 풀(추론 서버), 읽기/쓰기 독립 스케일
- **인증 IdP 실연결:** 조직 IdP 환경 확인 → 자체 JWT 또는 OIDC introspection으로 `JwtIdentityProvider` 교체/확장
- **모니터링:** `observability/tracing.py`를 실제 백엔드로(Prometheus+Grafana, Langfuse 등). trace_id → 로그 수집(ELK 등)
- **헬스/레디니스:** `/health/ready`에 의존성(Qdrant·PG·추론서버) 깊은 체크 추가
- **레이트 리미팅:** 사용자 단위(게이트웨이 앞단 또는 미들웨어) — 외부 LLM rate limit과 별개

---

## 7. 부하 테스트 + SLO 동결

`eval/perf.py`는 방법론 완성, 실측 데이터만 없음.

### 할 일
1. 배포 환경에서 open-loop 부하 주입(locust/k6)
2. 워밍업 제외·p95/p99·단계별 분해로 병목 식별
3. 부하별 곡선에서 "몇 RPS까지 SLO 유지"를 용량으로 확정(`eval/capacity.py` 역산 검증)
4. 측정값으로 SLO 동결 → `settings.py`의 SLO 플레이스홀더 확정

---

## 8. 데이터·운영 거버넌스 (코드 밖)

- **문서 수명주기:** 개정·폐기 시 재인덱싱·골드셋 stale 처리 운영 절차
- **권한 동기화:** 조직 인사이동 시 ACL 갱신 파이프라인
- **법무·컴플라이언스:** 외부 LLM 약관, 개인정보 영향평가, 데이터 리전
- **사고 대응:** 환각 신고 → trace_id 추적 → 골드셋 보강 루프

---

## 9. 코드로 못 막아 남는 것 (재확인)

서비스화해도 다음은 코드가 아니라 정책·운영의 몫이다:
- 추론 누수(권한 내 청크 조합) → 제품 정책
- 기대-역량 격차(판단 대행 기대) → 면책·UX
- LLM-judge 신뢰도 → 사람 교차검증
- NER 기반 PII(이름·주소) → 별도 NER 도입 검토
- 인젝션 신규 우회 → 인용 검증이 최종 방어선

---

## 요약 — 서비스화의 본질

지금까지가 **"올바르게 지을 수 있음을 증명"**한 단계라면,
서비스화는 **"실물을 끼우고 실제로 품질을 내는지 측정"**하는 단계다.

3개의 검증 관문이 순서대로 기다린다:
1. **HWP PoC** — 파싱이 충분한가? (GO/NO-GO)
2. **모델 벤치마크** — 검색·생성이 충분한가? (모델 선정)
3. **골드셋 품질 게이트** — 전체가 SLO를 넘는가? (출시 판정)

이 셋을 통과하기 전까지는 "잘 설계된 코드"이지 "믿을 수 있는 서비스"가 아니다.
잘 설계된 어댑터 구조 덕에 통과는 빠르게 시도할 수 있지만, 통과 자체는 실물과 데이터가 한다.
