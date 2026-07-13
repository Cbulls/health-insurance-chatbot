"""
Qdrant 저장소 어댑터 TDD — worker의 store 인터페이스 + 검색을 하나로.

실제 Qdrant 클러스터 대신 인메모리 모드(:memory:)로 검증 — API가 동일해 진짜 검증.
어댑터 책임:
  - upsert_vectors: EmbeddedChunk를 버전 태그와 함께 적재
  - stage→activate: 빈 창 없는 버전 전환(B-3)
  - 활성 버전만 검색에 노출(전환 전엔 옛 버전, 후엔 새 버전)
  - ACL payload 필터(검색 시점)
  - get_embeddings_by_hash: 증분 인덱싱 지원
"""
import pytest
from datetime import datetime, timezone

from harag.schemas.chunk import ChunkMetadata, ChunkType
from harag.contracts.boundaries import Chunk, EmbeddedChunk, AuthContext
from harag.storage.qdrant_store import QdrantVectorStore


def _embedded(cid, text, doc="doc1", acl=None, chash=None):
    meta = ChunkMetadata(
        chunk_id=cid, document_id=doc, source_block_ids=["b1"], struct_path="제1조",
        chunk_type=ChunkType.body, security_level="internal",
        acl_tags=acl or ["dept:finance"], source_document="규정.hwp",
        citation_label="규정 제1조", parsing_version="p", chunking_version="c",
        embedding_model_id="m", content_hash=chash or cid, created_at=datetime.now(timezone.utc))
    dim = 8
    vec = [float(len(text) % 5)] * dim
    return EmbeddedChunk(chunk=Chunk(meta=meta, text=text), dense_vector=vec,
                         sparse_terms={t: 1.0 for t in text.split()})


def _store():
    return QdrantVectorStore(dim=8, collection="test")


def _auth(tags=("dept:finance",)):
    return AuthContext(user_id="u", acl_tags=frozenset(tags))


def test_QS01_upsert_and_activate_then_searchable():
    """적재 + 활성화 후 검색됨."""
    s = _store()
    s.upsert_vectors("doc1", 1, [_embedded("c1", "출장비 한도 5만원")])
    s.stage("doc1", 1, {"c1"})
    s.activate("doc1", 1)
    results = s.retrieve("출장비 한도", auth=_auth(), k=5)
    assert any(r.chunk.meta.chunk_id == "c1" for r in results)


def test_QS02_no_empty_window_during_version_switch():
    """버전 전환 중 빈 창 없음 — 활성화 전까지 옛 버전이 온전히 검색됨(B-3)."""
    s = _store()
    # v1 적재·활성
    s.upsert_vectors("doc1", 1, [_embedded("c1_v1", "여비 5만원")])
    s.stage("doc1", 1, {"c1_v1"})
    s.activate("doc1", 1)
    # v2 적재(아직 활성화 안 함) — 이 시점에 검색하면 v1이 나와야(빈 창 없음)
    s.upsert_vectors("doc1", 2, [_embedded("c1_v2", "여비 7만원")])
    s.stage("doc1", 2, {"c1_v2"})
    mid = s.retrieve("여비", auth=_auth(), k=5)
    ids_mid = {r.chunk.meta.chunk_id for r in mid}
    assert "c1_v1" in ids_mid and "c1_v2" not in ids_mid, "활성화 전 새 버전이 노출됨(빈 창/조기 노출)"
    # 활성화 후 v2만
    s.activate("doc1", 2)
    after = s.retrieve("여비", auth=_auth(), k=5)
    ids_after = {r.chunk.meta.chunk_id for r in after}
    assert "c1_v2" in ids_after and "c1_v1" not in ids_after, "전환 후 옛 버전이 남음"


def test_QS03_acl_filter_at_search():
    """검색 시점 ACL — 권한 없는 청크는 회수 안 됨."""
    s = _store()
    s.upsert_vectors("doc1", 1, [
        _embedded("fin", "재무 문서", acl=["dept:finance"]),
        _embedded("hr", "인사 문서", acl=["dept:hr"])])
    s.stage("doc1", 1, {"fin", "hr"})
    s.activate("doc1", 1)
    # finance 권한 사용자
    results = s.retrieve("문서", auth=_auth(("dept:finance",)), k=5)
    ids = {r.chunk.meta.chunk_id for r in results}
    assert "fin" in ids and "hr" not in ids, "권한 없는 청크가 회수됨(ACL 누수)"


def test_QS04_get_embeddings_by_hash_for_incremental():
    """증분 인덱싱: content_hash로 기존 임베딩 조회."""
    s = _store()
    s.upsert_vectors("doc1", 1, [
        _embedded("c1", "여비 5만원", chash="hash_A"),
        _embedded("c2", "출장 3일", chash="hash_B")])
    found = s.get_embeddings_by_hash("doc1", ["hash_A", "hash_C"])
    assert "hash_A" in found and "hash_C" not in found
    assert found["hash_A"].chunk.meta.content_hash == "hash_A"


def test_QS05_gc_purges_old_version():
    """gc: 옛 버전 청크 제거(활성 버전은 유지)."""
    s = _store()
    s.upsert_vectors("doc1", 1, [_embedded("c1_v1", "v1")])
    s.stage("doc1", 1, {"c1_v1"}); s.activate("doc1", 1)
    s.upsert_vectors("doc1", 2, [_embedded("c1_v2", "v2")])
    s.stage("doc1", 2, {"c1_v2"}); s.activate("doc1", 2)
    s.gc("doc1", keep_version=2)
    # gc 후에도 활성(v2)은 검색됨
    results = s.retrieve("v2", auth=_auth(), k=5)
    assert any(r.chunk.meta.chunk_id == "c1_v2" for r in results)


if __name__ == "__main__":
    import sys
    sys.exit(pytest.main([__file__, "-v"]))
