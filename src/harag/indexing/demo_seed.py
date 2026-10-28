"""기동 시 demo_pdfs를 전사 지식 라이브러리로 시드.

HTTP/admin JWT를 쓰지 않는다. 안내 주경로는 시드 실패와 무관하다.
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


def seed_demo_brochures(ingest, store, demo_dir: Path | None = None) -> int:
    """인덱싱한(또는 스킵한) 파일 수. 예외는 호출측에서 삼킬 수 있다."""
    root = Path(demo_dir) if demo_dir is not None else default_demo_dir()
    if not root.is_dir():
        logger.info("demo_pdfs missing at %s — skip seed", root)
        return 0
    pdfs = sorted(p for p in root.glob("*.pdf") if p.is_file())
    if not pdfs:
        logger.info("demo_pdfs empty — skip seed")
        return 0
    meta = ingest._metadata
    coll = _ensure_collection(meta)
    tags = library_seed_tags()
    have_vecs = _has_library_vectors(store)
    done = 0
    for path in pdfs:
        doc_id = _file_id(path)
        rec = meta.get_for_owner(doc_id, SEED_OWNER)
        if rec is not None and rec.status == "ready" and have_vecs:
            logger.info("demo seed skip ready %s", path.name)
            upsert_catalog_profile(meta, doc_id, SEED_OWNER, path.name)
            done += 1
            continue
        ingest.register(
            doc_id, path.name, SEED_OWNER,
            scope="library", collection_id=coll.id)
        try:
            ingest.process(
                doc_id, path.read_bytes(), path.name, SEED_OWNER,
                acl_tags=tags)
            upsert_catalog_profile(meta, doc_id, SEED_OWNER, path.name)
            done += 1
            logger.info("demo seed indexed %s", path.name)
        except Exception:  # noqa: BLE001
            logger.exception("demo seed ingest failed %s", path.name)
    return done
