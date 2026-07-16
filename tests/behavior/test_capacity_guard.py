"""
용량 가드 TDD — Qdrant 무료 사양(4GiB 디스크) 한도 보호.

무료 클러스터는 한도 초과 시 과금 대신 저장 실패·장애가 난다. 앱이 먼저
디스크 예산(max_points)을 역산해, 넘길 인덱싱을 임베딩 호출 전에 거부해야 한다.
"""
from __future__ import annotations

import logging
from types import SimpleNamespace

import pytest

import harag.retrieval.qdrant_store as qs
from harag.api.ingest import InProcessIngest
from harag.retrieval.qdrant_store import CapacityExceededError, QdrantVectorStore
from harag.schemas.chunk import ChunkType


class FakeEmbedModel:
    model_id = "fake"

    def __init__(self, dim=8):
        self.dim = dim

    def encode(self, texts):
        return [[0.0] * self.dim for _ in texts]


# ════════ max_points 역산 ════════
def test_CG01_max_points_formula():
    """dim 512, payload 2KB, 예산 3072MB → 포인트당 5120B → 629,145 포인트."""
    got = QdrantVectorStore._compute_max_points(
        budget_mb=3072, dim=512, payload_bytes=2048)
    assert got == 3072 * 1024 * 1024 // (512 * 4 * 1.5 + 2048)


def test_CG02_zero_budget_disables_guard():
    assert QdrantVectorStore._compute_max_points(0, 1536, 2048) == 0
    store = QdrantVectorStore(FakeEmbedModel(), dim=8, collection="cg02",
                              disk_budget_mb=0)
    store.ensure_capacity_for(10 ** 9)  # 비활성 — 예외 없음
    assert store.capacity_status() is None


# ════════ 인덱싱 차단 ════════
def make_store(max_points: int) -> QdrantVectorStore:
    store = QdrantVectorStore(FakeEmbedModel(), dim=8,
                              collection=f"cg_{max_points}", disk_budget_mb=1)
    store._max_points = max_points  # 예산을 아주 작게 강제(테스트 용이성)
    return store


def test_CG03_ensure_capacity_rejects_over_budget():
    store = make_store(max_points=2)
    store.ensure_capacity_for(2)  # 0+2 <= 2 통과
    with pytest.raises(CapacityExceededError):
        store.ensure_capacity_for(3)  # 0+3 > 2 거부


def test_CG04_warns_at_80_percent(caplog):
    store = make_store(max_points=10)
    with caplog.at_level(logging.WARNING, logger="harag.qdrant"):
        store.ensure_capacity_for(8)  # 80% 도달 → 경고(통과는 함)
    assert any("한도에 근접" in r.message for r in caplog.records)


def test_CG05_capacity_status_reports_usage():
    store = make_store(max_points=100)
    st = store.capacity_status()
    assert st == {"points": 0, "max_points": 100, "used_pct": 0.0}


# ════════ 원격 컬렉션 저자원 구성 + Cloud 인증 ════════
def test_CG06_remote_collection_low_resource_config(monkeypatch):
    captured = {}

    class FakeClient:
        def __init__(self, **kwargs):
            captured["init"] = kwargs

        def collection_exists(self, coll):
            return False

        def create_collection(self, collection_name, **kwargs):
            captured["create"] = kwargs

        def create_payload_index(self, *a, **k):
            pass

    monkeypatch.setattr(qs, "QdrantClient", FakeClient)
    QdrantVectorStore(FakeEmbedModel(dim=512), dim=512,
                      url="https://x.cloud.qdrant.io:6333", api_key="secret",
                      disk_budget_mb=3072)

    assert captured["init"]["api_key"] == "secret"  # Cloud 인증 전달
    create = captured["create"]
    dense = create["vectors_config"]["dense"]
    assert dense.on_disk is True                    # 원본 벡터 디스크
    assert create["on_disk_payload"] is True        # payload 디스크
    assert create["hnsw_config"].on_disk is True    # HNSW 그래프 디스크
    assert create["quantization_config"] is not None  # int8 양자화(RAM 절약)
    assert create["optimizers_config"].default_segment_number == 1


def test_CG06b_existing_collection_dim_mismatch_fail_fast(monkeypatch):
    """기존 컬렉션 차원이 EMBEDDING_DIM과 다르면 즉시 RuntimeError."""

    class FakeClient:
        def __init__(self, **kwargs):
            pass

        def collection_exists(self, coll):
            return True

        def get_collection(self, coll):
            dense = SimpleNamespace(size=3072)
            vectors = {"dense": dense}
            params = SimpleNamespace(vectors=vectors)
            config = SimpleNamespace(params=params)
            return SimpleNamespace(config=config)

        def create_collection(self, *a, **k):
            raise AssertionError("기존 컬렉션이 있으면 create하지 않아야 함")

        def create_payload_index(self, *a, **k):
            pass

    monkeypatch.setattr(qs, "QdrantClient", FakeClient)
    with pytest.raises(RuntimeError, match="벡터 차원"):
        QdrantVectorStore(FakeEmbedModel(dim=768), dim=768,
                          url="https://x.cloud.qdrant.io:6333",
                          disk_budget_mb=0)


# ════════ 인제스트 사전 검사(임베딩 호출 전 차단) ════════
class FakeParser:
    def parse(self, raw, document_id, filename):
        block = SimpleNamespace(block_id="b1", block_type=None, is_noise=False)
        return SimpleNamespace(parse_status=SimpleNamespace(value="success"),
                               blocks=[block])


class FakeChunker:
    def chunk(self, ir, ctx):
        meta = SimpleNamespace(chunk_id="c1", source_block_ids=["b1"],
                               chunk_type=ChunkType.body)
        return [SimpleNamespace(meta=meta, text="본문")]


class TrackingEmbedder:
    model_id = "fake"

    def __init__(self):
        self.called = False

    def embed(self, chunks):
        self.called = True
        return list(chunks)


class FullStore:
    """예산이 이미 찬 저장소 대역."""
    def ensure_capacity_for(self, n):
        raise CapacityExceededError("full")

    def index(self, embedded):
        return len(embedded)


def test_CG07_ingest_fails_capacity_before_embedding():
    """용량 초과 문서는 임베딩 API를 부르지 않고 capacity_exceeded로 실패한다."""
    embedder = TrackingEmbedder()
    ingest = InProcessIngest(parser=FakeParser(), chunker=FakeChunker(),
                             embedder=embedder, store=FullStore())
    ingest.register("doc1", "a.pdf", "u1")
    ingest.process("doc1", b"%PDF-fake", "a.pdf", "u1")

    rec = ingest.status("doc1", "u1")
    assert rec.status == "failed"
    assert rec.error.startswith("capacity_exceeded")
    assert embedder.called is False  # 비용이 드는 임베딩 호출 안 함
