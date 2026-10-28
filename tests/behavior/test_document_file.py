"""공용 설명서 PDF — demo_pdfs 폴백, 경로 탈출 거부."""
from __future__ import annotations

from types import SimpleNamespace

from harag.api.document_file import (
    document_has_file, open_document_pdf, safe_library_pdf,
)
from harag.storage.object_store import InMemoryBackend, ObjectStore


def test_safe_library_pdf_reads_demo(tmp_path):
    path = tmp_path / "ok.pdf"
    path.write_bytes(b"%PDF-1.4 demo")
    found = safe_library_pdf("ok.pdf", tmp_path)
    assert found == path.resolve()
    assert found.read_bytes().startswith(b"%PDF")


def test_safe_library_pdf_rejects_traversal(tmp_path):
    outside = tmp_path.parent / "secret.pdf"
    outside.write_bytes(b"%PDF-1.4 secret")
    assert safe_library_pdf("../secret.pdf", tmp_path) is None
    assert safe_library_pdf(str(outside), tmp_path) is None
    assert safe_library_pdf("ok.txt", tmp_path) is None
    assert safe_library_pdf("", tmp_path) is None


def test_safe_library_pdf_missing(tmp_path):
    assert safe_library_pdf("nope.pdf", tmp_path) is None


def test_open_library_pdf_from_demo_dir(tmp_path):
    (tmp_path / "brochure.pdf").write_bytes(b"%PDF-1.4 lib")
    meta = SimpleNamespace(
        document_id="doc1", filename="brochure.pdf", scope="library")
    opened = open_document_pdf(meta, demo_dir=tmp_path)
    assert opened is not None
    assert opened.read_bytes() == b"%PDF-1.4 lib"
    assert document_has_file(meta, demo_dir=tmp_path)


def test_personal_without_store_has_no_file(tmp_path):
    (tmp_path / "mine.pdf").write_bytes(b"%PDF-1.4 mine")
    meta = SimpleNamespace(
        document_id="doc2", filename="mine.pdf", scope="personal")
    assert open_document_pdf(meta, demo_dir=tmp_path) is None
    assert document_has_file(meta, demo_dir=tmp_path) is False


def test_file_route_404_unknown_id(monkeypatch):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    from harag.api.acl_helpers import LIB_TAG
    from harag.api.auth import require_auth
    from harag.api.routes_ingest import router
    from harag.contracts.boundaries import AuthContext

    class _Meta:
        def find_accessible(self, *a, **k):
            return None

    class _Ingest:
        _metadata = _Meta()

    monkeypatch.setattr("harag.api.deps.get_ingest", lambda: _Ingest())
    app = FastAPI()
    app.include_router(router)
    app.dependency_overrides[require_auth] = lambda: AuthContext(
        user_id="u", acl_tags=frozenset({LIB_TAG, "owner:u"}))
    client = TestClient(app)
    res = client.get("/v1/documents/missing/file")
    assert res.status_code == 404


def test_file_route_korean_filename_200(tmp_path, monkeypatch):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    from harag.api.acl_helpers import LIB_TAG
    from harag.api.auth import require_auth
    from harag.api.routes_ingest import router
    from harag.contracts.boundaries import AuthContext

    pdf = tmp_path / "메리츠_요약서.pdf"
    pdf.write_bytes(b"%PDF-1.4 ko")
    rec = SimpleNamespace(
        document_id="dock", filename="메리츠_요약서.pdf", scope="library")

    class _Meta:
        def find_accessible(self, *a, **k):
            return rec

    class _Ingest:
        _metadata = _Meta()

    monkeypatch.setattr("harag.api.deps.get_ingest", lambda: _Ingest())
    monkeypatch.setattr(
        "harag.api.document_file.default_demo_dir", lambda: tmp_path)
    app = FastAPI()
    app.include_router(router)
    app.dependency_overrides[require_auth] = lambda: AuthContext(
        user_id="u", acl_tags=frozenset({LIB_TAG, "owner:u"}))
    client = TestClient(app)
    res = client.get("/v1/documents/dock/file")
    assert res.status_code == 200
    assert res.content.startswith(b"%PDF")


def test_file_route_library_200(tmp_path, monkeypatch):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    from harag.api.acl_helpers import LIB_TAG
    from harag.api.auth import require_auth
    from harag.api.routes_ingest import router
    from harag.contracts.boundaries import AuthContext

    pdf = tmp_path / "ok.pdf"
    pdf.write_bytes(b"%PDF-1.4 http")
    rec = SimpleNamespace(
        document_id="doc1", filename="ok.pdf", scope="library")

    class _Meta:
        def find_accessible(self, *a, **k):
            return rec

    class _Ingest:
        _metadata = _Meta()

    monkeypatch.setattr("harag.api.deps.get_ingest", lambda: _Ingest())
    monkeypatch.setattr(
        "harag.api.document_file.default_demo_dir", lambda: tmp_path)
    app = FastAPI()
    app.include_router(router)
    app.dependency_overrides[require_auth] = lambda: AuthContext(
        user_id="u", acl_tags=frozenset({LIB_TAG, "owner:u"}))
    client = TestClient(app)
    res = client.get("/v1/documents/doc1/file")
    assert res.status_code == 200
    assert res.headers["content-type"].startswith("application/pdf")
    assert res.content.startswith(b"%PDF")


def test_object_store_wins_over_demo(tmp_path):
    (tmp_path / "brochure.pdf").write_bytes(b"%PDF-1.4 disk")
    store = ObjectStore(InMemoryBackend())
    store.put_original("doc1", b"%PDF-1.4 store")
    ingest = SimpleNamespace(_object_store=store)
    meta = SimpleNamespace(
        document_id="doc1", filename="brochure.pdf", scope="library")
    opened = open_document_pdf(meta, ingest=ingest, demo_dir=tmp_path)
    assert opened == b"%PDF-1.4 store"
