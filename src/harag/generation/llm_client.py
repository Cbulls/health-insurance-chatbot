"""
외부 LLM 클라이언트 — LLMClient 인터페이스 구현 + 방어 로직.

LLM은 외부 API. rate limit·비용·타임아웃이 관심사.
실제 HTTP 호출(transport)은 주입식 분리 → 방어 로직을 네트워크 없이 검증.

방어 4종:
  - rate limit(429) → 지수 백오프 재시도
  - 비용 상한 → 호출 전 예상 토큰으로 차단(폭주 방어)
  - 타임아웃 → LLMTimeout
  - 영구 실패 → LLMError(상위가 graceful degradation)
"""
from __future__ import annotations

import time
from typing import Iterator, Protocol


class LLMError(Exception):
    """LLM 호출 영구 실패. 상위(파이프라인)가 잡아 degradation."""


class RateLimitError(LLMError):
    """429 — 재시도 대상."""


class CostLimitError(LLMError):
    """예상 비용이 상한 초과 — 호출 차단."""


class LLMTimeout(LLMError):
    """응답 타임아웃."""


class Transport(Protocol):
    """실제 HTTP 전송 계층. payload -> 응답 dict."""
    def post(self, payload: dict) -> dict: ...


def _estimate_tokens(texts: list[str], query: str) -> int:
    """입력 토큰 근사(한국어 팽창 고려)."""
    total_chars = sum(len(t) for t in texts) + len(query)
    return total_chars // 2 + 500


class ExternalLLMClient:
    """LLMClient 구현체. 생성기의 _StubLLM 자리에 주입된다."""

    def __init__(self, transport: Transport, model: str,
                 max_cost_per_query_usd: float = 0.50,
                 cost_per_1k_tokens: float = 0.01,
                 max_retries: int = 3, base_backoff_sec: float = 0.2,
                 system_instruction: str = "제공된 문서에만 근거해 한국어로 답하라. "
                                           "근거가 없으면 모른다고 답하라."):
        self._transport = transport
        self._model = model
        self._max_cost = max_cost_per_query_usd
        self._cost_per_1k = cost_per_1k_tokens
        self._max_retries = max_retries
        self._base_backoff = base_backoff_sec
        self._system_instruction = system_instruction

    def _build_payload(self, query: str, context_texts: list[str],
                       context_ids: list[str]) -> dict:
        """비용 상한 검사 + 인젝션 방어 프롬프트 조립(complete/stream 공통)."""
        # ── 비용 상한: 호출 전 차단(폭주 방어) ──
        est_tokens = _estimate_tokens(context_texts, query)
        est_cost = est_tokens / 1000 * self._cost_per_1k
        if est_cost > self._max_cost:
            raise CostLimitError(
                f"예상 비용 ${est_cost:.3f} > 상한 ${self._max_cost:.3f}")

        # 프롬프트 인젝션 방어: 시스템 지시와 문서를 구조적으로 분리(SEC-02).
        from harag.security.injection import build_safe_prompt
        safe_prompt = build_safe_prompt(
            system_instruction=self._system_instruction,
            query=query, context_texts=context_texts)

        return {
            "model": self._model,
            "prompt": safe_prompt,
            "context_ids": context_ids,
        }

    def complete(self, query: str, context_texts: list[str],
                 context_ids: list[str]) -> tuple[str, list[str]]:
        payload = self._build_payload(query, context_texts, context_ids)

        # ── rate limit 백오프 재시도 ──
        attempt = 0
        while True:
            try:
                resp = self._transport.post(payload)
                return self._parse(resp)
            except RateLimitError:
                if attempt >= self._max_retries:
                    raise LLMError("rate limit: 재시도 소진")
                time.sleep(self._base_backoff * (2 ** attempt))
                attempt += 1
            except LLMTimeout:
                raise

    def supports_streaming(self) -> bool:
        return callable(getattr(self._transport, "post_stream", None))

    def complete_stream(self, query: str, context_texts: list[str],
                        context_ids: list[str]) -> Iterator[str]:
        """토큰을 도착하는 대로 yield(TTFT 개선). 방어 로직은 complete와 동일.

        재시도는 '첫 토큰 전'까지만 — 이미 내보낸 토큰은 되감을 수 없다."""
        payload = self._build_payload(query, context_texts, context_ids)

        attempt = 0
        while True:
            emitted = False
            try:
                for piece in self._transport.post_stream(payload):
                    emitted = True
                    yield piece
                return
            except RateLimitError:
                if emitted or attempt >= self._max_retries:
                    raise LLMError("rate limit: 재시도 소진")
                time.sleep(self._base_backoff * (2 ** attempt))
                attempt += 1
            except LLMTimeout:
                raise

    def _parse(self, resp: dict) -> tuple[str, list[str]]:
        answer = resp.get("answer")
        cited = resp.get("citations", [])
        if answer is None:
            raise LLMError("프로바이더 응답에 answer 없음")
        return answer, list(cited)
