"""상태 기반 안내 라우트 — 원문을 세션에 저장하지 않는다."""
from __future__ import annotations

from fastapi import APIRouter, Depends

from harag.api.middleware import current_trace_id
from harag.api.ratelimit import enforce_rate_limit
from harag.api.schemas import (
    AssessRequest, AssessResponse, GuidanceRequest, GuidanceResponse,
    AreaItem, CoverageItem, SymptomItem, PurgeRequest, PurgeResponse,
)
from harag.contracts.boundaries import AuthContext

router = APIRouter(prefix="/v1", tags=["guidance"])


@router.post("/assess", response_model=AssessResponse)
async def assess(
    req: AssessRequest,
    auth: AuthContext = Depends(enforce_rate_limit),
):
    from harag.api.deps import get_guidance
    result = get_guidance().assess(
        req.text, occupation=req.occupation, concern=req.concern)
    return AssessResponse(
        masked_preview=result.masked_preview,
        pii_kinds=result.pii_kinds,
        symptoms=[SymptomItem(**s) for s in result.symptoms],
        occupation=result.occupation,
        occupation_label=result.occupation_label,
        concern=result.concern,
        concern_label=result.concern_label,
        abstained=result.abstained,
        abstain_reason=result.abstain_reason,
        trace_id=current_trace_id(),
    )


@router.post("/guidance", response_model=GuidanceResponse)
async def guidance(
    req: GuidanceRequest,
    auth: AuthContext = Depends(enforce_rate_limit),
):
    from harag.api.deps import get_guidance
    result = get_guidance().guide(
        req.text, occupation=req.occupation, concern=req.concern,
        symptom_codes=req.symptoms or None, auth=auth)
    return GuidanceResponse(
        profile=result.profile,
        areas=[AreaItem(**a) for a in result.areas],
        coverages=[CoverageItem(**c) for c in result.coverages],
        explanation=result.explanation,
        citations=result.citations,
        guard_flags=result.guard_flags,
        abstained=result.abstained,
        abstain_reason=result.abstain_reason,
        raw_stored=result.raw_stored,
        disclaimer=result.disclaimer,
        trace_id=current_trace_id(),
    )


@router.post("/privacy/purge", response_model=PurgeResponse)
async def privacy_purge(
    req: PurgeRequest,
    auth: AuthContext = Depends(enforce_rate_limit),
):
    from harag.api.deps import get_conversation_store_or_none
    store = get_conversation_store_or_none()
    cleared = False
    if store is not None and req.conversation_id:
        cleared = bool(store.clear(req.conversation_id))
    return PurgeResponse(
        raw_persisted=False,
        conversation_cleared=cleared,
        trace_id=current_trace_id(),
    )
