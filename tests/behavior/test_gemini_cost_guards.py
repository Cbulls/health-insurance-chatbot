"""Gemini 절약 가드 — QPM 기본·비용 env·rewrite 상한·예산 산수."""
from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from harag.config.settings import Settings, get_settings
from harag.generation.llm_client import ExternalLLMClient
from harag.llm.factory import build_llm_client
from harag.llm.local_rerank import LLMRewriteLLM


@pytest.fixture(autouse=True)
def _clear_settings_cache():
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


def test_default_rate_limit_qpm_is_gemini_safe(monkeypatch):
    monkeypatch.delenv("RATE_LIMIT_QPM", raising=False)
    s = get_settings()
    assert s.rate_limit_qpm == 5


def test_cost_and_retry_defaults(monkeypatch):
    for k in ("LLM_MAX_COST_PER_QUERY_USD", "LLM_COST_PER_1K_TOKENS",
              "LLM_MAX_RETRIES", "LLM_REWRITE_MAX_CHARS"):
        monkeypatch.delenv(k, raising=False)
    s = get_settings()
    assert s.llm_max_cost_per_query_usd == 0.05
    assert s.llm_cost_per_1k_tokens == 0.01
    assert s.llm_max_retries == 1
    assert s.llm_rewrite_max_chars == 2000


def test_build_llm_client_wires_cost_guards(monkeypatch):
    monkeypatch.setenv("LLM_API_KEY", "test-key")
    monkeypatch.setenv("LLM_PROVIDER", "openai")
    monkeypatch.setenv("LLM_MAX_COST_PER_QUERY_USD", "0.02")
    monkeypatch.setenv("LLM_COST_PER_1K_TOKENS", "0.05")
    monkeypatch.setenv("LLM_MAX_RETRIES", "0")
    s = get_settings()
    client = build_llm_client(s)
    assert isinstance(client, ExternalLLMClient)
    assert client._max_cost == 0.02
    assert client._cost_per_1k == 0.05
    assert client._max_retries == 0


def test_rewrite_llm_respects_max_chars():
    transport = MagicMock()
    transport.post.return_value = {"answer": "출장비 정산 기한"}
    llm = LLMRewriteLLM(transport, "lite", max_chars=400)
    long_hist = ["이전 질의 " + ("가" * 80)] * 10
    out = llm.rewrite("그건 언제?", long_hist)
    assert out == "출장비 정산 기한"
    prompt = transport.post.call_args[0][0]["prompt"]
    assert len(prompt) <= 400


def test_quota_budget_math():
    import importlib.util
    from pathlib import Path

    path = Path(__file__).resolve().parents[2] / "scripts" / "gemini_quota_budget.py"
    spec = importlib.util.spec_from_file_location("gemini_quota_budget", path)
    mod = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(mod)

    assert mod.recommend_qpm(10, 3) == 2
    assert mod.recommend_qpm(10, 2) == 4
    assert mod.daily_questions(1500, 2.5, 0.7) == 420
