"""
설정 — 코드 설계서 §9의 '미루는 결정'을 환경변수로 외부화한다.

원칙: 아직 확정 못 한 것(모델명, Qdrant 위치, SLO 수치)을 코드에 하드코딩하지 않는다.
환경변수로 주입하면, 결정이 내려졌을 때 코드 변경 없이 값만 바꾼다.
이것이 "조기 확정을 피한다"는 설계 원칙의 코드 구현이다.
"""
from __future__ import annotations

from functools import lru_cache
from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="HARAG_", env_file=".env", extra="ignore")

    # ── 인프라 위치 (§9: Qdrant self-host vs Cloud — 미정, 주입) ──
    qdrant_url: str = Field(default=":memory:")  # 개발 기본 인메모리
    postgres_dsn: str = Field(default="postgresql://localhost/harag")
    object_storage_endpoint: str = Field(default="")
    object_storage_bucket: str = Field(default="harag-originals")

    # ── 모델 (§9: 구체 모델명 — 벤치마크 후 확정, 주입) ──
    embedding_model_id: str = Field(default="UNSET-embedding-model")
    embedding_dim: int = Field(default=1024)
    reranker_model_id: str = Field(default="UNSET-reranker-model")
    embedding_server_url: str = Field(default="http://embedding-server:80")
    reranker_server_url: str = Field(default="http://reranker-server:80")

    # ── 외부 LLM (v4: 인터페이스 뒤, 프로바이더 교체 가능) ──
    llm_provider: str = Field(default="UNSET-llm-provider")
    llm_api_base: str = Field(default="")
    llm_model: str = Field(default="UNSET-llm-model")
    llm_rate_limit_rpm: int = Field(default=60)
    llm_timeout_sec: float = Field(default=30.0)
    llm_max_cost_per_query_usd: float = Field(default=0.50)  # 비용 폭주 방어

    # ── 인증 (§9: 자체 JWT vs OIDC — 미정, 주입) ──
    jwt_secret: str = Field(default="")          # 비면 IdP 미설정(fail-closed)
    jwt_algorithms: str = Field(default="HS256") # 쉼표 구분
    jwt_audience: str = Field(default="")
    jwt_issuer: str = Field(default="")

    # ── 검색·생성 파라미터 (§9: SLO 수치 — 실측 후 동결, 주입) ──
    retrieval_top_k: int = Field(default=40)        # 리랭킹 전 후보 수
    rerank_top_n: int = Field(default=5)            # 리랭킹 후 통과 수
    min_relevance_score: float = Field(default=0.5) # abstention 임계
    hnsw_ef: int = Field(default=128)               # recall↔지연 (실측 튜닝)

    # ── 청킹 (FR-3) ──
    max_chunk_tokens: int = Field(default=512)
    chunk_overlap_tokens: int = Field(default=64)

    # ── 품질 게이트 SLO (국면 C, 실측 후 동결) ──
    slo_min_recall_at_k: float = Field(default=0.85)
    slo_min_faithfulness: float = Field(default=0.90)
    slo_min_abstention_acc: float = Field(default=0.90)  # absent 단독 거부권

    # ── 파싱 품질 게이트 (국면 A PoC에서 동결) ──
    parse_min_table_recovery: float = Field(default=0.85)
    parse_max_broken_char_ratio: float = Field(default=0.05)


@lru_cache
def get_settings() -> Settings:
    """싱글톤 설정. UNSET-* 기본값은 '아직 결정 안 됨'을 명시적으로 드러낸다."""
    return Settings()
