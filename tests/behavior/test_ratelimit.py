"""
레이트리밋 TDD — owner별 토큰버킷.

LLM 호출은 건당 비용이 드는 외부 자원. 버스트는 상한까지 허용하되
초과분은 거부하고, 시간이 지나면 분당 속도로 회복돼야 한다.
"""
from __future__ import annotations

import harag.api.ratelimit as rl
from harag.api.ratelimit import TokenBucketLimiter


def test_RL01_burst_up_to_capacity_then_denied():
    """상한(분당 N)까지 버스트 허용, 초과분은 거부."""
    lim = TokenBucketLimiter(per_minute=3)
    assert [lim.allow("u1") for _ in range(3)] == [True, True, True]
    assert lim.allow("u1") is False


def test_RL02_keys_are_isolated():
    """한 owner의 폭주가 다른 owner를 막지 않는다."""
    lim = TokenBucketLimiter(per_minute=1)
    assert lim.allow("u1") is True
    assert lim.allow("u1") is False
    assert lim.allow("u2") is True


def test_RL03_refills_over_time(monkeypatch):
    """소진 후 시간이 지나면 분당 속도로 회복된다."""
    now = [1000.0]
    monkeypatch.setattr(rl.time, "monotonic", lambda: now[0])
    lim = TokenBucketLimiter(per_minute=60)  # 초당 1개 리필
    for _ in range(60):
        assert lim.allow("u1") is True
    assert lim.allow("u1") is False
    now[0] += 2.0  # 2초 경과 → 토큰 2개 회복
    assert lim.allow("u1") is True
    assert lim.allow("u1") is True
    assert lim.allow("u1") is False


def test_RL04_bucket_count_bounded_lru():
    """버킷 키가 무한히 쌓여 메모리가 새지 않는다(LRU 상한)."""
    lim = TokenBucketLimiter(per_minute=1, max_buckets=10)
    for i in range(100):
        lim.allow(f"user-{i}")
    assert len(lim._buckets) <= 10
