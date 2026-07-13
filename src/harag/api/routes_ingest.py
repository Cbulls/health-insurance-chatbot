"""
수집(인덱싱) 라우트 — 쓰기 경로의 진입점.

MVP: 업로드 → 즉시 202 → BackgroundTasks로 파싱·임베딩(비동기).
소유자(auth.user_id) 기준으로 문서를 격리한다(본인 문서만 조회).
"""
from __future__ import annotations

import asyncio
import hashlib

from fastapi import (
    APIRouter, Depends, UploadFile, File, HTTPException, status, BackgroundTasks,
)

from harag.api.auth import require_auth
from harag.api.middleware import current_trace_id
from harag.api.schemas import IngestResponse, DocumentStatus
from harag.config.settings import get_settings
from harag.contracts.boundaries import AuthContext

router = APIRouter(prefix="/v1", tags=["ingest"])

_ALLOWED_SUFFIX = ".pdf"


@router.post("/documents", response_model=IngestResponse,
             status_code=status.HTTP_202_ACCEPTED)
async def ingest_document(
    background: BackgroundTasks,
    file: UploadFile = File(...),
    auth: AuthContext = Depends(require_auth),
):
    settings = get_settings()
    filename = file.filename or "unknown.pdf"
    if not filename.lower().endswith(_ALLOWED_SUFFIX):
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST,
                            detail="MVP는 PDF만 지원합니다 (.pdf)")

    raw = await file.read()
    if len(raw) > settings.max_upload_bytes:
        raise HTTPException(
            status_code=status.HTTP_413_REQUEST_ENTITY_TOO_LARGE,
            detail="file too large")
    if not raw:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST,
                            detail="empty file")

    # 최대 max_upload_bytes(기본 100MB) 해싱은 CPU 작업이라 루프 밖에서 수행.
    digest = await asyncio.to_thread(lambda b: hashlib.sha256(b).hexdigest(), raw)
    document_id = digest[:32]

    from harag.api.deps import get_ingest
    ingest = get_ingest()
    result = ingest.register(document_id, filename, auth.user_id)
    if result == "accepted":
        background.add_task(ingest.process, document_id, raw, filename, auth.user_id)

    return IngestResponse(
        document_id=document_id, status=result, trace_id=current_trace_id())


@router.get("/documents", response_model=list[DocumentStatus])
async def list_documents(auth: AuthContext = Depends(require_auth)):
    from harag.api.deps import get_ingest
    ingest = get_ingest()
    return [
        DocumentStatus(document_id=r.document_id, status=r.status,
                       filename=r.filename, n_chunks=r.n_chunks, error=r.error)
        for r in ingest.list_for_owner(auth.user_id)
    ]


@router.get("/documents/{document_id}", response_model=DocumentStatus)
async def document_status(document_id: str,
                          auth: AuthContext = Depends(require_auth)):
    from harag.api.deps import get_ingest
    ingest = get_ingest()
    rec = ingest.status(document_id, auth.user_id)
    if rec is None:
        # 없음 또는 권한 없음 → 404(존재 누설 방지)
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND)
    return DocumentStatus(document_id=rec.document_id, status=rec.status,
                          filename=rec.filename, n_chunks=rec.n_chunks,
                          error=rec.error)
