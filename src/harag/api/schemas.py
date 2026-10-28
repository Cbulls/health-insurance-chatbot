"""API 요청/응답 스키마 — Pydantic 계약(외부 경계)."""
from __future__ import annotations

from typing import Optional, Union

from pydantic import BaseModel, Field


class QueryRequest(BaseModel):
    query: str = Field(min_length=1, max_length=2000)
    conversation_id: Optional[str] = None
    document_id: Optional[str] = Field(default=None, max_length=64)
    document_ids: list[str] = Field(default_factory=list, max_length=8)
    # 결과 채팅: guidance의 why·상품 요약·매칭 담보 (PDF에 없는 매칭 사실)
    match_context: str = Field(default="", max_length=4000)


class Citation(BaseModel):
    citation_label: str
    source_document: str
    page: Union[int, list[int], None] = None
    chunk_id: str = ""
    document_id: str = ""
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
    display_name: str = ""
    product_line: str = ""
    product_line_label: str = ""
    doc_kind_label: str = ""
    cost_nature_label: str = ""
    summary: str = ""
    insurer: str = ""
    source_url: str = ""
    has_file: bool = False


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


class AssessRequest(BaseModel):
    text: str = Field(min_length=1, max_length=2000)
    occupation: Optional[str] = None
    concern: Optional[str] = None
    concerns: list[str] = Field(default_factory=list)
    age_band: Optional[str] = None
    sex: Optional[str] = None


class RefCondition(BaseModel):
    kcd: str
    name: str
    source: str = ""


class SymptomItem(BaseModel):
    code: str
    label: str
    type: str = "usual"
    ref_conditions: list[RefCondition] = Field(default_factory=list)


class AssessResponse(BaseModel):
    masked_preview: str
    pii_kinds: dict[str, int] = Field(default_factory=dict)
    symptoms: list[SymptomItem] = Field(default_factory=list)
    occupation: Optional[str] = None
    occupation_label: Optional[str] = None
    concern: Optional[str] = None
    concern_label: Optional[str] = None
    concerns: list[str] = Field(default_factory=list)
    concern_labels: list[str] = Field(default_factory=list)
    age_band: Optional[str] = None
    sex: Optional[str] = None
    abstained: bool = False
    abstain_reason: Optional[str] = None
    trace_id: str = ""


class GuidanceRequest(BaseModel):
    text: str = Field(default="", max_length=2000)
    occupation: Optional[str] = None
    concern: Optional[str] = None
    concerns: list[str] = Field(default_factory=list)
    age_band: Optional[str] = None
    sex: Optional[str] = None
    symptoms: list[str] = Field(default_factory=list)


class PurgeRequest(BaseModel):
    conversation_id: Optional[str] = None


class PurgeResponse(BaseModel):
    raw_persisted: bool = False
    conversation_cleared: bool = False
    trace_id: str = ""


class AreaItem(BaseModel):
    label: str
    source: str = ""
    confidence: float = 0.0
    matched: int = 0
    selected: int = 0


class CoverageItem(BaseModel):
    code: str
    label: str
    summary: str = ""
    score: float = 0.0
    why: list[str] = Field(default_factory=list)


class RefStat(BaseModel):
    kcd: str
    name: str
    inpatient_share: float | None = None
    age_sex_count: int | None = None
    total_count: int | None = None
    coverage_link: str = ""
    source: str = "건강보험심사평가원 질병정보서비스"
    cached: bool = True


class ProductCard(BaseModel):
    document_id: str = ""
    title: str
    citation_label: str = ""
    snippet: str = ""
    matched_coverages: list[str] = Field(default_factory=list)
    scope: str = "personal"
    name: str = ""
    product_line: str = ""
    product_line_label: str = ""
    doc_kind_label: str = ""
    cost_nature_label: str = ""
    summary: str = ""
    insurer: str = ""
    source_url: str = ""


class GuidanceResponse(BaseModel):
    profile: dict = Field(default_factory=dict)
    areas: list[AreaItem] = Field(default_factory=list)
    coverages: list[CoverageItem] = Field(default_factory=list)
    products: list[ProductCard] = Field(default_factory=list)
    ref_stats: list[RefStat] = Field(default_factory=list)
    explanation: str = ""
    explanation_why: str = ""
    explanation_caution: str = ""
    citations: list[str] = Field(default_factory=list)
    guard_flags: list[str] = Field(default_factory=list)
    abstained: bool = False
    abstain_reason: Optional[str] = None
    raw_stored: bool = False
    disclaimer: str = ""
    trace_id: str = ""
