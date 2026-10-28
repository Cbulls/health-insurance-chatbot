"""검색 청크 → API Citation / SSE 페이로드."""
from __future__ import annotations

from harag.api.schemas import Citation
from harag.contracts.boundaries import ScoredChunk

_SNIPPET = 160


def citations_from_chunks(chunks: list[ScoredChunk],
                          snippet_len: int = _SNIPPET) -> list[Citation]:
    """내부 청크 → 사용자용 출처. chunk_id·조 경로·발췌 포함."""
    seen, out = set(), []
    for sc in chunks:
        cid = sc.chunk.meta.chunk_id
        if cid in seen:
            continue
        seen.add(cid)
        text = (sc.chunk.text or "").replace("\n", " ").strip()
        out.append(Citation(
            citation_label=sc.chunk.meta.citation_label,
            source_document=sc.chunk.meta.source_document,
            page=sc.chunk.meta.page_ref,
            chunk_id=cid,
            document_id=str(getattr(sc.chunk.meta, "document_id", "") or ""),
            struct_path=sc.chunk.meta.struct_path or "",
            snippet=text[:snippet_len],
        ))
    return out


def citations_sse_payload(chunks: list[ScoredChunk]) -> list[dict]:
    return [c.model_dump() for c in citations_from_chunks(chunks)]


def sources_sse_payload(chunks: list[ScoredChunk]) -> list[dict]:
    """컨텍스트 순서(1-based n) → document_id. [문서 N] 링크용."""
    out: list[dict] = []
    for i, sc in enumerate(chunks or [], start=1):
        meta = sc.chunk.meta
        out.append({
            "n": i,
            "document_id": str(getattr(meta, "document_id", "") or ""),
            "source_document": str(getattr(meta, "source_document", "") or ""),
        })
    return out
