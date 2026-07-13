"""
JWT 인증 백엔드 — IdentityProvider 구현체.

설계 원칙:
  - 서명·만료 검증은 PyJWT(검증된 라이브러리)에 위임. 암호 검증을 직접 짜지 않는다
    (직접 구현은 거의 항상 보안 구멍).
  - 우리 책임은 '검증된 클레임 → AuthContext(부서·권한 → acl_tags)' 매핑.
  - fail-closed: 무효·만료·서명 불일치는 AuthError. 권한 클레임 없으면 빈 acl_tags
    (게이트웨이 require_auth가 403 처리).
  - alg=none 공격 방어: 허용 알고리즘을 명시(algorithms 화이트리스트).

조직 IdP 방식(자체 JWT vs OIDC introspection)은 미정(§9).
  - 자체 JWT: 이 클래스로 충분.
  - OIDC: verify를 introspection 호출로 바꾼 다른 IdentityProvider 구현으로 교체.
  claim_mapper로 조직별 클레임 구조에 맞춤.
"""
from __future__ import annotations

from typing import Callable

import jwt

from harag.contracts.boundaries import AuthContext


class AuthError(Exception):
    """토큰 검증 실패. 게이트웨이가 401로 변환."""


def _default_claim_mapper(claims: dict) -> tuple[str, frozenset[str]]:
    """기본 매핑: departments → dept:*, roles → role:*.
    조직 클레임 구조가 다르면 claim_mapper로 교체."""
    tags = set()
    for dept in claims.get("departments", []):
        tags.add(f"dept:{dept}")
    for role in claims.get("roles", []):
        tags.add(f"role:{role}")
    user_id = claims.get("sub", "")
    return user_id, frozenset(tags)


class JwtIdentityProvider:
    """JWT 검증 + 권한 매핑. set_identity_provider로 게이트웨이에 주입."""

    def __init__(self, secret: str, algorithms: list[str],
                 claim_mapper: Callable[[dict], tuple[str, frozenset[str]]] | None = None,
                 audience: str | None = None, issuer: str | None = None):
        self._secret = secret
        self._algorithms = algorithms        # 화이트리스트 — alg=none 공격 방어
        self._mapper = claim_mapper or _default_claim_mapper
        self._audience = audience
        self._issuer = issuer

    def verify(self, token: str) -> AuthContext:
        try:
            options = {}
            kwargs = {"algorithms": self._algorithms}
            if self._audience:
                kwargs["audience"] = self._audience
            if self._issuer:
                kwargs["issuer"] = self._issuer
            # PyJWT가 서명·만료(exp)·aud·iss를 모두 검증. alg는 화이트리스트로 강제.
            claims = jwt.decode(token, self._secret, **kwargs)
        except jwt.ExpiredSignatureError as e:
            raise AuthError("token expired") from e
        except jwt.InvalidTokenError as e:
            # 서명 불일치·깨진 토큰·alg=none 등 모두 여기로
            raise AuthError("invalid token") from e

        user_id, acl_tags = self._mapper(claims)
        return AuthContext(user_id=user_id, acl_tags=acl_tags)
