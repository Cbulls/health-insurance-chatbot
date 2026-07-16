"""
OpenAI 호환 Chat Completions 전송 계층.

ExternalLLMClient의 Transport Protocol 구현. payload({model, prompt, context_ids})를
받아 실제 프로바이더(/chat/completions)를 호출하고 {"answer","citations"}로 매핑한다.

프로바이더 무관: OpenAI, Upstage Solar, 국내 OpenAI 호환 게이트웨이, 로컬 vLLM 등
base_url만 바꿔 끼운다. 429→RateLimitError, 타임아웃→LLMTimeout으로 방어 계층에 위임.

MVP는 LLM이 청크 id를 신뢰성 있게 인용하기 어렵다고 보고 citations=[]로 둔다
(파이프라인이 '검색된 청크'를 출처로 노출). Phase 2에서 구조화 인용으로 강화.
"""
from __future__ import annotations

import json
from typing import Iterator

import httpx

from harag.generation.llm_client import RateLimitError, LLMTimeout, LLMError


class OpenAIChatTransport:
    def __init__(self, api_base: str, api_key: str, timeout: float = 60.0):
        self._base = api_base.rstrip("/")
        self._key = api_key
        self._timeout = timeout
        # 커넥션 재사용(keep-alive) — 요청마다 새 TCP/TLS 핸드셰이크 방지.
        self._client = httpx.Client(
            base_url=self._base,
            headers={"Authorization": f"Bearer {self._key}"},
            timeout=timeout,
        )

    def post(self, payload: dict) -> dict:
        body = {
            "model": payload["model"],
            "messages": [{"role": "user", "content": payload["prompt"]}],
            "temperature": 0.0,
        }
        try:
            resp = self._client.post("/chat/completions", json=body)
        except httpx.TimeoutException as e:
            raise LLMTimeout("LLM 응답 타임아웃") from e
        except httpx.HTTPError as e:
            raise LLMError(f"LLM 전송 오류: {e}") from e

        if resp.status_code == 429:
            raise RateLimitError("429 rate limit")
        if resp.status_code >= 500:
            raise LLMError(f"LLM 서버 오류 {resp.status_code}")
        if resp.status_code >= 400:
            raise LLMError(f"LLM 요청 오류 {resp.status_code}: {resp.text[:200]}")

        data = resp.json()
        try:
            answer = data["choices"][0]["message"]["content"]
        except (KeyError, IndexError) as e:
            raise LLMError("프로바이더 응답 파싱 실패") from e
        return {"answer": answer, "citations": []}

    def post_stream(self, payload: dict) -> Iterator[str]:
        """스트리밍 호출(stream=true) — content 델타를 도착하는 대로 yield.

        전체 생성을 기다리지 않아 TTFT(첫 토큰까지 시간)가 실제로 짧아진다.
        오류 매핑은 post()와 동일(429→RateLimitError 등)."""
        body = {
            "model": payload["model"],
            "messages": [{"role": "user", "content": payload["prompt"]}],
            "temperature": 0.0,
            "stream": True,
        }
        try:
            with self._client.stream("POST", "/chat/completions", json=body) as resp:
                if resp.status_code == 429:
                    raise RateLimitError("429 rate limit")
                if resp.status_code >= 500:
                    raise LLMError(f"LLM 서버 오류 {resp.status_code}")
                if resp.status_code >= 400:
                    resp.read()
                    raise LLMError(
                        f"LLM 요청 오류 {resp.status_code}: {resp.text[:200]}")
                for line in resp.iter_lines():
                    if not line.startswith("data:"):
                        continue
                    data = line[5:].strip()
                    if data == "[DONE]":
                        return
                    try:
                        obj = json.loads(data)
                    except ValueError:
                        continue  # keep-alive 등 비JSON 라인 무시
                    choices = obj.get("choices") or []
                    if not choices:
                        continue
                    delta = (choices[0].get("delta") or {}).get("content")
                    if delta:
                        yield delta
        except httpx.TimeoutException as e:
            raise LLMTimeout("LLM 응답 타임아웃") from e
        except httpx.HTTPError as e:
            raise LLMError(f"LLM 전송 오류: {e}") from e
