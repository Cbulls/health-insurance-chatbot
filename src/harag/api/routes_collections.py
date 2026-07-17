"""사내 지식 라이브러리 — 컬렉션 CRUD."""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, status

from harag.api.auth import require_auth
from harag.api.acl_helpers import can_manage_library
from harag.api.middleware import current_trace_id
from harag.api.schemas import (
    CollectionCreate, CollectionDetail, CollectionOut, DocumentStatus,
)
from harag.contracts.boundaries import AuthContext

router = APIRouter(prefix="/v1/collections", tags=["collections"])


def _to_out(c) -> CollectionOut:
    return CollectionOut(
        id=c.id, slug=c.slug, title=c.title,
        description=c.description or "",
        created_by=c.created_by or "",
        n_documents=int(getattr(c, "n_documents", 0) or 0),
    )


@router.get("", response_model=list[CollectionOut])
async def list_collections(auth: AuthContext = Depends(require_auth)):
    from harag.api.deps import get_ingest
    meta = get_ingest()._metadata
    return [_to_out(c) for c in meta.list_collections()]


@router.post("", response_model=CollectionOut,
             status_code=status.HTTP_201_CREATED)
async def create_collection(
    body: CollectionCreate,
    auth: AuthContext = Depends(require_auth),
):
    if not can_manage_library(auth):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="컬렉션 생성은 admin 또는 doc_admin만 가능합니다.",
        )
    from harag.api.deps import get_ingest
    meta = get_ingest()._metadata
    rec = meta.create_collection(
        title=body.title.strip(),
        created_by=auth.user_id,
        description=(body.description or "").strip(),
        slug=(body.slug or None),
    )
    return _to_out(rec)


@router.get("/{collection_id}", response_model=CollectionDetail)
async def get_collection(
    collection_id: str,
    auth: AuthContext = Depends(require_auth),
):
    from harag.api.deps import get_ingest
    from harag.api.ingest import _from_meta
    from harag.api.routes_ingest import _to_status
    meta = get_ingest()._metadata
    coll = meta.get_collection(collection_id)
    if coll is None:
        raise HTTPException(status_code=404, detail="not found")
    docs = [
        _to_status(_from_meta(d))
        for d in meta.list_library_documents(collection_id)
    ]
    return CollectionDetail(
        **_to_out(coll).model_dump(),
        documents=docs,
    )


@router.delete("/{collection_id}")
async def delete_collection(
    collection_id: str,
    auth: AuthContext = Depends(require_auth),
):
    if not can_manage_library(auth):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="컬렉션 삭제는 admin 또는 doc_admin만 가능합니다.",
        )
    from harag.api.deps import get_ingest
    result = get_ingest()._metadata.delete_collection(collection_id)
    if result == "not_found":
        raise HTTPException(status_code=404, detail="not found")
    if result == "not_empty":
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="컬렉션에 문서가 남아 있습니다. 문서를 먼저 삭제하세요.",
        )
    return {"status": "deleted", "collection_id": collection_id,
            "trace_id": current_trace_id()}
