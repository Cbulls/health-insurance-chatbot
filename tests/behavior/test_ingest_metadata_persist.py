"""
InProcessIngest + MetadataStore 영속화 TDD.

SQLite 파일에 ready 상태를 쓰고, 새 Ingest 인스턴스에서도 목록이 보여야 한다.
"""
from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

from harag.api.ingest import InProcessIngest
from harag.schemas.chunk import ChunkType
from harag.storage.metadata_store import MetadataStore


class FakeParser:
    def parse(self, raw, document_id, filename):
        status = SimpleNamespace(value="success")
        block = SimpleNamespace(block_id="b1", block_type=None, is_noise=False)
        return SimpleNamespace(parse_status=status, blocks=[block])


class FakeChunker:
    def chunk(self, ir, ctx):
        meta = SimpleNamespace(chunk_id="c1", source_block_ids=["b1"],
                               chunk_type=ChunkType.body)
        return [SimpleNamespace(meta=meta, text="본문")]


class FakeEmbedder:
    model_id = "fake-model"

    def embed(self, chunks):
        return list(chunks)


class FakeStore:
    def index(self, embedded):
        return len(embedded)

    def delete_document(self, document_id, acl_tags):
        return 1


def test_ING_META01_ready_persists_across_ingest_instances(tmp_path: Path):
    """process → ready 영속 → 새 Ingest에서도 list에 보임."""
    db = tmp_path / "harag.db"
    dsn = f"sqlite:///{db}"
    meta = MetadataStore(dsn=dsn)

    ingest1 = InProcessIngest(
        parser=FakeParser(), chunker=FakeChunker(),
        embedder=FakeEmbedder(), store=FakeStore(), metadata=meta)
    assert ingest1.register("doc1", "a.pdf", "u1") == "accepted"
    ingest1.process("doc1", b"%PDF-fake", "a.pdf", "u1")
    rec = ingest1.status("doc1", "u1")
    assert rec is not None
    assert rec.status == "ready"
    assert rec.n_chunks == 1

    # 새 인스턴스 + 같은 DB 파일
    meta2 = MetadataStore(dsn=dsn)
    ingest2 = InProcessIngest(
        parser=FakeParser(), chunker=FakeChunker(),
        embedder=FakeEmbedder(), store=FakeStore(), metadata=meta2)
    listed = ingest2.list_for_owner("u1")
    assert len(listed) == 1
    assert listed[0].document_id == "doc1"
    assert listed[0].status == "ready"
    assert listed[0].n_chunks == 1


def test_ING_META02_delete_removes_from_db():
    meta = MetadataStore(dsn="sqlite:///:memory:")
    ingest = InProcessIngest(
        parser=FakeParser(), chunker=FakeChunker(),
        embedder=FakeEmbedder(), store=FakeStore(), metadata=meta)
    ingest.register("doc1", "a.pdf", "u1")
    ingest.process("doc1", b"%PDF", "a.pdf", "u1")
    assert ingest.delete("doc1", "u1") == "deleted"
    assert ingest.status("doc1", "u1") is None
    assert ingest.list_for_owner("u1") == []
