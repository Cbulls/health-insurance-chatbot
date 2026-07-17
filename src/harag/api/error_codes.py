"""
API 오류 코드 — 프론트가 기계 판독 가능한 detail 계약.

FastAPI HTTPException detail을 dict로 내려
`{"code": "...", "message": "..."}` 형태를 유지한다.
"""
from __future__ import annotations

from typing import Any

from fastapi import HTTPException, status


CODE_RATE_LIMIT = "rate_limit"
CODE_BUDGET_EXHAUSTED = "budget_exhausted"
CODE_LLM_UNAVAILABLE = "llm_unavailable"
CODE_AUTH_REQUIRED = "auth_required"


def error_detail(code: str, message: str, **extra: Any) -> dict[str, Any]:
    body: dict[str, Any] = {"code": code, "message": message}
    body.update(extra)
    return body


def http_error(
    status_code: int,
    code: str,
    message: str,
    *,
    headers: dict[str, str] | None = None,
    **extra: Any,
) -> HTTPException:
    return HTTPException(
        status_code=status_code,
        detail=error_detail(code, message, **extra),
        headers=headers,
    )


def rate_limit_error(retry_after: int = 30) -> HTTPException:
    return http_error(
        status.HTTP_429_TOO_MANY_REQUESTS,
        CODE_RATE_LIMIT,
        "요청이 너무 잦습니다. 잠시 후 다시 시도해 주세요.",
        headers={"Retry-After": str(retry_after)},
        retry_after_sec=retry_after,
    )


def budget_exhausted_error(
    *, kind: str = "questions", retry_after: int = 3600,
) -> HTTPException:
    if kind == "tokens":
        msg = ("오늘의 토큰 사용 한도를 모두 사용했습니다. "
               "내일(UTC 기준) 다시 시도해 주세요.")
    else:
        msg = ("오늘의 질문 한도를 모두 사용했습니다. "
               "내일(UTC 기준) 다시 시도해 주세요.")
    return http_error(
        status.HTTP_429_TOO_MANY_REQUESTS,
        CODE_BUDGET_EXHAUSTED,
        msg,
        headers={"Retry-After": str(retry_after)},
        retry_after_sec=retry_after,
        budget_kind=kind,
    )
