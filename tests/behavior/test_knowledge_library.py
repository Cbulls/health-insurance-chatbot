"""사내 지식 라이브러리 + 컬렉션 Q&A."""
from __future__ import annotations

from datetime import datetime

from harag.api.acl_helpers import (
    LIB_TAG, can_manage_library, library_acl_tags, with_library_access,
)
from harag.api.auth import require_auth
from harag.contracts.boundaries import AuthContext, Chunk, EmbeddedChunk
from harag.embedding.api_embedder import LocalHashEmbeddingModel, SimpleMorph
from harag.retrieval.qdrant_store import QdrantVectorStore
from harag.schemas.chunk import ChunkMetadata, ChunkType
from harag.storage.metadata_store import MetadataStore


def test_LIB01_auth_injects_lib_knowledge():
    base = AuthContext(user_id="u1", acl_tags=frozenset({"owner:u1"}))
    enriched = with_library_access(base)
    assert LIB_TAG in enriched.acl_tags
    assert "owner:u1" in enriched.acl_tags


def test_LIB02_library_acl_tags():
    auth = AuthContext(
        user_id="admin",
        acl_tags=frozenset({"owner:admin", "role:doc_admin"}),
    )
    tags = library_acl_tags(auth, "health-law")
    assert LIB_TAG in tags
    assert "collection:health-law" in tags
    assert "scope:library" in tags
    assert "owner:admin" in tags


def test_LIB03_collection_crud_and_library_list():
    meta = MetadataStore(dsn="sqlite:///:memory:")
    coll = meta.create_collection(
        title="건강보험법령", created_by="admin",
        description="참조용",
    )
    assert coll.slug
    assert meta.get_collection(coll.id) is not None
    listed = meta.list_collections()
    assert any(c.id == coll.id for c in listed)

    meta.register_for_owner(
        "law1", "law.pdf", "admin",
        scope="library", collection_id=coll.id,
    )
    lib = meta.list_library_documents(coll.id)
    assert len(lib) == 1
    assert lib[0].scope == "library"

    # 문서가 있으면 삭제 거부
    assert meta.delete_collection(coll.id) == "not_empty"
    meta.delete_document_row("law1", "admin")
    assert meta.delete_collection(coll.id) == "ok"


def test_LIB04_list_for_acl_includes_library_for_others():
    meta = MetadataStore(dsn="sqlite:///:memory:")
    coll = meta.create_collection("법령", "admin")
    meta.register_for_owner(
        "law1", "law.pdf", "admin",
        scope="library", collection_id=coll.id,
    )
    meta.register_for_owner(
        "pol1", "policy.pdf", "staff", scope="personal",
    )
    staff_view = meta.list_for_acl("staff", [])
    ids = {r.document_id for r in staff_view}
    assert "pol1" in ids
    assert "law1" in ids  # 타인 업로드 라이브러리도 보임


def test_LIB05_retrieve_merges_library_and_personal():
    """직원 약관 + 관리자 법령이 한 retrieve에서 MatchAny로 함께 나옴."""
    model = LocalHashEmbeddingModel(dim=32)
    store = QdrantVectorStore(
        model, dim=32, collection="lib_test", disk_budget_mb=0,
    )

    def _emb(cid: str, doc: str, text: str, tags: list[str]) -> EmbeddedChunk:
        meta = ChunkMetadata(
            chunk_id=cid, document_id=doc, source_block_ids=["b1"],
            chunk_type=ChunkType.body, security_level="normal",
            acl_tags=tags, source_document=doc + ".pdf",
            citation_label=doc, parsing_version="v1",
            chunking_version="v1", embedding_model_id="m1",
            content_hash=cid, created_at=datetime(2026, 1, 1),
        )
        dense = model.encode([text])[0]
        sparse = {t: 1.0 for t in SimpleMorph().tokens(text)}
        return EmbeddedChunk(
            chunk=Chunk(meta=meta, text=text),
            dense_vector=dense, sparse_terms=sparse,
        )

    store.index([
        _emb("c-policy", "policy", "약관 치과 한도 100만원",
             ["owner:staff"]),
        _emb("c-law", "law", "국민건강보험법 제41조 요양급여",
             [LIB_TAG, "collection:health-law", "scope:library",
              "owner:admin"]),
    ])

    auth = with_library_access(AuthContext(
        user_id="staff", acl_tags=frozenset({"owner:staff"}),
    ))
    hits = store.retrieve("치과 요양급여", auth=auth, k=5)
    docs = {h.chunk.meta.document_id for h in hits}
    assert "policy" in docs
    assert "law" in docs


def test_LIB06_staff_cannot_manage_library():
    staff = AuthContext(
        user_id="s", acl_tags=frozenset({"owner:s", "dept:claims"}))
    admin = AuthContext(
        user_id="a",
        acl_tags=frozenset({"owner:a", "role:doc_admin"}),
    )
    assert can_manage_library(staff) is False
    assert can_manage_library(admin) is True


def test_LIB07_require_auth_demo_has_lib(monkeypatch):
    import asyncio
    from starlette.requests import Request

    monkeypatch.setenv("AUTH_JWT_SECRET", "")
    monkeypatch.setenv("AUTH_OIDC_JWKS_URL", "")
    monkeypatch.setenv("AUTH_ALLOW_DEMO_OWNER", "true")
    from harag.config.settings import get_settings
    from harag.api.auth import clear_auth_cache
    get_settings.cache_clear()
    clear_auth_cache()

    scope = {"type": "http", "asgi": {"version": "3.0"}, "headers": [
        (b"x-owner-id", b"demo-user"),
    ], "method": "GET", "path": "/", "query_string": b"",
       "client": ("127.0.0.1", 0), "server": ("127.0.0.1", 80)}
    req = Request(scope)

    async def _run():
        return await require_auth(req, creds=None)

    ctx = asyncio.run(_run())
    assert ctx.user_id == "demo-user"
    assert LIB_TAG in ctx.acl_tags
