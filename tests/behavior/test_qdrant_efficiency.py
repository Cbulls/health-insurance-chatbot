"""Qdrant 호출·저장 효율화 회귀 — payload v2, replace GC, chunked upsert, count 캐시."""
from __future__ import annotations

from datetime import datetime, timezone
from unittest.mock import MagicMock

import pytest

from harag.contracts.boundaries import AuthContext, Chunk, EmbeddedChunk
from harag.embedding.api_embedder import LocalHashEmbeddingModel
from harag.retrieval.qdrant_store import (
    QdrantVectorStore, build_payload_v2, chunk_from_payload,
)
from harag.schemas.chunk import ChunkMetadata, ChunkType


def _meta(cid="c1", doc="d1", owner="u1", **kw):
    base = dict(
        chunk_id=cid, document_id=doc, source_block_ids=["b1"],
        chunk_type=ChunkType.body, security_level="internal",
        acl_tags=[f"owner:{owner}"], source_document="d.pdf",
        citation_label="d.pdf p.1", parsing_version="v1",
        chunking_version="v1", embedding_model_id="m1",
        content_hash="h-" + cid, created_at=datetime(2026, 1, 1, tzinfo=timezone.utc),
    )
    base.update(kw)
    return ChunkMetadata(**base)


def _ec(cid, doc, text, owner="u1", dim=16):
    meta = _meta(cid, doc, owner)
    dense = [float(i % 5) for i in range(dim)]
    return EmbeddedChunk(
        chunk=Chunk(meta=meta, text=text),
        dense_vector=dense,
        sparse_terms={t: 1.0 for t in text.split()[:3]},
    )


def test_payload_v2_roundtrip_without_full_meta():
    ec = _ec("c9", "d9", "출장비 한도 오만원")
    pl = build_payload_v2(ec)
    assert "meta" not in pl
    assert pl["schema"] == "v2"
    assert "citation_label" in pl
    restored = chunk_from_payload(pl)
    assert restored is not None
    assert restored.meta.chunk_id == "c9"
    assert restored.text.startswith("출장비")
    assert restored.meta.citation_label == "d.pdf p.1"


def test_payload_v1_legacy_meta_still_reads():
    meta = _meta("legacy", "dL")
    pl = {
        "text": "옛 스키마 본문",
        "meta": meta.model_dump(mode="json"),
        "chunk_id": "legacy",
        "document_id": "dL",
        "acl_tags": ["owner:u1"],
    }
    ch = chunk_from_payload(pl)
    assert ch is not None
    assert ch.meta.chunk_id == "legacy"
    assert ch.text == "옛 스키마 본문"


def test_index_writes_v2_and_retrieve_restores():
    model = LocalHashEmbeddingModel(dim=16)
    store = QdrantVectorStore(
        model, dim=16, collection="eff_v2", disk_budget_mb=0,
        upsert_batch_size=2)
    store.index([_ec("c1", "d1", "건강보험 피부양자")], skip_capacity_check=True)
    auth = AuthContext(user_id="u1", acl_tags=frozenset({"owner:u1"}))
    hits = store.retrieve("피부양자", auth, k=3)
    assert hits
    assert hits[0].chunk.meta.chunk_id == "c1"
    assert hits[0].chunk.meta.citation_label


def test_replace_document_removes_orphan_chunks():
    model = LocalHashEmbeddingModel(dim=16)
    store = QdrantVectorStore(
        model, dim=16, collection="eff_gc", disk_budget_mb=0)
    auth = AuthContext(user_id="u1", acl_tags=frozenset({"owner:u1"}))
    store.index([
        _ec("c1", "docX", "첫번째 청크 출장비"),
        _ec("c2", "docX", "두번째 청크 연차"),
    ], skip_capacity_check=True)
    assert store.count_total(exact=True) == 2
    # 재청킹: 청크 1개만 — replace로 고아 제거
    store.index(
        [_ec("c1", "docX", "첫번째 청크 출장비 수정")],
        skip_capacity_check=True,
        replace_document=True,
    )
    assert store.count_total(exact=True) == 1
    hits = store.retrieve("출장비", auth, k=5)
    ids = {h.chunk.meta.chunk_id for h in hits}
    assert "c1" in ids
    assert "c2" not in ids


def test_chunked_upsert_calls_client_multiple_times():
    model = LocalHashEmbeddingModel(dim=8)
    store = QdrantVectorStore(
        model, dim=8, collection="eff_batch", disk_budget_mb=0,
        upsert_batch_size=2)
    calls: list[int] = []
    real = store.client.upsert

    def wrapped(*a, **k):
        pts = k.get("points")
        if pts is None and len(a) >= 2:
            pts = a[1]
        calls.append(len(pts or []))
        return real(*a, **k)

    store.client.upsert = wrapped  # type: ignore[method-assign]
    ecs = [_ec(f"c{i}", "dB", f"텍스트 {i}", dim=8) for i in range(5)]
    store.index(ecs, skip_capacity_check=True)
    # 5 points / batch 2 → 3 upserts
    assert calls == [2, 2, 1]


def test_count_cache_avoids_repeat_rpc():
    model = LocalHashEmbeddingModel(dim=8)
    store = QdrantVectorStore(
        model, dim=8, collection="eff_cache", disk_budget_mb=0,
        count_cache_ttl_s=60.0)
    n_calls = {"n": 0}
    real = store.client.count

    def counting(*a, **k):
        n_calls["n"] += 1
        return real(*a, **k)

    store.client.count = counting  # type: ignore[method-assign]
    a = store.count_total(exact=False)
    b = store.count_total(exact=False)
    assert a == b
    assert n_calls["n"] == 1  # 두 번째는 캐시


def test_capacity_formula_more_conservative_than_legacy():
    legacy = 3072 * 1024 * 1024 // (512 * 4 * 1.5 + 2048)
    modern = QdrantVectorStore._compute_max_points(
        3072, 512, 1536, sparse_bytes=640, segment_factor=1.15)
    assert modern < legacy
