"""질의 document_id 필터 — 접근 검사와 검색 범위."""
from __future__ import annotations

from datetime import datetime, timezone
from types import SimpleNamespace
import pytest
from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient

from harag.api.acl_helpers import LIB_TAG
from harag.api.pipeline import QueryPipelineImpl
from harag.api.routes_query import (
    _resolve_document_id, _resolve_document_ids, router)
from harag.api.schemas import QueryRequest
from harag.contracts.boundaries import AuthContext, Chunk, ScoredChunk
from harag.schemas.chunk import ChunkMetadata, ChunkType


def _auth():
    return AuthContext(
        user_id="u1", acl_tags=frozenset({LIB_TAG, "owner:u1"}))


def _chunk(doc_id: str, text: str, cid: str) -> ScoredChunk:
    meta = ChunkMetadata(
        chunk_id=cid,
        document_id=doc_id,
        source_block_ids=["b1"],
        chunk_type=ChunkType.body,
        security_level="internal",
        acl_tags=[LIB_TAG],
        source_document=f"{doc_id}.pdf",
        citation_label=f"{doc_id} p.1",
        parsing_version="p",
        chunking_version="c",
        embedding_model_id="m",
        content_hash=cid,
        created_at=datetime.now(timezone.utc),
    )
    return ScoredChunk(chunk=Chunk(meta=meta, text=text), score=0.9)


class _FilterRetriever:
    def __init__(self, chunks: list[ScoredChunk]):
        self.chunks = chunks
        self.last_document_id = None
        self.last_document_ids = None

    def retrieve(self, query, auth, k=10, document_id=None,
                 document_ids=None):
        self.last_document_id = document_id
        self.last_document_ids = document_ids
        allowed = set(document_ids or [])
        if document_id:
            allowed.add(document_id)
        if allowed:
            return [c for c in self.chunks
                    if c.chunk.meta.document_id in allowed][:k]
        return self.chunks[:k]


class _Gen:
    def generate(self, query, context):
        from harag.contracts.boundaries import GenerationResult
        ids = [c.chunk.meta.chunk_id for c in context]
        return GenerationResult(
            answer="ok" if context else None,
            cited_chunk_ids=ids,
            abstained=not context,
            abstain_reason=None if context else "no_answer",
        )

    def precheck(self, results):
        return None

    def stream_tokens(self, query, results):
        return None


def test_pipeline_retrieve_passes_document_id():
    chunks = [
        _chunk("docA", "A 암진단", "a1"),
        _chunk("docB", "B 입원", "b1"),
    ]
    ret = _FilterRetriever(chunks)
    pipe = QueryPipelineImpl(ret, _Gen(), top_k=5)
    results, _ = pipe._retrieve_sync("암", _auth(), None, document_id="docA")
    assert ret.last_document_id == "docA"
    assert len(results) == 1
    assert results[0].chunk.meta.document_id == "docA"


def test_pipeline_retrieve_passes_document_ids():
    chunks = [
        _chunk("docA", "A 암진단", "a1"),
        _chunk("docB", "B 입원", "b1"),
        _chunk("docC", "C 메리츠", "c1"),
    ]
    ret = _FilterRetriever(chunks)
    pipe = QueryPipelineImpl(ret, _Gen(), top_k=5)
    results, _ = pipe._retrieve_sync(
        "암", _auth(), None, document_ids=["docA", "docB"])
    assert ret.last_document_ids == ["docA", "docB"]
    assert {r.chunk.meta.document_id for r in results} == {"docA", "docB"}


def test_resolve_document_ids_union_and_skip_inaccessible(monkeypatch):
    class _Meta:
        def find_accessible(self, doc_id, *a, **k):
            if doc_id in ("docA", "docB"):
                return SimpleNamespace(document_id=doc_id)
            return None

    class _Ingest:
        _metadata = _Meta()

    monkeypatch.setattr("harag.api.deps.get_ingest", lambda: _Ingest())
    req = QueryRequest(
        query="통원?", document_id="docA",
        document_ids=["docB", "missing", "docA"])
    assert _resolve_document_ids(req, _auth()) == ["docA", "docB"]


def test_resolve_document_ids_all_missing_404(monkeypatch):
    class _Meta:
        def find_accessible(self, *a, **k):
            return None

    class _Ingest:
        _metadata = _Meta()

    monkeypatch.setattr("harag.api.deps.get_ingest", lambda: _Ingest())
    req = QueryRequest(query="통원?", document_ids=["nope"])
    with pytest.raises(HTTPException) as ei:
        _resolve_document_ids(req, _auth())
    assert ei.value.status_code == 404


def test_resolve_document_id_404(monkeypatch):
    class _Meta:
        def find_accessible(self, *a, **k):
            return None

    class _Ingest:
        _metadata = _Meta()

    monkeypatch.setattr("harag.api.deps.get_ingest", lambda: _Ingest())
    req = QueryRequest(query="면책기간?", document_id="missing")
    with pytest.raises(HTTPException) as ei:
        _resolve_document_id(req, _auth())
    assert ei.value.status_code == 404


def test_resolve_document_id_ok(monkeypatch):
    class _Meta:
        def find_accessible(self, *a, **k):
            return SimpleNamespace(document_id="docA")

    class _Ingest:
        _metadata = _Meta()

    monkeypatch.setattr("harag.api.deps.get_ingest", lambda: _Ingest())
    req = QueryRequest(query="면책기간?", document_id="docA")
    assert _resolve_document_id(req, _auth()) == "docA"


def test_query_route_unknown_document_404(monkeypatch):
    class _Meta:
        def find_accessible(self, *a, **k):
            return None

    class _Ingest:
        _metadata = _Meta()

    monkeypatch.setattr("harag.api.deps.get_ingest", lambda: _Ingest())
    monkeypatch.setattr(
        "harag.api.daily_budget.check_budget", lambda *a, **k: None)

    app = FastAPI()
    app.include_router(router)

    async def _budget_auth():
        return _auth()

    from harag.api.routes_query import _auth_with_budget
    app.dependency_overrides[_auth_with_budget] = _budget_auth
    client = TestClient(app)
    res = client.post(
        "/v1/query",
        json={"query": "면책?", "document_id": "nope"},
    )
    assert res.status_code == 404
