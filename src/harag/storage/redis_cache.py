"""문서 상태 Redis 캐시 — MetadataStore가 진실원천, Redis는 TTL 읽기 가속."""
from __future__ import annotations

import json
import logging
from typing import Any

logger = logging.getLogger("harag.redis")

_DEFAULT_TTL = 300  # 폴링 구간 충분(1h는 과함)


def _norm_prefix(prefix: str) -> str:
    if not prefix:
        return "harag:"
    return prefix if prefix.endswith(":") else f"{prefix}:"


class DocStatusCache:
    """document_id+owner → {status, n_chunks, error, filename} JSON."""

    def __init__(self, redis_client: Any, ttl_sec: int = _DEFAULT_TTL,
                 prefix: str = "harag:"):
        self._r = redis_client
        self._ttl = ttl_sec
        self._prefix = _norm_prefix(prefix)

    def _key(self, document_id: str, owner: str) -> str:
        return f"{self._prefix}doc:{owner}:{document_id}"

    def get(self, document_id: str, owner: str) -> dict | None:
        try:
            raw = self._r.get(self._key(document_id, owner))
            return json.loads(raw) if raw else None
        except Exception:  # noqa: BLE001 — 캐시 실패는 무시(DB로 폴백)
            return None

    def set(self, document_id: str, owner: str, payload: dict) -> None:
        # ready/failed 시 filename이 비면 기존 캐시 filename 유지
        filename = payload.get("filename")
        if not filename:
            prev = self.get(document_id, owner)
            if prev and prev.get("filename"):
                payload = {**payload, "filename": prev["filename"]}
        try:
            self._r.setex(
                self._key(document_id, owner),
                self._ttl,
                json.dumps(payload, ensure_ascii=False),
            )
        except Exception:  # noqa: BLE001
            logger.debug("doc cache set failed", exc_info=True)

    def invalidate(self, document_id: str, owner: str) -> None:
        try:
            self._r.delete(self._key(document_id, owner))
        except Exception:  # noqa: BLE001
            pass
