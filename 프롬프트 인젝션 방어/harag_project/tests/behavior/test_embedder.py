"""
임베더 TDD — 청크 → dense + sparse 벡터(EmbeddedChunk).

dense(의미)는 임베딩 모델, sparse(키워드)는 형태소 분석기 — 둘 다 어댑터 뒤로 분리.
임베더의 책임(표본 없이 검증 가능):
  - dense·sparse 둘 다 생성(하이브리드, 한쪽만이면 위반)
  - sparse는 형태소 기반("출장비를"/"출장비가" 같은 어간)
  - dense 차원이 모델 사양과 일치
  - 청크 메타 보존(계보 유지)
"""
import pytest
from datetime import datetime, timezone

from harag.schemas.chunk import ChunkMetadata, ChunkType
from harag.contracts.boundaries import Chunk
from harag.embedding.embedder import HybridEmbedder, EmbeddingModel, Morph


def _chunk(text, cid="doc1-c0"):
    meta = ChunkMetadata(
        chunk_id=cid, document_id="doc1", source_block_ids=["doc1-b0"],
        struct_path="제1조", chunk_type=ChunkType.body, security_level="internal",
        acl_tags=["dept:finance"], source_document="여비규정.hwp",
        citation_label="여비규정 제1조", parsing_version="p1", chunking_version="c1",
        embedding_model_id="kure-v1", content_hash="h", created_at=datetime.now(timezone.utc))
    return Chunk(meta=meta, text=text)


# ── 가짜 어댑터(라이브러리 대역) ──
class FakeModel:
    dim = 8
    def encode(self, texts):
        # 결정적 더미 벡터(차원 8)
        return [[float((len(t) + i) % 5) for i in range(self.dim)] for t in texts]

class FakeMorph:
    def tokens(self, text):
        # 조사 제거한 어간 토큰(형태소 분석 대역)
        out = []
        for w in text.split():
            for josa in ("를", "가", "은", "는", "이", "의", "에"):
                if w.endswith(josa) and len(w) > len(josa):
                    w = w[:-len(josa)]; break
            out.append(w)
        return out


def _embedder():
    return HybridEmbedder(model=FakeModel(), morph=FakeMorph())


def test_E01_dense_dimension_matches_model():
    """dense 벡터 차원이 모델 사양과 일치."""
    emb = _embedder().embed([_chunk("국내출장 여비 한도")])
    assert len(emb[0].dense_vector) == 8, "dense 차원 불일치"


def test_E02_both_dense_and_sparse_present():
    """dense·sparse 둘 다 생성(하이브리드)."""
    emb = _embedder().embed([_chunk("출장비 정산 기한")])
    assert emb[0].dense_vector, "dense 없음"
    assert emb[0].sparse_terms, "sparse 없음 — 반쪽 하이브리드"


def test_E03_sparse_uses_morpheme_stems():
    """조사가 다른 같은 단어는 같은 sparse 토큰(형태소)."""
    e1 = _embedder().embed([_chunk("출장비를 정산", "c1")])[0]
    e2 = _embedder().embed([_chunk("출장비가 지급", "c2")])[0]
    assert "출장비" in e1.sparse_terms and "출장비" in e2.sparse_terms, \
        "조사 차이로 어간 매칭 실패(공백 토큰화 의심)"


def test_E_meta_preserved():
    """임베딩 후에도 청크 메타(계보) 보존."""
    emb = _embedder().embed([_chunk("본문")])
    assert emb[0].chunk.meta.source_block_ids == ["doc1-b0"], "계보 끊김"


def test_E_batch():
    """여러 청크 일괄 임베딩."""
    emb = _embedder().embed([_chunk("a", "c1"), _chunk("b", "c2"), _chunk("c", "c3")])
    assert len(emb) == 3


if __name__ == "__main__":
    import sys
    sys.exit(pytest.main([__file__, "-v"]))
