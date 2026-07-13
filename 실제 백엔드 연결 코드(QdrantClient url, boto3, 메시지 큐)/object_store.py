"""
오브젝트 스토리지 어댑터 — 원본·IR·청크 보존(NFR-8 재인덱싱 전제).

S3 호환 백엔드(운영: boto3로 S3/MinIO). 검증: 인메모리 fake.
Backend Protocol로 분리 → 실제 S3와 테스트 fake를 같은 인터페이스로.

키 설계:
  originals/{doc_id}                 원본 바이트
  ir/{doc_id}/v{version}.json        버전별 IR(재인덱싱 시 특정 버전 복원)
"""
from __future__ import annotations

from typing import Protocol


class StorageBackend(Protocol):
    def put(self, bucket: str, key: str, data: bytes) -> None: ...
    def get(self, bucket: str, key: str) -> bytes: ...      # 없으면 KeyError
    def exists(self, bucket: str, key: str) -> bool: ...


class InMemoryBackend:
    """테스트·개발용. 운영은 boto3 백엔드로 교체."""
    def __init__(self):
        self._data: dict[tuple[str, str], bytes] = {}
    def put(self, bucket, key, data):
        self._data[(bucket, key)] = data
    def get(self, bucket, key):
        if (bucket, key) not in self._data:
            raise KeyError(f"{bucket}/{key}")
        return self._data[(bucket, key)]
    def exists(self, bucket, key):
        return (bucket, key) in self._data


class S3Backend:
    """운영용 boto3 백엔드. 실제 S3/MinIO 연결."""
    def __init__(self, client, ):
        self._s3 = client   # boto3.client("s3", endpoint_url=...)
    def put(self, bucket, key, data):
        self._s3.put_object(Bucket=bucket, Key=key, Body=data)
    def get(self, bucket, key):
        try:
            return self._s3.get_object(Bucket=bucket, Key=key)["Body"].read()
        except Exception as e:  # botocore NoSuchKey 등
            raise KeyError(f"{bucket}/{key}") from e
    def exists(self, bucket, key):
        try:
            self._s3.head_object(Bucket=bucket, Key=key)
            return True
        except Exception:
            return False


class ObjectStore:
    def __init__(self, backend: StorageBackend, bucket: str = "harag-originals"):
        self._b = backend
        self._bucket = bucket

    # ── 원본 ──
    def put_original(self, doc_id: str, raw: bytes) -> None:
        self._b.put(self._bucket, f"originals/{doc_id}", raw)

    def get_original(self, doc_id: str) -> bytes:
        return self._b.get(self._bucket, f"originals/{doc_id}")

    def exists_original(self, doc_id: str) -> bool:
        return self._b.exists(self._bucket, f"originals/{doc_id}")

    # ── IR(버전별) ──
    def put_ir(self, doc_id: str, version: int, ir_json: str) -> None:
        self._b.put(self._bucket, f"ir/{doc_id}/v{version}.json", ir_json.encode())

    def get_ir(self, doc_id: str, version: int) -> str:
        return self._b.get(self._bucket, f"ir/{doc_id}/v{version}.json").decode()
