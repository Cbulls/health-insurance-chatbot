"""
의존성 주입 — 라우트가 쓰는 파이프라인·큐를 제공.

라우트는 구체 구현을 모른다. 여기서 주입한다 → 컴포넌트 교체·테스트 용이.
미구현 컴포넌트는 Protocol로 인터페이스만 두고, 앱 시작 시 실제 구현을 set한다.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Protocol, AsyncIterator

from harag.contracts.boundaries import AuthContext, ScoredChunk


@dataclass
class AnswerResult:
    answer: str | None
    context_chunks: list[ScoredChunk] = field(default_factory=list)
    abstained: bool = False
    abstain_reason: str | None = None


@dataclass
class StreamEvent:
    kind: str          # "token" | "abstain" | "done" | "citations"
    data: str

    def to_sse(self) -> str:
        import json
        return json.dumps({"kind": self.kind, "data": self.data})


class QueryPipeline(Protocol):
    """읽기 경로 오케스트레이션: 검색→리랭킹→생성. 미구현(다음 작업)."""
    async def answer(self, query: str, auth: AuthContext,
                     conversation_id: str | None) -> AnswerResult: ...
    def answer_stream(self, query: str, auth: AuthContext,
                      conversation_id: str | None) -> AsyncIterator[StreamEvent]: ...


class IngestQueue(Protocol):
    """수집 큐: 업로드를 워커로 전달. 미구현(다음 작업)."""
    async def enqueue(self, document_id: str, raw: bytes,
                      filename: str, uploaded_by: str) -> bool: ...


# ── 앱 시작 시 주입(main에서). 미주입 시 명확한 에러. ──
_query_pipeline: QueryPipeline | None = None
_ingest_queue: IngestQueue | None = None


def set_query_pipeline(p: QueryPipeline) -> None:
    global _query_pipeline
    _query_pipeline = p


def set_ingest_queue(q: IngestQueue) -> None:
    global _ingest_queue
    _ingest_queue = q


def get_query_pipeline() -> QueryPipeline:
    if _query_pipeline is None:
        raise RuntimeError("QueryPipeline 미주입 — 다음 작업으로 구현·주입 필요")
    return _query_pipeline


def get_ingest_queue() -> IngestQueue:
    if _ingest_queue is None:
        raise RuntimeError("IngestQueue 미주입 — 다음 작업으로 구현·주입 필요")
    return _ingest_queue
