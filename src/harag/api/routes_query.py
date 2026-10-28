"""
질의 라우트 — 읽기 경로의 진입점(얇은 라우트).

라우트는 (1) 인증 강제 (2) 컴포넌트 연결 (3) 안전한 응답 변환만 한다.
"""
from __future__ import annotations

import logging

from fastapi import APIRouter, Depends, HTTPException, status
from fastapi.responses import StreamingResponse

from harag.api.middleware import current_trace_id
from harag.api.ratelimit import enforce_rate_limit
from harag.api.citations_fmt import citations_from_chunks
from harag.api.schemas import QueryRequest, QueryResponse
from harag.contracts.boundaries import AuthContext

logger = logging.getLogger("harag.api")

router = APIRouter(prefix="/v1", tags=["query"])

_MAX_DOC_IDS = 8


async def _auth_with_budget(
    auth: AuthContext = Depends(enforce_rate_limit),
) -> AuthContext:
    # enforce_daily_budget는 require_auth를 다시 부르므로 rate limit 통과 후
    # 예산만 검사한다.
    from harag.api.daily_budget import check_budget
    check_budget(auth.user_id)
    return auth


def _resolve_document_id(req: QueryRequest, auth: AuthContext) -> str | None:
    """단일 document_id 접근 검사. 레거시·테스트용."""
    doc_id = (req.document_id or "").strip() or None
    if not doc_id:
        return None
    from harag.api.deps import get_ingest
    ingest = get_ingest()
    dept_tags = [t for t in auth.acl_tags if t.startswith("dept:")]
    meta = ingest._metadata.find_accessible(doc_id, auth.user_id, dept_tags)
    if meta is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND,
                            detail="document not found")
    return doc_id


def _resolve_document_ids(req: QueryRequest, auth: AuthContext) -> list[str]:
    """document_id + document_ids 합집합. 접근 불가 id는 건너뛰고, 후보가 있었는데 전부 실패면 404."""
    raw: list[str] = []
    single = (req.document_id or "").strip()
    if single:
        raw.append(single)
    for d in req.document_ids or []:
        s = (d or "").strip()
        if s:
            raw.append(s)
    # 순서 보존 중복 제거, 상한
    seen: set[str] = set()
    candidates: list[str] = []
    for d in raw:
        if d in seen:
            continue
        seen.add(d)
        candidates.append(d)
        if len(candidates) >= _MAX_DOC_IDS:
            break
    if not candidates:
        return []

    from harag.api.deps import get_ingest
    ingest = get_ingest()
    dept_tags = [t for t in auth.acl_tags if t.startswith("dept:")]
    allowed: list[str] = []
    for doc_id in candidates:
        meta = ingest._metadata.find_accessible(
            doc_id, auth.user_id, dept_tags)
        if meta is not None:
            allowed.append(doc_id)
    if not allowed:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND,
                            detail="document not found")
    return allowed


@router.post("/query", response_model=QueryResponse)
async def query(
    req: QueryRequest,
    auth: AuthContext = Depends(_auth_with_budget),
):
    document_ids = _resolve_document_ids(req, auth)
    from harag.api.deps import get_query_pipeline
    pipeline = get_query_pipeline()
    match_ctx = (req.match_context or "").strip() or None
    result = await pipeline.answer(
        query=req.query, auth=auth, conversation_id=req.conversation_id,
        document_ids=document_ids or None,
        match_context=match_ctx)
    return QueryResponse(
        answer=result.answer,
        citations=citations_from_chunks(result.context_chunks),
        abstained=result.abstained,
        abstain_reason=result.abstain_reason,
        trace_id=current_trace_id(),
    )


@router.post("/query/stream")
async def query_stream(
    req: QueryRequest,
    auth: AuthContext = Depends(_auth_with_budget),
):
    """스트리밍 질의(SSE). abstention은 스트리밍 전에 결정된다."""
    document_ids = _resolve_document_ids(req, auth)
    from harag.api.deps import get_query_pipeline, StreamEvent
    pipeline = get_query_pipeline()
    match_ctx = (req.match_context or "").strip() or None

    async def event_stream():
        # 스트림은 이미 200으로 시작됐으므로, 도중 예외를 그냥 전파하면
        # 연결이 뚝 끊긴다. error 이벤트로 변환해 클라이언트가 표시하게 한다.
        try:
            async for event in pipeline.answer_stream(
                query=req.query, auth=auth, conversation_id=req.conversation_id,
                document_ids=document_ids or None,
                match_context=match_ctx,
            ):
                yield f"data: {event.to_sse()}\n\n"
        except Exception:  # noqa: BLE001 — 스트림 최후 방어선(내부 미누설)
            logger.exception("stream error", extra={"trace_id": current_trace_id()})
            evt = StreamEvent(kind="error", data="internal_error")
            yield f"data: {evt.to_sse()}\n\n"

    return StreamingResponse(event_stream(), media_type="text/event-stream")
