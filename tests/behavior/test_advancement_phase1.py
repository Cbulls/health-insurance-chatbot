"""고도화 Phase 1 — PII·budget·metrics·audit."""
from __future__ import annotations

from harag.security.pii import PiiMasker
from harag.api.daily_budget import (
    check_budget, record_question, reset_for_tests, questions_used,
)
from harag.observability.metrics_export import (
    record_query, prometheus_text, reset_for_tests as reset_metrics,
)
from harag.storage.metadata_store import MetadataStore
from harag.parsing.ocr_policy import decide_ocr


def test_pii_masks_rrn_and_phone():
    m = PiiMasker()
    text, report = m.mask("주민 900101-1234567 전화 010-1234-5678 제10조")
    assert "[PII]" in text
    assert "900101" not in text
    assert "010-1234" not in text
    assert "제10조" in text
    assert report.get("resident_number", 0) >= 1
    assert report.get("phone", 0) >= 1


def test_daily_budget_blocks(monkeypatch):
    reset_for_tests()
    monkeypatch.setenv("DAILY_QUESTION_BUDGET", "2")
    from harag.config.settings import get_settings
    get_settings.cache_clear()
    record_question("u1")
    record_question("u1")
    assert questions_used("u1") >= 2
    import pytest
    from fastapi import HTTPException
    with pytest.raises(HTTPException) as ei:
        check_budget("u1")
    assert "budget_exhausted" in str(ei.value.detail)
    get_settings.cache_clear()
    monkeypatch.delenv("DAILY_QUESTION_BUDGET", raising=False)
    get_settings.cache_clear()


def test_metrics_prometheus_text():
    reset_metrics()
    record_query(abstained=False, top_score=0.9, stages={"retrieval": 0.01},
                 egress_tokens=100)
    record_query(abstained=True, top_score=0.1)
    body = prometheus_text()
    assert "harag_queries_total 2" in body
    assert "harag_queries_abstained_total 1" in body
    assert "harag_llm_egress_tokens_total 100" in body


def test_query_audit_log():
    meta = MetadataStore(dsn="sqlite:///:memory:")
    meta.log_audit("query", "u1", '{"query_redacted":"x"}', "tid-1")
    logs = meta.get_audit_logs(event="query")
    assert len(logs) == 1
    assert logs[0].user_id == "u1"


def test_ocr_policy_threshold():
    assert decide_ocr(0.10, 0.15).introduce_ocr is False
    assert decide_ocr(0.20, 0.15).introduce_ocr is True
