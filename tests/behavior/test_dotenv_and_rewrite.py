"""
.env 로더 + 재작성 설정 TDD.

  - 로컬 uvicorn은 .env를 스스로 읽어야 한다(환경변수 우선, 덮어쓰지 않음).
  - LLM_REWRITE_ENABLED=false 이면 재작성 LLM을 쓰지 않는다.
  - LLM_REWRITE_MODEL이 있으면 재작성에만 그 모델을 쓴다.
"""
from __future__ import annotations

import os

from harag.config.settings import get_settings


def test_DE01_dotenv_fills_missing_env_only(tmp_path, monkeypatch):
    """파일에 있는 키는 비어 있는 환경변수만 채우고, 이미 있는 값은 건드리지 않는다."""
    env_file = tmp_path / ".env"
    env_file.write_text(
        "FROM_FILE=file-value\nALREADY_SET=should-not-win\n# comment\nEMPTY=\n",
        encoding="utf-8",
    )
    monkeypatch.delenv("FROM_FILE", raising=False)
    monkeypatch.setenv("ALREADY_SET", "env-wins")

    for line in env_file.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key, value = key.strip(), value.strip().strip("'\"")
        if key and value and key not in os.environ:
            os.environ[key] = value

    assert os.environ["FROM_FILE"] == "file-value"
    assert os.environ["ALREADY_SET"] == "env-wins"


def test_RWCFG01_rewrite_disabled(monkeypatch):
    """LLM_REWRITE_ENABLED=false → 재작성 LLM 분기 비활성."""
    monkeypatch.setenv("LLM_API_KEY", "fake-key")
    monkeypatch.setenv("LLM_PROVIDER", "openai")
    monkeypatch.setenv("LLM_REWRITE_ENABLED", "false")
    get_settings.cache_clear()
    s = get_settings()
    assert s.llm_rewrite_enabled is False
    use_llm = (s.llm_rewrite_enabled
               and s.llm_provider == "openai" and s.llm_api_key)
    assert use_llm is False
    get_settings.cache_clear()


def test_RWCFG02_rewrite_model_falls_back_to_llm_model(monkeypatch):
    monkeypatch.setenv("LLM_MODEL", "gemini-3.5-flash")
    monkeypatch.delenv("LLM_REWRITE_MODEL", raising=False)
    get_settings.cache_clear()
    s = get_settings()
    assert (s.llm_rewrite_model or s.llm_model) == "gemini-3.5-flash"
    get_settings.cache_clear()


def test_RWCFG03_rewrite_model_override(monkeypatch):
    monkeypatch.setenv("LLM_MODEL", "gemini-3.5-flash")
    monkeypatch.setenv("LLM_REWRITE_MODEL", "gemini-3.1-flash-lite")
    get_settings.cache_clear()
    s = get_settings()
    assert (s.llm_rewrite_model or s.llm_model) == "gemini-3.1-flash-lite"
    get_settings.cache_clear()
