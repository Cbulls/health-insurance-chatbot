"""
컴포넌트 인터페이스 계약(경계).

원칙: 계약은 타입만이 아니라 '경계에서 무엇을 거부하는가'까지 포함한다.
각 경계는 방어선이다 — 한 컴포넌트가 깨진 데이터를 내보내도 다음 경계가 거부하면
오염이 파이프라인 전체로 번지지 않는다.
"""
from __future__ import annotations

from typing import Protocol, runtime_checkable
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
    sparse_terms: dict[str, float]   # 형태소 토큰 -> 가중치(BM25용, Phase 2)


class ScoredChunk(BaseModel):
    model_config = ConfigDict(extra="forbid")
    chunk: Chunk
    score: float


@dataclass(frozen=True)
class AuthContext:
    """경계4 필수 인자. 권한 없는 검색을 타입 레벨에서 불가능하게 한다.

    MVP: acl_tags = {f"owner:{user_id}"} 로 문서 소유자 격리(본인 문서만 검색).
    Phase 2: 조직 부서/역할 태그로 확장.
    """
    user_id: str
    acl_tags: frozenset[str]


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
    """경계2: M3를 닫는다(table 청크가 실제 table 블록에서 왔는가)."""
    block_by_id = {b.block_id: b for b in doc.blocks}
    for ch in chunks:
        for bid in ch.meta.source_block_ids:
            if bid not in block_by_id:
                raise ContractViolation(
                    f"chunk {ch.meta.chunk_id} cites non-existent block {bid}")
        if ch.meta.chunk_type == ChunkType.table:
            kinds = {block_by_id[b].block_type for b in ch.meta.source_block_ids}
            if BlockType.table not in kinds:
                raise ContractViolation(
                    f"table chunk {ch.meta.chunk_id} traces to no table block (M3)")
        if any(block_by_id[b].is_noise for b in ch.meta.source_block_ids):
            raise ContractViolation(
                f"chunk {ch.meta.chunk_id} derived from noise block")


def verify_boundary3(embedded: list[EmbeddedChunk], expected_dim: int,
                     expected_model_id: str) -> None:
    """경계3: 차원·모델 일치 + 하이브리드 양쪽 존재.

    임베딩 모델 교체 후 남은 stale 벡터(차원 불일치)나, sparse 표현이 빠진
    반쪽 하이브리드를 인덱싱 직전에 거부한다."""
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
    """경계4 보조: 청크 acl_tags와 사용자 권한 교집합 있어야 통과(defense in depth)."""
    out = []
    for sc in chunks:
        if set(sc.chunk.meta.acl_tags) & auth.acl_tags:
            out.append(sc)
    return out


def verify_boundary5_citations(answer_citation_ids: list[str],
                               context: list[ScoredChunk]) -> None:
    """경계5: 인용 ID가 받은 컨텍스트에 실재하는가(위조 인용 차단)."""
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
