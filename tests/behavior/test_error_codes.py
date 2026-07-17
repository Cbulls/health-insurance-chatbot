"""A1 — HTTP 429 code 분리 (rate_limit vs budget_exhausted)."""
from __future__ import annotations

from harag.api.error_codes import (
    CODE_BUDGET_EXHAUSTED, CODE_RATE_LIMIT,
    budget_exhausted_error, rate_limit_error,
)
from harag.api.daily_budget import (
    check_budget, record_question, reset_for_tests,
)
from harag.api.ratelimit import enforce_rate_limit, TokenBucketLimiter
import harag.api.ratelimit as rl


def test_A1_rate_limit_error_has_code():
    exc = rate_limit_error(retry_after=30)
    assert exc.status_code == 429
    assert exc.detail["code"] == CODE_RATE_LIMIT
    assert "잦" in exc.detail["message"]
    assert exc.headers["Retry-After"] == "30"


def test_A1_budget_error_has_distinct_message():
    exc = budget_exhausted_error(kind="questions")
    assert exc.detail["code"] == CODE_BUDGET_EXHAUSTED
    assert "질문 한도" in exc.detail["message"]
    assert "잦" not in exc.detail["message"]
    tok = budget_exhausted_error(kind="tokens")
    assert "토큰" in tok.detail["message"]


def test_A1_check_budget_raises_structured(monkeypatch):
    reset_for_tests()
    monkeypatch.setenv("DAILY_QUESTION_BUDGET", "1")
    from harag.config.settings import get_settings
    get_settings.cache_clear()
    record_question("budget-user")
    import pytest
    from fastapi import HTTPException
    with pytest.raises(HTTPException) as ei:
        check_budget("budget-user")
    assert ei.value.detail["code"] == "budget_exhausted"
    get_settings.cache_clear()
    monkeypatch.delenv("DAILY_QUESTION_BUDGET", raising=False)
    get_settings.cache_clear()


def test_A1_enforce_rate_limit_code(monkeypatch):
    """의존성 내부 limiter를 강제 소진해 code=rate_limit 확인."""
    import asyncio
    from harag.contracts.boundaries import AuthContext

    lim = TokenBucketLimiter(per_minute=1)
    assert lim.allow("rl-user") is True
    monkeypatch.setattr(rl, "_redis_limiter", None)
    monkeypatch.setattr(rl, "_get_limiter", lambda _qpm: lim)
    monkeypatch.setattr(
        "harag.api.ratelimit.get_settings",
        lambda: type("S", (), {"rate_limit_qpm": 1})(),
    )

    auth = AuthContext(user_id="rl-user", acl_tags=frozenset({"owner:rl-user"}))

    import pytest
    from fastapi import HTTPException

    with pytest.raises(HTTPException) as ei:
        asyncio.run(enforce_rate_limit(auth=auth))
    assert ei.value.detail["code"] == CODE_RATE_LIMIT
