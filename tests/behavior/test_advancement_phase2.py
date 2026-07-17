"""고도화 Phase 2 — object store · versioning · reindex."""
from __future__ import annotations

from datetime import datetime, timezone
from types import SimpleNamespace

from harag.storage.object_store import ObjectStore, InMemoryBackend
from harag.storage.metadata_store import MetadataStore
from harag.indexing.version_coord import DocumentVersionCoordinator
from harag.indexing.pdf_pipeline import PdfIngestPipeline
from harag.contracts.boundaries import Chunk, EmbeddedChunk
from harag.schemas.chunk import ChunkMetadata, ChunkType
from harag.security.pii import PiiMasker


def _meta(**kw):
    base = dict(
        chunk_id="c1", document_id="doc1",
        source_block_ids=["b1"],
        chunk_type=ChunkType.body,
        security_level="internal",
        acl_tags=["owner:u1"],
        source_document="a.pdf",
        citation_label="a.pdf p.1",
        parsing_version="t",
        chunking_version="t",
        embedding_model_id="local-test",
        content_hash="h1",
        created_at=datetime.now(timezone.utc),
    )
    base.update(kw)
    return ChunkMetadata(**base)


class _FakeParser:
    last_fail_code = None
    last_warning = None

    def parse(self, raw, document_id="", filename=""):
        block = SimpleNamespace(
            block_id="b1",
            text="출장비 한도는 1일 5만원입니다. 연락 010-9999-8888",
            table_content=None,
        )
        return SimpleNamespace(
            parse_status=SimpleNamespace(value="ok"),
            blocks=[block],
        )


class _FakeChunker:
    def chunk(self, ir, ctx):
        text = ir.blocks[0].text
        return [Chunk(
            meta=_meta(acl_tags=list(ctx.acl_tags)),
            text=text,
        )]


class _FakeEmbedder:
    model_id = "local-test"

    def embed(self, chunks):
        return [
            EmbeddedChunk(
                chunk=c, dense_vector=[0.1] * 8, sparse_terms={"a": 1.0},
            )
            for c in chunks
        ]


class _FakeStore:
    def __init__(self):
        self.indexed = []

    def ensure_capacity_for(self, n):
        return None

    def index(self, embedded, **kwargs):
        self.indexed.extend(embedded)
        return len(embedded)


def test_object_store_roundtrip():
    store = ObjectStore(InMemoryBackend())
    store.put_original("d1", b"%PDF-fake")
    assert store.exists_original("d1")
    assert store.get_original("d1") == b"%PDF-fake"


def test_version_coord_bumps():
    meta = MetadataStore(dsn="sqlite:///:memory:")
    meta.register_for_owner("d1", "a.pdf", "u1")
    coord = DocumentVersionCoordinator(meta)
    v1 = coord.next_version("d1", "u1")
    assert v1 == 1
    coord.stage_activate("d1", v1, {"c1"})
    meta.mark_ready("d1", "u1", 1, version=v1)
    v2 = coord.next_version("d1", "u1")
    assert v2 == 2


def test_pipeline_pii_and_object_and_reindex():
    meta = MetadataStore(dsn="sqlite:///:memory:")
    meta.register_for_owner("doc1", "a.pdf", "u1")
    obj = ObjectStore(InMemoryBackend())
    store = _FakeStore()
    pipe = PdfIngestPipeline(
        _FakeParser(), _FakeChunker(), _FakeEmbedder(), store, meta,
        pii_masker=PiiMasker(),
        object_store=obj,
        version_coord=DocumentVersionCoordinator(meta),
    )
    pipe.process("doc1", b"%PDF-x", "a.pdf", "u1", acl_tags=["owner:u1"])
    assert obj.exists_original("doc1")
    rec = meta.get_for_owner("doc1", "u1")
    assert rec is not None and rec.status == "ready"
    assert "010-9999" not in store.indexed[0].chunk.text
    assert "[PII]" in store.indexed[0].chunk.text
    assert pipe.reindex_from_store("doc1", "u1") is True
