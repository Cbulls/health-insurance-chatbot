"""
API 요청/응답 스키마 — API 경계의 계약.

원칙: 응답은 내부 구조를 누설하지 않는다(설계 원칙 3).
  - 청크 내부 메타(embedding_model_id 등)는 응답에 노출 안 함.
  - 출처는 사용자에게 보여줄 citation_label과 페이지만.
"""
from __future__ import annotations

from pydantic import BaseModel, Field


# ── 질의 ──
class QueryRequest(BaseModel):
    """질의 요청. 멀티턴은 conversation_id로 이어짐."""
    query: str = Field(min_length=1, max_length=2000)
    conversation_id: str | None = None
    # top_k 등은 서버 설정이 결정. 클라이언트가 검색 깊이를 못 키우게 함(남용 방지).


class Citation(BaseModel):
    """사용자에게 보여줄 출처. 내부 chunk_id가 아니라 표시용 정보만."""
    citation_label: str       # "여비규정 제12조"
    source_document: str      # 파일 표시명
    page: int | list[int] | None = None


class QueryResponse(BaseModel):
    """질의 응답. abstain이면 answer=null, abstain_reason 제공."""
    answer: str | None
    citations: list[Citation] = Field(default_factory=list)
    abstained: bool = False
    abstain_reason: str | None = None
    trace_id: str             # 관측·사후 추적용(NFR-6)


# ── 문서 수집(인덱싱) ──
class IngestResponse(BaseModel):
    """업로드 수락 응답. 인덱싱은 비동기(쓰기 경로 격리, NFR-2)."""
    document_id: str
    status: str               # accepted / quarantine / rejected
    trace_id: str


# ── 에러 (구조 누설 금지) ──
class ErrorResponse(BaseModel):
    """외부에 보이는 에러. 내부 스택·구조 정보 없음(설계 원칙 3)."""
    error: str                # 사용자용 안전 메시지
    trace_id: str             # 지원팀이 로그와 대조할 키(상세는 로그에만)
