"""질의 임베딩 LRU 캐시 — 동일 질의 재시도·유사 호출 지연 절감."""
from __future__ import annotations

import hashlib
import threading
import time
from collections import OrderedDict
from typing import Protocol


class _EncodeModel(Protocol):
    def encode(self, texts: list[str]) -> list[list[float]]: ...


class CachedEmbeddingModel:
    """단일 텍스트(질의) encode를 TTL+LRU로 캐시. 배치 인덱싱은 우회."""

    def __init__(
        self,
        inner: _EncodeModel,
        *,
        max_entries: int = 256,
        ttl_sec: float = 300.0,
    ):
        self._inner = inner
        self._max = max(8, max_entries)
        self._ttl = max(1.0, ttl_sec)
        self._cache: OrderedDict[str, tuple[float, list[float]]] = OrderedDict()
        self._lock = threading.Lock()
        # 위임 속성
        self.model_id = getattr(inner, "model_id", "cached")
        self.dim = getattr(inner, "dim", 0)

    def _key(self, text: str) -> str:
        return hashlib.sha256(text.encode("utf-8")).hexdigest()

    def encode(self, texts: list[str]) -> list[list[float]]:
        if not texts:
            return []
        # 인덱싱 배치(>1)는 캐시하지 않음
        if len(texts) != 1:
            return self._inner.encode(texts)
        text = texts[0]
        key = self._key(text)
        now = time.monotonic()
        with self._lock:
            hit = self._cache.get(key)
            if hit is not None:
                ts, vec = hit
                if now - ts <= self._ttl:
                    self._cache.move_to_end(key)
                    return [list(vec)]
                del self._cache[key]
        vecs = self._inner.encode(texts)
        with self._lock:
            self._cache[key] = (now, list(vecs[0]))
            self._cache.move_to_end(key)
            while len(self._cache) > self._max:
                self._cache.popitem(last=False)
        return vecs
