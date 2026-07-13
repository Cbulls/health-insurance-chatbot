"""
PostgreSQL 메타데이터 저장소 — 운영 메타의 진실 원천(설계 §4.1).

벡터=Qdrant, 원본=오브젝트 스토리지, 운영 메타=여기(PG).
SQLAlchemy 2.0 ORM으로 스키마 정의 → 운영은 postgresql:// DSN, 검증은 sqlite 인메모리.
같은 코드가 양쪽에서 동작(방언 차이는 SQLAlchemy가 흡수).

테이블:
  documents       문서 등록부(상태·활성 버전)
  doc_versions    버전 이력(시각·청크 수·품질)
  audit_logs      감사 로그(업로드·질의·외부 유출)

Qdrant의 활성 버전 포인터와 documents.active_version이 일관해야 한다.
PG가 진실 원천, Qdrant는 검색 인덱스.
"""
from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import (
    create_engine, String, Integer, Float, DateTime, Text, select, func
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, Session


class Base(DeclarativeBase):
    pass


class Document(Base):
    __tablename__ = "documents"
    document_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    filename: Mapped[str] = mapped_column(String(512))
    department: Mapped[str] = mapped_column(String(128), index=True)
    uploaded_by: Mapped[str] = mapped_column(String(128))
    status: Mapped[str] = mapped_column(String(32), default="registered")
    active_version: Mapped[int] = mapped_column(Integer, default=0)
    status_reason: Mapped[str] = mapped_column(String(256), default="")
    updated_at: Mapped[datetime] = mapped_column(DateTime)


class DocVersion(Base):
    __tablename__ = "doc_versions"
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    document_id: Mapped[str] = mapped_column(String(64), index=True)
    version: Mapped[int] = mapped_column(Integer)
    chunk_count: Mapped[int] = mapped_column(Integer)
    table_recovery: Mapped[float] = mapped_column(Float)
    indexed_at: Mapped[datetime] = mapped_column(DateTime)


class AuditLog(Base):
    __tablename__ = "audit_logs"
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    event: Mapped[str] = mapped_column(String(32), index=True)  # upload/query/egress
    user_id: Mapped[str] = mapped_column(String(128))
    detail: Mapped[str] = mapped_column(Text)
    trace_id: Mapped[str] = mapped_column(String(64), index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime)


def _now() -> datetime:
    return datetime.now(timezone.utc)


class MetadataStore:
    def __init__(self, dsn: str = "sqlite:///:memory:"):
        # 운영: postgresql+psycopg://user:pw@host/db
        self._engine = create_engine(dsn, future=True)
        Base.metadata.create_all(self._engine)

    # ── 문서 등록부 ──
    def register_document(self, document_id: str, filename: str,
                          department: str, uploaded_by: str) -> None:
        with Session(self._engine) as s:
            doc = s.get(Document, document_id)
            if doc is None:                       # 멱등: 있으면 갱신
                doc = Document(document_id=document_id)
                s.add(doc)
            doc.filename = filename
            doc.department = department
            doc.uploaded_by = uploaded_by
            doc.updated_at = _now()
            s.commit()

    def get_document(self, document_id: str) -> Document | None:
        with Session(self._engine) as s:
            return s.get(Document, document_id)

    def update_status(self, document_id: str, status: str, reason: str = "") -> None:
        with Session(self._engine) as s:
            doc = s.get(Document, document_id)
            if doc:
                doc.status = status
                doc.status_reason = reason
                doc.updated_at = _now()
                s.commit()

    def set_active_version(self, document_id: str, version: int) -> None:
        with Session(self._engine) as s:
            doc = s.get(Document, document_id)
            if doc:
                doc.active_version = version
                doc.status = "indexed"
                doc.updated_at = _now()
                s.commit()

    def list_documents(self, department: str | None = None) -> list[Document]:
        with Session(self._engine) as s:
            stmt = select(Document)
            if department:
                stmt = stmt.where(Document.department == department)
            return list(s.scalars(stmt))

    # ── 버전 이력 ──
    def record_version(self, document_id: str, version: int,
                       chunk_count: int, table_recovery: float) -> None:
        with Session(self._engine) as s:
            s.add(DocVersion(document_id=document_id, version=version,
                             chunk_count=chunk_count, table_recovery=table_recovery,
                             indexed_at=_now()))
            s.commit()

    def get_version_history(self, document_id: str) -> list[DocVersion]:
        with Session(self._engine) as s:
            return list(s.scalars(
                select(DocVersion).where(DocVersion.document_id == document_id)
                .order_by(DocVersion.version)))

    # ── 감사 로그 ──
    def log_audit(self, event: str, user_id: str, detail: str, trace_id: str) -> None:
        with Session(self._engine) as s:
            s.add(AuditLog(event=event, user_id=user_id, detail=detail,
                           trace_id=trace_id, created_at=_now()))
            s.commit()

    def get_audit_logs(self, event: str | None = None) -> list[AuditLog]:
        with Session(self._engine) as s:
            stmt = select(AuditLog)
            if event:
                stmt = stmt.where(AuditLog.event == event)
            return list(s.scalars(stmt.order_by(AuditLog.created_at)))
