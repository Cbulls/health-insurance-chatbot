"""
질의 라우트 — 읽기 경로의 진입점.

설계: 라우트는 얇다. 로직은 컴포넌트(retriever, generator)에 있고
라우트는 (1) 인증 강제 (2) 컴포넌트 연결 (3) 안전한 응답 변환만 한다.

모든 라우트가 require_auth를 의존성으로 받는다 →
인증 없는 접근이 구조적으로 불가능(신뢰 경계).
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
    """내부 청크 → 사용자용 출처. 내부 메타(embedding_model_id 등)는 떨군다(누설 방지)."""
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
    auth: AuthContext = Depends(require_auth),  # 신뢰 경계 — 없으면 라우트 진입 불가
):
    """질의 → 검색(ACL 적용)→리랭킹→생성→안전 응답.

    auth는 게이트웨이가 검증해 넘긴 권한. 이 auth가 retriever로 흘러
    검색 시점 ACL이 적용된다(B-4). 라우트는 권한을 재해석하지 않는다.
    """
    from harag.api.deps import get_query_pipeline  # 지연 import(순환 회피)
    pipeline = get_query_pipeline()

    result = await pipeline.answer(
        query=req.query,
        auth=auth,
        conversation_id=req.conversation_id,
    )
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
    """스트리밍 질의(SSE). TTFT 단축으로 체감 지연 개선(FR-8, NFR-1).

    abstention은 스트리밍 전에 결정되므로, 빈 컨텍스트면 토큰을 흘리기 전에
    abstain 이벤트를 보낸다(지어낸 토큰이 사용자에게 노출되지 않음).
    """
    from harag.api.deps import get_query_pipeline
    pipeline = get_query_pipeline()

    async def event_stream():
        async for event in pipeline.answer_stream(
            query=req.query, auth=auth, conversation_id=req.conversation_id,
        ):
            yield f"data: {event.to_sse()}\n\n"

    return StreamingResponse(event_stream(), media_type="text/event-stream")
