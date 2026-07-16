"""파이프라인이 under_load·dedupe를 리랭커에 전달하는지."""
from __future__ import annotations

from datetime import datetime, timezone
from unittest.mock import MagicMock

import pytest

from harag.api.pipeline import QueryPipelineImpl, _INFLIGHT
from harag.contracts.boundaries import AuthContext, Chunk, ScoredChunk
from harag.schemas.chunk import ChunkMetadata, ChunkType


def _sc(cid, text="t", score=0.5, content_hash=None):
    meta = ChunkMetadata(
        chunk_id=cid, document_id="d1", source_block_ids=["b1"], struct_path="x",
        chunk_type=ChunkType.body, security_level="internal",
        acl_tags=["owner:u"],
        source_document="d.pdf", citation_label="d 1p", parsing_version="p",
        chunking_version="c", embedding_model_id="m",
        content_hash=content_hash or ("h-" + cid),
        created_at=datetime.now(timezone.utc))
    return ScoredChunk(chunk=Chunk(meta=meta, text=text), score=score)


class RecordingReranker:
    def __init__(self):
        self.calls = []

    def rerank(self, query, candidates, under_load=False):
        self.calls.append({"under_load": under_load, "n": len(candidates)})
        return candidates[:2]


@pytest.fixture(autouse=True)
def _reset_inflight():
    _INFLIGHT._reset_for_tests()
    yield
    _INFLIGHT._reset_for_tests()


def test_under_load_flag_when_inflight_high():
    rr = RecordingReranker()
    retriever = MagicMock()
    retriever.retrieve.return_value = [_sc(f"c{i}") for i in range(5)]
    gen = MagicMock()
    pipe = QueryPipelineImpl(
        retriever=retriever, generator=gen, reranker=rr,
        top_k=5, under_load_inflight=2, context_dedupe=False,
    )
    auth = AuthContext(user_id="u", acl_tags=frozenset({"owner:u"}))

    # 인위적으로 동시성 2 이상
    with _INFLIGHT:
        with _INFLIGHT:
            pipe._retrieve_sync("q", auth, None)

    assert rr.calls and rr.calls[-1]["under_load"] is True


def test_dedupe_applied_in_pipeline():
    class Passthrough:
        def rerank(self, q, cands, under_load=False):
            return cands

    a = _sc("c1", "same text", 0.9, content_hash="dup")
    b = _sc("c2", "same text copy", 0.8, content_hash="dup")
    retriever = MagicMock()
    retriever.retrieve.return_value = [a, b]
    pipe = QueryPipelineImpl(
        retriever=retriever, generator=MagicMock(), reranker=Passthrough(),
        top_k=5, under_load_inflight=99, context_dedupe=True,
    )
    auth = AuthContext(user_id="u", acl_tags=frozenset({"owner:u"}))
    out = pipe._retrieve_sync("q", auth, None)
    assert len(out) == 1
