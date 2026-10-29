"""공용 데모 설명서 시드 — 라이브러리 ACL, ready 스킵, 벡터 0이면 재적재."""
from __future__ import annotations

from harag.api.acl_helpers import LIB_TAG
from harag.indexing.demo_seed import (
    SEED_OWNER, SEED_SLUG, reconcile_library_from_qdrant, seed_demo_brochures,
    _file_id,
)
from harag.storage.metadata_store import MetadataStore


class _Ingest:
    def __init__(self, meta: MetadataStore, store: _Store | None = None):
        self._metadata = meta
        self._store = store
        self.processed: list[tuple] = []

    def register(self, document_id, filename, owner, **kwargs):
        return self._metadata.register_for_owner(
            document_id, filename, owner, **kwargs)

    def process(self, document_id, raw, filename, owner, acl_tags=None,
                department=""):
        self.processed.append((document_id, filename, owner, list(acl_tags or [])))
        self._metadata.mark_ready(document_id, owner, n_chunks=1)
        if self._store is not None:
            self._store.doc_ids.add(document_id)
            if self._store.n <= 0:
                self._store.n = 1


class _Store:
    def __init__(self, n: int, doc_ids: set[str] | None = None):
        self.n = n
        # None/empty → per-doc vectors 없음(전체 재적재 테스트용).
        self.doc_ids = set() if doc_ids is None else set(doc_ids)

    def count_for_owner(self, acl_tags):
        assert LIB_TAG in acl_tags
        return self.n

    def count_for_document(self, document_id: str) -> int:
        if self.n <= 0:
            return 0
        return self.n if document_id in self.doc_ids else 0

    def list_library_document_sources(self):
        if self.n <= 0 or not self.doc_ids:
            return []
        return [
            {
                "document_id": d,
                "source_document": f"{d}.pdf",
                "n_chunks": self.n,
            }
            for d in sorted(self.doc_ids)
        ]


def _pdf(tmp_path, name="brochure.pdf"):
    path = tmp_path / name
    path.write_bytes(b"%PDF-1.4 demo brochure")
    return path


def test_library_seed_row_visible_to_other_owner():
    meta = MetadataStore(dsn="sqlite:///:memory:")
    coll = meta.create_collection(
        title="데모 상품 설명서", created_by=SEED_OWNER, slug=SEED_SLUG)
    meta.register_for_owner(
        "doc1", "뉴라이프.pdf", SEED_OWNER,
        scope="library", collection_id=coll.id,
    )
    view = meta.list_for_acl("demo-fit", [])
    rec = next(r for r in view if r.document_id == "doc1")
    assert rec.scope == "library"
    assert rec.uploaded_by == SEED_OWNER


def test_seed_skips_when_ready_and_vectors_exist(tmp_path):
    path = _pdf(tmp_path)
    doc_id = _file_id(path)
    meta = MetadataStore(dsn="sqlite:///:memory:")
    store = _Store(0)
    ingest = _Ingest(meta, store)
    seed_demo_brochures(ingest, store, demo_dir=tmp_path)
    assert len(ingest.processed) == 1
    first = ingest.processed[0]
    assert first[0] == doc_id
    ingest.processed.clear()
    store.n = 4
    n = seed_demo_brochures(ingest, store, demo_dir=tmp_path)
    assert n == 1
    assert ingest.processed == []
    rec = meta.get_for_owner(first[0], SEED_OWNER)
    assert rec is not None
    assert rec.status == "ready"
    assert rec.scope == "library"


def test_seed_meta_only_when_vectors_exist_without_ready_row(tmp_path):
    path = _pdf(tmp_path, "한화_311간편건강보험2604_상품요약서.pdf")
    doc_id = _file_id(path)
    meta = MetadataStore(dsn="sqlite:///:memory:")
    ingest = _Ingest(meta)
    n = seed_demo_brochures(
        ingest, _Store(12, doc_ids={doc_id}), demo_dir=tmp_path)
    assert n == 1
    assert ingest.processed == []
    rec = meta.get_for_owner(doc_id, SEED_OWNER)
    assert rec is not None
    assert rec.status == "ready"
    assert rec.filename == path.name


def test_reconcile_library_from_qdrant_creates_ready_rows():
    meta = MetadataStore(dsn="sqlite:///:memory:")
    store = _Store(3, doc_ids={"abc123", "def456"})
    n = reconcile_library_from_qdrant(store, meta)
    assert n == 2
    for doc_id in ("abc123", "def456"):
        rec = meta.get_for_owner(doc_id, SEED_OWNER)
        assert rec is not None
        assert rec.status == "ready"
        assert rec.scope == "library"
    # idempotent
    assert reconcile_library_from_qdrant(store, meta) == 2


def test_seed_reindexes_when_ready_but_no_vectors(tmp_path):
    path = _pdf(tmp_path)
    meta = MetadataStore(dsn="sqlite:///:memory:")
    store = _Store(0)
    ingest = _Ingest(meta, store)
    seed_demo_brochures(ingest, store, demo_dir=tmp_path)
    assert len(ingest.processed) == 1
    ingest.processed.clear()
    # 벡터 유실 시뮬레이션
    store.doc_ids.clear()
    store.n = 0
    seed_demo_brochures(ingest, store, demo_dir=tmp_path)
    assert len(ingest.processed) == 1
    tags = ingest.processed[0][3]
    assert LIB_TAG in tags
    assert "scope:library" in tags
    assert f"collection:{SEED_SLUG}" in tags
    assert f"owner:{SEED_OWNER}" in tags
    assert _file_id(path) == ingest.processed[0][0]


def test_seed_writes_newlife_catalog_profile(tmp_path):
    _pdf(tmp_path, "뉴라이프건강보험_상품요약서.pdf")
    meta = MetadataStore(dsn="sqlite:///:memory:")
    store = _Store(0)
    ingest = _Ingest(meta, store)
    seed_demo_brochures(ingest, store, demo_dir=tmp_path)
    assert ingest.processed
    doc_id = ingest.processed[0][0]
    rec = meta.get_brochure_profile(doc_id, SEED_OWNER)
    assert rec is not None
    assert rec.product_line == "health"
    assert rec.cost_nature == "mixed"
    assert rec.doc_kind == "summary"
    assert rec.display_name == "뉴라이프건강보험"
    assert rec.insurer == "KB손해보험"
    assert rec.source_url == "https://www.kbinsure.co.kr/CG802030002.ec"
    assert rec.filled_by == "catalog"
    assert "outpatient" in rec.coverage_codes


def test_seed_skips_missing_dir(tmp_path):
    meta = MetadataStore(dsn="sqlite:///:memory:")
    ingest = _Ingest(meta)
    n = seed_demo_brochures(ingest, _Store(0), demo_dir=tmp_path / "absent")
    assert n == 0
    assert ingest.processed == []
