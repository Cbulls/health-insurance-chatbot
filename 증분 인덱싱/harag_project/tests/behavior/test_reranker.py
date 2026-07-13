"""
리랭커 TDD — 검색 후보(top-K)를 정밀 재순위해 top-N 선별(FR-7).

cross-encoder 모델은 어댑터 뒤로 분리(GPU·모델 의존). 리랭커의 책임:
  - RR-01: top-N만 반환(N < K)
  - RR-02: 최저 점수 미달 청크 제외
  - RR-03: retrieval 순위를 실제로 재정렬(정답이 위로)
  - RR-04: 부하 시 입력 K 축소(graceful degradation)
"""
import pytest
from datetime import datetime, timezone

from harag.schemas.chunk import ChunkMetadata, ChunkType
from harag.contracts.boundaries import Chunk, ScoredChunk
from harag.retrieval.reranker import CrossEncoderReranker


def _sc(cid, text, score):
    meta = ChunkMetadata(
        chunk_id=cid, document_id="d1", source_block_ids=["b1"], struct_path="제1조",
        chunk_type=ChunkType.body, security_level="internal", acl_tags=["dept:finance"],
        source_document="규정.hwp", citation_label="규정 제1조", parsing_version="p",
        chunking_version="c", embedding_model_id="m", content_hash="h",
        created_at=datetime.now(timezone.utc))
    return ScoredChunk(chunk=Chunk(meta=meta, text=text), score=score)


# ── 가짜 cross-encoder(질의-청크 쌍에 관련도 점수) ──
class FakeCrossEncoder:
    """'정답' 단어가 들어간 청크에 높은 점수. 실제론 GPU cross-encoder."""
    def __init__(self): self.calls = 0
    def score_pairs(self, query, texts):
        self.calls += 1
        # query 단어가 많이 겹칠수록 높은 점수(간단한 대역)
        q_words = set(query.split())
        return [len(q_words & set(t.split())) / (len(q_words) or 1) for t in texts]


def _reranker(**kw):
    defaults = dict(top_n=3, min_score=0.1)
    defaults.update(kw)  # 테스트가 넘긴 값 우선
    return CrossEncoderReranker(model=FakeCrossEncoder(), **defaults)


def test_RR01_returns_top_n_only():
    """top-N만 반환(N < K)."""
    # 질의와 겹치는 청크들(cross-encoder가 점수를 주도록) — 이 테스트는 컷만 검증
    candidates = [_sc(f"c{i}", f"출장비 한도 {i}", 0.5) for i in range(10)]
    out = _reranker().rerank("출장비 한도", candidates)
    assert len(out) == 3, "top_n으로 컷 안 됨"


def test_RR02_excludes_below_min_score():
    """리랭킹 점수가 최저 미달이면 제외."""
    # 질의와 전혀 안 겹치는 청크들 → cross-encoder 점수 0 → min_score 미달
    candidates = [_sc("c1", "출장비 한도 정산", 0.9),   # 겹침
                  _sc("c2", "전혀 무관한 내용", 0.8)]    # 안 겹침
    out = _reranker(top_n=5).rerank("출장비 한도", candidates)
    ids = [s.chunk.meta.chunk_id for s in out]
    assert "c1" in ids, "관련 청크가 빠짐"
    assert "c2" not in ids, "무관(저점수) 청크가 통과함"


def test_RR03_reorders_by_relevance():
    """retrieval 순위를 재정렬 — 정답이 위로 올라온다."""
    # retrieval은 c_bad를 1등(0.9)으로 줬지만, 질의 관련은 c_good이 높음
    candidates = [_sc("c_bad", "무관한 잡담 텍스트", 0.9),
                  _sc("c_good", "출장비 한도 정산 기한", 0.5)]
    out = _reranker().rerank("출장비 한도 정산", candidates)
    assert out[0].chunk.meta.chunk_id == "c_good", "리랭킹이 순위를 안 바꿈(no-op 의심)"


def test_RR04_graceful_degradation_caps_input():
    """부하 신호 시 입력 K를 축소(지연 방어)."""
    candidates = [_sc(f"c{i}", f"출장비 {i}", 0.5) for i in range(40)]
    r = _reranker(max_candidates_under_load=10)
    out = r.rerank("출장비", candidates, under_load=True)
    # 축소된 후보(10개)만 cross-encoder에 넘어가야(모델 호출 비용 절감)
    assert r._model.calls == 1
    # 결과는 여전히 top_n
    assert len(out) <= 3


def test_RR_empty_input():
    """빈 후보 → 빈 결과(크래시 없음)."""
    assert _reranker().rerank("q", []) == []


if __name__ == "__main__":
    import sys
    sys.exit(pytest.main([__file__, "-v"]))
