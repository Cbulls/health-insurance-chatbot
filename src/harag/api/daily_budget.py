"""
일일 질문·토큰 예산 — Redis(공유) 또는 인메모리.

QPM(분당)과 별도로 RPD/일일 토큰 폭주를 막는다.
초과 시 HTTP 429 + detail=budget_exhausted.
"""
from __future__ import annotations

import threading
import time
from datetime import datetime, timezone

from fastapi import Depends, HTTPException, status

from harag.api.auth import require_auth
from harag.config.settings import get_settings
from harag.contracts.boundaries import AuthContext

_lock = threading.Lock()
_mem_q: dict[str, int] = {}
_mem_t: dict[str, int] = {}
_redis = None
_prefix = "harag:"


def configure_redis(redis_client, prefix: str = "harag:") -> None:
    global _redis, _prefix
    _redis = redis_client
    _prefix = prefix or "harag:"


def _day_key() -> str:
    return datetime.now(timezone.utc).strftime("%Y%m%d")


def _incr_mem(bucket: dict[str, int], key: str, n: int = 1) -> int:
    with _lock:
        bucket[key] = bucket.get(key, 0) + n
        return bucket[key]


def questions_used(owner: str) -> int:
    day = _day_key()
    key = f"{_prefix}budget:q:{day}:{owner}"
    if _redis is not None:
        try:
            v = _redis.get(key)
            return int(v or 0)
        except Exception:  # noqa: BLE001
            pass
    with _lock:
        return int(_mem_q.get(key, 0))


def tokens_used(owner: str) -> int:
    day = _day_key()
    key = f"{_prefix}budget:t:{day}:{owner}"
    if _redis is not None:
        try:
            v = _redis.get(key)
            return int(v or 0)
        except Exception:  # noqa: BLE001
            pass
    with _lock:
        return int(_mem_t.get(key, 0))


def record_question(owner: str) -> int:
    day = _day_key()
    key = f"{_prefix}budget:q:{day}:{owner}"
    if _redis is not None:
        try:
            n = int(_redis.incr(key))
            if n == 1:
                _redis.expire(key, 90_000)
            return n
        except Exception:  # noqa: BLE001
            pass
    return _incr_mem(_mem_q, key, 1)


def record_tokens(owner: str, tokens: int) -> int:
    if tokens <= 0:
        return tokens_used(owner)
    day = _day_key()
    key = f"{_prefix}budget:t:{day}:{owner}"
    if _redis is not None:
        try:
            n = int(_redis.incrby(key, int(tokens)))
            if n == int(tokens):
                _redis.expire(key, 90_000)
            return n
        except Exception:  # noqa: BLE001
            pass
    return _incr_mem(_mem_t, key, int(tokens))


def check_budget(owner: str) -> None:
    """초과 시 HTTPException. 통과만 하고 카운트는 질의 성공 후 record_*."""
    s = get_settings()
    q_limit = s.daily_question_budget
    t_limit = s.daily_token_budget
    if q_limit > 0 and questions_used(owner) >= q_limit:
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail="budget_exhausted (일일 질문 예산 초과)",
            headers={"Retry-After": "3600"},
        )
    if t_limit > 0 and tokens_used(owner) >= t_limit:
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail="budget_exhausted (일일 토큰 예산 초과)",
            headers={"Retry-After": "3600"},
        )


async def enforce_daily_budget(
    auth: AuthContext = Depends(require_auth),
) -> AuthContext:
    """레이트리밋 뒤에 붙이는 의존성 — 예산만 검사(카운트는 파이프라인)."""
    check_budget(auth.user_id)
    return auth


def reset_for_tests() -> None:
    with _lock:
        _mem_q.clear()
        _mem_t.clear()
