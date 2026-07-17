"""
인증·인가 — 게이트웨이의 신뢰 경계.

우선순위:
  1. AUTH_OIDC_JWKS_URL → OIDC JWKS 검증
  2. AUTH_JWT_SECRET → HMAC JWT 검증
  3. AUTH_ALLOW_DEMO_OWNER=true → X-Owner-Id / anonymous (데모)
  4. 그 외 → 401
"""
from __future__ import annotations

import re
from functools import lru_cache

from fastapi import Depends, HTTPException, Request, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

from harag.api.acl_helpers import with_library_access
from harag.api.auth_jwt import AuthError, JwtIdentityProvider
from harag.config.settings import get_settings
from harag.contracts.boundaries import AuthContext

_bearer = HTTPBearer(auto_error=False)
_SAFE = re.compile(r"[^0-9A-Za-z_.:-]")


def _owner_tag(owner: str) -> str:
    return f"owner:{owner}"


@lru_cache(maxsize=1)
def _identity_provider():
    s = get_settings()
    if s.auth_oidc_jwks_url:
        from harag.api.auth_oidc import OidcJwtIdentityProvider
        return OidcJwtIdentityProvider(
            jwks_url=s.auth_oidc_jwks_url,
            algorithms=list(s.auth_jwt_algorithms) or ["RS256"],
            audience=s.auth_jwt_audience or None,
            issuer=s.auth_jwt_issuer or None,
        )
    if s.auth_jwt_secret:
        return JwtIdentityProvider(
            secret=s.auth_jwt_secret,
            algorithms=list(s.auth_jwt_algorithms),
            audience=s.auth_jwt_audience or None,
            issuer=s.auth_jwt_issuer or None,
        )
    return None


def _auth_from_provider(provider, creds: HTTPAuthorizationCredentials | None
                        ) -> AuthContext:
    if creds is None or not creds.credentials:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED,
                            detail="Bearer token required",
                            headers={"WWW-Authenticate": "Bearer"})
    try:
        ctx = provider.verify(creds.credentials)
    except AuthError:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED,
                            detail="invalid or expired token",
                            headers={"WWW-Authenticate": "Bearer"})
    if not ctx.user_id:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED,
                            detail="token missing subject",
                            headers={"WWW-Authenticate": "Bearer"})
    return with_library_access(AuthContext(
        user_id=ctx.user_id,
        acl_tags=frozenset(ctx.acl_tags) | {_owner_tag(ctx.user_id)},
    ))


async def require_auth(
    request: Request,
    creds: HTTPAuthorizationCredentials | None = Depends(_bearer),
) -> AuthContext:
    provider = _identity_provider()
    if provider is not None:
        return _auth_from_provider(provider, creds)

    s = get_settings()
    if not s.auth_allow_demo_owner:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="authentication required (demo owner disabled)",
            headers={"WWW-Authenticate": "Bearer"},
        )

    owner = request.headers.get("X-Owner-Id")
    if not owner and creds and creds.credentials:
        owner = creds.credentials
    owner = (owner or "anonymous").strip()
    owner = _SAFE.sub("", owner)[:64] or "anonymous"
    return with_library_access(AuthContext(
        user_id=owner, acl_tags=frozenset({_owner_tag(owner)})))


def clear_auth_cache() -> None:
    _identity_provider.cache_clear()
