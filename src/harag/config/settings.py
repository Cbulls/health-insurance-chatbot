"""
설정 — 환경변수로 늦게 바인딩(코드/이미지에 시크릿 넣지 않음).

MVP 원칙: 키가 없으면 로컬 폴백으로 동작한다.
  - embedding_provider=local  → 결정적 해시 임베딩(오프라인 데모 가능)
  - llm_provider=local        → 추출형(extractive) 로컬 답변(오프라인 데모 가능)
운영은 provider=openai(호환) + API 키 주입으로 전환.
"""
from __future__ import annotations

import os
from dataclasses import dataclass
from functools import lru_cache


def _get(key: str, default: str = "") -> str:
    return os.environ.get(key, default)


def _get_int(key: str, default: int) -> int:
    try:
        return int(os.environ.get(key, str(default)))
    except ValueError:
        return default


def _get_float(key: str, default: float) -> float:
    try:
        return float(os.environ.get(key, str(default)))
    except ValueError:
        return default


@dataclass(frozen=True)
class Settings:
    # ── 임베딩 ──
    embedding_provider: str        # "openai" | "local"
    embedding_api_base: str
    embedding_api_key: str
    embedding_model: str
    embedding_dim: int

    # ── LLM ──
    llm_provider: str              # "openai" | "local"
    llm_api_base: str
    llm_api_key: str
    llm_model: str

    # ── Qdrant ──
    qdrant_url: str                # 비면 인메모리(:memory:)
    qdrant_collection: str

    # ── 검색/생성 파라미터 ──
    top_k: int
    min_score: float

    # ── 업로드 ──
    max_upload_bytes: int

    @property
    def qdrant_url_or_none(self) -> str | None:
        return self.qdrant_url or None


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    emb_provider = _get("EMBEDDING_PROVIDER", "").lower()
    emb_key = _get("EMBEDDING_API_KEY")
    # provider 미지정 시: 키가 있으면 openai, 없으면 local 폴백
    if not emb_provider:
        emb_provider = "openai" if emb_key else "local"

    llm_provider = _get("LLM_PROVIDER", "").lower()
    llm_key = _get("LLM_API_KEY")
    if not llm_provider:
        llm_provider = "openai" if llm_key else "local"

    # 임베딩 차원: openai text-embedding-3-small=1536, local 기본 512
    default_dim = 1536 if emb_provider == "openai" else 512
    emb_dim = _get_int("EMBEDDING_DIM", default_dim)

    return Settings(
        embedding_provider=emb_provider,
        embedding_api_base=_get("EMBEDDING_API_BASE", "https://api.openai.com/v1"),
        embedding_api_key=emb_key,
        embedding_model=_get("EMBEDDING_MODEL", "text-embedding-3-small"),
        embedding_dim=emb_dim,
        llm_provider=llm_provider,
        llm_api_base=_get("LLM_API_BASE", "https://api.openai.com/v1"),
        llm_api_key=llm_key,
        llm_model=_get("LLM_MODEL", "gpt-4o-mini"),
        qdrant_url=_get("QDRANT_URL", ""),
        qdrant_collection=_get("QDRANT_COLLECTION", "harag_pdf_mvp"),
        top_k=_get_int("TOP_K", 20),
        min_score=_get_float("MIN_SCORE", 0.15),
        max_upload_bytes=_get_int("MAX_UPLOAD_BYTES", 100 * 1024 * 1024),
    )
