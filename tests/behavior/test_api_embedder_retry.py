"""임베딩 API 429/5xx 재시도 + TPM 페이싱."""
from __future__ import annotations

import json

import httpx
import pytest

from harag.embedding.api_embedder import (
    ApiEmbeddingModel, EmbeddingRateLimitError, _approx_tokens, _tpm_pause,
)


def _ok(dim: int, n: int) -> httpx.Response:
    data = [{"index": i, "embedding": [0.1] * dim} for i in range(n)]
    return httpx.Response(200, json={"data": data})


def _model(handler, *, dim: int = 4, max_retries: int = 8, batch_size: int = 4,
           tpm_budget: int = 24000, max_tokens_per_request: int = 8000):
    sleeps: list[float] = []
    m = ApiEmbeddingModel(
        api_base="https://fake.local/v1", api_key="k",
        model="gemini-embedding-001", dim=dim,
        max_retries=max_retries, batch_size=batch_size,
        tpm_budget=tpm_budget, max_tokens_per_request=max_tokens_per_request,
        sleep=sleeps.append,
    )
    m._client = httpx.Client(
        base_url="https://fake.local/v1",
        transport=httpx.MockTransport(handler),
    )
    return m, sleeps


def test_retries_429_then_succeeds():
    calls = {"n": 0}

    def handler(request):
        calls["n"] += 1
        n = len(json.loads(request.read())["input"])
        if calls["n"] == 1:
            return httpx.Response(429, headers={"Retry-After": "2"}, json={})
        return _ok(4, n)

    m, sleeps = _model(handler)
    vecs = m.encode(["약관"])
    assert len(vecs) == 1
    assert calls["n"] == 2
    assert sleeps[0] == 2.0


def test_exhausted_429_raises():
    calls = {"n": 0}

    def handler(request):
        calls["n"] += 1
        return httpx.Response(429, json={})

    m, sleeps = _model(handler, max_retries=2)
    with pytest.raises(EmbeddingRateLimitError):
        m.encode(["약관"])
    assert calls["n"] == 3  # 최초 + 재시도 2
    assert all(s >= 65.0 for s in sleeps)


def test_retries_5xx_then_succeeds():
    calls = {"n": 0}

    def handler(request):
        calls["n"] += 1
        n = len(json.loads(request.read())["input"])
        if calls["n"] == 1:
            return httpx.Response(503, json={})
        return _ok(4, n)

    m, sleeps = _model(handler)
    assert len(m.encode(["약관"])) == 1
    assert calls["n"] == 2
    assert sleeps[0] == 1.5


def test_tpm_pacing_sleeps_between_large_batches():
    def handler(request):
        n = len(json.loads(request.read())["input"])
        return _ok(4, n)

    chunk = "가" * 4000  # ~2000 tokens
    budget = 24000
    m, sleeps = _model(handler, batch_size=1, tpm_budget=budget,
                       max_tokens_per_request=8000)
    vecs = m.encode([chunk, chunk])
    assert len(vecs) == 2
    expected = _tpm_pause(_approx_tokens(chunk), budget)
    assert sum(sleeps) >= expected
    assert expected > 1.0


def test_429_without_retry_after_waits_minute_window():
    calls = {"n": 0}

    def handler(request):
        calls["n"] += 1
        n = len(json.loads(request.read())["input"])
        if calls["n"] == 1:
            return httpx.Response(429, json={})
        return _ok(4, n)

    m, sleeps = _model(handler)
    assert len(m.encode(["약관"])) == 1
    assert calls["n"] == 2
    assert sleeps[0] >= 65.0
