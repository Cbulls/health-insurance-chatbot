"""
수집(인덱싱) 라우트 — 쓰기 경로의 진입점.

MVP: 업로드 → 즉시 202 → 큐/스레드풀로 파싱·임베딩(비동기).
소유자(auth.user_id) 기준으로 문서를 격리한다(본인 문서만 조회).
"""
from __future__ import annotations

import hashlib
import os
import tempfile

from fastapi import (
    APIRouter, Depends, UploadFile, File, Form, HTTPException, status,
)
from fastapi.responses import FileResponse, Response

from harag.api.auth import require_auth
from harag.api.acl_helpers import (
    can_manage_library, can_manage_shared, dept_from_auth,
    library_acl_tags, personal_acl_tags, shared_acl_tags,
)
from harag.api.middleware import current_trace_id
from harag.api.ratelimit import enforce_rate_limit
from harag.api.schemas import IngestResponse, DocumentStatus, DeleteResponse
from harag.config.settings import get_settings
from harag.contracts.boundaries import AuthContext

router = APIRouter(prefix="/v1", tags=["ingest"])

_ALLOWED_SUFFIXES = (".pdf", ".hwpx", ".docx", ".hwp", ".doc")
_SPOOL_CHUNK = 1024 * 1024  # 1MB


def _suffix_of(filename: str) -> str:
    lower = (filename or "").lower()
    # 긴 접미사 우선(.hwpx before .hwp)
    for s in (".hwpx", ".docx", ".hwp", ".doc", ".pdf"):
        if lower.endswith(s):
            return s
    return ""


async def _spool_upload(file: UploadFile, max_bytes: int,
                        suffix: str = ".pdf") -> tuple[str, str, int]:
    """업로드를 임시 파일에 청크 단위로 스풀하며 SHA-256·크기 제한을 동시 처리."""
    hasher = hashlib.sha256()
    total = 0
    spool_dir = os.environ.get("HARAG_SPOOL_DIR") or None
    if spool_dir:
        os.makedirs(spool_dir, exist_ok=True)
    fd, path = tempfile.mkstemp(
        prefix="harag_upload_", suffix=suffix or ".pdf", dir=spool_dir)
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


def _to_status(r) -> DocumentStatus:
    from harag.guidance.brochure import labels_for
    extra = {}
    owner = getattr(r, "owner", None) or getattr(r, "uploaded_by", None) or ""
    meta = None
    try:
        from harag.api.deps import get_ingest
        ingest = get_ingest()
        getter = getattr(ingest._metadata, "get_brochure_profile", None)
        if callable(getter):
            meta = getter(r.document_id, owner or None)
    except Exception:  # noqa: BLE001
        meta = None
    if meta is not None:
        extra = labels_for({
            "display_name": meta.display_name,
            "product_line": meta.product_line,
            "doc_kind": meta.doc_kind,
            "cost_nature": meta.cost_nature,
            "summary": meta.summary,
            "insurer": meta.insurer,
            "source_url": meta.source_url,
        })
    return DocumentStatus(
        document_id=r.document_id, status=r.status,
        filename=r.filename, n_chunks=r.n_chunks, error=r.error,
        scope=getattr(r, "scope", None) or "personal",
        uploaded_by=owner,
        department=getattr(r, "department", None) or "",
        collection_id=getattr(r, "collection_id", None) or "",
        display_name=extra.get("display_name", ""),
        product_line=extra.get("product_line", ""),
        product_line_label=extra.get("product_line_label", ""),
        doc_kind_label=extra.get("doc_kind_label", ""),
        cost_nature_label=extra.get("cost_nature_label", ""),
        summary=extra.get("summary", ""),
        insurer=extra.get("insurer", ""),
        source_url=extra.get("source_url", ""),
        has_file=_status_has_file(r),
    )


def _status_has_file(r) -> bool:
    try:
        from harag.api.deps import get_ingest
        from harag.api.document_file import document_has_file
        return document_has_file(r, ingest=get_ingest())
    except Exception:  # noqa: BLE001
        return getattr(r, "scope", "") == "library"


@router.post("/documents", response_model=IngestResponse,
             status_code=status.HTTP_202_ACCEPTED)
