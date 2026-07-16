"""
인제스트 스풀 처리 TDD — 임시 파일 기반 처리와 뒷정리.

업로드는 임시 파일로 스풀되고, 처리 스레드는 경로에서 읽은 뒤
성공·실패와 무관하게 임시 파일을 지워야 한다(디스크 누수 방지).
"""
from __future__ import annotations

import os
import tempfile
from types import SimpleNamespace

from harag.api.ingest import InProcessIngest
from harag.schemas.chunk import ChunkType


class FakeParser:
    """정상 파싱을 흉내내는 대역 — verify_boundary2가 참조하는 속성만 제공."""
    def __init__(self, fail: bool = False):
        self._fail = fail

    def parse(self, raw, document_id, filename):
        status = SimpleNamespace(value="failed" if self._fail else "success")
        block = SimpleNamespace(block_id="b1", block_type=None, is_noise=False)
        return SimpleNamespace(parse_status=status,
                               blocks=[] if self._fail else [block])


class FakeChunker:
    def chunk(self, ir, ctx):
        meta = SimpleNamespace(chunk_id="c1", source_block_ids=["b1"],
                               chunk_type=ChunkType.body)
        return [SimpleNamespace(meta=meta, text="본문")]


class FakeEmbedder:
    model_id = "fake-model"

    def embed(self, chunks):
        return list(chunks)


class FakeStore:
    def index(self, embedded):
        return len(embedded)


def _spool(data: bytes) -> str:
    fd, path = tempfile.mkstemp(prefix="harag_test_spool_")
    with os.fdopen(fd, "wb") as f:
        f.write(data)
    return path


def make_ingest(parser=None):
    return InProcessIngest(parser=parser or FakeParser(), chunker=FakeChunker(),
                           embedder=FakeEmbedder(), store=FakeStore())


def test_SP01_spool_file_deleted_after_processing():
    """정상 처리 후 상태는 ready, 임시 파일은 남지 않는다."""
    ingest = make_ingest()
    path = _spool(b"%PDF-fake")
    ingest.register("doc1", "a.pdf", "u1")
    ingest.process_file("doc1", path, "a.pdf", "u1")
    assert not os.path.exists(path)
    rec = ingest.status("doc1", "u1")
    assert rec.status == "ready"
    assert rec.n_chunks == 1


def test_SP02_spool_file_deleted_even_on_parse_failure():
    """파싱 실패 경로에서도 임시 파일은 삭제되고 상태는 failed."""
    ingest = make_ingest(parser=FakeParser(fail=True))
    path = _spool(b"%PDF-broken")
    ingest.register("doc2", "b.pdf", "u1")
    ingest.process_file("doc2", path, "b.pdf", "u1")
    assert not os.path.exists(path)
    rec = ingest.status("doc2", "u1")
    assert rec.status == "failed"


def test_SP03_missing_spool_marks_failed_not_crash():
    """스풀 파일이 사라져도 예외가 아니라 failed 상태로 기록된다."""
    ingest = make_ingest()
    ingest.register("doc3", "c.pdf", "u1")
    ingest.process_file("doc3", "/nonexistent/spool/path", "c.pdf", "u1")
    rec = ingest.status("doc3", "u1")
    assert rec.status == "failed"
