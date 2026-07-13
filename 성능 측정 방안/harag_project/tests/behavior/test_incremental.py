"""
증분 인덱싱 TDD — 변경된 청크만 재임베딩(content_hash 비교).

문제: 문서 일부만 바뀌어도 전체 재임베딩은 GPU·비용 낭비.
해결: 이전 버전 청크의 content_hash와 비교해, 바뀐 것만 임베딩하고 나머지 재사용.

원칙: 임베딩 비용만 절약, 정합성은 양보 안 함.
  - 버전 전환은 여전히 전체 청크 집합으로 원자적(빈 창 없음, B-3)
  - 해시 일치 = 내용 동일(SHA-256) → 재사용 안전
"""
import pytest

from harag.parsing.parser import RawBlock
from harag.indexing.worker import IndexingWorker, IngestJob, IngestStatus


class CountingModel:
    """encode 호출 시 임베딩한 텍스트 수를 센다(재임베딩 측정)."""
    dim = 8
    def __init__(self): self.embedded_texts = []
    def encode(self, texts):
        self.embedded_texts.extend(texts)
        return [[float(len(t) % 5)] * 8 for t in texts]


class Morph:
    def tokens(self, t): return t.split()


class HashAwareStore:
    """이전 버전 임베딩을 content_hash로 조회 가능한 store(증분 지원)."""
    def __init__(self):
        self.active = {}
        self.by_hash = {}     # content_hash -> EmbeddedChunk (재사용 풀)
        self.staged = {}
    def get_embeddings_by_hash(self, doc_id, hashes):
        """주어진 해시들 중 이미 임베딩된 것을 반환(증분 재사용)."""
        return {h: self.by_hash[h] for h in hashes if h in self.by_hash}
    def upsert_vectors(self, doc_id, version, embedded):
        for e in embedded:
            self.by_hash[e.chunk.meta.content_hash] = e
        self.staged[(doc_id, version)] = {e.chunk.meta.chunk_id for e in embedded}
    def stage(self, doc_id, version, chunk_ids): pass
    def activate(self, doc_id, version): self.active[doc_id] = version
    def gc(self, doc_id, keep_version): pass


def _doc(text_blocks):
    class Decoder:
        def decode(self, raw, fmt):
            blocks = []
            for i, t in enumerate(text_blocks):
                # 각 내용을 독립 조항으로 — heading이 struct_path를 바꿔 청크 분리
                blocks.append(RawBlock(kind="heading", text=f"제{i+1}조", level=1))
                blocks.append(RawBlock(kind="paragraph", text=t))
            return blocks
    return Decoder()


def _worker(decoder, model, store):
    return IndexingWorker(decoder=decoder, model=model, morph=Morph(),
                          store=store, incremental=True)


def _job(doc_id="reg1"):
    return IngestJob(document_id=doc_id, raw=b"x", filename="규정.hwp",
                     source_system="gov", department="fin",
                     security_level="internal", acl_tags=["dept:finance"])


def test_INC01_first_index_embeds_all():
    """첫 인덱싱은 전체 임베딩(재사용할 이전 버전 없음)."""
    model = CountingModel()
    store = HashAwareStore()
    w = _worker(_doc(["조항 A 내용", "조항 B 내용", "조항 C 내용"]), model, store)
    r = w.process(_job())
    assert r.status == IngestStatus.indexed
    assert len(model.embedded_texts) == 3, "첫 인덱싱은 전부 임베딩해야"


def test_INC02_unchanged_chunks_reused():
    """일부만 바뀌면 바뀐 것만 재임베딩."""
    store = HashAwareStore()
    # 1차: 3개 청크 전부 임베딩
    m1 = CountingModel()
    _worker(_doc(["조항 A 내용", "조항 B 내용", "조항 C 내용"]), m1, store).process(_job())
    assert len(m1.embedded_texts) == 3

    # 2차: B만 수정, A·C 동일
    m2 = CountingModel()
    _worker(_doc(["조항 A 내용", "조항 B 수정됨", "조항 C 내용"]), m2, store).process(_job())
    # 바뀐 B만 임베딩, A·C는 재사용
    assert len(m2.embedded_texts) == 1, f"변경분만 임베딩해야(실제 {len(m2.embedded_texts)})"
    assert "조항 B 수정됨" in m2.embedded_texts[0]


def test_INC03_no_change_embeds_nothing():
    """전혀 안 바뀌면 재임베딩 0건."""
    store = HashAwareStore()
    blocks = ["조항 A 내용", "조항 B 내용"]
    _worker(_doc(blocks), CountingModel(), store).process(_job())
    m2 = CountingModel()
    _worker(_doc(blocks), m2, store).process(_job())
    assert len(m2.embedded_texts) == 0, "변경 없으면 재임베딩 0이어야"


def test_INC04_full_chunk_set_still_activated():
    """증분이어도 버전 전환은 전체 청크 집합으로(정합성 — 빈 창 없음)."""
    store = HashAwareStore()
    blocks = ["조항 A 내용", "조항 B 내용", "조항 C 내용"]
    _worker(_doc(blocks), CountingModel(), store).process(_job())
    # B만 수정
    w = _worker(_doc(["조항 A 내용", "조항 B 수정됨", "조항 C 내용"]), CountingModel(), store)
    r = w.process(_job())
    # 활성 버전의 청크 집합은 3개 전부(재사용 2 + 신규 1)
    staged_ids = store.staged[(("reg1"), r.version)]
    assert len(staged_ids) == 3, "버전 전환이 전체 집합을 안 담음(정합성 위반)"


def test_INC05_incremental_off_embeds_all():
    """증분 비활성(기본)이면 항상 전체 임베딩(기존 동작 유지)."""
    store = HashAwareStore()
    blocks = ["조항 A 내용", "조항 B 내용"]
    IndexingWorker(decoder=_doc(blocks), model=CountingModel(), morph=Morph(),
                   store=store).process(_job())  # incremental 기본 False
    m2 = CountingModel()
    IndexingWorker(decoder=_doc(blocks), model=m2, morph=Morph(),
                   store=store).process(_job())
    assert len(m2.embedded_texts) == 2, "증분 off면 전체 임베딩"


if __name__ == "__main__":
    import sys
    sys.exit(pytest.main([__file__, "-v"]))
