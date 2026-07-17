"""
라이브 버전 조율 — VersionedStore(빈 창 방지) + 다음 버전 번호.

메타 active_version / DocVersion 기록은 MetadataStore.mark_ready가 담당.
"""
from __future__ import annotations

from harag.indexing.versioning import VersionedStore, upsert


class DocumentVersionCoordinator:
    def __init__(self, metadata_store=None, versioned: VersionedStore | None = None):
        self._meta = metadata_store
        self._vs = versioned or VersionedStore()

    def stage_activate(self, document_id: str, version: int,
                       chunk_ids: set[str]) -> int:
        upsert(self._vs, document_id, version, set(chunk_ids))
        try:
            self._vs.gc(document_id, keep_version=version)
        except RuntimeError:
            pass
        return version

    def next_version(self, document_id: str, owner: str) -> int:
        if self._meta is None:
            return 1
        rec = self._meta.get_for_owner(document_id, owner)
        prev = int(rec.active_version) if rec is not None else 0
        return prev + 1

    @property
    def store(self) -> VersionedStore:
        return self._vs
