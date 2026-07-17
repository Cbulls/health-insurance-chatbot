"""Settings → ObjectStore (MinIO/S3) 또는 None."""
from __future__ import annotations

import logging

from harag.config.settings import Settings
from harag.storage.object_store import ObjectStore, S3Backend, InMemoryBackend

logger = logging.getLogger("harag.storage")


def build_object_store(settings: Settings, *, allow_memory: bool = False
                       ) -> ObjectStore | None:
    """endpoint+키가 있으면 S3/MinIO. allow_memory면 테스트용 인메모리."""
    if settings.object_store_endpoint and settings.object_store_access_key:
        try:
            import boto3
            from botocore.client import Config
            client = boto3.client(
                "s3",
                endpoint_url=settings.object_store_endpoint,
                aws_access_key_id=settings.object_store_access_key,
                aws_secret_access_key=settings.object_store_secret_key,
                region_name=settings.object_store_region,
                config=Config(signature_version="s3v4"),
            )
            # 버킷 없으면 생성 시도(로컬 MinIO)
            try:
                client.head_bucket(Bucket=settings.object_store_bucket)
            except Exception:  # noqa: BLE001
                try:
                    client.create_bucket(Bucket=settings.object_store_bucket)
                except Exception:  # noqa: BLE001
                    logger.warning("object store bucket ensure failed")
            return ObjectStore(
                S3Backend(client), bucket=settings.object_store_bucket)
        except ImportError:
            logger.warning(
                "OBJECT_STORE_ENDPOINT set but boto3 missing — "
                "pip install boto3")
            return None
        except Exception:  # noqa: BLE001
            logger.exception("object store init failed")
            return None
    if allow_memory:
        return ObjectStore(InMemoryBackend())
    return None
