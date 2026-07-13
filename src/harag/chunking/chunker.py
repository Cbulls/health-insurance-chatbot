"""
청커 — IR을 구조 기반 청크로 변환(FR-3).

핵심: 토큰 고정 분할이 아니라 구조(struct_path·표) 단위가 1차.
  - 같은 struct_path의 연속 본문 블록을 한 청크로 묶음(MVP: 페이지 단위)
  - 표는 항상 독립 청크(chunk_type=table)
  - 노이즈 블록은 건너뜀
  - 토큰 상한 초과 시 블록 단위로 분할(폴백) — 발동률은 건강 지표
  - source_block_ids로 계보 보존, struct_path/page_ref 전파, 메타 채움(fail-closed)
"""
from __future__ import annotations

import hashlib
from dataclasses import dataclass
from datetime import datetime, timezone

from harag.schemas.ir import DocumentIR, Block, BlockType
from harag.schemas.chunk import ChunkMetadata, ChunkType
from harag.contracts.boundaries import Chunk


@dataclass
class ChunkingContext:
    """청킹에 필요한 문서 외 정보(권한·출처·모델·상한)."""
    acl_tags: list[str]
    source_document: str
    embedding_model_id: str
    max_tokens: int = 512
    chunking_version: str = "struct-mvp-0.1"


def _approx_tokens(text: str) -> int:
    return max(1, len(text) // 2)


def _citation_label(struct_path: str, source_document: str) -> str:
    base = source_document.rsplit(".", 1)[0]
    if not struct_path:
        return base
    # 페이지 프록시("p3") → 사람이 읽기 좋은 라벨
    if struct_path.startswith("p") and struct_path[1:].isdigit():
        return f"{base} {struct_path[1:]}페이지"
    return f"{base} {struct_path}".strip()


class StructuralChunker:
    def chunk(self, ir: DocumentIR, ctx: ChunkingContext) -> list[Chunk]:
        chunks: list[Chunk] = []
        buf: list[Block] = []

        def flush():
            if not buf:
                return
            self._emit_body(list(buf), ir, ctx, chunks)
            buf.clear()

        for b in ir.blocks:
            if b.is_noise:
                continue
            if b.block_type == BlockType.table:
                flush()
                self._emit_table(b, ir, ctx, chunks)
                continue
            # struct_path가 바뀌면 경계(MVP: 페이지 경계) → 마감
            if buf and buf[-1].struct_path != b.struct_path:
                flush()
            buf.append(b)
        flush()
        return chunks

    def _emit_body(self, blocks: list[Block], ir, ctx, out: list[Chunk]):
        text = "\n".join(b.text or "" for b in blocks).strip()
        if not text:
            return
        struct = blocks[0].struct_path
        page = blocks[0].page_ref
        src_ids = [b.block_id for b in blocks]

        if _approx_tokens(text) <= ctx.max_tokens:
            out.append(self._make(text, struct, page, src_ids, ChunkType.body,
                                  ir, ctx, idx=len(out)))
        else:
            # 폴백: 블록 단위로 쪼갬(의미 단위 유지 시도)
            for b in blocks:
                if b.text and b.text.strip():
                    out.append(self._make(b.text, struct, b.page_ref, [b.block_id],
                                          ChunkType.body, ir, ctx, idx=len(out)))

    def _emit_table(self, b: Block, ir, ctx, out: list[Chunk]):
        tc = b.table_content
        rows: dict[int, list[str]] = {}
        for c in tc.cells:
            rows.setdefault(c.row, []).append(c.text)
        text = " | ".join(" ".join(rows[r]) for r in sorted(rows)).strip()
        if not text:
            return
        out.append(self._make(text, b.struct_path, b.page_ref, [b.block_id],
                              ChunkType.table, ir, ctx, idx=len(out)))

    def _make(self, text, struct, page, src_ids, ctype, ir, ctx, idx) -> Chunk:
        content_hash = hashlib.sha256(text.encode()).hexdigest()[:16]
        meta = ChunkMetadata(
            chunk_id=f"{ir.document_id}-c{idx}",
            document_id=ir.document_id,
            source_block_ids=src_ids,
            struct_path=struct,
            chunk_type=ctype,
            page_ref=page,
            security_level=ir.source_metadata.security_level,
            acl_tags=list(ctx.acl_tags),              # fail-closed: 비면 인덱싱 거부
            source_document=ctx.source_document,
            citation_label=_citation_label(struct, ctx.source_document),
            parsing_version=ir.parser_version,
            chunking_version=ctx.chunking_version,
            embedding_model_id=ctx.embedding_model_id,
            content_hash=content_hash,
            created_at=datetime.now(timezone.utc),
        )
        return Chunk(meta=meta, text=text)
