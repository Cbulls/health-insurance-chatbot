"""
청커 — IR을 구조·의미 기반 청크로 변환(FR-3).

핵심:
  - 같은 struct_path의 연속 본문 블록을 한 청크로 묶음(조 단위, 폴백=페이지)
  - 상위 헤더(장>조)를 struct_path·텍스트 프리픽스로 상속, parent_chunk_id 채움
  - 표는 Markdown 표로 직렬화(행렬 관계 보존) + 섹션 프리픽스
  - 토큰 상한(500) 초과 시 문장 윈도우 + overlap(50 토큰)
  - (선택) 임베딩 유사도로 주제 전환 지점에서 의미 분할 — 로컬 해시면 스킵
  - 노이즈 블록은 건너뜀
"""
from __future__ import annotations

import hashlib
import math
import re
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Protocol

from harag.schemas.ir import DocumentIR, Block, BlockType
from harag.schemas.chunk import ChunkMetadata, ChunkType
from harag.contracts.boundaries import Chunk


@dataclass
class ChunkingContext:
    """청킹에 필요한 문서 외 정보(권한·출처·모델·상한)."""
    acl_tags: list[str]
    source_document: str
    embedding_model_id: str
    max_tokens: int = 500
    overlap_tokens: int = 50
    chunking_version: str = "struct-sem-0.3"
    # 의미 분할: None이거나 local-hash면 스킵. cosine 유사도 임계값 미만에서 분할.
    embedding_model: object | None = None
    semantic_threshold: float = 0.35


class EmbeddingModelLike(Protocol):
    model_id: str
    def encode(self, texts: list[str]) -> list[list[float]]: ...


_SENT_RE = re.compile(r"[^.!?。\n]+[.!?。]?\s*")
_CHAPTER_RE = re.compile(r"제\s*\d+\s*장|Section\s+\d+", re.I)
_CLAUSE_RE = re.compile(
    r"제\s*\d+\s*(?:조(?:의\s*\d+)?|절|관)|Article\s+\d+", re.I)


