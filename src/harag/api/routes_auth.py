"""
인증 UX — SSO 로그인/콜백·세션 정보·모의 IdP.

운영: AUTH_OIDC_AUTHORIZE_URL + CLIENT_ID (+ TOKEN_URL)로 실 IdP.
파일럿/테스트: AUTH_OIDC_MOCK=true + AUTH_JWT_SECRET → 모의 로그인으로 JWT 발급.
"""
from __future__ import annotations

import secrets
import time
import urllib.parse
from typing import Any

import httpx
import jwt
from fastapi import APIRouter, Depends, HTTPException, Query, Request, status
from fastapi.responses import HTMLResponse, RedirectResponse

from harag.api.acl_helpers import can_manage_shared, dept_from_auth
from harag.api.auth import require_auth
from harag.api.middleware import current_trace_id
from harag.config.settings import get_settings
from harag.contracts.boundaries import AuthContext
from harag.observability.metrics_export import record_auth_login

router = APIRouter(prefix="/v1/auth", tags=["auth"])

# 짧은 수명의 state (인메모리 — 단일 인스턴스 MVP)
_states: dict[str, float] = {}
_STATE_TTL = 600.0


def _prune_states() -> None:
    now = time.time()
    dead = [k for k, exp in _states.items() if exp < now]
    for k in dead:
        _states.pop(k, None)


def _sso_enabled(s) -> bool:
    if s.auth_oidc_mock and s.auth_jwt_secret:
        return True
    return bool(s.auth_oidc_authorize_url and s.auth_oidc_client_id)


@router.get("/config")
async def auth_config():
    s = get_settings()
    return {
        "demo_allowed": bool(s.auth_allow_demo_owner) and not (
            s.auth_jwt_secret or s.auth_oidc_jwks_url
        ),
        "demo_owner_header": bool(s.auth_allow_demo_owner),
        "sso_enabled": _sso_enabled(s),
        "login_url": "/v1/auth/login" if _sso_enabled(s) else "",
        "jwt_paste_allowed": True,
        "trace_id": current_trace_id(),
    }


@router.get("/me")
async def auth_me(auth: AuthContext = Depends(require_auth)):
    roles = sorted(t.split(":", 1)[1] for t in auth.acl_tags if t.startswith("role:"))
    return {
        "user_id": auth.user_id,
        "department": dept_from_auth(auth),
        "roles": roles,
        "can_share": can_manage_shared(auth),
        "acl_tags": sorted(auth.acl_tags),
    }


@router.get("/login")
async def auth_login(request: Request):
    s = get_settings()
    if not _sso_enabled(s):
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="SSO not configured (AUTH_OIDC_* or AUTH_OIDC_MOCK)",
        )
    _prune_states()
    state = secrets.token_urlsafe(24)
    _states[state] = time.time() + _STATE_TTL

    if s.auth_oidc_mock:
        return RedirectResponse(
            url=f"/v1/auth/mock-login?state={urllib.parse.quote(state)}",
            status_code=302,
        )

    redirect_uri = s.auth_oidc_redirect_uri or str(
        request.url_for("auth_callback"))
    params = {
        "response_type": "code",
        "client_id": s.auth_oidc_client_id,
        "redirect_uri": redirect_uri,
        "scope": s.auth_oidc_scopes or "openid profile",
        "state": state,
    }
    url = s.auth_oidc_authorize_url + (
        "&" if "?" in s.auth_oidc_authorize_url else "?"
    ) + urllib.parse.urlencode(params)
    return RedirectResponse(url=url, status_code=302)


@router.get("/mock-login")
async def auth_mock_login(
    state: str = Query(""),
    sub: str = Query("pilot-admin"),
    dept: str = Query("claims"),
    roles: str = Query("doc_admin"),
):
    """파일럿용 모의 IdP — HS256 JWT 발급 후 프론트로 리다이렉트."""
    s = get_settings()
    if not (s.auth_oidc_mock and s.auth_jwt_secret):
        raise HTTPException(status_code=404, detail="mock SSO disabled")
    _prune_states()
    if not state or state not in _states:
        raise HTTPException(status_code=400, detail="invalid state")
    _states.pop(state, None)

    role_list = [r.strip() for r in roles.split(",") if r.strip()]
    now = int(time.time())
    claims: dict[str, Any] = {
        "sub": sub,
        "dept": dept,
        "roles": role_list,
        "iat": now,
        "exp": now + 8 * 3600,
    }
    if s.auth_jwt_audience:
        claims["aud"] = s.auth_jwt_audience
    if s.auth_jwt_issuer:
        claims["iss"] = s.auth_jwt_issuer
    token = jwt.encode(
        claims, s.auth_jwt_secret,
        algorithm=(s.auth_jwt_algorithms[0] if s.auth_jwt_algorithms else "HS256"),
    )
    record_auth_login()
    # 프론트 루트로 토큰 전달(해시 — 서버 로그에 덜 남음)
    target = s.auth_frontend_redirect or "/"
    frag = urllib.parse.urlencode({"access_token": token})
    sep = "#" if "#" not in target else "&"
    return RedirectResponse(url=f"{target}{sep}{frag}", status_code=302)


@router.get("/callback", name="auth_callback")
async def auth_callback(
    request: Request,
    code: str = Query(""),
    state: str = Query(""),
    error: str = Query(""),
):
    s = get_settings()
    if error:
        raise HTTPException(status_code=400, detail=f"oidc error: {error}")
    _prune_states()
    if not state or state not in _states:
        raise HTTPException(status_code=400, detail="invalid state")
    _states.pop(state, None)
    if not code or not s.auth_oidc_token_url:
        raise HTTPException(status_code=400, detail="missing code or token url")

    redirect_uri = s.auth_oidc_redirect_uri or str(
        request.url_for("auth_callback"))
    data = {
        "grant_type": "authorization_code",
        "code": code,
        "redirect_uri": redirect_uri,
        "client_id": s.auth_oidc_client_id,
    }
    if s.auth_oidc_client_secret:
        data["client_secret"] = s.auth_oidc_client_secret
    try:
        with httpx.Client(timeout=20.0) as client:
            resp = client.post(s.auth_oidc_token_url, data=data)
            resp.raise_for_status()
            body = resp.json()
    except Exception as e:  # noqa: BLE001
        raise HTTPException(
            status_code=502, detail=f"token exchange failed: {e}") from e

    token = body.get("id_token") or body.get("access_token")
    if not token:
        raise HTTPException(status_code=502, detail="no token in response")
    record_auth_login()
    target = s.auth_frontend_redirect or "/"
    frag = urllib.parse.urlencode({"access_token": token})
    html = f"""<!DOCTYPE html><html><head><meta charset="utf-8">
<title>로그인 완료</title></head><body>
<p>로그인 처리 중…</p>
<script>
  location.replace({target!r} + "#" + {frag!r});
</script></body></html>"""
    return HTMLResponse(html)


@router.post("/logout")
async def auth_logout():
    """프론트 세션 클리어용 훅(서버 상태 없음)."""
    return {"ok": True, "trace_id": current_trace_id()}
