"""
인증·인가 — 게이트웨이의 신뢰 경계(MVP 단순화).

설계 원본은 조직 IdP(JWT/OIDC) 뒤에서 부서·역할 acl_tags를 해결한다. MVP는 "본인이
올린 PDF만 본인이 조회"하는 소유자 격리로 단순화한다:

  - X-Owner-Id 헤더(프론트가 브라우저별 UUID 발급) 또는 Bearer 토큰을 소유자 식별자로.
  - 없으면 익명 공유 소유자("anonymous")로 폴백(로컬 데모/curl 편의).
  - AuthContext.acl_tags = {f"owner:{owner}"} → 검색 pre-filter가 자연히 소유자 격리.

Phase 2: 여기에 JwtIdentityProvider(조직 IdP)를 끼워 부서/역할 ACL로 확장한다.
"""
from __future__ import annotations

import re

from fastapi import Depends, Request
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

from harag.contracts.boundaries import AuthContext

_bearer = HTTPBearer(auto_error=False)
_SAFE = re.compile(r"[^0-9A-Za-z_.:-]")


def _owner_tag(owner: str) -> str:
    return f"owner:{owner}"


async def require_auth(
    request: Request,
    creds: HTTPAuthorizationCredentials | None = Depends(_bearer),
) -> AuthContext:
    """모든 보호 라우트의 필수 의존성. 소유자 식별자를 AuthContext로 변환."""
    owner = request.headers.get("X-Owner-Id")
    if not owner and creds and creds.credentials:
        owner = creds.credentials
    owner = (owner or "anonymous").strip()
    owner = _SAFE.sub("", owner)[:64] or "anonymous"
    return AuthContext(user_id=owner, acl_tags=frozenset({_owner_tag(owner)}))
