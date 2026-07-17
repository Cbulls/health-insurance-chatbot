"""
OIDC JWT 검증 — JWKS 공개키로 RS256 등 검증.

AUTH_OIDC_JWKS_URL 설정 시 JwtIdentityProvider 대신 사용.
클레임 매핑은 auth_jwt._default_claim_mapper와 동일(departments/roles).
"""
from __future__ import annotations

import logging
import time
from typing import Callable

import jwt
from jwt import PyJWKClient

from harag.api.auth_jwt import AuthError, _default_claim_mapper
from harag.contracts.boundaries import AuthContext

logger = logging.getLogger("harag.auth")


class OidcJwtIdentityProvider:
    def __init__(
        self,
        jwks_url: str,
        algorithms: list[str] | None = None,
        audience: str | None = None,
        issuer: str | None = None,
        claim_mapper: Callable[[dict], tuple[str, frozenset[str]]] | None = None,
        jwks_cache_ttl: int = 3600,
    ):
        self._jwks = PyJWKClient(jwks_url, cache_jwk_set=True,
                                 lifespan=jwks_cache_ttl)
        self._algorithms = algorithms or ["RS256"]
        self._audience = audience
        self._issuer = issuer
        self._mapper = claim_mapper or _default_claim_mapper
        self._last_refresh = 0.0

    def verify(self, token: str) -> AuthContext:
        try:
            signing_key = self._jwks.get_signing_key_from_jwt(token)
            kwargs: dict = {
                "algorithms": self._algorithms,
                "key": signing_key.key,
            }
            if self._audience:
                kwargs["audience"] = self._audience
            if self._issuer:
                kwargs["issuer"] = self._issuer
            claims = jwt.decode(token, **kwargs)
        except jwt.ExpiredSignatureError as e:
            raise AuthError("token expired") from e
        except Exception as e:  # noqa: BLE001
            logger.info("OIDC verify failed: %s", type(e).__name__)
            raise AuthError("invalid token") from e

        user_id, acl_tags = self._mapper(claims)
        # groups 클레임도 dept로 흡수
        groups = claims.get("groups") or []
        if groups:
            extra = set(acl_tags)
            for g in groups:
                extra.add(f"dept:{g}")
            acl_tags = frozenset(extra)
        return AuthContext(user_id=user_id, acl_tags=acl_tags)
