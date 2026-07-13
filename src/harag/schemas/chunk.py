"""
청크 메타데이터 스키마 — 코드 계약.

핵심 불변식:
  M1. acl_tags는 null/빈 리스트 불허. 비면 '공개'가 아니라 '인덱싱 거부'(fail-closed).
  M2. source_block_ids는 1개 이상(인용 검증 가능성 보장).
  M3. chunk_type==table이면 source 표 블록 계보가 있어야 함.
"""
from __future__ import annotations

from datetime import datetime
from enum import Enum
from typing import Optional, Union

from pydantic import BaseModel, model_validator, ConfigDict

SCHEMA_VERSION = "chunk-meta-1.0.0"


class ChunkType(str, Enum):
    body = "body"
    table = "table"
    appendix = "appendix"


class ChunkMetadata(BaseModel):
    model_config = ConfigDict(extra="forbid")

    chunk_schema_version: str = SCHEMA_VERSION

    chunk_id: str
    document_id: str
    parent_chunk_id: Optional[str] = None
    source_block_ids: list[str]

    struct_path: str = ""
    chunk_type: ChunkType
    page_ref: Union[int, list[int], None] = None

    security_level: str
    acl_tags: list[str]

    source_document: str
    citation_label: str

    parsing_version: str
    chunking_version: str
    embedding_model_id: str

    content_hash: str
    created_at: datetime

    @model_validator(mode="after")
    def _chunk_invariants(self) -> "ChunkMetadata":
        if not self.acl_tags:
            raise ValueError(
                "acl_tags must be non-empty — empty means 'reject indexing', not 'public' (M1, fail-closed)"
            )
        if any(not t.strip() for t in self.acl_tags):
            raise ValueError("acl_tags must not contain empty/whitespace tags (M1)")
        if not self.source_block_ids:
            raise ValueError(
                "source_block_ids must have >=1 entry — unanchored chunks fail citation verification (M2)"
            )
        if self.chunk_type == ChunkType.table and not self.source_block_ids:
            raise ValueError("table chunk must trace to source table block(s) (M3)")
        return self
