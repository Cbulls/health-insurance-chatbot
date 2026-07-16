# Gemini(AI Studio) 활용·절약 CTO 운영 가이드

이 문서는 harag가 Google AI Studio Gemini 키를 쓸 때의 **과금 방지·한도 관리·모델 역할 분리** 정책이다.
코드 기본값·`.env.example`과 함께 유지한다.

## 1. 과금 위생 체크리스트 (사람 확인)

AI Studio에서 직접 확인한다 ([aistudio.google.com](https://aistudio.google.com/)):

- [ ] 프로젝트에 **Billing / 결제 프로필이 연결되지 않음** (Free tier 유지)
- [ ] 사용 모델이 **Flash / Flash-Lite / embedding** 뿐 (Pro·Search grounding 없음)
- [ ] Usage / Rate limits에서 **RPM·RPD·TPM** 스냅샷을 기록 (§3 예산표에 기입)
- [ ] API 키가 git·채팅·스크린샷에 노출되지 않음 (노출 시 즉시 재발급)
- [ ] 건강보험·행정문서 PoC면 Free tier **데이터 학습 사용** 약관 리스크를 인지

**원칙:** MVP·개인 데모는 결제를 연결하지 않는다. 과금 1순위 통제는 앱이 아니라 결제 미연결이다.  
한도 초과는 비용이 아니라 **429 / `llm_unavailable` abstain**(가용성)으로만 나타난다.

쿼터는 **키당이 아니라 프로젝트당**이다. 키를 여러 개 만들어도 한도가 늘지 않는다.

## 2. 모델 역할 (동결)

| 역할 | 권장 | `.env` |
|------|------|--------|
| 답변 | Flash (`gemini-*-flash*`, Pro 금지) | `LLM_MODEL` |
| 멀티턴 재작성 | Flash-Lite | `LLM_REWRITE_MODEL` |
| 임베딩 | `gemini-embedding-001` | `EMBEDDING_MODEL` + `DIM=768` |

데모·단일 턴 위주면 `LLM_REWRITE_ENABLED=false`로 질문당 콜을 줄인다.

## 3. 호출 예산 (앱 QPM ≠ Gemini RPM)

질의 1 HTTP ≈ 최대 **rewrite + embed + answer = 3** Gemini 콜.

```text
RATE_LIMIT_QPM ≤ floor(Gemini_RPM / max_calls_per_query) - 1
일일_질문_예산 ≤ floor(RPD × 0.7 / avg_calls_per_query)
```

예시 (Flash RPM≈10, RPD≈1500, 멀티턴 평균 2.5콜 가정):

| 항목 | 값 |
|------|-----|
| 권장 `RATE_LIMIT_QPM` | 3~5 |
| 일일 질문 예산(여유 30%) | ≈ 1500×0.7 / 2.5 ≈ **420** |

프로젝트 실측치를 아래에 기입하고 `python scripts/gemini_quota_budget.py`로 재계산한다.

```text
기록일: 2026-07-17 (공개 표 가정 — 프로젝트 Usage로 교체)
Gemini_RPM (Flash) = 10
Gemini_RPD (Flash) = 1500
Gemini_RPM (Lite)  = (프로젝트 Usage 확인)
avg_calls_per_query = 2.5   # rewrite on 멀티턴 비중 반영
RATE_LIMIT_QPM (적용) = 5   # 보수 산수상 3도 가능; .env 기본 5
일일_질문_예산 = 420        # python scripts/gemini_quota_budget.py
```

실측치로 다시 계산:

```bash
python scripts/gemini_quota_budget.py --rpm 10 --rpd 1500 --calls 2.5
```

## 4. 품질은 검색으로, 콜은 아끼기

- 실임베딩 유지(해시 폴백 금지)
- `TOP_K` / `RERANK_TOP_N`으로 컨텍스트·토큰 축소
- `MIN_SCORE`로 무관 질의 answer LLM 스킵
- 대량 재업로드 자제(임베딩 재과금·재쿼터)
- Pro·Google Search grounding 사용 금지

## 5. 코드로 고정된 방어

| 설정 | 기본 | 역할 |
|------|------|------|
| `RATE_LIMIT_QPM` | 5 | owner당 HTTP/분 (Gemini RPM보다 낮게) |
| `LLM_MAX_COST_PER_QUERY_USD` | 0.05 | 답변 LLM 예상비용 상한(유료 전환 시 폭주 차단) |
| `LLM_COST_PER_1K_TOKENS` | 0.01 | 상한 추정치용(실제 청구와 다를 수 있음) |
| `LLM_MAX_RETRIES` | 1 | 429 재시도(무료 티어 증폭 완화; 0이면 재시도 없음) |
| `LLM_REWRITE_MAX_CHARS` | 2000 | 재작성 프롬프트 상한 |

유료 전환 트리거(명시적 결심): 일 질의 ≫ free RPD, 또는 학습 미사용 약관 필수.  
그때도 모델은 Flash 유지, Pro 금지, 일 예산 알람.
