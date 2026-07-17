"""
Redis Streams 인제스트 큐 — at-least-once + reclaim.

  - PDF 바이트는 Redis에 넣지 않고 스풀 경로만 XADD.
  - Consumer group으로 XREADGROUP / XACK / XAUTOCLAIM.
  - Lua로 in-flight SET + XADD 원자화.
  - 실패는 재시도(XADD) 후 max attempts면 DLQ 스트림.
"""
from __future__ import annotations

import json
import logging
import socket
import time
import uuid
from dataclasses import asdict, dataclass, field
from typing import Any

logger = logging.getLogger("harag.redis")

_GROUP = "harag-workers"

# KEYS[1]=inflight KEYS[2]=stream
# ARGV[1]=document_id ARGV[2]=maxlen ARGV[3..]=field/value pairs for XADD
_ENQUEUE_LUA = """
if redis.call('SISMEMBER', KEYS[1], ARGV[1]) == 1 then
  return 0
end
redis.call('SADD', KEYS[1], ARGV[1])
local args = {'MAXLEN', '~', ARGV[2], '*'}
for i = 3, #ARGV do
  args[#args + 1] = ARGV[i]
end
redis.call('XADD', KEYS[2], unpack(args))
return 1
"""


@dataclass
class SpoolJob:
    """워커가 소비하는 스풀 기반 작업."""
    document_id: str
    spool_path: str
    filename: str
    uploaded_by: str
    source_system: str = "user-upload"
    department: str = "self"
    security_level: str = "internal"
    acl_tags: list[str] = field(default_factory=list)
    attempts: int = 0
    enqueued_at: float = 0.0
    # Streams 메시지 id (dequeue 후 채움)
    message_id: str = ""


