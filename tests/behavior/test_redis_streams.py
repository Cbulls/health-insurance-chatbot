"""Redis Streams 인제스트·멱등·레이트/대화 단위 테스트(FakeRedis)."""
from __future__ import annotations

import json
import time
import uuid

import pytest

from harag.storage.redis_ingest_queue import RedisIngestQueue, _ENQUEUE_LUA
from harag.storage.redis_stores import RedisTokenBucketLimiter, RedisConversationStore
from harag.storage.redis_cache import DocStatusCache


class FakeRedis:
    """Streams/SET/Hash/List/String + EVAL(enqueue·token-bucket) 최소 구현."""

    def __init__(self):
        self.sets: dict[str, set] = {}
        self.streams: dict[str, list] = {}  # name -> [(id, fields)]
        self.hashes: dict[str, dict] = {}
        self.lists: dict[str, list] = {}
        self.strings: dict[str, str] = {}
        self.groups: dict[str, set] = {}  # stream -> group names
        self._seq = 0
        self._pending: dict[str, list] = {}  # stream -> [(id, fields, idle_ms)]

    def ping(self):
        return True

    def sadd(self, key, *members):
        self.sets.setdefault(key, set()).update(members)
        return len(members)

    def srem(self, key, *members):
        s = self.sets.get(key, set())
        n = 0
        for m in members:
            if m in s:
                s.discard(m)
                n += 1
        return n

    def sismember(self, key, member):
        return 1 if member in self.sets.get(key, set()) else 0

    def scard(self, key):
        return len(self.sets.get(key, set()))

    def xgroup_create(self, name, groupname, id="0", mkstream=False):
        if mkstream:
            self.streams.setdefault(name, [])
        groups = self.groups.setdefault(name, set())
        if groupname in groups:
            raise Exception("BUSYGROUP Consumer Group name already exists")
        groups.add(groupname)

    def xadd(self, name, fields, id="*", maxlen=None, approximate=False):
        self._seq += 1
        mid = f"{int(time.time() * 1000)}-{self._seq}"
        entry = (mid, dict(fields))
        stream = self.streams.setdefault(name, [])
        stream.append(entry)
        if maxlen and len(stream) > maxlen:
            del stream[: len(stream) - maxlen]
        return mid

    def xlen(self, name):
        return len(self.streams.get(name, []))

    def xreadgroup(self, groupname, consumername, streams, count=1, block=0):
        name = next(iter(streams))
        stream = self.streams.get(name, [])
        if not stream:
            return []
        # 새 메시지: 스트림에서 꺼내 PEL로
        out = []
        for _ in range(count):
            if not stream:
                break
            mid, fields = stream.pop(0)
            self._pending.setdefault(name, []).append((mid, fields, 0))
            out.append((mid, fields))
        return [(name, out)] if out else []

    def xack(self, name, groupname, *ids):
        pel = self._pending.get(name, [])
        keep = [p for p in pel if p[0] not in ids]
        n = len(pel) - len(keep)
        self._pending[name] = keep
        return n

    def xautoclaim(self, name, groupname, consumername, min_idle_time,
                   start_id="0-0", count=1):
        pel = self._pending.get(name, [])
        claimed = []
        rest = []
        for mid, fields, idle in pel:
            if idle >= min_idle_time and len(claimed) < count:
                claimed.append((mid, fields))
            else:
                rest.append((mid, fields, idle))
        self._pending[name] = rest + [
            (m, f, 0) for m, f in claimed]
        return ["0-0", claimed, []]

    def xpending(self, name, groupname):
        return {"pending": len(self._pending.get(name, []))}

    def age_pending(self, name, idle_ms: int):
        """테스트용: PEL idle 시간을 올린다."""
        self._pending[name] = [
            (m, f, idle_ms) for m, f, _ in self._pending.get(name, [])]

    def eval(self, script, numkeys, *keys_and_args):
        keys = list(keys_and_args[:numkeys])
        argv = list(keys_and_args[numkeys:])
        if "SISMEMBER" in script and "XADD" in script:
            # enqueue lua
            inflight, stream = keys[0], keys[1]
            doc_id, maxlen = argv[0], int(argv[1])
            if self.sismember(inflight, doc_id):
                return 0
            self.sadd(inflight, doc_id)
            fields = {}
            pairs = argv[2:]
            for i in range(0, len(pairs), 2):
                fields[pairs[i]] = pairs[i + 1]
            self.xadd(stream, fields, maxlen=maxlen, approximate=True)
            return 1
        if "HMGET" in script and "tokens" in script:
            # token bucket lua
            rk = keys[0]
            capacity = float(argv[0])
            rate = float(argv[1])
            now = float(argv[2])
            ttl = int(argv[3])
            data = self.hashes.get(rk, {})
            tokens = float(data["tokens"]) if "tokens" in data else capacity
            last = float(data["last"]) if "last" in data else now
            tokens = min(capacity, tokens + (now - last) * rate)
            allowed = 0
            if tokens >= 1.0:
                tokens -= 1.0
                allowed = 1
            self.hashes[rk] = {"tokens": str(tokens), "last": str(now)}
            return allowed
        raise NotImplementedError(script[:80])

    def hset(self, name, mapping=None, **kwargs):
        h = self.hashes.setdefault(name, {})
        if mapping:
            h.update({str(k): str(v) for k, v in mapping.items()})
        return 1

    def hgetall(self, name):
        return dict(self.hashes.get(name, {}))

    def expire(self, name, ttl):
        return 1

    def pipeline(self, transaction=True):
        return FakePipeline(self)

    def rpush(self, key, *vals):
        self.lists.setdefault(key, []).extend(vals)
        return len(self.lists[key])

    def ltrim(self, key, start, end):
        lst = self.lists.get(key, [])
        n = len(lst)
        if end < 0:
            end = n + end
        if start < 0:
            start = n + start
        self.lists[key] = lst[start: end + 1]
        return True

    def lrange(self, key, start, end):
        lst = self.lists.get(key, [])
        if end == -1:
            end = len(lst) - 1
        return lst[start: end + 1]

    def get(self, key):
        return self.strings.get(key)

    def setex(self, key, ttl, value):
        self.strings[key] = value
        return True

    def delete(self, *keys):
        n = 0
        for k in keys:
            if k in self.strings:
                del self.strings[k]
                n += 1
            if k in self.sets:
                del self.sets[k]
                n += 1
        return n


