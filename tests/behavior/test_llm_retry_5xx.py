"""5xx/LLMError는 첫 토큰 전 재시도, CostLimit·Timeout은 즉시 실패."""
from __future__ import annotations

import pytest

from harag.generation.llm_client import (
    CostLimitError, ExternalLLMClient, LLMError, LLMTimeout, RateLimitError,
)


class _FlakyPost:
    def __init__(self, fails: int, err: Exception):
        self._fails = fails
        self._err = err
        self.calls = 0

    def post(self, payload: dict) -> dict:
        self.calls += 1
        if self.calls <= self._fails:
            raise self._err
        return {"answer": "ok", "citations": []}


class _FlakyStream:
    def __init__(self, fails: int, err: Exception):
        self._fails = fails
        self._err = err
        self.calls = 0

    def post(self, payload: dict) -> dict:
        return {"answer": "n/a", "citations": []}

    def post_stream(self, payload: dict):
        self.calls += 1
        if self.calls <= self._fails:
            raise self._err
        yield "토큰"


def test_complete_retries_5xx_then_succeeds(monkeypatch):
    monkeypatch.setattr("harag.generation.llm_client.time.sleep", lambda *_: None)
    t = _FlakyPost(fails=2, err=LLMError("LLM 서버 오류 503"))
    c = ExternalLLMClient(t, "m", max_retries=2, base_backoff_sec=0.01,
                          max_cost_per_query_usd=10.0)
    ans, _ = c.complete("q", ["ctx"], ["c1"])
    assert ans == "ok"
    assert t.calls == 3


def test_stream_retries_5xx_before_first_token(monkeypatch):
    monkeypatch.setattr("harag.generation.llm_client.time.sleep", lambda *_: None)
    t = _FlakyStream(fails=1, err=LLMError("LLM 서버 오류 503"))
    c = ExternalLLMClient(t, "m", max_retries=2, base_backoff_sec=0.01,
                          max_cost_per_query_usd=10.0)
    out = "".join(c.complete_stream("q", ["ctx"], ["c1"]))
    assert out == "토큰"
    assert t.calls == 2


def test_cost_limit_not_retried():
    class Boom:
        def post(self, payload):
            raise CostLimitError("too costly")
    c = ExternalLLMClient(Boom(), "m", max_retries=3, max_cost_per_query_usd=10.0)
    # CostLimit from transport path —_build_payload may also raise; force via low cap
    c2 = ExternalLLMClient(_FlakyPost(0, LLMError("x")), "m",
                           max_cost_per_query_usd=0.000001, max_retries=3)
    with pytest.raises(CostLimitError):
        c2.complete("q", ["아주 긴 " * 500], ["c1"])


def test_timeout_not_retried(monkeypatch):
    monkeypatch.setattr("harag.generation.llm_client.time.sleep", lambda *_: None)
    t = _FlakyPost(fails=5, err=LLMTimeout("timeout"))
    c = ExternalLLMClient(t, "m", max_retries=3, max_cost_per_query_usd=10.0)
    with pytest.raises(LLMTimeout):
        c.complete("q", ["ctx"], ["c1"])
    assert t.calls == 1


def test_rate_limit_still_retried(monkeypatch):
    monkeypatch.setattr("harag.generation.llm_client.time.sleep", lambda *_: None)
    t = _FlakyPost(fails=1, err=RateLimitError("429"))
    c = ExternalLLMClient(t, "m", max_retries=2, base_backoff_sec=0.01,
                          max_cost_per_query_usd=10.0)
    ans, _ = c.complete("q", ["ctx"], ["c1"])
    assert ans == "ok" and t.calls == 2