class RedisIngestQueue:
    def __init__(self, redis_client: Any, *,
                 prefix: str = "harag:",
                 stream_maxlen: int = 10000,
                 visibility_sec: int = 120,
                 max_attempts: int = 3,
                 consumer_name: str | None = None):
        self._r = redis_client
        self._prefix = prefix if prefix.endswith(":") else f"{prefix}:"
        self._stream = f"{self._prefix}ingest:jobs"
        self._dead = f"{self._prefix}ingest:dead"
        self._inflight = f"{self._prefix}ingest:inflight"
        self._maxlen = max(100, stream_maxlen)
        self._visibility = max(30, visibility_sec)
        self._max_attempts = max(1, max_attempts)
        self._consumer = consumer_name or (
            f"{socket.gethostname()}-{uuid.uuid4().hex[:8]}")
        self._ensure_group()

    def _ensure_group(self) -> None:
        try:
            self._r.xgroup_create(
                self._stream, _GROUP, id="0", mkstream=True)
        except Exception as e:  # noqa: BLE001 — BUSYGROUP면 이미 존재
            if "BUSYGROUP" not in str(e).upper():
                # 스트림이 없어도 mkstream=True로 생성됨. 그 외는 로그.
                if "already exists" not in str(e).lower():
                    logger.debug("xgroup_create: %s", e)

    def enqueue(self, document_id: str, spool_path: str,
                filename: str, uploaded_by: str,
                source_system: str = "user-upload",
                department: str = "self",
                security_level: str = "internal",
                acl_tags: list[str] | None = None,
                attempts: int = 0) -> bool:
        """원자적 적재. in-flight면 False."""
        job = SpoolJob(
            document_id=document_id, spool_path=spool_path,
            filename=filename, uploaded_by=uploaded_by,
            source_system=source_system, department=department,
            security_level=security_level,
            acl_tags=acl_tags or [f"owner:{uploaded_by}"],
            attempts=attempts,
            enqueued_at=time.time(),
        )
        fields = self._job_to_fields(job)
        argv = [document_id, str(self._maxlen), *fields]
        try:
            ok = self._r.eval(
                _ENQUEUE_LUA, 2, self._inflight, self._stream, *argv)
            return bool(int(ok))
        except Exception:  # noqa: BLE001
            logger.exception("enqueue failed for %s", document_id)
            return False

    async def enqueue_async(self, document_id: str, spool_path: str,
                            filename: str, uploaded_by: str, **kw) -> bool:
        return self.enqueue(document_id, spool_path, filename, uploaded_by, **kw)

    @staticmethod
    def _job_to_fields(job: SpoolJob) -> list[str]:
        d = asdict(job)
        d.pop("message_id", None)
        d["acl_tags"] = json.dumps(d.get("acl_tags") or [], ensure_ascii=False)
        out: list[str] = []
        for k, v in d.items():
            out.append(k)
            out.append("" if v is None else str(v))
        return out

    @staticmethod
    def _fields_to_job(message_id: str, fields: dict) -> SpoolJob:
        acl = fields.get("acl_tags") or "[]"
        try:
            acl_tags = json.loads(acl) if isinstance(acl, str) else list(acl)
        except json.JSONDecodeError:
            acl_tags = []
        return SpoolJob(
            document_id=fields.get("document_id", ""),
            spool_path=fields.get("spool_path", ""),
            filename=fields.get("filename", ""),
            uploaded_by=fields.get("uploaded_by", ""),
            source_system=fields.get("source_system", "user-upload"),
            department=fields.get("department", "self"),
            security_level=fields.get("security_level", "internal"),
            acl_tags=acl_tags,
            attempts=int(fields.get("attempts") or 0),
            enqueued_at=float(fields.get("enqueued_at") or 0),
            message_id=message_id,
        )

    def dequeue(self, timeout_sec: int = 5) -> SpoolJob | None:
        """새 메시지 또는 reclaim된 stale 메시지 하나."""
        # 1) stale PEL reclaim
        job = self._reclaim_one()
        if job is not None:
            return job
        # 2) 신규 메시지
        try:
            rows = self._r.xreadgroup(
                groupname=_GROUP, consumername=self._consumer,
                streams={self._stream: ">"}, count=1,
                block=max(1, timeout_sec) * 1000,
            )
        except Exception:  # noqa: BLE001
            logger.exception("xreadgroup failed")
            return None
        if not rows:
            return None
        _stream_name, messages = rows[0]
        if not messages:
            return None
        mid, fields = messages[0]
        return self._fields_to_job(mid, fields)

    def _reclaim_one(self) -> SpoolJob | None:
        try:
            # redis-py: xautoclaim(name, groupname, consumername, min_idle_time, start_id='0-0', count=1)
            result = self._r.xautoclaim(
                self._stream, _GROUP, self._consumer,
                min_idle_time=self._visibility * 1000,
                start_id="0-0", count=1,
            )
        except Exception:  # noqa: BLE001 — Redis < 6.2면 스킵
            return None
        # result: [next_start_id, [(id, fields), ...], deleted_ids?]
        if not result or len(result) < 2:
            return None
        messages = result[1] or []
        if not messages:
            return None
        mid, fields = messages[0]
        logger.warning("reclaimed stale ingest job %s id=%s",
                       fields.get("document_id"), mid)
        return self._fields_to_job(mid, fields)

    def ack_success(self, job: SpoolJob) -> None:
        """처리 성공 — XACK + in-flight 해제."""
        if job.message_id:
            try:
                self._r.xack(self._stream, _GROUP, job.message_id)
            except Exception:  # noqa: BLE001
                logger.exception("xack failed %s", job.message_id)
        self.clear_inflight(job.document_id)

    def on_failed(self, document_id: str) -> None:
        """파이프라인 논리 실패(parse_failed 등) — 재업로드 가능하도록 in-flight 해제."""
        self.clear_inflight(document_id)

    def clear_inflight(self, document_id: str) -> None:
        try:
            self._r.srem(self._inflight, document_id)
        except Exception:  # noqa: BLE001
            pass

    # 하위 호환 별칭
    def clear_seen(self, document_id: str) -> None:
        self.clear_inflight(document_id)

    def nack_retry_or_dead(self, job: SpoolJob, reason: str) -> None:
        """워커 예외: attempts 남으면 재적재, 아니면 DLQ."""
        if job.message_id:
            try:
                self._r.xack(self._stream, _GROUP, job.message_id)
            except Exception:  # noqa: BLE001
                pass
        next_attempt = job.attempts + 1
        if next_attempt < self._max_attempts:
            self.clear_inflight(job.document_id)
            ok = self.enqueue(
                job.document_id, job.spool_path, job.filename,
                job.uploaded_by, source_system=job.source_system,
                department=job.department, security_level=job.security_level,
                acl_tags=job.acl_tags, attempts=next_attempt)
            logger.warning("requeued %s attempt=%d ok=%s reason=%s",
                           job.document_id, next_attempt, ok, reason)
            return
        # DLQ
        try:
            payload = asdict(job)
            payload.pop("message_id", None)
            payload["reason"] = reason
            payload["acl_tags"] = json.dumps(
                payload.get("acl_tags") or [], ensure_ascii=False)
            fields = {
                k: ("" if v is None else str(v)) for k, v in payload.items()}
            self._r.xadd(
                self._dead, fields, maxlen=1000, approximate=True)
        except Exception:  # noqa: BLE001
            logger.exception("dlq xadd failed")
        self.clear_inflight(job.document_id)
        logger.warning("dead-letter %s after %d attempts: %s",
                       job.document_id, next_attempt, reason)

    def mark_failed(self, job: SpoolJob, reason: str) -> None:
        """하위 호환 — nack_retry_or_dead로 위임."""
        self.nack_retry_or_dead(job, reason)

    def depth(self) -> int:
        try:
            return int(self._r.xlen(self._stream))
        except Exception:  # noqa: BLE001
            return 0

    def pending_count(self) -> int:
        try:
            info = self._r.xpending(self._stream, _GROUP)
            # redis-py: {'pending': N, ...} or [N, ...]
            if isinstance(info, dict):
                return int(info.get("pending") or 0)
            if isinstance(info, (list, tuple)) and info:
                return int(info[0])
            return 0
        except Exception:  # noqa: BLE001
            return 0

    def dead_count(self) -> int:
        try:
            return int(self._r.xlen(self._dead))
        except Exception:  # noqa: BLE001
            return 0

    def stats(self) -> dict:
        return {
            "stream": self._stream,
            "depth": self.depth(),
            "pending": self.pending_count(),
            "dead": self.dead_count(),
            "inflight": int(self._r.scard(self._inflight) or 0),
        }
