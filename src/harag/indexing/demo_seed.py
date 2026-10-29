"""기동 시 demo_pdfs를 전사 지식 라이브러리로 시드.

HTTP/admin JWT를 쓰지 않는다. 안내 주경로는 시드 실패와 무관하다.

AWS 등에서 Qdrant만 채워지고 메타DB가 비는 split-brain을 막기 위해
기동 시 reconcile_library_from_qdrant로 메타를 복구한다.
"""
from __future__ import annotations

import hashlib
import logging
from pathlib import Path

from harag.api.acl_helpers import LIB_TAG
from harag.guidance.brochure import upsert_catalog_profile

logger = logging.getLogger("harag.demo_seed")

SEED_OWNER = "library"
SEED_SLUG = "demo-brochures"


def default_demo_dir() -> Path:
    here = Path(__file__).resolve()
    repo = here.parents[3] / "demo_pdfs"
    if repo.is_dir():
        return repo
    return Path.cwd() / "demo_pdfs"


def library_seed_tags() -> list[str]:
    return [
        LIB_TAG,
        "scope:library",
        f"collection:{SEED_SLUG}",
        f"owner:{SEED_OWNER}",
    ]


def _file_id(path: Path) -> str:
    digest = hashlib.sha256()
    digest.update(path.read_bytes())
    return digest.hexdigest()[:32]


def _has_library_vectors(store) -> bool:
    fn = getattr(store, "count_for_owner", None)
    if not callable(fn):
        return False
    try:
        return int(fn([LIB_TAG]) or 0) > 0
    except Exception:  # noqa: BLE001
        logger.info("library vector count failed")
        return False


def _document_has_vectors(store, document_id: str) -> bool:
    """True only when this document_id already has points (partial seed safe)."""
    fn = getattr(store, "count_for_document", None)
    if callable(fn):
        try:
            return int(fn(document_id) or 0) > 0
        except Exception:  # noqa: BLE001
            logger.info("document vector count failed id=%s", document_id[:12])
    return False


def _chunk_count(store, document_id: str) -> int:
    fn = getattr(store, "count_for_document", None)
    if not callable(fn):
        return 1
    try:
        return max(1, int(fn(document_id) or 1))
    except Exception:  # noqa: BLE001
        return 1


def _ensure_collection(meta):
    rec = meta.get_collection_by_slug(SEED_SLUG)
    if rec is not None:
        return rec
    return meta.create_collection(
        title="데모 상품 설명서",
        created_by=SEED_OWNER,
        description="전원이 검색하는 참고 설명서",
        slug=SEED_SLUG,
    )


def _ensure_ready_library_row(
    meta, *, document_id: str, filename: str, collection_id: str, n_chunks: int,
) -> None:
    """메타에 library ready 행이 없으면 만들고, 프로필을 카탈로그로 채운다."""
    name = (filename or "").strip() or f"{document_id}.pdf"
    meta.register_for_owner(
        document_id, name, SEED_OWNER,
        scope="library", collection_id=collection_id)
    meta.mark_ready(document_id, SEED_OWNER, n_chunks=max(1, int(n_chunks)))
    upsert_catalog_profile(meta, document_id, SEED_OWNER, name)


