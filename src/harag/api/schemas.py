"""API 요청/응답 스키마 — Pydantic 계약(외부 경계)."""
from __future__ import annotations

from typing import Optional, Union

from pydantic import BaseModel, Field


class QueryRequest(BaseModel):
    query: str = Field(min_length=1, max_length=2000)
    conversation_id: Optional[str] = None


class Citation(BaseModel):
    citation_label: str
    source_document: str
    page: Union[int, list[int], None] = None


class QueryResponse(BaseModel):
    answer: Optional[str]
    citations: list[Citation] = Field(default_factory=list)
    abstained: bool = False
    abstain_reason: Optional[str] = None
    trace_id: str = ""


class IngestResponse(BaseModel):
    document_id: str
    status: str            # "accepted" | "duplicate"
    trace_id: str = ""


class DocumentStatus(BaseModel):
    document_id: str
    status: str            # "processing" | "ready" | "failed"
    filename: str
    n_chunks: int = 0
    error: Optional[str] = None
