"""
임베딩 차원 축소 옵션 TDD — dimensions 파라미터 전달.

OpenAI text-embedding-3-*는 dimensions로 차원을 줄일 수 있다(저장량 절감 —
Qdrant 무료 사양 대응). 호환 프로바이더 오류 방지를 위해 옵트인이어야 한다.
"""
from __future__ import annotations

import json

import httpx

from harag.embedding.api_embedder import ApiEmbeddingModel


def make_model(send_dimensions: bool, captured: dict) -> ApiEmbeddingModel:
    dim = 4

    def handler(request):
        captured["body"] = json.loads(request.read())
        n = len(captured["body"]["input"])
        data = [{"index": i, "embedding": [0.1] * dim} for i in range(n)]
        return httpx.Response(200, json={"data": data})

    m = ApiEmbeddingModel(api_base="https://fake.local/v1", api_key="k",
                          model="text-embedding-3-small", dim=dim,
                          send_dimensions=send_dimensions)
    m._client = httpx.Client(base_url="https://fake.local/v1",
                             transport=httpx.MockTransport(handler))
    return m


def test_ED01_dimensions_sent_when_enabled():
    captured = {}
    make_model(True, captured).encode(["텍스트"])
    assert captured["body"]["dimensions"] == 4


def test_ED02_dimensions_omitted_by_default():
    captured = {}
    make_model(False, captured).encode(["텍스트"])
    assert "dimensions" not in captured["body"]
