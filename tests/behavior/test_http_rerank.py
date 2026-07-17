"""HttpCrossEncoder + 점수 정규화 + Lexical/blend 단위 테스트."""
from __future__ import annotations

import json
from http.server import BaseHTTPRequestHandler, HTTPServer
from threading import Thread

import pytest

from harag.llm.http_rerank import HttpCrossEncoder, normalize_scores, sigmoid
from harag.llm.local_rerank import LexicalCrossEncoder
from harag.retrieval.reranker import CrossEncoderReranker
from harag.config.settings import Settings, resolve_rerank_defaults
from harag.schemas.chunk import ChunkMetadata, ChunkType
from harag.contracts.boundaries import Chunk, ScoredChunk
from datetime import datetime, timezone


def _sc(cid, text, score):
    meta = ChunkMetadata(
        chunk_id=cid, document_id="d1", source_block_ids=["b1"], struct_path="제1조",
        chunk_type=ChunkType.body, security_level="internal", acl_tags=["dept:finance"],
        source_document="규정.hwp", citation_label="규정 제1조", parsing_version="p",
        chunking_version="c", embedding_model_id="m", content_hash="h-" + cid,
        created_at=datetime.now(timezone.utc))
    return ScoredChunk(chunk=Chunk(meta=meta, text=text), score=score)


def test_normalize_auto_leaves_probabilities():
    assert normalize_scores([0.1, 0.9], "auto") == [0.1, 0.9]


def test_normalize_auto_sigmoid_logits():
    out = normalize_scores([-2.0, 2.0], "auto")
    assert out[0] < 0.5 < out[1]
    assert abs(out[1] - sigmoid(2.0)) < 1e-9


def test_lexical_promotes_overlap():
    ce = LexicalCrossEncoder()
    scores = ce.score_pairs("출장비 한도", ["무관한 잡담", "출장비 한도 규정"])
    assert scores[1] > scores[0]


def test_blend_preserves_retrieval_when_lexical_zero():
    """한↔영: lexical=0이어도 blend로 retrieval 점수 유지."""
    class ZeroCE:
        def score_pairs(self, query, texts):
            return [0.0 for _ in texts]

    r = CrossEncoderReranker(model=ZeroCE(), top_n=2, min_score=0.0,
                             retrieval_blend=0.7)
    out = r.rerank("출장비", [
        _sc("en", "domestic travel limit 50000 KRW", 0.8),
        _sc("ko", "출장비", 0.2),
    ])
    assert out[0].chunk.meta.chunk_id == "en"
    assert abs(out[0].score - 0.7 * 0.8) < 1e-6


def test_resolve_defaults_http_vs_lexical():
    base = dict(
        embedding_provider="local", embedding_api_base="", embedding_api_key="",
        embedding_model="x", embedding_dim=8, embedding_send_dimensions=False,
        llm_provider="local", llm_api_base="", llm_api_key="", llm_model="x",
        llm_rewrite_enabled=True, llm_rewrite_model="",
        llm_max_cost_per_query_usd=0.05, llm_cost_per_1k_tokens=0.01,
        llm_max_retries=1, llm_rewrite_max_chars=2000,
        qdrant_url="", qdrant_api_key="", qdrant_collection="c",
        qdrant_disk_budget_mb=0, qdrant_payload_bytes_per_point=1,
        qdrant_sparse_bytes_per_point=640, qdrant_segment_factor=1.15,
        qdrant_upsert_batch_size=64, hybrid_prefetch_mult=2,
        qdrant_count_cache_ttl_s=5.0,
        top_k=20, min_score=0.15,
        rerank_enabled=True, reranker_server_url="",
        rerank_top_n=5, rerank_timeout_ms=1000,
        rerank_min_score=-1.0, retrieval_blend=-1.0,
        rerank_under_load_inflight=4, context_dedupe=True,
        max_upload_bytes=1, rate_limit_qpm=0, allowed_origins=(),
        auth_jwt_secret="", auth_jwt_algorithms=("HS256",),
        auth_jwt_audience="", auth_jwt_issuer="",
        database_url="sqlite:///:memory:",
    )
    lex = Settings(**base)
    b, m = resolve_rerank_defaults(lex)
    assert b == 0.7 and m == 0.0

    http = Settings(**{**base, "reranker_server_url": "http://rr:80"})
    b2, m2 = resolve_rerank_defaults(http)
    assert b2 == 0.0 and m2 == 0.15


class _Handler(BaseHTTPRequestHandler):
    def do_POST(self):  # noqa: N802
        n = int(self.headers.get("Content-Length", 0))
        body = json.loads(self.rfile.read(n))
        texts = body["texts"]
        # 두 번째 텍스트를 높게
        payload = [{"index": i, "score": 0.1 if i == 0 else 0.9}
                   for i in range(len(texts))]
        data = json.dumps(payload).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def log_message(self, *args):  # silence
        pass


@pytest.fixture(scope="module")
def tei_server():
    httpd = HTTPServer(("127.0.0.1", 0), _Handler)
    port = httpd.server_address[1]
    t = Thread(target=httpd.serve_forever, daemon=True)
    t.start()
    yield f"http://127.0.0.1:{port}"
    httpd.shutdown()


def test_http_cross_encoder_tei(tei_server):
    ce = HttpCrossEncoder(tei_server, timeout_ms=2000)
    scores = ce.score_pairs("q", ["a", "b"])
    assert scores[1] > scores[0]
    ce.close()


def test_http_reranker_reorders(tei_server):
    r = CrossEncoderReranker(
        model=HttpCrossEncoder(tei_server, timeout_ms=2000),
        top_n=2, min_score=0.0, retrieval_blend=0.0,
    )
    out = r.rerank("q", [_sc("a", "aaa", 0.99), _sc("b", "bbb", 0.1)])
    assert out[0].chunk.meta.chunk_id == "b"
