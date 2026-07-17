"""
생성 전 컨텍스트 압축·확장.

1) Jaccard 중복 제거
2) parent_chunk_id / 동일 struct_path 이웃 확장 (후보 풀 안)
3) 토큰 예산으로 패킹 (점수 순)
"""
from __future__ import annotations

import re

from harag.contracts.boundaries import ScoredChunk

_TOKEN = re.compile(r"[0-9A-Za-z가-힣]+")


def _norm_tokens(text: str) -> frozenset[str]:
    return frozenset(_TOKEN.findall((text or "").lower()))


def approx_tokens(text: str) -> int:
    """한국어 위주 대략 토큰(문자/2, 하한 1)."""
    n = len(text or "")
    return max(1, n // 2)


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


def _struct_prefix(path: str) -> str:
    """'제12조 > 제1항' → '제12조' (같은 조·장 이웃 묶음)."""
    parts = [p.strip() for p in (path or "").split(">") if p.strip()]
    if not parts:
        return ""
    return parts[0]


def expand_neighbors(
    ranked: list[ScoredChunk],
    pool: list[ScoredChunk],
    *,
    max_extra_per_hit: int = 2,
) -> list[ScoredChunk]:
    """상위 히트에 대해 풀 안에서 부모·동일 구조 이웃을 끼워 넣는다."""
    by_id = {sc.chunk.meta.chunk_id: sc for sc in pool}
    seen = {sc.chunk.meta.chunk_id for sc in ranked}
    out = list(ranked)

    for seed in list(ranked):
        meta = seed.chunk.meta
        extras: list[ScoredChunk] = []
        extra_ids: set[str] = set()
        parent = getattr(meta, "parent_chunk_id", None) or ""
        if parent and parent in by_id and parent not in seen:
            extras.append(by_id[parent])
            extra_ids.add(parent)
        pref = _struct_prefix(meta.struct_path or "")
        doc = meta.document_id
        if pref:
            for sc in pool:
                cid = sc.chunk.meta.chunk_id
                if cid in seen or cid in extra_ids or cid == meta.chunk_id:
                    continue
                if sc.chunk.meta.document_id != doc:
                    continue
                if _struct_prefix(sc.chunk.meta.struct_path or "") != pref:
                    continue
                extras.append(sc)
                extra_ids.add(cid)
                if len(extras) >= max_extra_per_hit:
                    break
        for sc in extras[:max_extra_per_hit]:
            cid = sc.chunk.meta.chunk_id
            if cid in seen:
                continue
            # 확장 청크는 시드보다 약간 낮은 점수로
            out.append(ScoredChunk(chunk=sc.chunk, score=seed.score * 0.95))
            seen.add(cid)
    return out


def pack_by_token_budget(
    chunks: list[ScoredChunk],
    *,
    max_tokens: int = 3500,
) -> list[ScoredChunk]:
    """점수 내림차순으로 토큰 예산까지 채운다."""
    ordered = sorted(chunks, key=lambda s: s.score, reverse=True)
    kept: list[ScoredChunk] = []
    used = 0
    for sc in ordered:
        cost = approx_tokens(sc.chunk.text)
        if kept and used + cost > max_tokens:
            continue
        kept.append(sc)
        used += cost
        if used >= max_tokens:
            break
    # 원래 상위 순위 감각 유지: 점수순 유지
    kept.sort(key=lambda s: s.score, reverse=True)
    return kept


def prepare_context(
    candidates: list[ScoredChunk],
    *,
    jaccard_threshold: float = 0.92,
    max_tokens: int = 3500,
    expand: bool = True,
) -> list[ScoredChunk]:
    """dedupe → (선택) 이웃 확장 → 토큰 패킹."""
    if not candidates:
        return []
    ranked = dedupe_scored_chunks(
        candidates, jaccard_threshold=jaccard_threshold)
    if expand:
        ranked = expand_neighbors(ranked, candidates)
        ranked = dedupe_scored_chunks(
            ranked, jaccard_threshold=jaccard_threshold)
    return pack_by_token_budget(ranked, max_tokens=max_tokens)
