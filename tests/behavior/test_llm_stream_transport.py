"""
LLM 스트리밍 전송 TDD — OpenAI 호환 SSE 파싱과 오류 매핑.

post_stream은 content 델타만 순서대로 yield하고, [DONE]에서 끝나며,
429는 RateLimitError로 매핑돼야 한다(complete와 동일한 방어 계층 위임).
"""
from __future__ import annotations

import json

import httpx
import pytest

from harag.generation.llm_client import RateLimitError
from harag.llm.http_transport import OpenAIChatTransport


def _sse_lines(*chunks: str) -> bytes:
    lines = []
    for c in chunks:
        obj = {"choices": [{"delta": {"content": c}}]}
        lines.append(f"data: {json.dumps(obj, ensure_ascii=False)}\n\n")
    lines.append("data: [DONE]\n\n")
    return "".join(lines).encode("utf-8")


def make_transport(handler) -> OpenAIChatTransport:
    t = OpenAIChatTransport(api_base="https://fake.local/v1", api_key="k")
    t._client = httpx.Client(base_url="https://fake.local/v1",
                             transport=httpx.MockTransport(handler))
    return t


def test_ST01_stream_yields_content_deltas_in_order():
    def handler(request):
        assert json.loads(request.read())["stream"] is True
        return httpx.Response(200, content=_sse_lines("안녕", "하세요"))

    t = make_transport(handler)
    assert list(t.post_stream({"model": "m", "prompt": "p"})) == ["안녕", "하세요"]


def test_ST02_stream_429_raises_ratelimit():
    def handler(request):
        return httpx.Response(429, content=b"")

    t = make_transport(handler)
    with pytest.raises(RateLimitError):
        list(t.post_stream({"model": "m", "prompt": "p"}))


def test_ST03_stream_ignores_non_json_keepalives():
    def handler(request):
        body = b": keep-alive\n\n" + _sse_lines("응답")
        return httpx.Response(200, content=body)

    t = make_transport(handler)
    assert list(t.post_stream({"model": "m", "prompt": "p"})) == ["응답"]
