"""
인증·인가 — 게이트웨이의 신뢰 경계(MVP 단순화).

설계 원본은 조직 IdP(JWT/OIDC) 뒤에서 부서·역할 acl_tags를 해결한다. MVP는 "본인이
올린 PDF만 본인이 조회"하는 소유자 격리로 단순화한다:

  - X-Owner-Id 헤더(프론트가 브라우저별 UUID 발급) 또는 Bearer 토큰을 소유자 식별자로.
  - 없으면 익명 공유 소유자("anonymous")로 폴백(로컬 데모/curl 편의).
  - AuthContext.acl_tags = {f"owner:{owner}"} → 검색 pre-filter가 자연히 소유자 격리.

운영 모드: AUTH_JWT_SECRET가 설정되면 Bearer JWT 검증을 강제한다(fail-closed).
  - 헤더 기반 소유자 식별(X-Owner-Id)은 위조 가능하므로 완전히 비활성화된다.
  - 검증된 sub를 소유자로, IdP 클레임(부서·역할)을 acl_tags에 병합한다.
"""
from __future__ import annotations

import re
from functools import lru_cache

from fastapi import Depends, HTTPException, Request, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

from harag.api.auth_jwt import AuthError, JwtIdentityProvider
from harag.config.settings import get_settings
from harag.contracts.boundaries import AuthContext

_bearer = HTTPBearer(auto_error=False)
_SAFE = re.compile(r"[^0-9A-Za-z_.:-]")


def _owner_tag(owner: str) -> str:
    return f"owner:{owner}"


@lru_cache(maxsize=1)
def _jwt_provider() -> JwtIdentityProvider | None:
    s = get_settings()
    if not s.auth_jwt_secret:
        return None
    return JwtIdentityProvider(
        secret=s.auth_jwt_secret,
        algorithms=list(s.auth_jwt_algorithms),
        audience=s.auth_jwt_audience or None,
        issuer=s.auth_jwt_issuer or None,
    )


def _auth_from_jwt(provider: JwtIdentityProvider,
                   creds: HTTPAuthorizationCredentials | None) -> AuthContext:
    """JWT 모드 — 토큰 없음/무효는 401(fail-closed). 헤더 소유자는 무시."""
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
    # 본인 문서(owner 태그) + IdP 클레임(부서/역할) 태그를 함께 부여
    return AuthContext(
        user_id=ctx.user_id,
        acl_tags=frozenset(ctx.acl_tags) | {_owner_tag(ctx.user_id)},
    )


async def require_auth(
    request: Request,
    creds: HTTPAuthorizationCredentials | None = Depends(_bearer),
) -> AuthContext:
    """모든 보호 라우트의 필수 의존성. 소유자 식별자를 AuthContext로 변환."""
    provider = _jwt_provider()
    if provider is not None:
        return _auth_from_jwt(provider, creds)

    owner = request.headers.get("X-Owner-Id")
    if not owner and creds and creds.credentials:
        owner = creds.credentials
    owner = (owner or "anonymous").strip()
    owner = _SAFE.sub("", owner)[:64] or "anonymous"
    return AuthContext(user_id=owner, acl_tags=frozenset({_owner_tag(owner)}))
