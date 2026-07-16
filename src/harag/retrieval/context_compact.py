"""
생성 전 컨텍스트 압축 — top-N 안 중복 청크 제거.

리랭커가 올린 후보라도 동일·거의 동일 문장이 여러 개면 LLM 컨텍스트만
늘고 답이 흔들린다. 정확도(체감) 안정화용 후처리.
"""
from __future__ import annotations

import re

from harag.contracts.boundaries import ScoredChunk

_TOKEN = re.compile(r"[0-9A-Za-z가-힣]+")


def _norm_tokens(text: str) -> frozenset[str]:
    return frozenset(_TOKEN.findall((text or "").lower()))


def jaccard(a: frozenset[str], b: frozenset[str]) -> float:
    if not a and not b:
        return 1.0
    if not a or not b:
        return 0.0
    return len(a & b) / len(a | b)


def dedupe_scored_chunks(
    chunks: list[ScoredChunk],
    *,
    jaccard_threshold: float = 0.92,
) -> list[ScoredChunk]:
    """순위 순으로 보며 거의 동일한 텍스트는 뒤쪽을 버린다."""
    kept: list[ScoredChunk] = []
    seen_hashes: set[str] = set()
    kept_toks: list[frozenset[str]] = []
    for sc in chunks:
        h = (sc.chunk.meta.content_hash or "").strip()
        if h and h in seen_hashes:
            continue
        toks = _norm_tokens(sc.chunk.text)
        if any(jaccard(toks, prev) >= jaccard_threshold for prev in kept_toks):
            continue
        if h:
            seen_hashes.add(h)
        kept.append(sc)
        kept_toks.append(toks)
    return kept
