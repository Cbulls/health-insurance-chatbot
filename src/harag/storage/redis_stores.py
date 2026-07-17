"""Redis 기반 레이트제한·대화 이력 — 다중 API 인스턴스 공유."""
from __future__ import annotations

import logging
import time
from typing import Any

logger = logging.getLogger("harag.redis")

# KEYS[1]=bucket  ARGV[1]=capacity ARGV[2]=rate_per_sec ARGV[3]=now ARGV[4]=ttl
_TOKEN_BUCKET_LUA = """
local capacity = tonumber(ARGV[1])
local rate = tonumber(ARGV[2])
local now = tonumber(ARGV[3])
local ttl = tonumber(ARGV[4])
local data = redis.call('HMGET', KEYS[1], 'tokens', 'last')
local tokens = tonumber(data[1])
local last = tonumber(data[2])
if tokens == nil then
  tokens = capacity
  last = now
end
tokens = math.min(capacity, tokens + (now - last) * rate)
local allowed = 0
if tokens >= 1.0 then
  tokens = tokens - 1.0
  allowed = 1
end
redis.call('HSET', KEYS[1], 'tokens', tostring(tokens), 'last', tostring(now))
redis.call('EXPIRE', KEYS[1], ttl)
return allowed
"""


def _norm_prefix(prefix: str) -> str:
    if not prefix:
        return "harag:"
    return prefix if prefix.endswith(":") else f"{prefix}:"


class RedisTokenBucketLimiter:
    """owner별 분당 토큰버킷 — 단일 Lua EVAL로 refill+consume+EXPIRE 원자화."""

    def __init__(self, redis_client: Any, per_minute: int,
                 prefix: str = "harag:"):
        self._r = redis_client
        self._capacity = float(per_minute)
        self._rate = per_minute / 60.0
        self._prefix = _norm_prefix(prefix)

    def allow(self, key: str) -> bool:
        rk = f"{self._prefix}rl:{key}"
        now = time.time()
        try:
            allowed = self._r.eval(
                _TOKEN_BUCKET_LUA, 1, rk,
                str(self._capacity), str(self._rate), str(now), "120")
            return bool(int(allowed))
        except Exception:  # noqa: BLE001 — Redis 장애 시 통과(가용성 우선)
            logger.warning("redis rate-limit failed — allow", exc_info=True)
            return True


class RedisConversationStore:
    """conversation_id별 질의 이력(List + TTL). 답변·청크는 저장하지 않음."""

    def __init__(self, redis_client: Any, max_turns: int = 5,
                 ttl_sec: int = 86400, prefix: str = "harag:"):
        self._r = redis_client
        self._max_turns = max_turns
        self._ttl = ttl_sec
        self._prefix = _norm_prefix(prefix)

    def _key(self, conversation_id: str) -> str:
        return f"{self._prefix}conv:{conversation_id}"

    def append(self, conversation_id: str, query: str) -> None:
        k = self._key(conversation_id)
        try:
            pipe = self._r.pipeline(transaction=False)
            pipe.rpush(k, query)
            pipe.ltrim(k, -self._max_turns, -1)
            pipe.expire(k, self._ttl)
            pipe.execute()
        except Exception:  # noqa: BLE001
            logger.warning("redis conv append failed", exc_info=True)

    def get(self, conversation_id: str) -> list[str]:
        try:
            return list(self._r.lrange(self._key(conversation_id), 0, -1) or [])
        except Exception:  # noqa: BLE001
            return []
