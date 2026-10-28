"""설명서 원본 PDF — object store 우선, 공용은 demo_pdfs만 폴백."""
from __future__ import annotations

from pathlib import Path

from harag.indexing.demo_seed import default_demo_dir


def safe_library_pdf(filename: str, demo_dir: Path | None = None) -> Path | None:
    raw = filename or ""
    name = Path(raw).name
    if not name or name != raw or ".." in name:
        return None
    if not name.lower().endswith(".pdf"):
        return None
    root = Path(demo_dir) if demo_dir is not None else default_demo_dir()
    try:
        root = root.resolve()
    except OSError:
        return None
    path = (root / name).resolve()
    try:
        path.relative_to(root)
    except ValueError:
        return None
    return path if path.is_file() else None


def object_store_of(ingest):
    pipe = getattr(ingest, "pipeline", None) or getattr(ingest, "_pipeline", None)
    if pipe is not None:
        store = getattr(pipe, "_object_store", None)
        if store is not None:
            return store
    return getattr(ingest, "_object_store", None)


def _from_object_store(store, document_id: str) -> bytes | None:
    if store is None or not document_id:
        return None
    try:
        exists = getattr(store, "exists_original", None)
        if callable(exists) and not exists(document_id):
            return None
        return store.get_original(document_id)
    except Exception:  # noqa: BLE001
        return None


def document_has_file(meta, ingest=None, demo_dir: Path | None = None) -> bool:
    doc_id = getattr(meta, "document_id", "") or ""
    store = object_store_of(ingest) if ingest is not None else None
    if _from_object_store(store, doc_id) is not None:
        return True
    if getattr(meta, "scope", "") == "library":
        return safe_library_pdf(getattr(meta, "filename", "") or "", demo_dir) is not None
    return False


def open_document_pdf(meta, ingest=None, demo_dir: Path | None = None):
    """bytes 또는 Path. 없으면 None."""
    doc_id = getattr(meta, "document_id", "") or ""
    store = object_store_of(ingest) if ingest is not None else None
    raw = _from_object_store(store, doc_id)
    if raw is not None:
        return raw
    if getattr(meta, "scope", "") == "library":
        return safe_library_pdf(getattr(meta, "filename", "") or "", demo_dir)
    return None
