"""
질의 라우트 — 읽기 경로의 진입점(얇은 라우트).

라우트는 (1) 인증 강제 (2) 컴포넌트 연결 (3) 안전한 응답 변환만 한다.
"""
from __future__ import annotations

import logging

from fastapi import APIRouter, Depends
from fastapi.responses import StreamingResponse

from harag.api.middleware import current_trace_id
from harag.api.ratelimit import enforce_rate_limit
from harag.api.citations_fmt import citations_from_chunks
from harag.api.schemas import QueryRequest, QueryResponse
from harag.contracts.boundaries import AuthContext

logger = logging.getLogger("harag.api")

router = APIRouter(prefix="/v1", tags=["query"])


async def _auth_with_budget(
    auth: AuthContext = Depends(enforce_rate_limit),
) -> AuthContext:
    # enforce_daily_budget는 require_auth를 다시 부르므로 rate limit 통과 후
    # 예산만 검사한다.
    from harag.api.daily_budget import check_budget
    check_budget(auth.user_id)
    return auth


@router.post("/query", response_model=QueryResponse)
async def query(
    req: QueryRequest,
    auth: AuthContext = Depends(_auth_with_budget),
):
    from harag.api.deps import get_query_pipeline
    pipeline = get_query_pipeline()
    result = await pipeline.answer(
        query=req.query, auth=auth, conversation_id=req.conversation_id)
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
    from harag.api.deps import get_query_pipeline, StreamEvent
    pipeline = get_query_pipeline()

    async def event_stream():
        # 스트림은 이미 200으로 시작됐으므로, 도중 예외를 그냥 전파하면
        # 연결이 뚝 끊긴다. error 이벤트로 변환해 클라이언트가 표시하게 한다.
        try:
            async for event in pipeline.answer_stream(
                query=req.query, auth=auth, conversation_id=req.conversation_id,
            ):
                yield f"data: {event.to_sse()}\n\n"
        except Exception:  # noqa: BLE001 — 스트림 최후 방어선(내부 미누설)
            logger.exception("stream error", extra={"trace_id": current_trace_id()})
            evt = StreamEvent(kind="error", data="internal_error")
            yield f"data: {evt.to_sse()}\n\n"

    return StreamingResponse(event_stream(), media_type="text/event-stream")