def reconcile_library_from_qdrant(store, meta) -> int:
    """Qdrant library 포인트에서 (document_id, source_document)를 모아 메타만 복구.

    재임베딩하지 않는다. PDF 디렉터리가 없어도 UI「설명서 N건」을 살릴 수 있다.
    반환: ready로 맞춘(또는 이미 ready인) 문서 수.
    """
    list_fn = getattr(store, "list_library_document_sources", None)
    if not callable(list_fn):
        logger.info("reconcile skip — store has no list_library_document_sources")
        return 0
    try:
        rows = list(list_fn() or [])
    except Exception:  # noqa: BLE001
        logger.exception("reconcile: list library sources failed")
        return 0
    if not rows:
        logger.info("reconcile: no library documents in Qdrant")
        return 0

    coll = _ensure_collection(meta)
    done = 0
    for item in rows:
        if isinstance(item, dict):
            doc_id = str(item.get("document_id") or "").strip()
            filename = str(item.get("source_document") or "").strip()
            n_chunks = int(item.get("n_chunks") or 0)
        else:
            # (document_id, source_document, n_chunks)
            doc_id = str(item[0] or "").strip()
            filename = str(item[1] or "").strip() if len(item) > 1 else ""
            n_chunks = int(item[2]) if len(item) > 2 else 0
        if not doc_id:
            continue
        if n_chunks <= 0:
            n_chunks = _chunk_count(store, doc_id)
        rec = meta.get_for_owner(doc_id, SEED_OWNER)
        if rec is not None and rec.status == "ready":
            upsert_catalog_profile(
                meta, doc_id, SEED_OWNER,
                filename or rec.filename or f"{doc_id}.pdf")
            done += 1
            continue
        try:
            _ensure_ready_library_row(
                meta,
                document_id=doc_id,
                filename=filename or f"{doc_id}.pdf",
                collection_id=coll.id,
                n_chunks=n_chunks,
            )
            done += 1
            logger.info(
                "reconcile library meta %s (%s, %d chunks)",
                filename or doc_id[:12], doc_id[:12], n_chunks)
        except Exception:  # noqa: BLE001
            logger.exception("reconcile failed id=%s", doc_id[:12])
    logger.info("reconcile library done count=%s", done)
    return done


def seed_demo_brochures(ingest, store, demo_dir: Path | None = None) -> int:
    """인덱싱한(또는 스킵/메타복구한) 파일 수. 예외는 호출측에서 삼킬 수 있다."""
    root = Path(demo_dir) if demo_dir is not None else default_demo_dir()
    if not root.is_dir():
        logger.info("demo_pdfs missing at %s — skip seed", root)
        return 0
    # Smallest first — fewer chunks before Gemini TPM window fills.
    pdfs = sorted(
        (p for p in root.glob("*.pdf") if p.is_file()),
        key=lambda p: p.stat().st_size,
    )
    if not pdfs:
        logger.info("demo_pdfs empty — skip seed")
        return 0
    meta = ingest._metadata
    coll = _ensure_collection(meta)
    tags = library_seed_tags()
    have_vecs = _has_library_vectors(store)
    if not have_vecs:
        logger.warning(
            "Qdrant library vectors empty — seeding %d demo PDF(s); "
            "RAG will abstain (empty_context) until seed finishes",
            len(pdfs),
        )
    done = 0
    for i, path in enumerate(pdfs):
        doc_id = _file_id(path)
        rec = meta.get_for_owner(doc_id, SEED_OWNER)
        has_vec = _document_has_vectors(store, doc_id)
        if (
            rec is not None
            and rec.status == "ready"
            and have_vecs
            and has_vec
        ):
            logger.info("demo seed skip ready %s", path.name)
            upsert_catalog_profile(meta, doc_id, SEED_OWNER, path.name)
            done += 1
            continue
        # 벡터만 있으면 재임베딩하지 않고 메타만 맞춤(Jina/쿼터 절약).
        if has_vec:
            try:
                _ensure_ready_library_row(
                    meta,
                    document_id=doc_id,
                    filename=path.name,
                    collection_id=coll.id,
                    n_chunks=_chunk_count(store, doc_id),
                )
                done += 1
                logger.info("demo seed meta-only (vectors exist) %s", path.name)
            except Exception:  # noqa: BLE001
                logger.exception("demo seed meta-only failed %s", path.name)
            continue
        ingest.register(
            doc_id, path.name, SEED_OWNER,
            scope="library", collection_id=coll.id)
        try:
            logger.info(
                "demo seed indexing %s (%d/%d, %d bytes)",
                path.name, i + 1, len(pdfs), path.stat().st_size,
            )
            ingest.process(
                doc_id, path.read_bytes(), path.name, SEED_OWNER,
                acl_tags=tags)
            if not _document_has_vectors(store, doc_id):
                logger.error(
                    "demo seed finished without vectors %s — not counting",
                    path.name)
                continue
            upsert_catalog_profile(meta, doc_id, SEED_OWNER, path.name)
            done += 1
            logger.info("demo seed indexed %s", path.name)
        except Exception:  # noqa: BLE001
            logger.exception("demo seed ingest failed %s", path.name)
    return done
