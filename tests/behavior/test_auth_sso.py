"""A3 — SSO config·모의 로그인·/me."""
from __future__ import annotations

import jwt
from fastapi import FastAPI
from fastapi.testclient import TestClient
from urllib.parse import parse_qs


def _auth_app(monkeypatch, **env):
    for k, v in env.items():
        if v is None:
            monkeypatch.delenv(k, raising=False)
        else:
            monkeypatch.setenv(k, str(v))
    from harag.config.settings import get_settings
    from harag.api.auth import clear_auth_cache
    get_settings.cache_clear()
    clear_auth_cache()
    from harag.api import routes_auth
    app = FastAPI()
    app.include_router(routes_auth.router)
    return TestClient(app)


def test_A3_auth_config_sso_flag(monkeypatch):
    c = _auth_app(
        monkeypatch,
        AUTH_JWT_SECRET="test-secret-key-32bytes-minimum!!",
        AUTH_OIDC_MOCK="true",
    )
    r = c.get("/v1/auth/config")
    assert r.status_code == 200
    assert r.json()["sso_enabled"] is True
    assert r.json()["login_url"] == "/v1/auth/login"


def test_A3_mock_login_issues_jwt(monkeypatch):
    secret = "test-secret-key-32bytes-minimum!!"
    c = _auth_app(
        monkeypatch,
        AUTH_JWT_SECRET=secret,
        AUTH_OIDC_MOCK="true",
        AUTH_ALLOW_DEMO_OWNER="false",
    )
    r = c.get("/v1/auth/login", follow_redirects=False)
    assert r.status_code == 302
    loc = r.headers["location"]
    assert "/v1/auth/mock-login" in loc
    r2 = c.get(loc, follow_redirects=False)
    assert r2.status_code == 302
    frag = r2.headers["location"].split("#", 1)[-1]
    token = parse_qs(frag)["access_token"][0]
    claims = jwt.decode(token, secret, algorithms=["HS256"])
    assert claims["sub"] == "pilot-admin"
    assert claims["dept"] == "claims"

    # /me는 require_auth — 동일 앱에 auth 의존성 쓰려면 JWT provider 활성
    me = c.get("/v1/auth/me", headers={"Authorization": f"Bearer {token}"})
    assert me.status_code == 200
    body = me.json()
    assert body["user_id"] == "pilot-admin"
    assert body["can_share"] is True
    assert body["department"] == "claims"


def test_A3_sso_disabled_without_config(monkeypatch):
    c = _auth_app(
        monkeypatch,
        AUTH_JWT_SECRET=None,
        AUTH_OIDC_MOCK="false",
        AUTH_OIDC_AUTHORIZE_URL=None,
        AUTH_OIDC_CLIENT_ID=None,
    )
    r = c.get("/v1/auth/config")
    assert r.json()["sso_enabled"] is False
    r2 = c.get("/v1/auth/login")
    assert r2.status_code == 503
