"""설명서 원본 PDF — object store 우선, 공용은 demo_pdfs만 폴백."""
from __future__ import annotations

import hashlib
from functools import lru_cache
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


def _content_id(path: Path) -> str:
    """데모 seed document_id와 동일: SHA256 앞 32자."""
    digest = hashlib.sha256()
    digest.update(path.read_bytes())
    return digest.hexdigest()[:32]


@lru_cache(maxsize=8)
def _demo_pdf_index(demo_dir_key: str) -> dict[str, str]:
    """document_id → absolute path str. demo_dir mtime 무관 단순 캐시."""
    root = Path(demo_dir_key)
    out: dict[str, str] = {}
    if not root.is_dir():
        return out
    try:
        for path in root.glob("*.pdf"):
            if not path.is_file():
                continue
            try:
                out[_content_id(path)] = str(path.resolve())
            except OSError:
                continue
    except OSError:
        return out
    return out


def clear_demo_pdf_index_cache() -> None:
    _demo_pdf_index.cache_clear()


def library_pdf_by_document_id(
    document_id: str, demo_dir: Path | None = None,
) -> Path | None:
    doc_id = (document_id or "").strip()
    if not doc_id:
        return None
    root = Path(demo_dir) if demo_dir is not None else default_demo_dir()
    try:
        root = root.resolve()
    except OSError:
        return None
    # 캐시 키는 resolve된 경로 문자열
    indexed = _demo_pdf_index(str(root))
    hit = indexed.get(doc_id)
    if not hit:
        # 캐시 miss 후 디렉터리 변경 대비: 캐시 비우고 1회 재시도
        clear_demo_pdf_index_cache()
        indexed = _demo_pdf_index(str(root))
        hit = indexed.get(doc_id)
    if not hit:
        return None
    path = Path(hit)
    try:
        path.relative_to(root)
    except ValueError:
        return None
    return path if path.is_file() else None


def resolve_library_pdf(meta, demo_dir: Path | None = None) -> Path | None:
    """filename 일치 → document_id(콘텐츠 해시) 매칭 순."""
    by_name = safe_library_pdf(
        getattr(meta, "filename", "") or "", demo_dir)
    if by_name is not None:
        return by_name
    return library_pdf_by_document_id(
        getattr(meta, "document_id", "") or "", demo_dir)


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
        return resolve_library_pdf(meta, demo_dir) is not None
    return False


def open_document_pdf(meta, ingest=None, demo_dir: Path | None = None):
    """bytes 또는 Path. 없으면 None."""
    doc_id = getattr(meta, "document_id", "") or ""
    store = object_store_of(ingest) if ingest is not None else None
    raw = _from_object_store(store, doc_id)
    if raw is not None:
        return raw
    if getattr(meta, "scope", "") == "library":
        return resolve_library_pdf(meta, demo_dir)
    return None
