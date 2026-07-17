"""
API 게이트웨이 진입점 + 컴포넌트 조립.

미들웨어 순서(바깥→안쪽): SafeError(최외곽) → Trace → 라우트.
lifespan에서 실제 구현(임베딩·Qdrant·LLM·수집)을 주입한다.
프론트엔드(정적)는 같은 앱에서 StaticFiles로 서빙(MVP: 별도 컨테이너 불필요).
"""
from __future__ import annotations

import logging
import os
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles

from harag.api.middleware import TraceMiddleware, SafeErrorMiddleware
from harag.api import routes_query, routes_ingest
from harag.config.settings import get_settings

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("harag.api")

# /health Redis 관측용(선택)
_redis_client = None
_ingest_queue = None


def _build_and_inject() -> None:
    """설정을 읽어 파이프라인·수집 서비스를 만들어 주입한다."""
    from harag.embedding.api_embedder import build_embedding_model, build_morph
    from harag.embedding.embedder import HybridEmbedder
    from harag.retrieval.qdrant_store import QdrantVectorStore
    from harag.parsing.document_parser import DocumentParser
    from harag.chunking.chunker import StructuralChunker
    from harag.generation.generator import AnswerGenerator
    from harag.llm.factory import build_llm_client
    from harag.llm.http_transport import OpenAIChatTransport
    from harag.llm.local_rerank import (
        LexicalCrossEncoder, LLMCrossEncoder, IdentityRewriteLLM, LLMRewriteLLM,
    )
    from harag.llm.http_rerank import HttpCrossEncoder
    from harag.retrieval.reranker import CrossEncoderReranker
    from harag.retrieval.rewriter import QueryRewriter, ConversationStore
    from harag.api.pipeline import QueryPipelineImpl
    from harag.api.ingest import InProcessIngest
    from harag.api.deps import set_query_pipeline, set_ingest, set_vector_store
    from harag.storage.metadata_store import MetadataStore
    from harag.storage.redis_client import get_redis
    from harag.storage.redis_cache import DocStatusCache
    from harag.storage.redis_ingest_queue import RedisIngestQueue
    from harag.storage.redis_stores import RedisConversationStore
    from harag.api import ratelimit as ratelimit_mod
    from harag.api import daily_budget as daily_budget_mod
    from harag.config.settings import resolve_rerank_defaults
    from harag.security.pii import PiiMasker
    from harag.storage.object_store_factory import build_object_store
    from harag.indexing.version_coord import DocumentVersionCoordinator

    settings = get_settings()

    # Phase A: 회수 천장 — local hash는 데모 전용(운영 정확도 상한을 깎음).
    if settings.embedding_provider == "local" or not settings.embedding_api_key:
        logger.warning(
            "embedding=local(hash) — 데모용. 운영 정확도를 위해 "
            "EMBEDDING_API_KEY + OpenAI호환(Gemini 등) 실임베딩을 쓰세요.")

    # 문서 등록부: SQLite 파일이면 data/ 보장. DATABASE_URL로 Postgres 전환 가능.
    db_url = settings.database_url
    if db_url.startswith("sqlite:///./") or db_url.startswith("sqlite:///"):
        # sqlite:///./data/harag.db → ./data
        raw_path = db_url.removeprefix("sqlite:///")
        if raw_path and raw_path != ":memory:":
            Path(raw_path).parent.mkdir(parents=True, exist_ok=True)
    metadata = MetadataStore(dsn=db_url)

    # Redis(선택): Streams 큐·상태캐시·대화이력·레이트제한. URL 없거나 실패 시 인메모리.
    global _redis_client, _ingest_queue
    if settings.redis_url and not os.environ.get("HARAG_SPOOL_DIR"):
        raise RuntimeError(
            "REDIS_URL이 설정됐는데 HARAG_SPOOL_DIR이 없습니다. "
            "API와 워커가 같은 스풀 디렉터리를 공유해야 합니다 "
            "(예: export HARAG_SPOOL_DIR=/tmp/harag_spool).")

    redis = get_redis(settings.redis_url) if settings.redis_url else None
    prefix = settings.redis_key_prefix
    status_cache = (
        DocStatusCache(redis, prefix=prefix) if redis is not None else None)
    ingest_queue = (
        RedisIngestQueue(
            redis,
            prefix=prefix,
            stream_maxlen=settings.redis_stream_maxlen,
            visibility_sec=settings.ingest_visibility_sec,
            max_attempts=settings.ingest_max_attempts,
        ) if redis is not None else None)
    _redis_client = redis
    _ingest_queue = ingest_queue
    if redis is not None:
        ratelimit_mod.configure_redis(redis, prefix=prefix)
        daily_budget_mod.configure_redis(redis, prefix=prefix)
        conv_store = RedisConversationStore(redis, prefix=prefix)
    else:
        conv_store = ConversationStore()

    pii_masker = PiiMasker() if settings.pii_mask_enabled else None
    object_store = build_object_store(settings)
    version_coord = DocumentVersionCoordinator(metadata)

    embedding_model = build_embedding_model(settings)
    # 적재(embedder)와 질의(store)가 반드시 같은 토크나이저를 공유해야
    # sparse 인덱스가 일치한다(kiwipiepy 설치 시 형태소, 아니면 어절 폴백).
    morph = build_morph()
    embedder = HybridEmbedder(embedding_model, morph)
    store = QdrantVectorStore(
        embedding_model=embedding_model,
        dim=embedding_model.dim,
        collection=settings.qdrant_collection,
        url=settings.qdrant_url_or_none,
        api_key=settings.qdrant_api_key or None,
        disk_budget_mb=settings.qdrant_disk_budget_mb,
        payload_bytes_per_point=settings.qdrant_payload_bytes_per_point,
        morph=morph,
        sparse_bytes_per_point=settings.qdrant_sparse_bytes_per_point,
        segment_factor=settings.qdrant_segment_factor,
        upsert_batch_size=settings.qdrant_upsert_batch_size,
        hybrid_prefetch_mult=settings.hybrid_prefetch_mult,
        count_cache_ttl_s=settings.qdrant_count_cache_ttl_s,
    )
    if (settings.embedding_provider == "openai"
            and settings.embedding_dim > 768
            and not settings.embedding_send_dimensions):
        logger.warning(
            "EMBEDDING_DIM=%d without EMBEDDING_SEND_DIMENSIONS — "
            "무료 Qdrant 디스크를 아끼려면 EMBEDDING_DIM=512 + "
            "EMBEDDING_SEND_DIMENSIONS=true 권장(컬렉션 재생성 필요)",
            settings.embedding_dim)
    llm = build_llm_client(settings)
    generator = AnswerGenerator(llm=llm, min_score=settings.min_score)

    # 리랭커 우선순위: LLM pointwise → TEI HTTP CE → Lexical 폴백.
    # top_k로 넓게 회수 → top_n으로 정밀 컷.
    blend, rerank_min = resolve_rerank_defaults(settings)
    rerank_mode = "off"
    reranker = None
    llm_rerank = (settings.rerank_enabled and settings.rerank_llm_enabled
                  and settings.llm_provider == "openai" and settings.llm_api_key)
    if llm_rerank:
        rerank_transport = OpenAIChatTransport(
            api_base=settings.llm_api_base, api_key=settings.llm_api_key)
        rerank_model = (settings.rerank_llm_model
                        or settings.llm_rewrite_model or settings.llm_model)
        ce_model = LLMCrossEncoder(rerank_transport, rerank_model)
        # LLM 점수는 질의-청크를 함께 본 강한 신호 → retrieval 블렌드 낮춤
        blend, rerank_min = 0.3, 0.0
        rerank_mode = "llm"
        reranker = CrossEncoderReranker(
            model=ce_model,
            top_n=min(settings.rerank_top_n, settings.top_k),
            min_score=rerank_min,
            retrieval_blend=blend,
        )
    elif settings.rerank_enabled:
        top_n = min(settings.rerank_top_n, settings.top_k)
        if settings.reranker_server_url:
            ce_model = HttpCrossEncoder(
                settings.reranker_server_url,
                timeout_ms=settings.rerank_timeout_ms,
            )
            rerank_mode = "http"
        else:
            ce_model = LexicalCrossEncoder()
            rerank_mode = "lexical"
        reranker = CrossEncoderReranker(
            model=ce_model,
            top_n=top_n,
            min_score=rerank_min,
            retrieval_blend=blend,
        )

    # 멀티턴 재작성: LLM 키 있으면 지시어 해소, 없으면 identity(원본) 폴백.
    # 재작성은 질의마다 LLM을 한 번 더 부르므로(쿼터·지연), 설정으로 끄거나
    # 답변 모델보다 싸고 빠른 모델(LLM_REWRITE_MODEL)로 분리할 수 있다.
    if (settings.llm_rewrite_enabled
            and settings.llm_provider == "openai" and settings.llm_api_key):
        rewrite_transport = OpenAIChatTransport(
            api_base=settings.llm_api_base, api_key=settings.llm_api_key)
        rewrite_model = settings.llm_rewrite_model or settings.llm_model
        rewrite_llm = LLMRewriteLLM(
            rewrite_transport, rewrite_model,
            max_chars=settings.llm_rewrite_max_chars)
    else:
        rewrite_llm = IdentityRewriteLLM()
        if settings.llm_rewrite_enabled:
            logger.warning(
                "rewrite=Identity — LLM 키 없음. 멀티턴 지시어 해소 불가 "
                "(리랭커로 복구 안 됨). LLM_API_KEY 또는 LLM_REWRITE_ENABLED=false")
    rewriter = QueryRewriter(rewrite_llm, conv_store)

    pipeline = QueryPipelineImpl(
        retriever=store, generator=generator,
        reranker=reranker, rewriter=rewriter,
        top_k=settings.top_k,
        under_load_inflight=settings.rerank_under_load_inflight,
        context_dedupe=settings.context_dedupe,
        metadata=metadata,
    )
    ingest = InProcessIngest(
        parser=DocumentParser(), chunker=StructuralChunker(),
        embedder=embedder, store=store, metadata=metadata,
        queue=ingest_queue, status_cache=status_cache,
        pii_masker=pii_masker, object_store=object_store,
        version_coord=version_coord)

    set_query_pipeline(pipeline)
    set_ingest(ingest)
    set_vector_store(store)

    hybrid_flag = getattr(store, "_hybrid", None)
    logger.info(
        "assembled: embedding=%s(dim=%d) llm=%s qdrant=%s hybrid=%s db=%s "
        "redis=%s ingest=%s rerank=%s(blend=%.2f,min=%.3f,top_n=%d) rewrite=%s",
        embedding_model.model_id, embedding_model.dim,
        settings.llm_provider, settings.qdrant_url or ":memory:",
        hybrid_flag,
        "sqlite" if db_url.startswith("sqlite") else "postgres",
        "on" if redis is not None else "off",
        "queue+worker" if ingest_queue is not None else "in-process",
        rerank_mode, blend, rerank_min,
        min(settings.rerank_top_n, settings.top_k) if settings.rerank_enabled else 0,
        type(rewrite_llm).__name__)