class FakePipeline:
    def __init__(self, r: FakeRedis):
        self._r = r
        self._ops = []

    def rpush(self, *a):
        self._ops.append(("rpush", a))
        return self

    def ltrim(self, *a):
        self._ops.append(("ltrim", a))
        return self

    def expire(self, *a):
        self._ops.append(("expire", a))
        return self

    def hgetall(self, *a):
        self._ops.append(("hgetall", a))
        return self

    def execute(self):
        out = []
        for name, args in self._ops:
            out.append(getattr(self._r, name)(*args))
        return out


@pytest.fixture
def prefix():
    return f"t{uuid.uuid4().hex[:8]}:"


def test_enqueue_idempotent_lua(prefix):
    r = FakeRedis()
    q = RedisIngestQueue(r, prefix=prefix, visibility_sec=1, max_attempts=3)
    assert q.enqueue("d1", "/tmp/a.pdf", "a.pdf", "u1") is True
    assert q.enqueue("d1", "/tmp/a.pdf", "a.pdf", "u1") is False
    job = q.dequeue(timeout_sec=1)
    assert job is not None
    assert job.document_id == "d1"
    assert job.attempts == 0
    q.ack_success(job)
    # ack 후 재적재 가능
    assert q.enqueue("d1", "/tmp/a.pdf", "a.pdf", "u1") is True


def test_nack_retries_then_dlq(prefix):
    r = FakeRedis()
    q = RedisIngestQueue(r, prefix=prefix, max_attempts=2)
    assert q.enqueue("d2", "/spool/x.pdf", "x.pdf", "u") is True
    job = q.dequeue(timeout_sec=1)
    assert job is not None
    q.nack_retry_or_dead(job, "boom")
    job2 = q.dequeue(timeout_sec=1)
    assert job2 is not None
    assert job2.attempts == 1
    q.nack_retry_or_dead(job2, "boom2")
    assert q.dead_count() == 1
    assert q.dequeue(timeout_sec=1) is None


def test_reclaim_stale_pending(prefix):
    r = FakeRedis()
    q = RedisIngestQueue(r, prefix=prefix, visibility_sec=30)
    assert q.enqueue("d3", "/spool/y.pdf", "y.pdf", "u") is True
    job = q.dequeue(timeout_sec=1)
    assert job is not None
    # ack 없이 idle ≥ visibility(초)*1000 → reclaim
    r.age_pending(q._stream, idle_ms=30_000)
    job2 = q.dequeue(timeout_sec=1)
    assert job2 is not None
    assert job2.document_id == "d3"
    q.ack_success(job2)


def test_on_failed_clears_inflight_allows_reupload(prefix):
    r = FakeRedis()
    q = RedisIngestQueue(r, prefix=prefix)
    assert q.enqueue("d4", "/spool/z.pdf", "z.pdf", "u") is True
    assert q.enqueue("d4", "/spool/z.pdf", "z.pdf", "u") is False
    q.on_failed("d4")
    assert q.enqueue("d4", "/spool/z2.pdf", "z2.pdf", "u") is True


def test_token_bucket_lua_atomic(prefix):
    r = FakeRedis()
    lim = RedisTokenBucketLimiter(r, per_minute=2, prefix=prefix)
    assert lim.allow("owner") is True
    assert lim.allow("owner") is True
    assert lim.allow("owner") is False


def test_conversation_pipeline_trim(prefix):
    r = FakeRedis()
    store = RedisConversationStore(r, max_turns=2, prefix=prefix)
    store.append("c1", "q1")
    store.append("c1", "q2")
    store.append("c1", "q3")
    assert store.get("c1") == ["q2", "q3"]


def test_doc_cache_keeps_filename_on_empty(prefix):
    r = FakeRedis()
    cache = DocStatusCache(r, ttl_sec=300, prefix=prefix)
    cache.set("d", "o", {
        "document_id": "d", "filename": "keep.pdf",
        "owner": "o", "status": "processing", "n_chunks": 0, "error": None,
    })
    cache.set("d", "o", {
        "document_id": "d", "filename": "",
        "owner": "o", "status": "failed", "n_chunks": 0, "error": "x",
    })
    got = cache.get("d", "o")
    assert got["filename"] == "keep.pdf"
    assert got["status"] == "failed"


def test_enqueue_lua_script_exported():
    assert "XADD" in _ENQUEUE_LUA
    assert "SISMEMBER" in _ENQUEUE_LUA
