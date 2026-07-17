"""
의존성 주입 — 라우트가 쓰는 파이프라인·수집 서비스를 제공.

라우트는 구체 구현을 모른다. 앱 시작 시 여기서 주입한다 → 교체·테스트 용이.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Protocol, AsyncIterator, Optional

from harag.contracts.boundaries import AuthContext, ScoredChunk


@dataclass
class AnswerResult:
    answer: str | None
    context_chunks: list[ScoredChunk] = field(default_factory=list)
    abstained: bool = False
    abstain_reason: str | None = None


@dataclass
class StreamEvent:
    kind: str          # "token" | "abstain" | "done" | "citations" | "error"
    data: object       # str, 또는 citations일 때 list[dict]

    def to_sse(self) -> str:
        import json
        return json.dumps({"kind": self.kind, "data": self.data}, ensure_ascii=False)


class QueryPipeline(Protocol):
    async def answer(self, query: str, auth: AuthContext,
                     conversation_id: str | None) -> AnswerResult: ...
    def answer_stream(self, query: str, auth: AuthContext,
                      conversation_id: str | None) -> AsyncIterator[StreamEvent]: ...


class IngestQueue(Protocol):
    """수집 큐: 업로드를 워커로 전달(읽기/쓰기 격리, NFR-2).

    storage.ingest_queue.InMemoryIngestQueue가 이 프로토콜을 구현.
    운영은 메시지 큐(SQS/RabbitMQ) 백엔드로 교체."""
    async def enqueue(self, document_id: str, raw: bytes,
                      filename: str, uploaded_by: str) -> bool: ...


# ── 앱 시작 시 주입(main에서). 미주입 시 명확한 에러. ──
_query_pipeline: Optional[QueryPipeline] = None
_ingest = None  # InProcessIngest(MVP 인프로세스 수집 — hot path 유지)
_ingest_queue: Optional[IngestQueue] = None  # 워커 배선용 큐(라이브러리 경로)
_vector_store = None  # QdrantVectorStore(관측용 — /health 용량 노출)


def set_query_pipeline(p: QueryPipeline) -> None:
    global _query_pipeline
    _query_pipeline = p


def get_query_pipeline() -> QueryPipeline:
    if _query_pipeline is None:
        raise RuntimeError("QueryPipeline 미주입")
    return _query_pipeline


def set_ingest(service) -> None:
    global _ingest
    _ingest = service


def get_ingest():
    if _ingest is None:
        raise RuntimeError("Ingest 서비스 미주입")
    return _ingest


def set_vector_store(store) -> None:
    global _vector_store
    _vector_store = store


def get_vector_store_or_none():
    """관측용 — 미주입이어도 에러 대신 None(헬스체크는 항상 동작해야 함)."""
    return _vector_store


def set_ingest_queue(q: IngestQueue) -> None:
    global _ingest_queue
    _ingest_queue = q


def get_ingest_queue() -> IngestQueue:
    if _ingest_queue is None:
        raise RuntimeError("IngestQueue 미주입 — 워커/큐 배선 필요")
    return _ingest_queue
