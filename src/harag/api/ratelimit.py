"""
레이트리밋 — owner별 토큰버킷(인메모리, MVP).

LLM 호출은 건당 비용이 드는 외부 자원이라, 익명 사용자가 /v1/query를
무한 호출하면 비용이 폭주한다. owner(인증 식별자)별로 분당 요청 수를
제한하고 초과 시 429를 반환한다.

  - RATE_LIMIT_QPM env로 분당 허용량 설정(기본 20, 0이면 비활성).
  - 토큰버킷: 버스트는 상한까지 허용, 이후 분당 속도로 리필.
  - 버킷 수는 LRU 상한(max_buckets)으로 묶어 키 폭주로 메모리가 새지 않게.

운영(다중 인스턴스)은 Redis 등 공유 저장소 기반으로 교체한다.
"""
from __future__ import annotations

import threading
import time
from collections import OrderedDict

from fastapi import Depends, HTTPException, status

from harag.api.auth import require_auth
from harag.config.settings import get_settings
from harag.contracts.boundaries import AuthContext


class TokenBucketLimiter:
    def __init__(self, per_minute: int, max_buckets: int = 10000):
        self._capacity = float(per_minute)
        self._rate_per_sec = per_minute / 60.0
        self._max_buckets = max(1, max_buckets)
        # key -> (tokens, last_refill_ts)
        self._buckets: OrderedDict[str, tuple[float, float]] = OrderedDict()
        self._lock = threading.Lock()

    def allow(self, key: str) -> bool:
        now = time.monotonic()
        with self._lock:
            tokens, last = self._buckets.get(key, (self._capacity, now))
            tokens = min(self._capacity, tokens + (now - last) * self._rate_per_sec)
            allowed = tokens >= 1.0
            if allowed:
                tokens -= 1.0
            self._buckets[key] = (tokens, now)
            self._buckets.move_to_end(key)
            while len(self._buckets) > self._max_buckets:
                self._buckets.popitem(last=False)
            return allowed


_limiter: TokenBucketLimiter | None = None
_limiter_lock = threading.Lock()


def _get_limiter(per_minute: int) -> TokenBucketLimiter:
    global _limiter
    with _limiter_lock:
        if _limiter is None:
            _limiter = TokenBucketLimiter(per_minute)
        return _limiter


async def enforce_rate_limit(
    auth: AuthContext = Depends(require_auth),
) -> AuthContext:
    """require_auth를 감싸는 라우트 의존성 — 인증 후 owner별 요청 제한."""
    qpm = get_settings().rate_limit_qpm
    if qpm > 0 and not _get_limiter(qpm).allow(auth.user_id):
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail="요청이 너무 잦습니다. 잠시 후 다시 시도해 주세요.",
            headers={"Retry-After": "30"},
        )
    return auth