@asynccontextmanager
async def lifespan(app: FastAPI):
    logger.info("startup: harag api gateway (MVP)")
    _build_and_inject()
    yield
    logger.info("shutdown: harag api gateway")


def _frontend_dir() -> Path | None:
    env = os.environ.get("FRONTEND_DIR")
    candidates = []
    if env:
        candidates.append(Path(env))
    # repo 루트 추정: src/harag/api/main.py → parents[3]
    candidates.append(Path(__file__).resolve().parents[3] / "frontend")
    candidates.append(Path.cwd() / "frontend")
    for c in candidates:
        if c.is_dir() and (c / "index.html").is_file():
            return c
    return None


def create_app() -> FastAPI:
    app = FastAPI(title="harag — 한글 행정문서 RAG (PDF MVP)",
                  version="0.1.0-mvp", lifespan=lifespan)

    # 미들웨어: 나중에 add된 것이 더 바깥. SafeError를 가장 바깥에.
    app.add_middleware(TraceMiddleware)
    app.add_middleware(SafeErrorMiddleware)

    # CORS: 프론트는 같은 오리진에서 서빙되므로 기본은 교차 오리진 차단.
    # 별도 도메인 프론트가 생기면 ALLOWED_ORIGINS(콤마 구분)로만 개방한다.
    origins = get_settings().allowed_origins
    if origins:
        app.add_middleware(
            CORSMiddleware, allow_origins=list(origins), allow_methods=["*"],
            allow_headers=["*"], expose_headers=["X-Trace-Id"],
        )

    app.include_router(routes_query.router)
    app.include_router(routes_ingest.router)

    @app.get("/health")
    async def health():
        import asyncio
        body = {"status": "ok"}
        # 용량 사용률(무료 사양 한도 접근을 미리 보이게). 조회 실패 시 생략.
        # count는 블로킹 HTTP라 이벤트 루프를 막지 않게 오프로드.
        from harag.api.deps import get_vector_store_or_none
        store = get_vector_store_or_none()
        if store is not None:
            cap = await asyncio.to_thread(store.capacity_status)
            if cap is not None:
                body["capacity"] = cap

        def _redis_health():
            if _redis_client is None:
                return {"enabled": False}
            try:
                pong = bool(_redis_client.ping())
            except Exception:  # noqa: BLE001
                return {"enabled": True, "ok": False}
            out = {"enabled": True, "ok": pong}
            if _ingest_queue is not None:
                try:
                    out.update(_ingest_queue.stats())
                except Exception:  # noqa: BLE001
                    pass
            return out

        body["redis"] = await asyncio.to_thread(_redis_health)
        return body

    @app.get("/metrics")
    async def metrics():
        from fastapi.responses import PlainTextResponse
        from harag.observability.metrics_export import prometheus_text
        return PlainTextResponse(
            prometheus_text(), media_type="text/plain; version=0.0.4")

    # 프론트엔드 정적 서빙(있을 때만). API 라우트 뒤에 mount → /v1·/health 우선.
    fe = _frontend_dir()
    if fe is not None:
        app.mount("/", StaticFiles(directory=str(fe), html=True), name="frontend")
        logger.info("frontend mounted from %s", fe)
    else:
        logger.warning("frontend 디렉터리 미발견 — API만 서빙")

    return app


app = create_app()
