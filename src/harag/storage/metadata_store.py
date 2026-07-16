"""
문서 메타데이터 저장소 — 운영 메타의 진실 원천(설계 §4.1).

벡터=Qdrant, 원본=오브젝트 스토리지, 운영 메타=여기(SQLite 기본 / PostgreSQL 선택).
SQLAlchemy 2.0 ORM. 기본 DSN은 sqlite 파일, DATABASE_URL이 있으면 Postgres.

테이블:
  documents       문서 등록부(owner 스코프, 상태·청크 수)
  doc_versions    버전 이력
  audit_logs      감사 로그

PK는 (uploaded_by, document_id) — MVP owner 격리와 일치.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone

from sqlalchemy import (
    create_engine, String, Integer, Float, DateTime, Text, select,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, Session, sessionmaker


class Base(DeclarativeBase):
    pass


class Document(Base):
    __tablename__ = "documents"
    uploaded_by: Mapped[str] = mapped_column(String(128), primary_key=True)
    document_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    filename: Mapped[str] = mapped_column(String(512))
    department: Mapped[str] = mapped_column(String(128), index=True, default="")
    status: Mapped[str] = mapped_column(String(32), default="registered")
    active_version: Mapped[int] = mapped_column(Integer, default=0)
    n_chunks: Mapped[int] = mapped_column(Integer, default=0)
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
    event: Mapped[str] = mapped_column(String(32), index=True)
    user_id: Mapped[str] = mapped_column(String(128))
    detail: Mapped[str] = mapped_column(Text)
    trace_id: Mapped[str] = mapped_column(String(64), index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime)


@dataclass(frozen=True)
class DocumentRecord:
    """세션 밖에서도 안전한 문서 스냅샷(DetachedInstanceError 방지)."""
    document_id: str
    filename: str
    uploaded_by: str
    status: str
    n_chunks: int = 0
    department: str = ""
    active_version: int = 0
    status_reason: str = ""
    updated_at: datetime | None = None


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _to_record(doc: Document) -> DocumentRecord:
    return DocumentRecord(
        document_id=doc.document_id,
        filename=doc.filename,
        uploaded_by=doc.uploaded_by,
        status=doc.status,
        n_chunks=int(doc.n_chunks or 0),
        department=doc.department or "",
        active_version=int(doc.active_version or 0),
        status_reason=doc.status_reason or "",
        updated_at=doc.updated_at,
    )


class MetadataStore:
    def __init__(self, dsn: str = "sqlite:///:memory:"):
        # 운영 PG: postgresql+psycopg://user:pw@host/db
        # 기본 로컬: sqlite:///./data/harag.db
        connect_args = {"check_same_thread": False} if dsn.startswith("sqlite") else {}
        self._engine = create_engine(dsn, future=True, connect_args=connect_args)
        Base.metadata.create_all(self._engine)
        self._Session = sessionmaker(
            self._engine, expire_on_commit=False, future=True,
        )

    def _session(self) -> Session:
        return self._Session()

    def _get(self, s: Session, document_id: str, uploaded_by: str) -> Document | None:
        return s.get(Document, (uploaded_by, document_id))

    # ── 라이브 ingest용 owner 스코프 API ──
    def register_for_owner(
        self, document_id: str, filename: str, owner: str,
    ) -> str:
        """등록. ready/processing이면 'duplicate', 신규·failed 재시도면 'accepted'."""
        with self._session() as s:
            doc = self._get(s, document_id, owner)
            if doc is not None:
                if doc.status in ("ready", "processing"):
                    return "duplicate"
                # failed → 재업로드 허용
                doc.filename = filename
                doc.status = "processing"
                doc.status_reason = ""
                doc.n_chunks = 0
                doc.updated_at = _now()
                s.commit()
            else:
                s.add(Document(
                    uploaded_by=owner,
                    document_id=document_id,
                    filename=filename,
                    department="",
                    status="processing",
                    active_version=0,
                    n_chunks=0,
                    status_reason="",
                    updated_at=_now(),
                ))
                s.commit()
        self.log_audit(
            event="upload", user_id=owner,
            detail=f"register {document_id} {filename}",
            trace_id=document_id,
        )
        return "accepted"

    def get_for_owner(self, document_id: str, owner: str) -> DocumentRecord | None:
        with self._session() as s:
            doc = self._get(s, document_id, owner)
            return _to_record(doc) if doc else None

    def list_for_owner(self, owner: str) -> list[DocumentRecord]:
        with self._session() as s:
            rows = list(s.scalars(
                select(Document).where(Document.uploaded_by == owner)
                .order_by(Document.updated_at.desc())
            ))
            return [_to_record(d) for d in rows]

    def delete_for_owner(self, document_id: str, owner: str) -> bool:
        with self._session() as s:
            doc = self._get(s, document_id, owner)
            if doc is None:
                return False
            s.delete(doc)
            s.commit()
        self.log_audit(
            event="delete", user_id=owner,
            detail=f"delete {document_id}",
            trace_id=document_id,
        )
        return True

    def mark_ready(self, document_id: str, owner: str, n_chunks: int) -> None:
        with self._session() as s:
            doc = self._get(s, document_id, owner)
            if doc is None:
                return
            doc.status = "ready"
            doc.n_chunks = int(n_chunks)
            doc.active_version = max(doc.active_version, 1)
            doc.status_reason = ""
            doc.updated_at = _now()
            s.commit()
        self.record_version(
            document_id, version=1, chunk_count=n_chunks, table_recovery=0.0,
        )

    def mark_failed(self, document_id: str, owner: str, reason: str) -> None:
        with self._session() as s:
            doc = self._get(s, document_id, owner)
            if doc is None:
                return
            doc.status = "failed"
            doc.status_reason = (reason or "")[:256]
            doc.updated_at = _now()
            s.commit()

    def mark_processing(self, document_id: str, owner: str) -> None:
        with self._session() as s:
            doc = self._get(s, document_id, owner)
            if doc is None:
                return
            doc.status = "processing"
            doc.status_reason = ""
            doc.updated_at = _now()
            s.commit()

    # ── 워커/레거시 API (uploaded_by 포함) ──
    def register_document(self, document_id: str, filename: str,
                          department: str, uploaded_by: str) -> None:
        with self._session() as s:
            doc = self._get(s, document_id, uploaded_by)
            if doc is None:
                doc = Document(
                    uploaded_by=uploaded_by,
                    document_id=document_id,
                    filename=filename,
                    department=department,
                    status="registered",
                    updated_at=_now(),
                )
                s.add(doc)
            else:
                doc.filename = filename
                doc.department = department
                doc.updated_at = _now()
            s.commit()

    def get_document(self, document_id: str,
                     uploaded_by: str = "") -> DocumentRecord | None:
        with self._session() as s:
            doc = self._get(s, document_id, uploaded_by)
            return _to_record(doc) if doc else None

    def update_status(self, document_id: str, status: str,
                      reason: str = "", uploaded_by: str = "") -> None:
        with self._session() as s:
            doc = self._get(s, document_id, uploaded_by)
            if doc:
                doc.status = status
                doc.status_reason = reason
                doc.updated_at = _now()
                s.commit()

    def set_active_version(self, document_id: str, version: int,
                           uploaded_by: str = "") -> None:
        with self._session() as s:
            doc = self._get(s, document_id, uploaded_by)
            if doc:
                doc.active_version = version
                doc.status = "indexed"
                doc.updated_at = _now()
                s.commit()

    def list_documents(self, department: str | None = None,
                       uploaded_by: str | None = None) -> list[DocumentRecord]:
        with self._session() as s:
            stmt = select(Document)
            if department:
                stmt = stmt.where(Document.department == department)
            if uploaded_by is not None:
                stmt = stmt.where(Document.uploaded_by == uploaded_by)
            return [_to_record(d) for d in s.scalars(stmt)]

    # ── 버전 이력 ──
    def record_version(self, document_id: str, version: int,
                       chunk_count: int, table_recovery: float) -> None:
        with self._session() as s:
            s.add(DocVersion(
                document_id=document_id, version=version,
                chunk_count=chunk_count, table_recovery=table_recovery,
                indexed_at=_now(),
            ))
            s.commit()

    def get_version_history(self, document_id: str) -> list[DocVersion]:
        with self._session() as s:
            return list(s.scalars(
                select(DocVersion).where(DocVersion.document_id == document_id)
                .order_by(DocVersion.version)
            ))

    # ── 감사 로그 ──
    def log_audit(self, event: str, user_id: str, detail: str, trace_id: str) -> None:
        with self._session() as s:
            s.add(AuditLog(
                event=event, user_id=user_id, detail=detail,
                trace_id=trace_id, created_at=_now(),
            ))
            s.commit()

    def get_audit_logs(self, event: str | None = None) -> list[AuditLog]:
        with self._session() as s:
            stmt = select(AuditLog)
            if event:
                stmt = stmt.where(AuditLog.event == event)
            return list(s.scalars(stmt.order_by(AuditLog.created_at)))
