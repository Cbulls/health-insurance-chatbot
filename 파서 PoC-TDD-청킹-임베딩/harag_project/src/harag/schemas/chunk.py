"""
청크 메타데이터 스키마 — 코드 계약.

설계 원칙:
  - 모든 필드는 '있으면 좋은' 것이 아니라 '빼면 죽는 기능'으로 역산해 존재한다.
  - fail-closed: 권한/근거가 불확실하면 통과가 아니라 거부한다.

핵심 불변식(국면 B에서 동결):
  M1. acl_tags는 null/빈 리스트 불허. 비면 '공개'가 아니라 '인덱싱 거부'(fail-closed).
      → 권한 미상 청크가 검색에 노출되는 사고를 스키마가 원천 차단.
  M2. source_block_ids는 1개 이상. 어떤 IR 블록에도 안 매달린 청크는 인용 검증 불가 → reject.
  M3. chunk_type==table이면 source 표 블록 계보가 있어야 함(본문과 섞이지 않음).
"""
from __future__ import annotations

from datetime import datetime
from enum import Enum
from typing import Optional, Union

from pydantic import BaseModel, Field, model_validator, ConfigDict

SCHEMA_VERSION = "chunk-meta-1.0.0"


class ChunkType(str, Enum):
    body = "body"
    table = "table"
    appendix = "appendix"


class ChunkMetadata(BaseModel):
    model_config = ConfigDict(extra="forbid")

    # ── 버전(1번 필드) ──────────────────────────────
    chunk_schema_version: str = SCHEMA_VERSION

    # ── 식별·계보 ──────────────────────────────────
    chunk_id: str
    document_id: str                       # upsert 단위(FR-5): 전량 삭제·재적재 키
    parent_chunk_id: Optional[str] = None  # small-to-big: 검색=자식, 생성=부모
    source_block_ids: list[str]            # M2: IR 블록 역추적(인용 검증의 근거)

    # ── 검색 필터용 ─────────────────────────────────
    struct_path: str = ""                  # IR에서 전파. 조항 단위 필터·인용 표시
    chunk_type: ChunkType
    page_ref: Union[int, list[int], None] = None

    # ── 접근통제(NFR-5) ────────────────────────────
    security_level: str
    acl_tags: list[str]                    # M1: dense·sparse 양쪽 인덱스에 동일 적재

    # ── 출처·인용(FR-8) ────────────────────────────
    source_document: str
    citation_label: str

    # ── 버전·재인덱싱(NFR-8) ───────────────────────
    parsing_version: str
    chunking_version: str
    embedding_model_id: str                # 모델 교체 감지·부분 재임베딩 판단

    # ── 무결성 ──────────────────────────────────────
    content_hash: str                      # 변경 감지(증분 업데이트)
    created_at: datetime

    @model_validator(mode="after")
    def _chunk_invariants(self) -> "ChunkMetadata":
        # M1: acl_tags fail-closed
        if not self.acl_tags:
            raise ValueError(
                "acl_tags must be non-empty — empty means 'reject indexing', not 'public' (M1, fail-closed)"
            )
        if any(not t.strip() for t in self.acl_tags):
            raise ValueError("acl_tags must not contain empty/whitespace tags (M1)")
        # M2: 인용 검증 가능성 보장
        if not self.source_block_ids:
            raise ValueError(
                "source_block_ids must have >=1 entry — unanchored chunks fail citation verification (M2)"
            )
        # M3: 표 청크는 본문과 섞이지 않음(계보로 표 블록을 가리켜야 함)
        #     (블록 타입 교차검증은 IR과 조인하는 빌드 단계 책임; 여기선 형식 보장)
        if self.chunk_type == ChunkType.table and not self.source_block_ids:
            raise ValueError("table chunk must trace to source table block(s) (M3)")
        return self
