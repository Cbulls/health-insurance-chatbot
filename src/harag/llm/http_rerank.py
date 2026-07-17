"""
HTTP Cross-Encoder 어댑터 — TEI/vLLM 호환 /rerank.

설계상 self-host GPU 리랭커 서버를 CrossEncoder Protocol 뒤로 연결다.
URL이 없으면 조립층에서 LexicalCrossEncoder로 폴백한다.

요청(TEI):
  POST {base}/rerank
  {"query": "...", "texts": ["...", "..."], "raw_scores": false}

응답(허용 형식):
  [{"index": 0, "score": 0.9}, ...]
  또는 {"results": [{"index": 0, "score": 0.9}, ...]}
  또는 {"scores": [0.9, 0.1, ...]}  (texts 순서와 동일)
"""
from __future__ import annotations

import logging
import math
from typing import Sequence

import httpx

logger = logging.getLogger("harag.rerank")


def sigmoid(x: float) -> float:
    # overflow 방지
    if x >= 20:
        return 1.0
    if x <= -20:
        return 0.0
    return 1.0 / (1.0 + math.exp(-x))


def normalize_scores(raw: Sequence[float], mode: str = "auto") -> list[float]:
    """CE logits/확률을 0~1로 맞춤 — MIN_SCORE·리랭커 min_score와 한 축.

    mode:
      - auto: 값이 전부 [0,1]이면 그대로, 아니면 sigmoid
      - sigmoid: 항상 sigmoid
      - none: 그대로
    """
    vals = [float(x) for x in raw]
    if not vals:
        return []
    m = (mode or "auto").lower()
    if m == "none":
        return vals
    if m == "sigmoid":
        return [sigmoid(x) for x in vals]
    # auto
    if all(0.0 <= x <= 1.0 for x in vals):
        return vals
    return [sigmoid(x) for x in vals]


class HttpCrossEncoder:
    """TEI 호환 HTTP cross-encoder (CrossEncoder Protocol)."""

    def __init__(
        self,
        base_url: str,
        timeout_ms: int = 3000,
        normalize: str = "auto",
        path: str = "/rerank",
    ):
        self._base = base_url.rstrip("/")
        self._path = path if path.startswith("/") else f"/{path}"
        self._normalize = normalize
        self._client = httpx.Client(
            base_url=self._base,
            timeout=timeout_ms / 1000.0,
        )

    def score_pairs(self, query: str, texts: list[str]) -> list[float]:
        if not texts:
            return []
        body = {"query": query, "texts": texts, "raw_scores": False}
        try:
            resp = self._client.post(self._path, json=body)
        except httpx.TimeoutException:
            logger.warning("reranker timeout — zeros (caller may blend/abstain)")
            return [0.0] * len(texts)
        except httpx.HTTPError as e:
            logger.warning("reranker HTTP error: %s — zeros", e)
            return [0.0] * len(texts)

        if resp.status_code >= 400:
            logger.warning(
                "reranker status %s: %s — zeros",
                resp.status_code, resp.text[:200],
            )
            return [0.0] * len(texts)

        data = resp.json()
        raw = _parse_scores(data, len(texts))
        return normalize_scores(raw, self._normalize)

    def close(self) -> None:
        self._client.close()


def _parse_scores(data, n: int) -> list[float]:
    """다양한 TEI/프록시 응답을 texts 길이의 점수 배열로."""
    if isinstance(data, list):
        items = data
    elif isinstance(data, dict):
        if "scores" in data and isinstance(data["scores"], list):
            scores = [float(x) for x in data["scores"]]
            if len(scores) != n:
                raise ValueError(f"scores length {len(scores)} != texts {n}")
            return scores
        items = data.get("results") or data.get("data") or []
    else:
        raise ValueError("unexpected rerank response type")

    out = [0.0] * n
    for item in items:
        if not isinstance(item, dict):
            continue
        idx = int(item.get("index", -1))
        if 0 <= idx < n:
            out[idx] = float(item.get("score", 0.0))
    return out
