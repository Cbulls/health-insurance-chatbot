"""failed 후 재업로드가 inflight를 지우고 enqueue를 재시도하는지."""
from __future__ import annotations

import os
import tempfile
from pathlib import Path
from unittest.mock import MagicMock

from harag.api.ingest import InProcessIngest
from harag.storage.metadata_store import MetadataStore


def test_failed_reupload_clears_inflight_and_retries_enqueue():
    meta = MetadataStore(dsn="sqlite:///:memory:")
    meta.register_for_owner("doc1", "a.pdf", "owner")
    meta.mark_failed("doc1", "owner", "parse_failed")

    queue = MagicMock()
    # 첫 enqueue는 stale inflight로 거절 → clear 후 재시도 성공
    queue.enqueue.side_effect = [False, True]

    ingest = InProcessIngest(
        parser=MagicMock(), chunker=MagicMock(),
        embedder=MagicMock(), store=MagicMock(),
        metadata=meta, queue=queue)

    assert ingest.register("doc1", "a.pdf", "owner") == "accepted"
    with tempfile.NamedTemporaryFile(suffix=".pdf", delete=False) as f:
        f.write(b"%PDF-1.4")
        spool = f.name
    try:
        ingest.submit("doc1", spool, "a.pdf", "owner")
        assert Path(spool).exists()
        queue.clear_inflight.assert_called_once_with("doc1")
        assert queue.enqueue.call_count == 2
    finally:
        try:
            os.unlink(spool)
        except OSError:
            pass


def test_duplicate_ready_drops_spool():
    meta = MetadataStore(dsn="sqlite:///:memory:")
    meta.register_for_owner("doc1", "a.pdf", "owner")
    meta.mark_ready("doc1", "owner", 3)

    queue = MagicMock()
    queue.enqueue.return_value = False

    ingest = InProcessIngest(
        parser=MagicMock(), chunker=MagicMock(),
        embedder=MagicMock(), store=MagicMock(),
        metadata=meta, queue=queue)

    # register는 duplicate라 submit 안 타는 게 정상이지만,
    # submit이 직접 호출될 때 ready면 스풀 삭제
    with tempfile.NamedTemporaryFile(suffix=".pdf", delete=False) as f:
        f.write(b"%PDF-1.4")
        spool = f.name
    ingest.submit("doc1", spool, "a.pdf", "owner")
    assert not Path(spool).exists()
