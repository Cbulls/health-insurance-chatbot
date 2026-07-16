"""
청커 — IR을 구조 기반 청크로 변환(FR-3).

핵심: 토큰 고정 분할이 아니라 구조(struct_path·표) 단위가 1차.
  - 같은 struct_path의 연속 본문 블록을 한 청크로 묶음(조 단위, 폴백=페이지)
  - 표는 항상 독립 청크(chunk_type=table)
      · 행 단위 "헤더: 값" 직렬화 — 열 헤더와 셀의 관계를 보존
      · 섹션(struct_path) 프리픽스 — 표만 검색돼도 어느 조항인지 안다
  - 노이즈 블록은 건너뜀
  - 토큰 상한 초과 시 문장 경계로 분할 + 1문장 오버랩(폴백) — 발동률은 건강 지표
  - source_block_ids로 계보 보존, struct_path/page_ref 전파, 메타 채움(fail-closed)
"""
from __future__ import annotations

import hashlib
import re
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
    chunking_version: str = "struct-mvp-0.2"


_SENT_RE = re.compile(r"[^.!?。\n]+[.!?。]?\s*")


def _approx_tokens(text: str) -> int:
    return max(1, len(text) // 2)


def _split_sentences(text: str) -> list[str]:
    sents = [m.group().strip() for m in _SENT_RE.finditer(text)]
    return [s for s in sents if s]


def _is_page_proxy(struct_path: str) -> bool:
    return struct_path.startswith("p") and struct_path[1:].isdigit()


def _citation_label(struct_path: str, source_document: str) -> str:
    base = source_document.rsplit(".", 1)[0]
    if not struct_path:
        return base
    # 페이지 프록시("p3") → 사람이 읽기 좋은 라벨
    if _is_page_proxy(struct_path):
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
            # struct_path가 바뀌면 경계(조 또는 페이지 경계) → 마감
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
        src_ids = [b.block_id for b in blocks]
        page = self._page_span(blocks)

        if _approx_tokens(text) <= ctx.max_tokens:
            out.append(self._make(text, struct, page, src_ids, ChunkType.body,
                                  ir, ctx, idx=len(out)))
            return

        # 폴백: 문장 경계로 분할 + 1문장 오버랩(경계에 걸친 단서 보존)
        for part in self._split_with_overlap(text, ctx.max_tokens):
            out.append(self._make(part, struct, page, src_ids, ChunkType.body,
                                  ir, ctx, idx=len(out)))

    @staticmethod
    def _page_span(blocks: list[Block]):
        pages: list[int] = []
        for b in blocks:
            ref = b.page_ref
            if ref is None:
                continue
            for p in (ref if isinstance(ref, list) else [ref]):
                if p not in pages:
                    pages.append(p)
        if not pages:
            return None
        return pages[0] if len(pages) == 1 else pages

    @staticmethod
    def _split_with_overlap(text: str, max_tokens: int) -> list[str]:
        """문장 단위로 max_tokens 이하 조각을 만들고, 조각 사이에
        직전 조각의 마지막 문장을 오버랩한다(경계 단서 손실 방지)."""
        sentences = _split_sentences(text)
        if not sentences:
            return [text]
        parts: list[str] = []
        cur: list[str] = []
        cur_tokens = 0
        for s in sentences:
            s_tokens = _approx_tokens(s)
            if cur and cur_tokens + s_tokens > max_tokens:
                parts.append(" ".join(cur))
                overlap = cur[-1]
                cur = [overlap] if _approx_tokens(overlap) < max_tokens else []
                cur_tokens = _approx_tokens(overlap) if cur else 0
            cur.append(s)
            cur_tokens += s_tokens
        if cur:
            parts.append(" ".join(cur))
        return [p for p in parts if p.strip()] or [text]

    def _emit_table(self, b: Block, ir, ctx, out: list[Chunk]):
        text = self._serialize_table(b, ctx)
        if not text:
            return
        out.append(self._make(text, b.struct_path, b.page_ref, [b.block_id],
                              ChunkType.table, ir, ctx, idx=len(out)))

    @staticmethod
    def _serialize_table(b: Block, ctx: ChunkingContext) -> str:
        """표를 행 단위 '헤더: 값'으로 직렬화 — 헤더-값 관계 보존.

        예)  Item: Outpatient | Annual Limit: 200,000 KRW | Copayment: 30%
        헤더가 없거나 1행뿐이면 셀을 ' | '로 잇는 기존 방식으로 폴백한다.
        섹션(struct_path) 프리픽스로 표만 검색돼도 소속 조항을 알 수 있게 한다.
        """
        tc = b.table_content
        rows: dict[int, dict[int, str]] = {}
        for c in tc.cells:
            rows.setdefault(c.row, {})[c.col] = c.text
        if not rows:
            return ""

        header_row = min(tc.header_rows) if tc.header_rows else min(rows)
        headers = rows.get(header_row, {})
        data_rows = [r for r in sorted(rows) if r != header_row]

        lines: list[str] = []
        if headers and data_rows:
            for r in data_rows:
                pairs = []
                for col in sorted(rows[r]):
                    val = rows[r][col].strip()
                    if not val:
                        continue
                    hdr = headers.get(col, "").strip()
                    pairs.append(f"{hdr}: {val}" if hdr else val)
                if pairs:
                    lines.append(" | ".join(pairs))
        if not lines:  # 헤더 없음/1행 표 → 평탄화 폴백
            lines = [" ".join(v for _, v in sorted(rows[r].items())).strip()
                     for r in sorted(rows)]
            lines = [ln for ln in lines if ln]
        if not lines:
            return ""

        body = "\n".join(lines)
        if b.struct_path and not _is_page_proxy(b.struct_path):
            return f"[{b.struct_path}] {body}"
        return body

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
