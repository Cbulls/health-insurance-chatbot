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


def _build_and_inject() -> None:
    """설정을 읽어 파이프라인·수집 서비스를 만들어 주입한다."""
    from harag.embedding.api_embedder import build_embedding_model, SimpleMorph
    from harag.embedding.embedder import HybridEmbedder
    from harag.retrieval.qdrant_store import QdrantVectorStore
    from harag.parsing.pdf_parser import PdfParser
    from harag.chunking.chunker import StructuralChunker
    from harag.generation.generator import AnswerGenerator
    from harag.llm.factory import build_llm_client
    from harag.llm.http_transport import OpenAIChatTransport
    from harag.llm.local_rerank import (
        LexicalCrossEncoder, IdentityRewriteLLM, LLMRewriteLLM,
    )
    from harag.retrieval.reranker import CrossEncoderReranker
    from harag.retrieval.rewriter import QueryRewriter, ConversationStore
    from harag.api.pipeline import QueryPipelineImpl
    from harag.api.ingest import InProcessIngest
    from harag.api.deps import set_query_pipeline, set_ingest

    settings = get_settings()

    embedding_model = build_embedding_model(settings)
    embedder = HybridEmbedder(embedding_model, SimpleMorph())
    store = QdrantVectorStore(
        embedding_model=embedding_model,
        dim=embedding_model.dim,
        collection=settings.qdrant_collection,
        url=settings.qdrant_url_or_none,
    )
    llm = build_llm_client(settings)
    generator = AnswerGenerator(llm=llm, min_score=settings.min_score)

    # 리랭커: 로컬 어절-겹침 cross-encoder 폴백(키 없이도 실제 재순위).
    # top_k로 넓게 회수 → top_n으로 정밀 컷. reranker 서버 URL 생기면 교체.
    reranker = CrossEncoderReranker(
        model=LexicalCrossEncoder(),
        top_n=min(5, settings.top_k),
        min_score=0.0,
    )

    # 멀티턴 재작성: LLM 키 있으면 지시어 해소, 없으면 identity(원본) 폴백.
    if settings.llm_provider == "openai" and settings.llm_api_key:
        rewrite_transport = OpenAIChatTransport(
            api_base=settings.llm_api_base, api_key=settings.llm_api_key)
        rewrite_llm = LLMRewriteLLM(rewrite_transport, settings.llm_model)
    else:
        rewrite_llm = IdentityRewriteLLM()
    rewriter = QueryRewriter(rewrite_llm, ConversationStore())

    pipeline = QueryPipelineImpl(retriever=store, generator=generator,
                                 reranker=reranker, rewriter=rewriter,
                                 top_k=settings.top_k)
    ingest = InProcessIngest(
        parser=PdfParser(), chunker=StructuralChunker(),
        embedder=embedder, store=store)

    set_query_pipeline(pipeline)
    set_ingest(ingest)

    logger.info("assembled: embedding=%s(dim=%d) llm=%s qdrant=%s rerank=lexical rewrite=%s",
                embedding_model.model_id, embedding_model.dim,
                settings.llm_provider, settings.qdrant_url or ":memory:",
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
    app.add_middleware(
        CORSMiddleware, allow_origins=["*"], allow_methods=["*"],
        allow_headers=["*"], expose_headers=["X-Trace-Id"],
    )

    app.include_router(routes_query.router)
    app.include_router(routes_ingest.router)

    @app.get("/health")
    async def health():
        return {"status": "ok"}

    # 프론트엔드 정적 서빙(있을 때만). API 라우트 뒤에 mount → /v1·/health 우선.
    fe = _frontend_dir()
    if fe is not None:
        app.mount("/", StaticFiles(directory=str(fe), html=True), name="frontend")
        logger.info("frontend mounted from %s", fe)
    else:
        logger.warning("frontend 디렉터리 미발견 — API만 서빙")

    return app


app = create_app()
