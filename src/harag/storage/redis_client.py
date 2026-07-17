"""Redis 클라이언트 팩토리 — 연결 성공만 캐시, 실패는 TTL 후 재시도."""
from __future__ import annotations

import logging
import time
from typing import Any

logger = logging.getLogger("harag.redis")

# url -> (client | None, expires_at_monotonic)
_CACHE: dict[str, tuple[Any | None, float]] = {}
_OK_TTL = 3600.0       # 성공 클라이언트 재사용
_FAIL_TTL = 30.0       # 실패 후 재시도 대기


def get_redis(url: str) -> Any | None:
    """redis.Redis 인스턴스. URL 비었거나 연결 실패 시 None.

    실패를 영구 캐시하지 않는다 — FAIL_TTL 후 다시 ping한다.
    """
    if not url:
        return None
    now = time.monotonic()
    cached = _CACHE.get(url)
    if cached is not None:
        client, expires = cached
        if now < expires:
            return client
        # TTL 만료 — 성공 클라이언트가 살아 있으면 연장
        if client is not None:
            try:
                client.ping()
                _CACHE[url] = (client, now + _OK_TTL)
                return client
            except Exception:  # noqa: BLE001
                _CACHE.pop(url, None)

    try:
        import redis
    except ImportError:
        logger.warning("redis 패키지 미설치 — 인메모리 폴백 (pip install redis)")
        _CACHE[url] = (None, now + _FAIL_TTL)
        return None

    try:
        client = redis.Redis.from_url(
            url,
            decode_responses=True,
            socket_connect_timeout=2,
            socket_timeout=5,
            health_check_interval=30,
            max_connections=20,
        )
        client.ping()
        logger.info("redis connected: %s", url.split("@")[-1])
        _CACHE[url] = (client, now + _OK_TTL)
        return client
    except Exception:  # noqa: BLE001
        logger.warning("redis 연결 실패 — 인메모리 폴백", exc_info=True)
        _CACHE[url] = (None, now + _FAIL_TTL)
        return None


def reset_redis_cache() -> None:
    """테스트용 캐시 초기화."""
    _CACHE.clear()
