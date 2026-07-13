"""LLM 클라이언트 팩토리 — 설정에 따라 운영(API) 또는 폴백(로컬) 선택."""
from __future__ import annotations

from harag.config.settings import Settings
from harag.generation.llm_client import ExternalLLMClient
from harag.llm.http_transport import OpenAIChatTransport
from harag.llm.local_llm import LocalExtractiveLLM


def build_llm_client(settings: Settings):
    if settings.llm_provider == "openai" and settings.llm_api_key:
        transport = OpenAIChatTransport(
            api_base=settings.llm_api_base,
            api_key=settings.llm_api_key,
        )
        return ExternalLLMClient(transport=transport, model=settings.llm_model)
    return LocalExtractiveLLM()
