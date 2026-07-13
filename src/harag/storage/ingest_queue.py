"""
인덱싱 큐 어댑터 — 읽기/쓰기 격리(NFR-2).

업로드를 받아 워커가 비동기 소비. API는 enqueue 후 즉시 반환(인덱싱 안 기다림).
deps.py의 IngestQueue 프로토콜 구현. 운영은 메시지 큐(SQS/RabbitMQ), 검증은 인메모리.

멱등: 같은 document_id(내용 해시 기반)는 중복 등록 거부.
dead-letter: 처리 실패 작업을 격리해 재처리·조사 대상으로.
"""
from __future__ import annotations

from collections import deque

from harag.indexing.worker import IngestJob


class InMemoryIngestQueue:
    """테스트·단일 노드용. 분산 운영은 메시지 큐 백엔드로 교체."""

    def __init__(self):
        self._q: deque[IngestJob] = deque()
        self._seen: set[str] = set()        # 멱등: 등록된 document_id
        self._dead: list[IngestJob] = []

    async def enqueue(self, document_id: str, raw: bytes,
                      filename: str, uploaded_by: str,
                      source_system: str = "", department: str = "",
                      security_level: str = "internal",
                      acl_tags: list[str] | None = None) -> bool:
        # 멱등: 이미 등록된 문서면 거부
        if document_id in self._seen:
            return False
        self._seen.add(document_id)
        self._q.append(IngestJob(
            document_id=document_id, raw=raw, filename=filename,
            source_system=source_system, department=department,
            security_level=security_level,
            acl_tags=acl_tags or [],
        ))
        return True

    async def dequeue(self) -> IngestJob | None:
        """워커가 호출. 빈 큐면 None(폴링 루프에서 대기 판단)."""
        if not self._q:
            return None
        return self._q.popleft()

    def mark_failed(self, job: IngestJob, reason: str) -> None:
        """처리 실패 작업을 dead-letter로 격리."""
        self._dead.append(job)

    def dead_letters(self) -> list[IngestJob]:
        return list(self._dead)

    def depth(self) -> int:
        """큐 깊이(관측·백프레셔 판단용)."""
        return len(self._q)
