"""
미들웨어 — 횡단 관심사. 모든 요청에 일괄 적용.

  - trace_id: 요청마다 부여, 응답·로그·하위 컴포넌트 trace에 전파(NFR-6).
  - 안전한 에러 변환: 처리 안 된 예외를 잡아 내부 구조 누설 없이 변환(설계 원칙 3).
    스택트레이스·내부 메시지는 로그에만, 응답엔 trace_id만.
"""
from __future__ import annotations

import logging
import uuid
from contextvars import ContextVar

from fastapi import Request
from fastapi.responses import JSONResponse
from starlette.middleware.base import BaseHTTPMiddleware

logger = logging.getLogger("harag.api")

# 요청 스코프 trace_id — 로깅·하위 컴포넌트가 참조
trace_id_var: ContextVar[str] = ContextVar("trace_id", default="")


def current_trace_id() -> str:
    return trace_id_var.get()


class TraceMiddleware(BaseHTTPMiddleware):
    """요청마다 trace_id 부여·전파. 단계별 trace를 하나로 묶는 뿌리(NFR-6)."""

    async def dispatch(self, request: Request, call_next):
        trace_id = request.headers.get("X-Trace-Id") or uuid.uuid4().hex
        token = trace_id_var.set(trace_id)
        try:
            response = await call_next(request)
            response.headers["X-Trace-Id"] = trace_id
            return response
        finally:
            trace_id_var.reset(token)


class SafeErrorMiddleware(BaseHTTPMiddleware):
    """처리 안 된 예외를 안전하게 변환. 내부 구조를 응답에 누설하지 않는다.

    상세(스택·내부 메시지)는 trace_id와 함께 로그에만 남기고,
    응답엔 안전 메시지 + trace_id만. 지원팀은 trace_id로 로그를 대조한다.
    """

    async def dispatch(self, request: Request, call_next):
        try:
            return await call_next(request)
        except Exception:  # noqa: BLE001 — 최후 방어선
            tid = current_trace_id()
            # 상세는 로그에만(누설 금지)
            logger.exception("unhandled error", extra={"trace_id": tid})
            return JSONResponse(
                status_code=500,
                content={"error": "internal error", "trace_id": tid},
            )
