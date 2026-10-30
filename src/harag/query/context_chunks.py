"""FAQ·보험료 안내용 합성 ScoredChunk."""
from __future__ import annotations

from datetime import datetime, timezone

from harag.contracts.boundaries import Chunk, ScoredChunk
from harag.query.faq import FaqCard, format_faq_answer, premium_template_answer
from harag.schemas.chunk import ChunkMetadata, ChunkType

FAQ_CHUNK_ID = "faq-context"
PREMIUM_CHUNK_ID = "premium-context"
_FAQ_SCORE = 1.0


def scored_faq_chunk(card: FaqCard, *, extra: str = "") -> ScoredChunk:
    text = (
        "공통 FAQ 안내\n"
        f"주제: {card.id}\n"
        "아래는 큐레이션된 일반 안내이다. 상품별 확정 수치·일수는 "
        "상품요약서·공식 사이트 근거가 있을 때만 말해라.\n\n"
        + format_faq_answer(card, extra=extra)
    )
    meta = ChunkMetadata(
        chunk_id=FAQ_CHUNK_ID,
        document_id="faq",
        source_block_ids=[FAQ_CHUNK_ID],
        struct_path="FAQ",
        chunk_type=ChunkType.body,
        security_level="internal",
        acl_tags=["scope:library", "lib:knowledge"],
        source_document="공통 FAQ",
        citation_label=f"FAQ · {card.id}",
        parsing_version="faq-1",
        chunking_version="faq-1",
        embedding_model_id="none",
        content_hash=f"faq-{card.id}",
        created_at=datetime.now(timezone.utc),
    )
    return ScoredChunk(chunk=Chunk(meta=meta, text=text), score=_FAQ_SCORE)


def scored_premium_chunk(
    *,
    display_name: str = "",
    insurer: str = "",
    summary: str = "",
    cost_nature: str = "",
    source_url: str = "",
) -> ScoredChunk:
    body = premium_template_answer(
        display_name=display_name,
        insurer=insurer,
        summary=summary,
        cost_nature=cost_nature,
        source_url=source_url,
    )
    text = (
        "보험료 견적 안내\n"
        "개인 월납을 확정하지 마라. 아래 안내와 공식 사이트·공시 경로만 활용하라.\n\n"
        + body
    )
    meta = ChunkMetadata(
        chunk_id=PREMIUM_CHUNK_ID,
        document_id="faq",
        source_block_ids=[PREMIUM_CHUNK_ID],
        struct_path="보험료안내",
        chunk_type=ChunkType.body,
        security_level="internal",
        acl_tags=["scope:library", "lib:knowledge"],
        source_document="보험료 안내",
        citation_label="보험료 안내",
        parsing_version="faq-1",
        chunking_version="faq-1",
        embedding_model_id="none",
        content_hash="premium-guide",
        created_at=datetime.now(timezone.utc),
    )
    return ScoredChunk(chunk=Chunk(meta=meta, text=text), score=_FAQ_SCORE)


def resolve_selected_products(
    document_ids: list[str] | None,
    metadata,
) -> list[dict]:
    """선택 문서 → {display_name, insurer, summary, cost_nature, source_url}."""
    from harag.guidance.brochure import (
        COST_NATURES, catalog_entry, load_brochure_catalog,
    )

    out: list[dict] = []
    if not document_ids:
        return out
    catalog = load_brochure_catalog()
    for did in document_ids:
        display = insurer = summary = cost = source = ""
        filename = ""
        if metadata is not None:
            try:
                prof = metadata.get_brochure_profile(did)
            except Exception:  # noqa: BLE001
                prof = None
            if prof is not None:
                display = getattr(prof, "display_name", "") or ""
                insurer = getattr(prof, "insurer", "") or ""
                summary = getattr(prof, "summary", "") or ""
                cn = getattr(prof, "cost_nature", "") or ""
                cost = COST_NATURES.get(cn, cn)
                source = getattr(prof, "source_url", "") or ""
            try:
                doc = metadata.get_document(did)
            except Exception:  # noqa: BLE001
                doc = None
            if doc is not None:
                filename = getattr(doc, "filename", "") or ""
        if filename:
            entry = catalog_entry(filename) or catalog.get(filename)
            if isinstance(entry, dict):
                display = display or str(entry.get("display_name") or "")
                insurer = insurer or str(entry.get("insurer") or "")
                summary = summary or str(entry.get("summary") or "")
                if not cost:
                    cn = entry.get("cost_nature") or ""
                    cost = COST_NATURES.get(cn, cn) if cn else ""
                source = source or str(entry.get("source_url") or "")
        if not (display or insurer or source):
            # 파일명만으로 catalog 키 추정 실패 시 skip
            continue
        out.append({
            "document_id": did,
            "display_name": display,
            "insurer": insurer,
            "summary": summary,
            "cost_nature": cost,
            "source_url": source,
        })
    return out