async def ingest_document(
    file: UploadFile = File(...),
    shared: bool = Form(False),
    collection_id: str = Form(""),
    auth: AuthContext = Depends(enforce_rate_limit),
):
    settings = get_settings()
    filename = file.filename or "unknown.pdf"
    suffix = _suffix_of(filename)
    if suffix not in _ALLOWED_SUFFIXES:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=("지원 형식: PDF, HWPX, DOCX, HWP, DOC "
                    "(.pdf .hwpx .docx .hwp .doc)"),
        )
    if suffix == ".doc" and not settings.enable_doc_convert:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=("구형 DOC 변환이 비활성입니다. DOCX로 저장하거나 "
                    "ENABLE_DOC_CONVERT=true 및 LibreOffice를 설정하세요."),
        )

    scope = "personal"
    dept = ""
    coll_id = (collection_id or "").strip()
    tags: list[str]

    if coll_id:
        if not can_manage_library(auth):
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="사내 지식 라이브러리 등록은 admin 또는 doc_admin이 필요합니다.",
            )
        from harag.api.deps import get_ingest
        coll = get_ingest()._metadata.get_collection(coll_id)
        if coll is None:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="collection not found",
            )
        scope = "library"
        tags = library_acl_tags(auth, coll.slug)
    elif shared:
        if not can_manage_shared(auth):
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="공용 규정 등록은 admin 또는 doc_admin 역할이 필요합니다.",
            )
        dept = dept_from_auth(auth)
        if not dept:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="공용 등록에는 JWT에 부서(dept) 클레임이 필요합니다.",
            )
        scope = "shared"
        tags = shared_acl_tags(auth)
    else:
        tags = personal_acl_tags(auth)
        if not tags:
            tags = [f"owner:{auth.user_id}"]

    spool_path, digest, total = await _spool_upload(
        file, settings.max_upload_bytes, suffix=suffix)
    document_id = digest[:32]

    from harag.api.deps import get_ingest
    ingest = get_ingest()
    try:
        if total == 0:
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST,
                                detail="empty file")
        result = ingest.register(
            document_id, filename, auth.user_id,
            department=dept, scope=scope, collection_id=coll_id)
    except BaseException:
        try:
            os.unlink(spool_path)
        except OSError:
            pass
        raise

    if result == "accepted":
        ingest.submit(
            document_id, spool_path, filename, auth.user_id,
            acl_tags=tags, department=dept)
    else:
        try:
            os.unlink(spool_path)
        except OSError:
            pass

    return IngestResponse(
        document_id=document_id, status=result, trace_id=current_trace_id())


@router.post("/documents/{document_id}/reindex",
             status_code=status.HTTP_202_ACCEPTED)
async def reindex_document(document_id: str,
                           auth: AuthContext = Depends(require_auth)):
    """ObjectStore에 보존된 원본으로 재인덱싱."""
    from harag.api.deps import get_ingest
    ingest = get_ingest()
    dept = ""
    for t in auth.acl_tags:
        if t.startswith("dept:"):
            dept = t.split(":", 1)[1]
            break
    ok = ingest.pipeline.reindex_from_store(
        document_id, auth.user_id,
        acl_tags=sorted(auth.acl_tags), department=dept)
    if not ok:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="original not found in object store",
        )
    return {"document_id": document_id, "status": "accepted",
            "trace_id": current_trace_id()}


@router.get("/documents", response_model=list[DocumentStatus])
async def list_documents(auth: AuthContext = Depends(require_auth)):
    from harag.api.deps import get_ingest
    from harag.api.ingest import _from_meta
    ingest = get_ingest()
    dept_tags = [t for t in auth.acl_tags if t.startswith("dept:")]
    if hasattr(ingest._metadata, "list_for_acl"):
        rows = ingest._metadata.list_for_acl(auth.user_id, dept_tags)
        docs = [_from_meta(r) for r in rows]
    else:
        docs = ingest.list_for_owner(auth.user_id)
    return [_to_status(r) for r in docs]


@router.get("/documents/{document_id}", response_model=DocumentStatus)
async def get_document(document_id: str,
                       auth: AuthContext = Depends(require_auth)):
    from harag.api.deps import get_ingest
    from harag.api.ingest import _from_meta
    ingest = get_ingest()
    dept_tags = [t for t in auth.acl_tags if t.startswith("dept:")]
    meta = ingest._metadata.find_accessible(
        document_id, auth.user_id, dept_tags)
    if meta is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND,
                            detail="not found")
    return _to_status(_from_meta(meta))


@router.get("/documents/{document_id}/file")
async def get_document_file(document_id: str,
                            auth: AuthContext = Depends(require_auth)):
    from harag.api.deps import get_ingest
    from harag.api.document_file import open_document_pdf
    ingest = get_ingest()
    dept_tags = [t for t in auth.acl_tags if t.startswith("dept:")]
    meta = ingest._metadata.find_accessible(
        document_id, auth.user_id, dept_tags)
    if meta is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND,
                            detail="not found")
    opened = open_document_pdf(meta, ingest=ingest)
    if opened is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND,
                            detail="file not found")
    headers = {"Content-Disposition": 'inline; filename="brochure.pdf"'}
    if hasattr(opened, "read_bytes"):
        return FileResponse(
            opened, media_type="application/pdf", filename="brochure.pdf",
            content_disposition_type="inline", headers=headers)
    return Response(
        content=opened, media_type="application/pdf", headers=headers)


@router.delete("/documents/{document_id}", response_model=DeleteResponse)
async def delete_document(document_id: str,
                          auth: AuthContext = Depends(require_auth)):
    from harag.api.deps import get_ingest
    ingest = get_ingest()
    dept_tags = [t for t in auth.acl_tags if t.startswith("dept:")]
    meta = ingest._metadata.find_accessible(
        document_id, auth.user_id, dept_tags)
    if meta is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND,
                            detail="not found")
    if meta.scope == "shared" and not can_manage_shared(auth):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="공용 규정 삭제는 admin 또는 doc_admin만 가능합니다.",
        )
    if meta.scope == "library" and not can_manage_library(auth):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="라이브러리 문서 삭제는 admin 또는 doc_admin만 가능합니다.",
        )
    result = ingest.delete(
        document_id, auth.user_id, uploaded_by=meta.uploaded_by)
    if result == "not_found":
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND,
                            detail="not found")
    if result == "busy":
        raise HTTPException(status_code=status.HTTP_409_CONFLICT,
                            detail="document is still processing")
    return DeleteResponse(
        document_id=document_id, status="deleted",
        trace_id=current_trace_id())
