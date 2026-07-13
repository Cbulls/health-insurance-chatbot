"""
오브젝트 스토리지 어댑터 TDD — 원본·IR·청크 보존(NFR-8 재인덱싱 전제).

임베딩 모델이 바뀌면 원본→IR→청크를 재실행해야 한다. 임베딩(Qdrant)만 들고 있으면
모델 교체 시 복구 불가. 그래서 원본·IR·청크를 별도 보존한다.

S3 호환 인터페이스(boto3 put_object/get_object 패턴). 검증은 인메모리 fake로.
어댑터 책임:
  - 원본 바이트 저장/조회(문서 ID 기반 키)
  - IR(JSON) 저장/조회
  - 버전별 경로 분리(재인덱싱 시 특정 버전 복원)
  - 존재 확인, 없는 키 조회 시 명확한 처리
"""
import pytest

from harag.storage.object_store import ObjectStore, InMemoryBackend


def _store():
    return ObjectStore(backend=InMemoryBackend(), bucket="harag-originals")


def test_OS01_put_get_original():
    """원본 바이트 저장·조회."""
    s = _store()
    s.put_original("doc1", b"\x50\x4b\x03\x04 hwp bytes")
    assert s.get_original("doc1") == b"\x50\x4b\x03\x04 hwp bytes"


def test_OS02_put_get_ir_json():
    """IR(JSON) 저장·조회 — 재인덱싱용."""
    s = _store()
    ir_json = '{"document_id": "doc1", "blocks": [{"text": "제1조"}]}'
    s.put_ir("doc1", version=1, ir_json=ir_json)
    assert s.get_ir("doc1", version=1) == ir_json


def test_OS03_version_isolation():
    """버전별 경로 분리 — 특정 버전 IR 복원."""
    s = _store()
    s.put_ir("doc1", version=1, ir_json='{"v": 1}')
    s.put_ir("doc1", version=2, ir_json='{"v": 2}')
    assert s.get_ir("doc1", version=1) == '{"v": 1}'
    assert s.get_ir("doc1", version=2) == '{"v": 2}'


def test_OS04_exists_check():
    """존재 확인."""
    s = _store()
    s.put_original("doc1", b"data")
    assert s.exists_original("doc1")
    assert not s.exists_original("nonexistent")


def test_OS05_missing_key_raises():
    """없는 키 조회 → 명확한 예외(크래시 아님)."""
    s = _store()
    with pytest.raises(KeyError):
        s.get_original("nonexistent")


def test_OS06_overwrite_original_idempotent():
    """같은 문서 재업로드 — 덮어쓰기(멱등)."""
    s = _store()
    s.put_original("doc1", b"v1")
    s.put_original("doc1", b"v2")
    assert s.get_original("doc1") == b"v2"


if __name__ == "__main__":
    import sys
    sys.exit(pytest.main([__file__, "-v"]))
