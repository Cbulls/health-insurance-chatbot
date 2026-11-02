"""Jina /v1/rerank CrossEncoder 어댑터."""
from __future__ import annotations

import logging

import httpx

from harag.llm.http_rerank import normalize_scores
from harag.retrieval.reranker import RerankUnavailable

logger = logging.getLogger("harag.rerank.jina")


class JinaCrossEncoder:
    """Jina Reranker API → CrossEncoder Protocol (score_pairs)."""

    def __init__(
        self,
        *,
        api_base: str,
        api_key: str,
        model: str = "jina-reranker-v3.5",
        timeout_ms: int = 8000,
        normalize: str = "auto",
    ):
        self._base = (api_base or "https://api.jina.ai/v1").rstrip("/")
        self._model = model or "jina-reranker-v3.5"
        self._normalize = normalize
        self._client = httpx.Client(
            base_url=self._base,
            headers={"Authorization": f"Bearer {api_key}"},
            timeout=max(1.0, timeout_ms / 1000.0),
        )

    def score_pairs(self, query: str, texts: list[str]) -> list[float]:
        if not texts:
            return []
        body = {
            "model": self._model,
            "query": query,
            "documents": texts,
            "top_n": len(texts),
            "return_documents": False,
        }
        try:
            resp = self._client.post("/rerank", json=body)
            resp.raise_for_status()
            data = resp.json()
        except Exception as e:  # noqa: BLE001
            # 0점 배열을 돌려주면 min_score 필터가 후보를 전부 지워
            # 빈 컨텍스트가 되므로, 장애는 예외로 알려 retrieval 폴백을 유도.
            logger.warning("jina rerank failed: %s: %s",
                           type(e).__name__, str(e)[:160])
            raise RerankUnavailable(f"jina rerank {type(e).__name__}") from e

        results = data.get("results")
        if results is None and isinstance(data.get("data"), list):
            results = data["data"]
        if not isinstance(results, list):
            logger.warning("jina rerank unexpected payload keys=%s", list(data)[:8])
            raise RerankUnavailable("jina rerank unexpected payload")

        scores = [0.0] * len(texts)
        for item in results:
            if not isinstance(item, dict):
                continue
            idx = item.get("index")
            if idx is None:
                continue
            try:
                i = int(idx)
            except (TypeError, ValueError):
                continue
            if not (0 <= i < len(texts)):
                continue
            raw = item.get("relevance_score", item.get("score", 0.0))
            try:
                scores[i] = float(raw)
            except (TypeError, ValueError):
                scores[i] = 0.0
        return normalize_scores(scores, self._normalize)
