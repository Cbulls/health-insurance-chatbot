"""
문서 메타데이터 저장소 — 운영 메타의 진실 원천(설계 §4.1).

벡터=Qdrant, 원본=오브젝트 스토리지, 운영 메타=여기(SQLite 기본 / PostgreSQL 선택).
SQLAlchemy 2.0 ORM. 기본 DSN은 sqlite 파일, DATABASE_URL이 있으면 Postgres.

테이블:
  documents       문서 등록부(owner 스코프, 상태·청크 수)
  collections     사내 지식 라이브러리 컬렉션
  doc_versions    버전 이력
  audit_logs      감사 로그

PK는 (uploaded_by, document_id) — MVP owner 격리와 일치.
"""
from __future__ import annotations

import re
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone

from sqlalchemy import (
    create_engine, String, Integer, Float, DateTime, Text, select, func,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, Session, sessionmaker


class Base(DeclarativeBase):
    pass


class Collection(Base):
    __tablename__ = "collections"
    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    slug: Mapped[str] = mapped_column(String(128), unique=True, index=True)
    title: Mapped[str] = mapped_column(String(256))
    description: Mapped[str] = mapped_column(String(1024), default="")
    created_by: Mapped[str] = mapped_column(String(128), default="")
    created_at: Mapped[datetime] = mapped_column(DateTime)


class Document(Base):
    __tablename__ = "documents"
    uploaded_by: Mapped[str] = mapped_column(String(128), primary_key=True)
    document_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    filename: Mapped[str] = mapped_column(String(512))
    department: Mapped[str] = mapped_column(String(128), index=True, default="")
    # personal | shared | library
    scope: Mapped[str] = mapped_column(String(32), default="personal")
    collection_id: Mapped[str] = mapped_column(String(64), index=True, default="")
    status: Mapped[str] = mapped_column(String(32), default="registered")
    active_version: Mapped[int] = mapped_column(Integer, default=0)
    n_chunks: Mapped[int] = mapped_column(Integer, default=0)
    status_reason: Mapped[str] = mapped_column(String(256), default="")
    object_key: Mapped[str] = mapped_column(String(512), default="")
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
    scope: str = "personal"
    collection_id: str = ""
    active_version: int = 0
    status_reason: str = ""
    object_key: str = ""
    updated_at: datetime | None = None


@dataclass(frozen=True)
class CollectionRecord:
    id: str
    slug: str
    title: str
    description: str = ""
    created_by: str = ""
    created_at: datetime | None = None
    n_documents: int = 0


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _slugify(title: str) -> str:
    raw = (title or "").strip().lower()
    raw = re.sub(r"\s+", "-", raw)
    raw = re.sub(r"[^\w\-가-힣]", "", raw, flags=re.UNICODE)
    return (raw[:80] or uuid.uuid4().hex[:12])


def _infer_scope(doc: Document) -> str:
    raw = (getattr(doc, "scope", None) or "").strip()
    if raw in ("personal", "shared", "library"):
        return raw
    # 레거시: department가 있으면 공유로 간주
    return "shared" if (doc.department or "").strip() else "personal"


def _to_record(doc: Document) -> DocumentRecord:
    return DocumentRecord(
        document_id=doc.document_id,
        filename=doc.filename,
        uploaded_by=doc.uploaded_by,
        status=doc.status,
        n_chunks=int(doc.n_chunks or 0),
        department=doc.department or "",
        scope=_infer_scope(doc),
        collection_id=getattr(doc, "collection_id", None) or "",
        active_version=int(doc.active_version or 0),
        status_reason=doc.status_reason or "",
        object_key=getattr(doc, "object_key", None) or "",
        updated_at=doc.updated_at,
    )


def _to_collection(row: Collection, n_documents: int = 0) -> CollectionRecord:
    return CollectionRecord(
        id=row.id,
        slug=row.slug,
        title=row.title,
        description=row.description or "",
        created_by=row.created_by or "",
        created_at=row.created_at,
        n_documents=n_documents,
    )


class MetadataStore:
    def __init__(self, dsn: str = "sqlite:///:memory:"):
        # 운영 PG: postgresql+psycopg://user:pw@host/db
        # 기본 로컬: sqlite:///./data/harag.db
        connect_args = {"check_same_thread": False} if dsn.startswith("sqlite") else {}
        self._engine = create_engine(dsn, future=True, connect_args=connect_args)
        Base.metadata.create_all(self._engine)
        self._migrate_sqlite_columns()
        self._Session = sessionmaker(
            self._engine, expire_on_commit=False, future=True,
        )

    def _migrate_sqlite_columns(self) -> None:
        """기존 sqlite 파일에 신규 컬럼 추가(create_all은 컬럼을 안 넣음)."""
        if not str(self._engine.url).startswith("sqlite"):
            return
        with self._engine.begin() as conn:
            rows = conn.exec_driver_sql("PRAGMA table_info(documents)").fetchall()
            cols = {r[1] for r in rows}
            if "object_key" not in cols:
                conn.exec_driver_sql(
                    "ALTER TABLE documents ADD COLUMN object_key VARCHAR(512) DEFAULT ''"
                )
            if "scope" not in cols:
                conn.exec_driver_sql(
                    "ALTER TABLE documents ADD COLUMN scope VARCHAR(32) DEFAULT 'personal'"
                )
                # 기존 부서 태깅 문서는 공유 선반으로 승격
                conn.exec_driver_sql(
                    "UPDATE documents SET scope='shared' "
                    "WHERE department IS NOT NULL AND TRIM(department) != ''"
                )
            if "collection_id" not in cols:
                conn.exec_driver_sql(
                    "ALTER TABLE documents ADD COLUMN collection_id "
                    "VARCHAR(64) DEFAULT ''"
                )

    def _session(self) -> Session:
        return self._Session()

    def _get(self, s: Session, document_id: str, uploaded_by: str) -> Document | None:
        return s.get(Document, (uploaded_by, document_id))

    # ── 라이브 ingest용 owner 스코프 API ──
    def register_for_owner(
        self, document_id: str, filename: str, owner: str,
        department: str = "",
        scope: str = "personal",
        collection_id: str = "",
    ) -> str:
        """등록. ready/processing이면 'duplicate', 신규·failed 재시도면 'accepted'."""
        scope_n = scope if scope in ("personal", "shared", "library") else "personal"
        if scope_n == "shared":
            dept = department or ""
            coll = ""
        elif scope_n == "library":
            dept = ""
            coll = collection_id or ""
        else:
            dept = ""
            coll = ""
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
                doc.department = dept
                doc.scope = scope_n
                doc.collection_id = coll
                doc.updated_at = _now()
                s.commit()
            else:
                s.add(Document(
                    uploaded_by=owner,
                    document_id=document_id,
                    filename=filename,
                    department=dept,
                    scope=scope_n,
                    collection_id=coll,
                    status="processing",
                    active_version=0,
                    n_chunks=0,
                    status_reason="",
                    object_key="",
                    updated_at=_now(),
                ))
                s.commit()
        self.log_audit(
            event="upload", user_id=owner,
            detail=(f"register {document_id} {filename} scope={scope_n}"
                    f" collection={coll}"),
            trace_id=document_id,
        )
        return "accepted"

    def set_object_key(self, document_id: str, owner: str, object_key: str) -> None:
        with self._session() as s:
            doc = self._get(s, document_id, owner)
            if doc is None:
                return
            doc.object_key = (object_key or "")[:512]
            doc.updated_at = _now()
            s.commit()

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

    def mark_ready(self, document_id: str, owner: str, n_chunks: int,
                   warning: str | None = None,
                   version: int | None = None) -> None:
        with self._session() as s:
            doc = self._get(s, document_id, owner)
            if doc is None:
                return
            doc.status = "ready"
            doc.n_chunks = int(n_chunks)
            if version is not None:
                doc.active_version = int(version)
            else:
                doc.active_version = max(int(doc.active_version or 0) + 1, 1)
            # ready여도 파서 제한 경고(예: HWP 표)를 남길 수 있다.
            doc.status_reason = (warning or "")[:256]
            doc.updated_at = _now()
            ver = int(doc.active_version)
            s.commit()
        self.record_version(
            document_id, version=ver, chunk_count=n_chunks, table_recovery=0.0,
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
                # ready 유지(창구 상태). 레거시 'indexed'는 ready와 동치로 취급.
                if doc.status not in ("failed", "processing"):
                    doc.status = "ready"
                doc.updated_at = _now()
                s.commit()

    def list_for_acl(self, owner: str, dept_tags: list[str] | None = None
                     ) -> list[DocumentRecord]:
        """본인 문서 + 동일 부서 shared + 전사 지식 라이브러리."""
        with self._session() as s:
            rows = list(s.scalars(
                select(Document).where(Document.uploaded_by == owner)
                .order_by(Document.updated_at.desc())
            ))
            seen = {(r.uploaded_by, r.document_id) for r in rows}
            depts = []
            for t in dept_tags or []:
                if t.startswith("dept:"):
                    depts.append(t.split(":", 1)[1])
            if depts:
                candidates = list(s.scalars(
                    select(Document).where(Document.department.in_(depts))
                    .order_by(Document.updated_at.desc())
                ))
                for d in candidates:
                    if _infer_scope(d) != "shared":
                        continue
                    key = (d.uploaded_by, d.document_id)
                    if key not in seen:
                        rows.append(d)
                        seen.add(key)
            # 사내 지식 라이브러리 — 전원 열람
            lib_docs = list(s.scalars(
                select(Document).where(Document.scope == "library")
                .order_by(Document.updated_at.desc())
            ))
            for d in lib_docs:
                key = (d.uploaded_by, d.document_id)
                if key not in seen:
                    rows.append(d)
                    seen.add(key)
            return [_to_record(d) for d in rows]

    def list_library_documents(
        self, collection_id: str | None = None,
    ) -> list[DocumentRecord]:
        with self._session() as s:
            stmt = select(Document).where(Document.scope == "library")
            if collection_id:
                stmt = stmt.where(Document.collection_id == collection_id)
            stmt = stmt.order_by(Document.updated_at.desc())
            return [_to_record(d) for d in s.scalars(stmt)]

    def find_accessible(
        self, document_id: str, owner: str, dept_tags: list[str] | None = None,
    ) -> DocumentRecord | None:
        """본인·공유·라이브러리 문서."""
        own = self.get_for_owner(document_id, owner)
        if own is not None:
            return own
        for rec in self.list_for_acl(owner, dept_tags):
            if rec.document_id == document_id:
                return rec
        return None

    # ── 사내 지식 라이브러리 컬렉션 ──
    def create_collection(
        self, title: str, created_by: str, description: str = "",
        slug: str | None = None,
    ) -> CollectionRecord:
        cid = uuid.uuid4().hex[:16]
        base = _slugify(slug or title)
        candidate = base
        n = 0
        with self._session() as s:
            while s.scalar(
                select(Collection).where(Collection.slug == candidate)
            ) is not None:
                n += 1
                candidate = f"{base}-{n}"
            row = Collection(
                id=cid,
                slug=candidate,
                title=(title or candidate)[:256],
                description=(description or "")[:1024],
                created_by=created_by or "",
                created_at=_now(),
            )
            s.add(row)
            s.commit()
            rec = _to_collection(row, 0)
        self.log_audit(
            event="collection_create", user_id=created_by,
            detail=f"collection {cid} {candidate}",
            trace_id=cid,
        )
        return rec

    def list_collections(self) -> list[CollectionRecord]:
        with self._session() as s:
            rows = list(s.scalars(
                select(Collection).order_by(Collection.created_at.desc())
            ))
            out: list[CollectionRecord] = []
            for row in rows:
                n = s.scalar(
                    select(func.count()).select_from(Document).where(
                        Document.collection_id == row.id,
                        Document.scope == "library",
                    )
                ) or 0
                out.append(_to_collection(row, int(n)))
            return out

    def get_collection(self, collection_id: str) -> CollectionRecord | None:
        with self._session() as s:
            row = s.get(Collection, collection_id)
            if row is None:
                return None
            n = s.scalar(
                select(func.count()).select_from(Document).where(
                    Document.collection_id == collection_id,
                    Document.scope == "library",
                )
            ) or 0
            return _to_collection(row, int(n))

    def get_collection_by_slug(self, slug: str) -> CollectionRecord | None:
        with self._session() as s:
            row = s.scalar(
                select(Collection).where(Collection.slug == slug)
            )
            if row is None:
                return None
            return self.get_collection(row.id)

    def delete_collection(self, collection_id: str) -> str:
        """성공 시 'ok'. 문서가 있으면 'not_empty'. 없으면 'not_found'."""
        with self._session() as s:
            row = s.get(Collection, collection_id)
            if row is None:
                return "not_found"
            n = s.scalar(
                select(func.count()).select_from(Document).where(
                    Document.collection_id == collection_id,
                    Document.scope == "library",
                )
            ) or 0
            if int(n) > 0:
                return "not_empty"
            s.delete(row)
            s.commit()
        return "ok"

    def delete_document_row(self, document_id: str, uploaded_by: str) -> bool:
        """PK(uploaded_by, document_id)로 삭제(관리자 공유 삭제용)."""
        with self._session() as s:
            doc = self._get(s, document_id, uploaded_by)
            if doc is None:
                return False
            s.delete(doc)
            s.commit()
        self.log_audit(
            event="delete", user_id=uploaded_by,
            detail=f"delete {document_id}",
            trace_id=document_id,
        )
        return True

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
