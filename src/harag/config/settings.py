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
    # Gemini/유료 폭주 방어 — 답변 LLM 예상 비용 상한(휴리스틱 토큰×단가).
    llm_max_cost_per_query_usd: float
    llm_cost_per_1k_tokens: float
    # 429 재시도 횟수. 무료 티어는 1 이하 권장(쿼터 소진 시 증폭 방지).
    llm_max_retries: int
    # 재작성 프롬프트 문자 상한(이력+질의). 초과 시 이력을 자른다.
    llm_rewrite_max_chars: int

    # ── Qdrant ──
    qdrant_url: str                # 비면 인메모리(:memory:)
    qdrant_api_key: str            # Qdrant Cloud 인증 키(로컬/도커는 비움)
    qdrant_collection: str
    # 디스크 예산(MB). 이 예산을 넘길 인덱싱은 거부(무료 사양 초과 방지).
    # 0이면 가드 비활성. 기본 3072 = 무료 4GiB의 75%(공식 80% 경고선 아래).
    qdrant_disk_budget_mb: int
    # 청크(포인트)당 payload 예상 크기(바이트) — 슬림 v2 기준.
    qdrant_payload_bytes_per_point: int
    # sparse 벡터 예상 바이트(하이브리드 용량 공식). dense-only면 무시.
    qdrant_sparse_bytes_per_point: int
    # 세그먼트·인덱스 오버헤드 배수(용량 공식).
    qdrant_segment_factor: float
    # upsert 배치 크기(대문서 HTTP 바디·타임아웃 완화).
    qdrant_upsert_batch_size: int
    # 하이브리드 prefetch = top_k * mult (기본 2).
    hybrid_prefetch_mult: int
    # approximate count 캐시 TTL(초). 0이면 캐시 없음.
    qdrant_count_cache_ttl_s: float

    # ── 검색/생성 파라미터 ──
    top_k: int
    min_score: float
    # LLM 리랭커: 질의마다 저비용 LLM 한 번으로 top-K를 정밀 재순위.
    # 끄면(false) 어휘 겹침 리랭커 폴백. LLM 키 없으면 자동 폴백.
    rerank_llm_enabled: bool
    rerank_llm_model: str          # 비면 llm_rewrite_model → llm_model 순

    # ── 리랭커 ──
    # enabled + URL → HttpCrossEncoder(TEI). URL 없으면 Lexical 폴백(데모).
    rerank_enabled: bool
    reranker_server_url: str
    rerank_top_n: int
    rerank_timeout_ms: int
    rerank_min_score: float          # 리랭커 자체 필터(RR-02). CE면 의미 있게 >0
    # 0=CE만, 1=retrieval만. Lexical 폴백 기본 0.7, HTTP CE면 기본 0.
    retrieval_blend: float
    rerank_under_load_inflight: int  # 동시 질의 ≥ 이 값이면 under_load(RR-04)
    context_dedupe: bool             # 생성 전 중복 청크 제거

    # ── 업로드 ──
    max_upload_bytes: int

    # ── 보호 장치 ──
    rate_limit_qpm: int            # owner별 분당 요청 상한(0이면 비활성)
    allowed_origins: tuple[str, ...]  # CORS 허용 오리진(비면 CORS 미들웨어 생략)

    # ── 인증(JWT / OIDC, 선택) ──
    auth_jwt_secret: str           # 설정 시 Bearer JWT 검증 강제(fail-closed)
    auth_jwt_algorithms: tuple[str, ...]
    auth_jwt_audience: str
    auth_jwt_issuer: str

    # ── 문서 등록부(메타 DB) ──
    # 기본: sqlite:///./data/harag.db. PostgreSQL 예:
    #   postgresql+psycopg://harag:harag@localhost:5432/harag
    database_url: str

    # ── Redis(선택) ──
    # 비우면 인메모리 큐/캐시/레이트·대화 스토어 폴백.
    # compose: redis://redis:6379/0  /  로컬: redis://localhost:6379/0
    redis_url: str
    redis_key_prefix: str          # 키 네임스페이스(기본 harag:)
    redis_stream_maxlen: int       # 인제스트 스트림 approximate trim
    ingest_visibility_sec: int     # PEL idle 후 reclaim(초)
    ingest_max_attempts: int       # 실패 재시도 상한 후 DLQ

    # ── 파서·변환 ──
    parse_min_table_recovery: float  # HWP5 표 복원 경고 임계(미만이면 UI 경고)
    enable_doc_convert: bool         # .doc → LibreOffice DOCX
    libreoffice_bin: str
    doc_convert_timeout_sec: int

    # ── 고도화(기본값 있음 — 테스트 Settings(**base) 호환) ──
    auth_oidc_jwks_url: str = ""
    auth_allow_demo_owner: bool = True
    # SSO UX (authorization code)
    auth_oidc_client_id: str = ""
    auth_oidc_client_secret: str = ""
    auth_oidc_authorize_url: str = ""
    auth_oidc_token_url: str = ""
    auth_oidc_redirect_uri: str = ""
    auth_oidc_scopes: str = "openid profile"
    auth_oidc_mock: bool = False
    auth_frontend_redirect: str = "/"
    pii_mask_enabled: bool = True
    daily_question_budget: int = 0
    daily_token_budget: int = 0
    object_store_endpoint: str = ""
    object_store_access_key: str = ""
    object_store_secret_key: str = ""
    object_store_bucket: str = "harag-originals"
    object_store_region: str = "us-east-1"
    ocr_scan_ratio_threshold: float = 0.15

    # ── SEC-02 v2 인젝션 방어 ──
    injection_defense_enabled: bool = True
    injection_datamark_enabled: bool = True
    injection_hard_refuse_score: int = 2
    injection_ingest_action: str = "tag"  # tag | quarantine
    injection_scan_query: bool = True
    injection_canary_enabled: bool = True

    # ── 스프린트 Q: 컨텍스트·리랭크·임베딩 캐시 ──
    context_expand: bool = True
    context_max_tokens: int = 3500
    rerank_max_candidates: int = 12
    rerank_max_candidates_under_load: int = 8
    query_embed_cache_size: int = 256
    query_embed_cache_ttl_s: float = 300.0

    @property
    def qdrant_url_or_none(self) -> str | None:
        return self.qdrant_url or None

    @property
    def redis_url_or_none(self) -> str | None:
        return self.redis_url or None


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
        # 무료 티어 기본: 낮은 상한·적은 재시도. 유료 전환 시 env로 완화.
        llm_max_cost_per_query_usd=_get_float("LLM_MAX_COST_PER_QUERY_USD", 0.05),
        llm_cost_per_1k_tokens=_get_float("LLM_COST_PER_1K_TOKENS", 0.01),
        llm_max_retries=_get_int("LLM_MAX_RETRIES", 1),
        llm_rewrite_max_chars=_get_int("LLM_REWRITE_MAX_CHARS", 2000),
        qdrant_url=_get("QDRANT_URL", ""),
        qdrant_api_key=_get("QDRANT_API_KEY", ""),
        qdrant_collection=_get("QDRANT_COLLECTION", "harag_pdf_mvp"),
        qdrant_disk_budget_mb=_get_int("QDRANT_DISK_BUDGET_MB", 3072),
        qdrant_payload_bytes_per_point=_get_int(
            "QDRANT_PAYLOAD_BYTES_PER_POINT", 1536),
        qdrant_sparse_bytes_per_point=_get_int(
            "QDRANT_SPARSE_BYTES_PER_POINT", 640),
        qdrant_segment_factor=_get_float("QDRANT_SEGMENT_FACTOR", 1.15),
        qdrant_upsert_batch_size=_get_int("QDRANT_UPSERT_BATCH_SIZE", 64),
        hybrid_prefetch_mult=_get_int("HYBRID_PREFETCH_MULT", 2),
        qdrant_count_cache_ttl_s=_get_float("QDRANT_COUNT_CACHE_TTL_S", 5.0),
        top_k=_get_int("TOP_K", 20),
        min_score=_get_float("MIN_SCORE", 0.15),
        rerank_llm_enabled=_get_bool("RERANK_LLM_ENABLED", True),
        rerank_llm_model=_get("RERANK_LLM_MODEL", ""),
        rerank_enabled=_get_bool("RERANK_ENABLED", True),
        reranker_server_url=_get("RERANKER_SERVER_URL", ""),
        rerank_top_n=_get_int("RERANK_TOP_N", 5),
        rerank_timeout_ms=_get_int("RERANK_TIMEOUT_MS", 3000),
        rerank_min_score=_get_float("RERANK_MIN_SCORE", -1.0),  # -1=어댑터별 기본
        # blend: 미지정 시 URL 유무로 결정(아래 후처리)
        retrieval_blend=_get_float("RETRIEVAL_BLEND", -1.0),
        rerank_under_load_inflight=_get_int("RERANK_UNDER_LOAD_INFLIGHT", 4),
        context_dedupe=_get_bool("CONTEXT_DEDUPE", True),
        max_upload_bytes=_get_int("MAX_UPLOAD_BYTES", 100 * 1024 * 1024),
        # Gemini 무료 ~10 RPM, 질문당 최대 3콜 → 기본 5 이하.
        rate_limit_qpm=_get_int("RATE_LIMIT_QPM", 5),
        allowed_origins=_split_csv(_get("ALLOWED_ORIGINS", "")),
        auth_jwt_secret=_get("AUTH_JWT_SECRET", ""),
        auth_jwt_algorithms=_split_csv(_get("AUTH_JWT_ALGORITHMS", "HS256")),
        auth_jwt_audience=_get("AUTH_JWT_AUDIENCE", ""),
        auth_jwt_issuer=_get("AUTH_JWT_ISSUER", ""),
        auth_oidc_jwks_url=_get("AUTH_OIDC_JWKS_URL", ""),
        auth_allow_demo_owner=_get_bool("AUTH_ALLOW_DEMO_OWNER", True),
        auth_oidc_client_id=_get("AUTH_OIDC_CLIENT_ID", ""),
        auth_oidc_client_secret=_get("AUTH_OIDC_CLIENT_SECRET", ""),
        auth_oidc_authorize_url=_get("AUTH_OIDC_AUTHORIZE_URL", ""),
        auth_oidc_token_url=_get("AUTH_OIDC_TOKEN_URL", ""),
        auth_oidc_redirect_uri=_get("AUTH_OIDC_REDIRECT_URI", ""),
        auth_oidc_scopes=_get("AUTH_OIDC_SCOPES", "openid profile")
                         or "openid profile",
        auth_oidc_mock=_get_bool("AUTH_OIDC_MOCK", False),
        auth_frontend_redirect=_get("AUTH_FRONTEND_REDIRECT", "/") or "/",
        database_url=_get("DATABASE_URL", "sqlite:///./data/harag.db")
                       or "sqlite:///./data/harag.db",
        redis_url=_get("REDIS_URL", ""),
        redis_key_prefix=_get("REDIS_KEY_PREFIX", "harag:") or "harag:",
        redis_stream_maxlen=_get_int("REDIS_STREAM_MAXLEN", 10000),
        ingest_visibility_sec=_get_int("INGEST_VISIBILITY_SEC", 120),
        ingest_max_attempts=_get_int("INGEST_MAX_ATTEMPTS", 3),
        parse_min_table_recovery=_get_float("PARSE_MIN_TABLE_RECOVERY", 0.5),
        enable_doc_convert=_get_bool("ENABLE_DOC_CONVERT", True),
        libreoffice_bin=_get("LIBREOFFICE_BIN", "soffice") or "soffice",
        doc_convert_timeout_sec=_get_int("DOC_CONVERT_TIMEOUT_SEC", 120),
        pii_mask_enabled=_get_bool("PII_MASK_ENABLED", True),
        daily_question_budget=_get_int("DAILY_QUESTION_BUDGET", 0),
        daily_token_budget=_get_int("DAILY_TOKEN_BUDGET", 0),
        object_store_endpoint=_get("OBJECT_STORE_ENDPOINT", ""),
        object_store_access_key=_get("OBJECT_STORE_ACCESS_KEY", ""),
        object_store_secret_key=_get("OBJECT_STORE_SECRET_KEY", ""),
        object_store_bucket=_get("OBJECT_STORE_BUCKET", "harag-originals")
                            or "harag-originals",
        object_store_region=_get("OBJECT_STORE_REGION", "us-east-1")
                            or "us-east-1",
        ocr_scan_ratio_threshold=_get_float("OCR_SCAN_RATIO_THRESHOLD", 0.15),
        injection_defense_enabled=_get_bool("INJECTION_DEFENSE_ENABLED", True),
        injection_datamark_enabled=_get_bool("INJECTION_DATAMARK_ENABLED", True),
        injection_hard_refuse_score=_get_int("INJECTION_HARD_REFUSE_SCORE", 2),
        injection_ingest_action=_get("INJECTION_INGEST_ACTION", "tag") or "tag",
        injection_scan_query=_get_bool("INJECTION_SCAN_QUERY", True),
        injection_canary_enabled=_get_bool("INJECTION_CANARY_ENABLED", True),
        context_expand=_get_bool("CONTEXT_EXPAND", True),
        context_max_tokens=_get_int("CONTEXT_MAX_TOKENS", 3500),
        rerank_max_candidates=_get_int("RERANK_MAX_CANDIDATES", 12),
        rerank_max_candidates_under_load=_get_int(
            "RERANK_MAX_CANDIDATES_UNDER_LOAD", 8),
        query_embed_cache_size=_get_int("QUERY_EMBED_CACHE_SIZE", 256),
        query_embed_cache_ttl_s=_get_float("QUERY_EMBED_CACHE_TTL_S", 300.0),
    )


def resolve_rerank_defaults(settings: Settings) -> tuple[float, float]:
    """(retrieval_blend, rerank_min_score) — env -1 센티널을 어댑터별 기본으로.

    HTTP CE: blend=0, min_score=settings.min_score(생성 게이트와 한 축).
    Lexical: blend=0.7(한↔영 붕괴 방어), min_score=0.0(겹침 0이 흔해 필터 off).
    """
    use_http = bool(settings.rerank_enabled and settings.reranker_server_url)
    blend = settings.retrieval_blend
    if blend < 0:
        blend = 0.0 if use_http else 0.7
    rmin = settings.rerank_min_score
    if rmin < 0:
        rmin = settings.min_score if use_http else 0.0
    return blend, rmin
