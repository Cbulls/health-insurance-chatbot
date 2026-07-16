"""
문서 삭제 + 하이브리드 검색 TDD.
"""
from __future__ import annotations

from datetime import datetime
from types import SimpleNamespace

import pytest

from harag.api.ingest import InProcessIngest
from harag.contracts.boundaries import AuthContext, Chunk, EmbeddedChunk
from harag.embedding.api_embedder import LocalHashEmbeddingModel, SimpleMorph
from harag.embedding.embedder import HybridEmbedder
from harag.retrieval.qdrant_store import QdrantVectorStore
from harag.schemas.chunk import ChunkMetadata, ChunkType


def _meta(chunk_id: str, doc_id: str, owner: str = "u1") -> ChunkMetadata:
    return ChunkMetadata(
        chunk_id=chunk_id, document_id=doc_id, source_block_ids=["b1"],
        chunk_type=ChunkType.body, security_level="normal",
        acl_tags=[f"owner:{owner}"], source_document="d.pdf",
        citation_label="d.pdf p.1", parsing_version="v1",
        chunking_version="v1", embedding_model_id="m1",
        content_hash="h", created_at=datetime(2026, 1, 1),
    )


def _embedded(chunk_id: str, doc_id: str, text: str, owner: str = "u1",
              dim: int = 8) -> EmbeddedChunk:
    chunk = Chunk(meta=_meta(chunk_id, doc_id, owner), text=text)
    # 간단한 dense: 텍스트 길이 기반
    dense = [float((len(text) + i) % 7) for i in range(dim)]
    sparse = {t: 1.0 for t in SimpleMorph().tokens(text)}
    return EmbeddedChunk(chunk=chunk, dense_vector=dense, sparse_terms=sparse)


# ════════ 삭제 ════════
def test_DEL01_delete_removes_points_and_blocks_retrieve():
    model = LocalHashEmbeddingModel(dim=32)
    store = QdrantVectorStore(model, dim=32, collection="del01", disk_budget_mb=0)
    auth = AuthContext(user_id="u1", acl_tags=frozenset({"owner:u1"}))
    store.index([_embedded("c1", "docA", "출장비 한도 오만원", dim=32)])
    assert store.retrieve("출장비", auth, k=5)
    n = store.delete_document("docA", ["owner:u1"])
    assert n == 1
    assert store.retrieve("출장비", auth, k=5) == []


def test_DEL02_delete_respects_owner_acl():
    model = LocalHashEmbeddingModel(dim=32)
    store = QdrantVectorStore(model, dim=32, collection="del02", disk_budget_mb=0)
    store.index([_embedded("c1", "docA", "비밀 문서", owner="u1", dim=32)])
    # 다른 owner로 삭제 시도 → 0건
    assert store.delete_document("docA", ["owner:u2"]) == 0
    auth1 = AuthContext(user_id="u1", acl_tags=frozenset({"owner:u1"}))
    assert store.retrieve("비밀", auth1, k=5)


def test_DEL03_ingest_delete_busy_and_not_found():
    class StubStore:
        def delete_document(self, document_id, acl_tags):
            return 0

    from harag.storage.metadata_store import MetadataStore

    meta = MetadataStore(dsn="sqlite:///:memory:")
    ingest = InProcessIngest(
        parser=None, chunker=None, embedder=SimpleNamespace(model_id="m"),
        store=StubStore(), metadata=meta)
    assert ingest.delete("missing", "u1") == "not_found"
    ingest.register("doc1", "a.pdf", "u1")  # processing
    assert ingest.delete("doc1", "u1") == "busy"
    meta.mark_ready("doc1", "u1", n_chunks=1)
    assert ingest.delete("doc1", "u1") == "deleted"
    assert ingest.status("doc1", "u1") is None


# ════════ 하이브리드 ════════
def test_HY01_new_collection_enables_hybrid():
    model = LocalHashEmbeddingModel(dim=32)
    store = QdrantVectorStore(model, dim=32, collection="hy01", disk_budget_mb=0)
    assert store._hybrid is True


def test_HY02_hybrid_indexes_sparse_and_retrieves():
    model = LocalHashEmbeddingModel(dim=64)
    store = QdrantVectorStore(model, dim=64, collection="hy02", disk_budget_mb=0)
    embedder = HybridEmbedder(model, SimpleMorph())
    chunks = [
        Chunk(meta=_meta("c1", "d1"), text="건강보험 피부양자 등록 절차"),
        Chunk(meta=_meta("c2", "d1"), text="출장비 정산 기한은 다음 달 말일"),
    ]
    embedded = embedder.embed(chunks)
    assert any(ec.sparse_terms for ec in embedded)
    assert store.index(embedded) == 2
    auth = AuthContext(user_id="u1", acl_tags=frozenset({"owner:u1"}))
    # 키워드 sparse가 돕는지: 질의에 '피부양자' 포함
    hits = store.retrieve("피부양자 등록은?", auth, k=2)
    assert hits
    assert any("피부양자" in h.chunk.text for h in hits)


def test_HY03_dense_only_fallback_when_hybrid_disabled():
    model = LocalHashEmbeddingModel(dim=32)
    store = QdrantVectorStore(
        model, dim=32, collection="hy03", disk_budget_mb=0, hybrid=False)
    assert store._hybrid is False
    store.index([_embedded("c1", "d1", "출장비 한도", dim=32)])
    auth = AuthContext(user_id="u1", acl_tags=frozenset({"owner:u1"}))
    assert store.retrieve("출장비", auth, k=3)
