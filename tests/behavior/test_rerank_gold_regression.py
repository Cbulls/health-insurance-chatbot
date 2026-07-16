"""
리랭커 골드 회귀 — Lexical vs 강한 CE가 nDCG/노이즈에 미치는 영향.

실 GPU 없이 FakeCE로 '고도화 후' 방향을 고정한다.
gold가 후보 pool 안에 있을 때 정밀 CE가 상단으로 올리고 노이즈를 줄이는지.
"""
from datetime import datetime, timezone

from harag.schemas.chunk import ChunkMetadata, ChunkType
from harag.contracts.boundaries import Chunk, ScoredChunk
from harag.retrieval.reranker import CrossEncoderReranker
from harag.eval.harness import ndcg_at_k, context_noise_rate


def _sc(cid, text, score):
    meta = ChunkMetadata(
        chunk_id=cid, document_id="d1", source_block_ids=["b1"], struct_path="제1조",
        chunk_type=ChunkType.body, security_level="internal", acl_tags=["dept:x"],
        source_document="규정.hwp", citation_label="규정 제1조", parsing_version="p",
        chunking_version="c", embedding_model_id="m", content_hash="h-" + cid,
        created_at=datetime.now(timezone.utc))
    return ScoredChunk(chunk=Chunk(meta=meta, text=text), score=score)


class StrongCE:
    """정답 청크 id가 텍스트에 있으면 고득점(실 CE 대역)."""
    def score_pairs(self, query, texts):
        return [1.0 if "GOLD_HIT" in t else 0.05 for t in texts]


class WeakLexicalLike:
    """토큰 겹침만 — 동의어 GOLD_HIT는 못 올림."""
    def score_pairs(self, query, texts):
        q = set(query.split())
        return [len(q & set(t.split())) / (len(q) or 1) for t in texts]


def test_strong_ce_improves_ndcg_over_weak():
    # 한국어 질의 + 영문 gold — Lexical 겹침 0, 강한 CE만 승격
    query = "국내 출장 여비 상한"
    pool = [
        _sc("noise1", "연차 휴가 신청 방법", 0.9),
        _sc("noise2", "복무 규정 총칙", 0.85),
        _sc("gold", "GOLD_HIT domestic travel per diem limit 50000 KRW", 0.4),
        _sc("noise3", "복리후생 안내", 0.35),
    ]
    gold = {"gold"}

    weak = CrossEncoderReranker(WeakLexicalLike(), top_n=3, min_score=0.0,
                                retrieval_blend=0.7)
    strong = CrossEncoderReranker(StrongCE(), top_n=3, min_score=0.0,
                                  retrieval_blend=0.0)

    weak_ids = [s.chunk.meta.chunk_id for s in weak.rerank(query, pool)]
    strong_ids = [s.chunk.meta.chunk_id for s in strong.rerank(query, pool)]

    assert ndcg_at_k(gold, strong_ids, k=3) > ndcg_at_k(gold, weak_ids, k=3)
    assert strong_ids[0] == "gold"
    # top-1 노이즈: 강 CE는 0, 약 CE(blend)는 gold가 3위라 1
    assert context_noise_rate(gold, strong_ids, k=1) < context_noise_rate(
        gold, weak_ids, k=1)
