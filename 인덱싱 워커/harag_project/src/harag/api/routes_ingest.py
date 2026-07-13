"""
수집(인덱싱) 라우트 — 쓰기 경로의 진입점.

설계: 인덱싱은 비동기다(NFR-2 읽기/쓰기 격리).
업로드를 받으면 즉시 수락 응답 → 실제 파싱·임베딩은 워커가 큐에서 처리.
라우트가 동기로 파싱까지 하면 무거운 작업이 읽기 경로를 막는다.

업로드 권한도 require_auth로 검증 — 아무나 문서를 인덱싱하지 못한다.
"""
from __future__ import annotations

import hashlib

from fastapi import APIRouter, Depends, UploadFile, File, HTTPException, status

from harag.api.auth import require_auth
from harag.api.middleware import current_trace_id
from harag.api.schemas import IngestResponse
from harag.contracts.boundaries import AuthContext

router = APIRouter(prefix="/v1", tags=["ingest"])

# 수집 허용 권한(업로드는 일반 조회보다 강한 권한 요구 — 예시)
_INGEST_REQUIRED_TAG = "role:document-admin"

_MAX_UPLOAD_BYTES = 100 * 1024 * 1024  # 100MB 상한(남용 방지)


@router.post("/documents", response_model=IngestResponse, status_code=status.HTTP_202_ACCEPTED)
async def ingest_document(
    file: UploadFile = File(...),
    auth: AuthContext = Depends(require_auth),
):
    """문서 업로드 → 수집 큐에 등록(비동기). 202 Accepted 반환.

    실제 파싱·청킹·임베딩·버전전환은 indexing-worker가 처리.
    여기선 (1) 권한 확인 (2) 멱등 키 생성 (3) 큐 등록만.
    """
    # 업로드 권한 확인(조회 권한과 분리)
    if _INGEST_REQUIRED_TAG not in auth.acl_tags:
        # 권한 없음 — 404로 누설 방지(이 엔드포인트의 존재를 알리지 않음)
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND)

    raw = await file.read()
    if len(raw) > _MAX_UPLOAD_BYTES:
        raise HTTPException(
            status_code=status.HTTP_413_REQUEST_ENTITY_TOO_LARGE,
            detail="file too large",
        )
    if not raw:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="empty file")

    # 멱등 키: 내용 해시 기반(FR-1 멱등 수집). 같은 문서 재업로드 시 중복 방지.
    document_id = hashlib.sha256(raw).hexdigest()[:32]

    from harag.api.deps import get_ingest_queue
    queue = get_ingest_queue()
    # 큐에 등록(파일명·업로더·원본을 워커가 처리). 매직바이트 판별·파싱은 워커에서.
    accepted = await queue.enqueue(
        document_id=document_id,
        raw=raw,
        filename=file.filename or "unknown",
        uploaded_by=auth.user_id,
    )

    return IngestResponse(
        document_id=document_id,
        status="accepted" if accepted else "duplicate",
        trace_id=current_trace_id(),
    )
