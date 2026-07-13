"""
인증·인가 — 게이트웨이의 신뢰 경계(trust boundary).

설계 핵심:
  - 이 계층을 통과한 뒤로 모든 컴포넌트는 "권한은 이미 해결됐다"를 전제한다(보고서 §3, 토폴로지).
  - ACL을 FastAPI 의존성으로 강제 주입 → 보호된 라우트가 AuthContext 없이 동작 불가.
    (B-2에서 AuthContext를 필수 인자로 박은 것과 같은 정신.)
  - 인증 방식(자체 JWT vs 기업 SSO/OIDC)은 미정(코드 설계서 §9) → 인터페이스로 추상화, 주입.

403 vs 404: 권한 없는 리소스는 404로 통일한다. 403("존재하지만 권한 없음")은
존재 여부를 누설하므로 행정 도메인에서 위험.
"""
from __future__ import annotations

from typing import Protocol

from fastapi import Depends, HTTPException, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

from harag.contracts.boundaries import AuthContext


class IdentityProvider(Protocol):
    """인증 백엔드 추상화. 토큰 → 검증된 신원·권한.

    구현 후보(미정, §9): 자체 JWT 검증, 기업 OIDC introspection, SSO.
    어느 쪽이든 이 인터페이스 뒤에 끼운다 → 게이트웨이 코드는 불변.
    """
    def verify(self, token: str) -> AuthContext:
        """유효하면 AuthContext, 아니면 예외를 던진다."""
        ...


class _UnconfiguredIdP:
    """IdP 미설정 시. fail-closed — 인증을 통과시키지 않는다."""
    def verify(self, token: str) -> AuthContext:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="identity provider not configured",
        )


# 앱 시작 시 실제 IdP 주입(main에서 교체). 기본은 fail-closed.
_identity_provider: IdentityProvider = _UnconfiguredIdP()


def set_identity_provider(idp: IdentityProvider) -> None:
    global _identity_provider
    _identity_provider = idp


_bearer = HTTPBearer(auto_error=False)


async def require_auth(
    creds: HTTPAuthorizationCredentials | None = Depends(_bearer),
) -> AuthContext:
    """모든 보호된 라우트의 필수 의존성.

    이 함수가 게이트웨이의 신뢰 경계다. 반환된 AuthContext만이
    아래 컴포넌트(검색·생성)로 흐른다. 토큰 없거나 무효면 여기서 차단.
    """
    if creds is None or not creds.credentials:
        # 인증 정보 없음 — 401(인증 필요)
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="authentication required",
            headers={"WWW-Authenticate": "Bearer"},
        )
    # IdP가 토큰을 검증하고 권한 태그를 채운 AuthContext를 만든다.
    # 검증 실패 시 IdP가 예외(401/503)를 던진다.
    auth = _identity_provider.verify(creds.credentials)

    # fail-closed 재확인: 권한 태그 없는 신원은 통과시키지 않는다.
    # (B-4의 acl_tags fail-closed 원칙을 게이트웨이에서도 강제.)
    if not auth.acl_tags:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="no access scope",
        )
    return auth
