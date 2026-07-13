"""
질의 라우트 — 읽기 경로의 진입점(얇은 라우트).

라우트는 (1) 인증 강제 (2) 컴포넌트 연결 (3) 안전한 응답 변환만 한다.
"""
from __future__ import annotations

from fastapi import APIRouter, Depends
from fastapi.responses import StreamingResponse

from harag.api.auth import require_auth
from harag.api.middleware import current_trace_id
from harag.api.schemas import QueryRequest, QueryResponse, Citation
from harag.contracts.boundaries import AuthContext, ScoredChunk

router = APIRouter(prefix="/v1", tags=["query"])


def _to_citations(chunks: list[ScoredChunk]) -> list[Citation]:
    """내부 청크 → 사용자용 출처. 내부 메타는 떨군다(누설 방지)."""
    seen, out = set(), []
    for sc in chunks:
        label = sc.chunk.meta.citation_label
        if label in seen:
            continue
        seen.add(label)
        out.append(Citation(
            citation_label=label,
            source_document=sc.chunk.meta.source_document,
            page=sc.chunk.meta.page_ref,
        ))
    return out


@router.post("/query", response_model=QueryResponse)
async def query(
    req: QueryRequest,
    auth: AuthContext = Depends(require_auth),
):
    from harag.api.deps import get_query_pipeline
    pipeline = get_query_pipeline()
    result = await pipeline.answer(
        query=req.query, auth=auth, conversation_id=req.conversation_id)
    return QueryResponse(
        answer=result.answer,
        citations=_to_citations(result.context_chunks),
        abstained=result.abstained,
        abstain_reason=result.abstain_reason,
        trace_id=current_trace_id(),
    )


@router.post("/query/stream")
async def query_stream(
    req: QueryRequest,
    auth: AuthContext = Depends(require_auth),
):
    """스트리밍 질의(SSE). abstention은 스트리밍 전에 결정된다."""
    from harag.api.deps import get_query_pipeline
    pipeline = get_query_pipeline()

    async def event_stream():
        async for event in pipeline.answer_stream(
            query=req.query, auth=auth, conversation_id=req.conversation_id,
        ):
            yield f"data: {event.to_sse()}\n\n"

    return StreamingResponse(event_stream(), media_type="text/event-stream")
