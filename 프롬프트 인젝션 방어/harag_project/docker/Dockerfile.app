# 앱 컨테이너 (api-gateway / query-service / indexing-worker 공용)
# GPU 불필요 — 무거운 ML 스택은 추론 서버(Dockerfile.inference)에만.
# 멀티스테이지: 빌드 의존성과 런타임 분리 → 이미지 경량화.

# ── 빌드 스테이지 ──
FROM python:3.12-slim AS builder

WORKDIR /build
RUN pip install --no-cache-dir hatchling

COPY pyproject.toml ./
COPY src ./src

# 의존성 wheel 빌드 (parsing은 PoC 후 확정되므로 base만)
RUN pip wheel --no-cache-dir --wheel-dir /wheels . && \
    pip wheel --no-cache-dir --wheel-dir /wheels \
        "pydantic-settings>=2.0"

# ── 런타임 스테이지 ──
FROM python:3.12-slim AS runtime

# 비루트 사용자 (보안)
RUN useradd --create-home --uid 10001 harag
WORKDIR /app

# 빌드된 wheel만 복사 → 빌드 도구 미포함
COPY --from=builder /wheels /wheels
RUN pip install --no-cache-dir /wheels/*.whl && rm -rf /wheels

COPY --chown=harag:harag src ./src
ENV PYTHONPATH=/app/src PYTHONUNBUFFERED=1
USER harag

# 헬스체크 (API 모드 기준)
HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
    CMD python -c "import httpx; httpx.get('http://localhost:8000/health', timeout=3)" || exit 1

# 실행 역할은 환경변수 HARAG_ROLE로 분기(api/query/indexing).
# 기본: API 게이트웨이.
EXPOSE 8000
CMD ["uvicorn", "harag.api.main:app", "--host", "0.0.0.0", "--port", "8000"]
