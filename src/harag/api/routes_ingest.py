"""
수집(인덱싱) 라우트 — 쓰기 경로의 진입점.

MVP: 업로드 → 즉시 202 → BackgroundTasks로 파싱·임베딩(비동기).
소유자(auth.user_id) 기준으로 문서를 격리한다(본인 문서만 조회).
"""
from __future__ import annotations

import hashlib
import os
import tempfile

from fastapi import (
    APIRouter, Depends, UploadFile, File, HTTPException, status,
)

from harag.api.auth import require_auth
from harag.api.middleware import current_trace_id
from harag.api.ratelimit import enforce_rate_limit
from harag.api.schemas import IngestResponse, DocumentStatus, DeleteResponse
from harag.config.settings import get_settings
from harag.contracts.boundaries import AuthContext

router = APIRouter(prefix="/v1", tags=["ingest"])

_ALLOWED_SUFFIX = ".pdf"
_SPOOL_CHUNK = 1024 * 1024  # 1MB


async def _spool_upload(file: UploadFile, max_bytes: int) -> tuple[str, str, int]:
    """업로드를 임시 파일에 청크 단위로 스풀하며 SHA-256·크기 제한을 동시 처리.

    100MB를 통째로 메모리에 올리지 않고, 상한 초과는 조기에 413으로 끊는다.
    반환: (임시 파일 경로, 해시, 총 크기). 예외 시 임시 파일은 삭제된다.
    """
    hasher = hashlib.sha256()
    total = 0
    fd, path = tempfile.mkstemp(prefix="harag_upload_", suffix=".pdf")
    try:
        with os.fdopen(fd, "wb") as out:
            while True:
                chunk = await file.read(_SPOOL_CHUNK)
                if not chunk:
                    break
                total += len(chunk)
                if total > max_bytes:
                    raise HTTPException(
                        status_code=status.HTTP_413_REQUEST_ENTITY_TOO_LARGE,
                        detail="file too large")
                hasher.update(chunk)
                out.write(chunk)
    except BaseException:
        try:
            os.unlink(path)
        except OSError:
            pass
        raise
    return path, hasher.hexdigest(), total


@router.post("/documents", response_model=IngestResponse,
             status_code=status.HTTP_202_ACCEPTED)
async def ingest_document(
    file: UploadFile = File(...),
    # 업로드는 파싱·임베딩 비용이 커 레이트리밋 적용(조회·폴링은 제외).
    auth: AuthContext = Depends(enforce_rate_limit),
):
    settings = get_settings()
    filename = file.filename or "unknown.pdf"
    if not filename.lower().endswith(_ALLOWED_SUFFIX):
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST,
                            detail="MVP는 PDF만 지원합니다 (.pdf)")

    spool_path, digest, total = await _spool_upload(
        file, settings.max_upload_bytes)
    document_id = digest[:32]

    from harag.api.deps import get_ingest
    ingest = get_ingest()
    try:
        if total == 0:
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST,
                                detail="empty file")
        result = ingest.register(document_id, filename, auth.user_id)
    except BaseException:
        try:
            os.unlink(spool_path)
        except OSError:
            pass
        raise

    if result == "accepted":
        # 전용 인제스트 스레드풀로 위임(임시 파일은 처리 후 삭제됨)
        ingest.submit(document_id, spool_path, filename, auth.user_id)
    else:
        try:
            os.unlink(spool_path)
        except OSError:
            pass

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


@router.delete("/documents/{document_id}", response_model=DeleteResponse)
async def delete_document(document_id: str,
                          auth: AuthContext = Depends(require_auth)):
    """본인 문서를 삭제(Qdrant 포인트 + 상태). 용량 한도 회수용."""
    from harag.api.deps import get_ingest
    ingest = get_ingest()
    result = ingest.delete(document_id, auth.user_id)
    if result == "not_found":
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND)
    if result == "busy":
        raise HTTPException(status_code=status.HTTP_409_CONFLICT,
                            detail="문서가 아직 처리 중입니다. 완료 후 다시 시도하세요.")
    return DeleteResponse(document_id=document_id, status="deleted",
                          trace_id=current_trace_id())
