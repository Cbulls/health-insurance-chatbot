# ADR: 프롬프트 인젝션 방어 SEC-02 v2

## 상태

Accepted (2026-07)

## 맥락

창구 RAG는 업로드 문서·사용자 질의·rewrite/rerank LLM 호출이 같은 신뢰 경계에 섞인다.
설계 시점 방어는 생성 시점 정규식 태깅 + system/user 분리뿐이었고, 다음이 가능했다.

- 질의 직접 인젝션
- 고정 delimiter breakout
- rewrite/rerank 사이드채널
- 스트림 경로의 출력 가드 스킵
- 인제스트 미검사(독성 문서 PERSISTED)

연구 합의(OWASP LLM01 2025/2026, Spotlighting/Hines et al. 2024,
Open-Prompt-Injection/Liu et al. USENIX 2024): **완벽한 차단은 불가능**하다.
LLM이 instruction과 data를 동일 토큰 스트림으로 처리하기 때문이다.

## 결정

1. **in-process 다층 방어**만 채택한다(외부 Guardrails SaaS·상시 Dual-LLM 판정 없음).
2. 층 구성:
   - 확장 `InjectionScanner` + `clean|soft|hard` 판정
   - Spotlighting **datamarking** + **요청별 random delimiter**
   - 세션 **canary** (시스템 프롬프트에만 심고 출력 검사)
   - 질의 **hard refuse** (`injection_blocked`)
   - 인제스트 스캔 (`tag` 기본 / `quarantine` 선택)
   - rewrite/rerank **sidechannel** system+UNTRUSTED 래퍼
   - 스트림 종료 후 `OutputGuard` + SSE `revoke`
3. 환경변수: `INJECTION_*` (기본 on). 상세는 `.env.example`.
4. 문서화: “완벽 방어”를 주장하지 않는다. 성공률·유출·오염을 운영 가능 수준으로 낮춘다.

## 결과

- 코드: [`src/harag/security/injection.py`](../../src/harag/security/injection.py),
  [`output_guard.py`](../../src/harag/security/output_guard.py),
  pipeline / llm_client / generator / local_rerank / pdf_pipeline
- 테스트: `tests/behavior/test_injection.py`, `test_injection_redteam.py`
- 한계: 신규 패러프레이즈·모델 복종·진짜 청크를 인용한 악성 사실 주입은
  여전히 잔존한다. 인용 검증은 ID 위조만 막는다.

## 관련

- [SYSTEM_ADVANCEMENT.md](../SYSTEM_ADVANCEMENT.md)
- Hines et al., *Defending Against Indirect Prompt Injection Attacks With Spotlighting* (2024)
- Liu et al., *Formalizing and Benchmarking Prompt Injection Attacks and Defenses* (USENIX Security 2024)
- OWASP Top 10 for LLM Applications — LLM01 Prompt Injection
