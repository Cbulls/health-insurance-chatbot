"""
PostgreSQL 메타데이터 저장소 TDD — 운영 메타의 진실 원천(설계 §4.1).

벡터=Qdrant, 원본=오브젝트 스토리지, 운영 메타=PG.
PG가 담는 것: 문서 등록부, 버전 이력, 감사 로그.

SQLAlchemy로 백엔드 교체 가능(운영 PG / 검증 SQLite 인메모리 — 같은 코드).
어댑터 책임:
  - 문서 등록·상태 갱신(indexed/quarantined)
  - 활성 버전 추적(Qdrant 활성 포인터와 일관)
  - 버전 이력 기록(시각·청크 수·품질)
  - 감사 로그(업로드·질의·외부 유출)
"""
import pytest

from harag.storage.metadata_store import MetadataStore


def _store():
    # 인메모리 SQLite — 운영은 postgresql:// DSN
    return MetadataStore(dsn="sqlite:///:memory:")


def test_MD01_register_document():
    """문서 등록 → 조회 가능."""
    s = _store()
    s.register_document(document_id="doc1", filename="규정.hwp",
                        department="finance", uploaded_by="admin")
    doc = s.get_document("doc1", uploaded_by="admin")
    assert doc is not None
    assert doc.filename == "규정.hwp"
    assert doc.department == "finance"


def test_MD02_record_version_and_activate():
    """버전 기록 + 활성 버전 추적."""
    s = _store()
    s.register_document("doc1", "규정.hwp", "finance", "admin")
    s.record_version("doc1", version=1, chunk_count=10, table_recovery=0.95)
    s.set_active_version("doc1", 1, uploaded_by="admin")
    doc = s.get_document("doc1", uploaded_by="admin")
    assert doc.active_version == 1


def test_MD03_version_history():
    """버전 이력 — 여러 버전이 시간순 기록."""
    s = _store()
    s.register_document("doc1", "규정.hwp", "finance", "admin")
    s.record_version("doc1", version=1, chunk_count=10, table_recovery=0.9)
    s.record_version("doc1", version=2, chunk_count=12, table_recovery=0.95)
    history = s.get_version_history("doc1")
    assert len(history) == 2
    assert {h.version for h in history} == {1, 2}


def test_MD04_update_status_quarantine():
    """파싱 실패 → 상태 quarantined 기록."""
    s = _store()
    s.register_document("doc1", "규정.hwp", "finance", "admin")
    s.update_status("doc1", "quarantined", reason="parse_failed", uploaded_by="admin")
    doc = s.get_document("doc1", uploaded_by="admin")
    assert doc.status == "quarantined"


def test_MD05_audit_log_query():
    """질의 감사 로그 기록·조회."""
    s = _store()
    s.log_audit(event="query", user_id="u1", detail="출장비 한도?", trace_id="t1")
    logs = s.get_audit_logs(event="query")
    assert len(logs) == 1
    assert logs[0].user_id == "u1"
    assert logs[0].trace_id == "t1"


def test_MD06_audit_log_egress():
    """외부 유출 감사 로그(외부 LLM 전송 기록 — v4 경계)."""
    s = _store()
    s.log_audit(event="egress", user_id="u1", detail="ext-llm tokens=120", trace_id="t1")
    egress = s.get_audit_logs(event="egress")
    assert len(egress) == 1


def test_MD07_list_documents_by_department():
    """부서별 문서 목록(운영 조회)."""
    s = _store()
    s.register_document("doc1", "a.hwp", "finance", "admin")
    s.register_document("doc2", "b.hwp", "hr", "admin")
    s.register_document("doc3", "c.hwp", "finance", "admin")
    fin = s.list_documents(department="finance")
    assert len(fin) == 2


def test_MD08_register_idempotent():
    """같은 (owner, document_id) 재등록 → 갱신(멱등, 중복 행 없음)."""
    s = _store()
    s.register_document("doc1", "규정.hwp", "finance", "admin")
    s.register_document("doc1", "규정_개정.hwp", "finance", "admin")
    docs = s.list_documents(department="finance")
    assert len(docs) == 1, "중복 등록으로 행이 늘어남"
    assert s.get_document("doc1", uploaded_by="admin").filename == "규정_개정.hwp"


def test_MD09_owner_scoped_register_ready_list_delete():
    """owner 스코프: 등록 멱등, ready/failed, 목록 격리, 삭제."""
    s = _store()
    assert s.register_for_owner("doc1", "a.pdf", "u1") == "accepted"
    assert s.register_for_owner("doc1", "a.pdf", "u1") == "duplicate"
    # 다른 owner는 같은 document_id 가능
    assert s.register_for_owner("doc1", "a.pdf", "u2") == "accepted"

    s.mark_ready("doc1", "u1", n_chunks=3)
    rec = s.get_for_owner("doc1", "u1")
    assert rec is not None
    assert rec.status == "ready"
    assert rec.n_chunks == 3

    s.mark_failed("doc1", "u2", "parse_failed")
    assert s.get_for_owner("doc1", "u2").status == "failed"

    assert {r.document_id for r in s.list_for_owner("u1")} == {"doc1"}
    assert s.list_for_owner("u1")[0].status == "ready"
    assert s.list_for_owner("u3") == []

    assert s.delete_for_owner("doc1", "u1") is True
    assert s.get_for_owner("doc1", "u1") is None
    assert s.get_for_owner("doc1", "u2") is not None  # 다른 owner 유지


def test_MD10_failed_reregister_accepted():
    """failed 문서는 재등록 가능(accepted)."""
    s = _store()
    s.register_for_owner("doc1", "a.pdf", "u1")
    s.mark_failed("doc1", "u1", "boom")
    assert s.register_for_owner("doc1", "a2.pdf", "u1") == "accepted"
    assert s.get_for_owner("doc1", "u1").status == "processing"
    assert s.get_for_owner("doc1", "u1").filename == "a2.pdf"


if __name__ == "__main__":
    import sys
    sys.exit(pytest.main([__file__, "-v"]))
