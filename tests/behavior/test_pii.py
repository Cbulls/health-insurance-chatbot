"""
PII 마스킹 TDD — 한국 행정문서의 민감정보를 인덱싱 시점에 마스킹(SEC-03).

설계: 적재 전에 마스킹 → 그 뒤 검색·LLM 컨텍스트·외부 전송·로그 어디에도 원본 PII 없음.
응답 시점 마스킹은 이미 외부로 나간 뒤라 늦다(v4 외부 API 경계).

검증 책임:
  - 주민번호·휴대폰·계좌·이메일·외국인등록번호 등 한국 PII 탐지·마스킹
  - 오탐 방지: 조항번호("제24조")·금액·일반 날짜는 마스킹 안 함(품질 보존)
  - 어떤 종류가 몇 건 마스킹됐는지 보고(감사)
  - 인덱싱 워커가 청킹 전 본문에 적용
"""
import pytest

from harag.security.pii import PiiMasker


def test_PII01_resident_number():
    """주민등록번호 마스킹."""
    m = PiiMasker()
    out, report = m.mask("신청인 901010-1234567 확인")
    assert "901010-1234567" not in out
    assert report["resident_number"] == 1


def test_PII02_phone_number():
    """휴대폰 번호 마스킹."""
    m = PiiMasker()
    out, _ = m.mask("연락처 010-1234-5678 입니다")
    assert "010-1234-5678" not in out


def test_PII03_email():
    """이메일 마스킹."""
    m = PiiMasker()
    out, _ = m.mask("담당자 hong@example.go.kr 로 문의")
    assert "hong@example.go.kr" not in out


def test_PII04_account_number():
    """계좌번호(긴 숫자열) 마스킹."""
    m = PiiMasker()
    out, _ = m.mask("입금계좌 110-234-567890")
    assert "110-234-567890" not in out


def test_PII05_foreign_resident_number():
    """외국인등록번호 마스킹(주민번호와 같은 형식)."""
    m = PiiMasker()
    out, report = m.mask("외국인등록번호 901010-5234567")
    assert "901010-5234567" not in out


def test_PII06_no_false_positive_on_clause_number():
    """오탐 방지: 조항번호는 PII 아님 — 마스킹하면 검색 품질 파괴."""
    m = PiiMasker()
    out, report = m.mask("제24조의2 여비 한도는 50000원")
    assert "제24조의2" in out, "조항번호가 마스킹됨(오탐)"
    assert "50000" in out, "금액이 마스킹됨(오탐)"
    assert sum(report.values()) == 0, "PII 아닌데 마스킹 발생"


def test_PII07_preserves_surrounding_text():
    """PII만 가리고 주변 텍스트는 보존(맥락 유지)."""
    m = PiiMasker()
    out, _ = m.mask("국내출장 여비는 1일 5만원, 문의 010-1111-2222")
    assert "국내출장 여비는 1일 5만원" in out, "본문 손상"


def test_PII08_report_counts_by_type():
    """종류별 건수 보고(감사용)."""
    m = PiiMasker()
    _, report = m.mask("010-1234-5678 그리고 a@b.kr 그리고 010-9999-8888")
    assert report["phone"] == 2
    assert report["email"] == 1


def test_PII09_integrates_with_indexing_worker():
    """인덱싱 워커가 마스킹을 적용 — 적재된 청크에 원본 PII 없음."""
    from harag.parsing.parser import RawBlock
    from harag.indexing.worker import IndexingWorker, IngestJob, IngestStatus

    class Decoder:
        def decode(self, raw, fmt):
            return [RawBlock(kind="paragraph", text="민원인 901010-1234567 연락처 010-1234-5678")]
    class Model:
        dim = 8
        def encode(self, ts): return [[1.0]*8 for _ in ts]
    class Morph:
        def tokens(self, t): return t.split()
    class Store:
        def __init__(self): self.texts = []
        def upsert_vectors(self, d, v, embedded):
            self.texts = [e.chunk.text for e in embedded]
        def stage(self, d, v, ids): pass
        def activate(self, d, v): pass
        def gc(self, d, k): pass

    store = Store()
    w = IndexingWorker(decoder=Decoder(), model=Model(), morph=Morph(),
                       store=store, pii_masker=PiiMasker())
    result = w.process(IngestJob(document_id="d1", raw=b"x", filename="민원.hwp",
                                 source_system="gov", department="civil",
                                 security_level="internal", acl_tags=["dept:civil"]))
    assert result.status == IngestStatus.indexed
    joined = " ".join(store.texts)
    assert "901010-1234567" not in joined, "주민번호가 적재됨(마스킹 누락)"
    assert "010-1234-5678" not in joined, "전화번호가 적재됨"


if __name__ == "__main__":
    import sys
    sys.exit(pytest.main([__file__, "-v"]))
