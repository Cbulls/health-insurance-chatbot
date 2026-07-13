"""
API 게이트웨이 진입점.

미들웨어 순서가 중요하다(바깥→안쪽으로 요청 통과):
  SafeError(최외곽, 모든 예외 포획) → Trace(trace_id 부여) → 라우트
이 순서라야 에러 응답에도 trace_id가 붙는다.

생명주기: 시작 시 실제 구현(IdP·파이프라인·큐)을 주입. 지금은 골격이라 미주입.
"""
from __future__ import annotations

from contextlib import asynccontextmanager
import logging

from fastapi import FastAPI

from harag.api.middleware import TraceMiddleware, SafeErrorMiddleware
from harag.api import routes_query, routes_ingest
from harag.config.settings import get_settings

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("harag.api")


@asynccontextmanager
async def lifespan(app: FastAPI):
    settings = get_settings()
    logger.info("startup: harag api gateway")
    # ── 인증 IdP 주입 ──
    # JWT 시크릿이 설정돼 있으면 JWT IdP 주입. 비면 fail-closed 유지(503).
    # OIDC 등 다른 방식은 IdentityProvider 구현을 바꿔 끼움(§9).
    if settings.jwt_secret:
        from harag.api.auth import set_identity_provider
        from harag.api.auth_jwt import JwtIdentityProvider
        set_identity_provider(JwtIdentityProvider(
            secret=settings.jwt_secret,
            algorithms=[a.strip() for a in settings.jwt_algorithms.split(",")],
            audience=settings.jwt_audience or None,
            issuer=settings.jwt_issuer or None,
        ))
        logger.info("identity provider: JWT")
    else:
        logger.warning("identity provider 미설정 — 인증 fail-closed(503)")
    # 파이프라인·큐 주입은 검색·생성·워커 조립 후(다음 작업)
    # from harag.api.deps import set_query_pipeline, set_ingest_queue
    yield
    logger.info("shutdown: harag api gateway")


def create_app() -> FastAPI:
    app = FastAPI(
        title="harag — 한글 행정문서 RAG",
        version="0.1.0",
        lifespan=lifespan,
    )
    # 미들웨어: 나중에 add된 것이 더 바깥. SafeError를 가장 바깥에.
    app.add_middleware(TraceMiddleware)
    app.add_middleware(SafeErrorMiddleware)

    app.include_router(routes_query.router)
    app.include_router(routes_ingest.router)

    @app.get("/health")
    async def health():
        """헬스체크. 의존성(Qdrant 등) 깊은 체크는 /health/ready로 분리 가능."""
        return {"status": "ok"}

    return app


app = create_app()
