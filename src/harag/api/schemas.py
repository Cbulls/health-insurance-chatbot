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
    chunk_id: str = ""
    struct_path: str = ""
    snippet: str = ""


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
    scope: str = "personal"          # personal | shared | library
    uploaded_by: str = ""
    department: str = ""
    collection_id: str = ""


class DeleteResponse(BaseModel):
    document_id: str
    status: str = "deleted"
    trace_id: str = ""


class CollectionCreate(BaseModel):
    title: str = Field(min_length=1, max_length=256)
    description: str = Field(default="", max_length=1024)
    slug: Optional[str] = Field(default=None, max_length=128)


class CollectionOut(BaseModel):
    id: str
    slug: str
    title: str
    description: str = ""
    created_by: str = ""
    n_documents: int = 0


class CollectionDetail(CollectionOut):
    documents: list[DocumentStatus] = Field(default_factory=list)
