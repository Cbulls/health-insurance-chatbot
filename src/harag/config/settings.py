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
from pathlib import Path


def _load_dotenv_once() -> None:
    """저장소 루트의 .env를 읽어 '아직 없는' 환경변수만 채운다.

    docker compose는 .env를 자동 주입하지만 로컬 uvicorn 실행은 아무도 안 읽어
    조용히 로컬 폴백으로 돌게 된다. 실제 환경변수가 항상 우선한다(덮어쓰지 않음).
    """
    # settings.py → config → harag → src → repo 루트
    candidates = [
        Path(__file__).resolve().parents[3] / ".env",
        Path.cwd() / ".env",
    ]
    for env_path in candidates:
        if not env_path.is_file():
            continue
        for line in env_path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, _, value = line.partition("=")
            key, value = key.strip(), value.strip().strip("'\"")
            if key and value and key not in os.environ:
                os.environ[key] = value
        return  # 첫 번째로 발견된 .env만 사용


_load_dotenv_once()


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


def _split_csv(value: str) -> tuple[str, ...]:
    return tuple(v.strip() for v in value.split(",") if v.strip())


def _get_bool(key: str, default: bool = False) -> bool:
    raw = os.environ.get(key)
    if raw is None:
        return default
    return raw.strip().lower() in ("1", "true", "yes", "on")


@dataclass(frozen=True)
class Settings:
    # ── 임베딩 ──
    embedding_provider: str        # "openai" | "local"
    embedding_api_base: str
    embedding_api_key: str
    embedding_model: str
    embedding_dim: int
    # OpenAI /embeddings에 dimensions 파라미터 전달(차원 축소 → 저장량 절감).
    # OpenAI 외 호환 프로바이더는 미지원일 수 있어 기본 꺼짐.
    embedding_send_dimensions: bool

    # ── LLM ──
    llm_provider: str              # "openai" | "local"
    llm_api_base: str
    llm_api_key: str
    llm_model: str
    # 멀티턴 질의 재작성: 질의마다 LLM을 한 번 더 부른다(쿼터·지연 비용).
    # 끄거나(false), 답변보다 싸고 빠른 모델로 분리할 수 있다.
    llm_rewrite_enabled: bool
    llm_rewrite_model: str         # 비면 llm_model 사용

    # ── Qdrant ──
    qdrant_url: str                # 비면 인메모리(:memory:)
    qdrant_api_key: str            # Qdrant Cloud 인증 키(로컬/도커는 비움)
    qdrant_collection: str
    # 디스크 예산(MB). 이 예산을 넘길 인덱싱은 거부(무료 사양 초과 방지).
    # 0이면 가드 비활성. 기본 3072 = 무료 4GiB의 75%(공식 80% 경고선 아래).
    qdrant_disk_budget_mb: int
    # 청크(포인트)당 payload 예상 크기(바이트) — 원문 텍스트+메타.
    qdrant_payload_bytes_per_point: int

    # ── 검색/생성 파라미터 ──
    top_k: int
    min_score: float
    # LLM 리랭커: 질의마다 저비용 LLM 한 번으로 top-K를 정밀 재순위.
    # 끄면(false) 어휘 겹침 리랭커 폴백. LLM 키 없으면 자동 폴백.
    rerank_llm_enabled: bool
    rerank_llm_model: str          # 비면 llm_rewrite_model → llm_model 순

    # ── 업로드 ──
    max_upload_bytes: int

    # ── 보호 장치 ──
    rate_limit_qpm: int            # owner별 분당 요청 상한(0이면 비활성)
    allowed_origins: tuple[str, ...]  # CORS 허용 오리진(비면 CORS 미들웨어 생략)

    # ── 인증(JWT, 선택) ──
    auth_jwt_secret: str           # 설정 시 Bearer JWT 검증 강제(fail-closed)
    auth_jwt_algorithms: tuple[str, ...]
    auth_jwt_audience: str
    auth_jwt_issuer: str

    # ── 문서 등록부(메타 DB) ──
    # 기본: sqlite:///./data/harag.db. PostgreSQL 예:
    #   postgresql+psycopg://harag:harag@localhost:5432/harag
    database_url: str

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
        embedding_send_dimensions=_get_bool("EMBEDDING_SEND_DIMENSIONS", False),
        llm_provider=llm_provider,
        llm_api_base=_get("LLM_API_BASE", "https://api.openai.com/v1"),
        llm_api_key=llm_key,
        llm_model=_get("LLM_MODEL", "gpt-4o-mini"),
        llm_rewrite_enabled=_get_bool("LLM_REWRITE_ENABLED", True),
        llm_rewrite_model=_get("LLM_REWRITE_MODEL", ""),
        qdrant_url=_get("QDRANT_URL", ""),
        qdrant_api_key=_get("QDRANT_API_KEY", ""),
        qdrant_collection=_get("QDRANT_COLLECTION", "harag_pdf_mvp"),
        qdrant_disk_budget_mb=_get_int("QDRANT_DISK_BUDGET_MB", 3072),
        qdrant_payload_bytes_per_point=_get_int(
            "QDRANT_PAYLOAD_BYTES_PER_POINT", 2048),
        top_k=_get_int("TOP_K", 20),
        min_score=_get_float("MIN_SCORE", 0.15),
        rerank_llm_enabled=_get_bool("RERANK_LLM_ENABLED", True),
        rerank_llm_model=_get("RERANK_LLM_MODEL", ""),
        max_upload_bytes=_get_int("MAX_UPLOAD_BYTES", 100 * 1024 * 1024),
        rate_limit_qpm=_get_int("RATE_LIMIT_QPM", 20),
        allowed_origins=_split_csv(_get("ALLOWED_ORIGINS", "")),
        auth_jwt_secret=_get("AUTH_JWT_SECRET", ""),
        auth_jwt_algorithms=_split_csv(_get("AUTH_JWT_ALGORITHMS", "HS256")),
        auth_jwt_audience=_get("AUTH_JWT_AUDIENCE", ""),
        auth_jwt_issuer=_get("AUTH_JWT_ISSUER", ""),
        database_url=_get("DATABASE_URL", "sqlite:///./data/harag.db")
                       or "sqlite:///./data/harag.db",
    )
