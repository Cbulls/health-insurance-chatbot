"""
B-2 컴포넌트 인터페이스 계약.

원칙: 계약은 타입만이 아니라 '경계에서 무엇을 거부하는가'까지 포함한다.
각 경계는 방어선이다 — 한 컴포넌트가 깨진 데이터를 내보내도 다음 경계가 거부하면
오염이 파이프라인 전체로 번지지 않는다.

이 파일이 닫는 것:
  - 경계2에서 M3 교차검증(table 청크가 실제 IR table 블록에서 왔는가)
  - 경계1에서 PoC 임계(국면 A)를 런타임 게이트로 강제
  - 경계4에서 권한 컨텍스트를 타입 레벨 필수로(권한 없는 검색 불가능)
  - 경계5에서 인용 ID 실재성 검증(source_block_ids 체인의 런타임 완성)
"""
from __future__ import annotations

from typing import Protocol, Optional, runtime_checkable
from dataclasses import dataclass

from pydantic import BaseModel, ConfigDict

from harag.schemas.ir import DocumentIR, BlockType, ParseStatus
from harag.schemas.chunk import ChunkMetadata, ChunkType


# ── 경계를 흐르는 합성 타입(스키마 + 페이로드) ───────────────
class Chunk(BaseModel):
    """경계2를 흐름: 메타데이터 + 본문 텍스트."""
    model_config = ConfigDict(extra="forbid")
    meta: ChunkMetadata
    text: str


class EmbeddedChunk(BaseModel):
    model_config = ConfigDict(extra="forbid", arbitrary_types_allowed=True)
    chunk: Chunk
    dense_vector: list[float]
    sparse_terms: dict[str, float]   # 형태소 토큰 -> 가중치(BM25용)


class ScoredChunk(BaseModel):
    model_config = ConfigDict(extra="forbid")
    chunk: Chunk
    score: float


@dataclass(frozen=True)
class AuthContext:
    """경계4 필수 인자. 권한 없는 검색을 타입 레벨에서 불가능하게 한다."""
    user_id: str
    acl_tags: frozenset[str]   # 이 사용자가 접근 가능한 태그 집합


# ── PoC 임계(국면 A에서 동결한 값이 런타임 게이트가 됨) ──────
@dataclass(frozen=True)
class QualityGate:
    min_table_recovery: float
    max_broken_char_ratio: float
    min_order_confidence: float


# ════════ 컴포넌트 Protocol ════════
@runtime_checkable
class Parser(Protocol):
    def parse(self, raw: bytes, source_format: str) -> DocumentIR: ...

@runtime_checkable
class Chunker(Protocol):
    def chunk(self, doc: DocumentIR) -> list[Chunk]: ...

@runtime_checkable
class Embedder(Protocol):
    model_id: str
    dim: int
    def embed(self, chunks: list[Chunk]) -> list[EmbeddedChunk]: ...

@runtime_checkable
class Retriever(Protocol):
    # auth는 기본값 없는 필수 인자 — 권한 없이 호출 불가
    def retrieve(self, query: str, auth: AuthContext, k: int) -> list[ScoredChunk]: ...


# ════════ 경계 검증 함수(거부 책임의 구현) ════════
class ContractViolation(Exception):
    pass


def gate_boundary1(doc: DocumentIR, gate: QualityGate) -> bool:
    """경계1: Chunker가 청킹 전에 호출. 통과 못 하면 청킹 안 함(quarantine)."""
    if doc.parse_status == ParseStatus.failed:
        return False
    q = doc.parse_quality
    if q.table_recovery_avg < gate.min_table_recovery:
        return False
    if q.broken_char_ratio > gate.max_broken_char_ratio:
        return False
    if q.order_confidence < gate.min_order_confidence:
        return False
    return True


def verify_boundary2(doc: DocumentIR, chunks: list[Chunk]) -> None:
    """경계2: M3를 닫는다. Chunker는 IR과 청크를 둘 다 쥔 유일한 컴포넌트라
    여기서만 'table 청크가 실제 table 블록에서 왔는가'를 교차검증할 수 있다."""
    block_by_id = {b.block_id: b for b in doc.blocks}
    for ch in chunks:
        # 모든 source_block_id가 IR에 실재해야 함(인용 체인의 뿌리)
        for bid in ch.meta.source_block_ids:
            if bid not in block_by_id:
                raise ContractViolation(
                    f"chunk {ch.meta.chunk_id} cites non-existent block {bid}")
        # M3: table 청크는 적어도 하나의 table 블록에서 와야 함
        if ch.meta.chunk_type == ChunkType.table:
            kinds = {block_by_id[b].block_type for b in ch.meta.source_block_ids}
            if BlockType.table not in kinds:
                raise ContractViolation(
                    f"table chunk {ch.meta.chunk_id} traces to no table block (M3)")
        # 노이즈 블록에서 온 청크는 금지(머리말 오염 차단)
        if any(block_by_id[b].is_noise for b in ch.meta.source_block_ids):
            raise ContractViolation(
                f"chunk {ch.meta.chunk_id} derived from noise block")


def verify_boundary3(embedded: list[EmbeddedChunk], expected_dim: int,
                     expected_model_id: str) -> None:
    """경계3: 차원·모델 일치 + 하이브리드 양쪽 존재."""
    for ec in embedded:
        if len(ec.dense_vector) != expected_dim:
            raise ContractViolation(
                f"dim mismatch: {len(ec.dense_vector)} != {expected_dim} "
                f"(stale vectors from model swap?)")
        if ec.chunk.meta.embedding_model_id != expected_model_id:
            raise ContractViolation(
                f"model id mismatch on {ec.chunk.meta.chunk_id}")
        if not ec.sparse_terms:
            raise ContractViolation(
                f"chunk {ec.chunk.meta.chunk_id} missing sparse repr (hybrid is half-broken)")


def filter_by_auth(chunks: list[ScoredChunk], auth: AuthContext) -> list[ScoredChunk]:
    """경계4 보조: 청크의 acl_tags와 사용자 권한이 교집합 있어야 통과.
    (실제 적용은 인덱스 검색 시점이지만, 후처리 방어선으로도 둔다 = defense in depth)"""
    out = []
    for sc in chunks:
        if set(sc.chunk.meta.acl_tags) & auth.acl_tags:
            out.append(sc)
    return out


def verify_boundary5_citations(answer_citation_ids: list[str],
                               context: list[ScoredChunk]) -> None:
    """경계5: 인용 ID가 받은 컨텍스트에 실재하는가.
    source_block_ids 체인이 런타임에 완성되는 지점 — 모델이 인용을 위조하면 여기서 걸린다."""
    available = {sc.chunk.meta.chunk_id for sc in context}
    for cid in answer_citation_ids:
        if cid not in available:
            raise ContractViolation(
                f"answer cites chunk {cid} not in retrieved context (fabricated citation)")


def must_abstain(context: list[ScoredChunk], min_score: float) -> bool:
    """경계5: 빈 컨텍스트 또는 전 후보 저점수면 생성 대신 abstention(FR-8)."""
    if not context:
        return True
    return max(sc.score for sc in context) < min_score