def _approx_tokens(text: str) -> int:
    return max(1, len(text) // 2)


def _split_sentences(text: str) -> list[str]:
    sents = [m.group().strip() for m in _SENT_RE.finditer(text)]
    return [s for s in sents if s]


def _is_page_proxy(struct_path: str) -> bool:
    # "p3" 또는 경로의 마지막 조각이 페이지 프록시
    leaf = struct_path.rsplit(" > ", 1)[-1]
    return leaf.startswith("p") and leaf[1:].isdigit()


def _citation_label(struct_path: str, source_document: str) -> str:
    base = source_document.rsplit(".", 1)[0]
    if not struct_path:
        return base
    leaf = struct_path.rsplit(" > ", 1)[-1]
    if _is_page_proxy(leaf) and " > " not in struct_path:
        return f"{base} {leaf[1:]}페이지"
    return f"{base} {struct_path}".strip()


def _cosine(a: list[float], b: list[float]) -> float:
    dot = sum(x * y for x, y in zip(a, b))
    na = math.sqrt(sum(x * x for x in a))
    nb = math.sqrt(sum(y * y for y in b))
    if na == 0.0 or nb == 0.0:
        return 0.0
    return dot / (na * nb)


def _is_local_hash(model) -> bool:
    mid = getattr(model, "model_id", "") or ""
    return mid.startswith("local-hash")


class StructuralChunker:
    def chunk(self, ir: DocumentIR, ctx: ChunkingContext) -> list[Chunk]:
        chunks: list[Chunk] = []
        buf: list[Block] = []
        # 장/조 스택 → 상속 경로. parent_chunk_id용: 경로별 최근 본문 청크
        chapter: str | None = None
        last_body_by_parent: dict[str, str] = {}
        last_body_id: str | None = None

        def flush():
            nonlocal last_body_id
            if not buf:
                return
            inherited = self._inherited_path(buf[0].struct_path, chapter)
            before = len(chunks)
            self._emit_body(list(buf), ir, ctx, chunks, inherited,
                            parent_id=last_body_by_parent.get(
                                self._parent_key(inherited)))
            for c in chunks[before:]:
                last_body_id = c.meta.chunk_id
                last_body_by_parent[self._parent_key(inherited)] = last_body_id
                # 현재 경로 자체도 다음 형제의 parent 후보
                last_body_by_parent[inherited] = last_body_id
            buf.clear()

        for b in ir.blocks:
            if b.is_noise:
                continue
            # 장 헤더면 스택 갱신(본문 버퍼는 struct_path 변경 시 flush)
            if b.block_type != BlockType.table and b.text:
                ch = self._as_chapter(b.struct_path) or self._as_chapter(b.text)
                if ch:
                    chapter = ch
            if b.block_type == BlockType.table:
                flush()
                inherited = self._inherited_path(b.struct_path, chapter)
                parent = last_body_by_parent.get(self._parent_key(inherited))
                self._emit_table(b, ir, ctx, chunks, inherited, parent)
                continue
            if buf and buf[-1].struct_path != b.struct_path:
                flush()
            buf.append(b)
        flush()
        return chunks

    @staticmethod
    def _as_chapter(label: str) -> str | None:
        m = _CHAPTER_RE.search(label or "")
        if m is None:
            return None
        return re.sub(r"\s+", " ", m.group(0)).strip()

    @staticmethod
    def _inherited_path(struct: str, chapter: str | None) -> str:
        if not struct:
            return chapter or ""
        if _is_page_proxy(struct):
            return struct
        if chapter and chapter not in struct and not _CHAPTER_RE.search(struct):
            return f"{chapter} > {struct}"
        return struct

    @staticmethod
    def _parent_key(path: str) -> str:
        if " > " in path:
            return path.rsplit(" > ", 1)[0]
        return ""

    def _emit_body(self, blocks: list[Block], ir, ctx, out: list[Chunk],
                   struct: str, parent_id: str | None):
        text = "\n".join(b.text or "" for b in blocks).strip()
        if not text:
            return
        src_ids = [b.block_id for b in blocks]
        page = self._page_span(blocks)

        # 의미 분할 → 조각 리스트, 각 조각을 토큰 윈도우로 재분할
        segments = self._semantic_segments(text, ctx)
        for seg in segments:
            if _approx_tokens(seg) <= ctx.max_tokens:
                parts = [seg]
            else:
                parts = self._split_with_overlap(
                    seg, ctx.max_tokens, ctx.overlap_tokens)
            for part in parts:
                prefixed = self._prefix_context(part, struct)
                out.append(self._make(
                    prefixed, struct, page, src_ids, ChunkType.body,
                    ir, ctx, idx=len(out), parent_id=parent_id))

    def _semantic_segments(self, text: str, ctx: ChunkingContext) -> list[str]:
        """문장 임베딩 유사도가 임계값 미만인 지점에서 분할.

        로컬 해시·모델 없음·문장 2개 미만이면 분할하지 않는다.
        """
        model = ctx.embedding_model
        sentences = _split_sentences(text)
        if (model is None or _is_local_hash(model) or len(sentences) < 3
                or _approx_tokens(text) <= ctx.max_tokens):
            return [text]
        try:
            vecs = model.encode(sentences)
        except Exception:  # noqa: BLE001 — 의미 분할 실패 시 구조 단위 유지
            return [text]
        if len(vecs) != len(sentences):
            return [text]

        segments: list[list[str]] = [[sentences[0]]]
        for i in range(1, len(sentences)):
            sim = _cosine(vecs[i - 1], vecs[i])
            if sim < ctx.semantic_threshold and _approx_tokens(
                    " ".join(segments[-1])) >= ctx.max_tokens // 3:
                segments.append([sentences[i]])
            else:
                segments[-1].append(sentences[i])
        return [" ".join(s) for s in segments if s]

    @staticmethod
    def _prefix_context(text: str, struct: str) -> str:
        if not struct or _is_page_proxy(struct):
            return text
        if text.startswith(f"[{struct}]"):
            return text
        return f"[{struct}]\n{text}"

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
    def _split_with_overlap(text: str, max_tokens: int,
                            overlap_tokens: int) -> list[str]:
        """문장 단위 슬라이딩 윈도우. overlap_tokens만큼 이전 문장들을 겹친다."""
        sentences = _split_sentences(text)
        if not sentences:
            return [text]
        parts: list[str] = []
        i = 0
        n = len(sentences)
        while i < n:
            cur: list[str] = []
            cur_tokens = 0
            j = i
            while j < n:
                s_tok = _approx_tokens(sentences[j])
                if cur and cur_tokens + s_tok > max_tokens:
                    break
                cur.append(sentences[j])
                cur_tokens += s_tok
                j += 1
            if not cur:
                # 한 문장이 상한 초과 → 강제 수용
                cur = [sentences[i]]
                j = i + 1
            parts.append(" ".join(cur))
            if j >= n:
                break
            # 오버랩: 끝에서부터 overlap_tokens만큼 문장 포함해 다음 시작점 결정
            overlap: list[str] = []
            ov = 0
            for s in reversed(cur):
                t = _approx_tokens(s)
                if overlap and ov + t > overlap_tokens:
                    break
                overlap.insert(0, s)
                ov += t
            # 다음 윈도우는 오버랩 시작 = 현재 구간의 (len(cur)-len(overlap)) 지점
            advance = max(1, len(cur) - len(overlap))
            i += advance
        return [p for p in parts if p.strip()] or [text]

    def _emit_table(self, b: Block, ir, ctx, out: list[Chunk],
                    struct: str, parent_id: str | None):
        text = self._serialize_table(b, struct)
        if not text:
            return
        out.append(self._make(text, struct, b.page_ref, [b.block_id],
                              ChunkType.table, ir, ctx, idx=len(out),
                              parent_id=parent_id))

    @staticmethod
    def _serialize_table(b: Block, struct: str) -> str:
        """표를 Markdown으로 직렬화 — 헤더/구분선/데이터 행."""
        tc = b.table_content
        rows: dict[int, dict[int, str]] = {}
        for c in tc.cells:
            rows.setdefault(c.row, {})[c.col] = c.text
        if not rows:
            return ""

        n_cols = max((max(r.keys()) + 1 for r in rows.values()), default=0)
        if n_cols == 0:
            return ""

        def row_cells(r: int) -> list[str]:
            return [(rows.get(r, {}).get(c, "") or "").replace("|", "\\|")
                    for c in range(n_cols)]

        header_row = min(tc.header_rows) if tc.header_rows else min(rows)
        header = row_cells(header_row)
        sep = ["---"] * n_cols
        md_lines = [
            "| " + " | ".join(header) + " |",
            "| " + " | ".join(sep) + " |",
        ]
        for r in sorted(rows):
            if r == header_row:
                continue
            md_lines.append("| " + " | ".join(row_cells(r)) + " |")

        body = "\n".join(md_lines)
        if struct and not _is_page_proxy(struct):
            return f"[{struct}]\n{body}"
        return body

    def _make(self, text, struct, page, src_ids, ctype, ir, ctx, idx,
              parent_id=None) -> Chunk:
        content_hash = hashlib.sha256(text.encode()).hexdigest()[:16]
        meta = ChunkMetadata(
            chunk_id=f"{ir.document_id}-c{idx}",
            document_id=ir.document_id,
            parent_chunk_id=parent_id,
            source_block_ids=src_ids,
            struct_path=struct,
            chunk_type=ctype,
            page_ref=page,
            security_level=ir.source_metadata.security_level,
            acl_tags=list(ctx.acl_tags),
            source_document=ctx.source_document,
            citation_label=_citation_label(struct, ctx.source_document),
            parsing_version=ir.parser_version,
            chunking_version=ctx.chunking_version,
            embedding_model_id=ctx.embedding_model_id,
            content_hash=content_hash,
            created_at=datetime.now(timezone.utc),
        )
        return Chunk(meta=meta, text=text)
