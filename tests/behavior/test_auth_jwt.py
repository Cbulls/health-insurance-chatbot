"""
JWT IdP TDD — 토큰 검증 + 권한 매핑(인증 백엔드).

설계: 서명·만료 검증은 PyJWT(검증된 라이브러리)에 위임. 직접 암호 검증 안 함.
우리 책임은 '검증된 클레임 → AuthContext(부서·권한 → acl_tags)' 매핑.

fail-closed 일관성: 무효 토큰·만료·권한 클레임 없음 → 거부(예외).
조직 IdP 방식(자체 JWT vs OIDC)은 미정 → 검증 콜백 주입으로 교체 가능.
"""
import time
import jwt
import pytest

from harag.api.auth_jwt import JwtIdentityProvider, AuthError
from harag.contracts.boundaries import AuthContext


SECRET = "test-secret-key"


def _token(claims, secret=SECRET, exp_offset=3600, alg="HS256"):
    payload = {"exp": int(time.time()) + exp_offset, **claims}
    return jwt.encode(payload, secret, algorithm=alg)


def _idp():
    return JwtIdentityProvider(secret=SECRET, algorithms=["HS256"])


def test_IDP01_valid_token_returns_authcontext():
    """유효 토큰 → AuthContext(user_id + acl_tags)."""
    token = _token({"sub": "user123", "departments": ["finance"], "roles": ["viewer"]})
    auth = _idp().verify(token)
    assert isinstance(auth, AuthContext)
    assert auth.user_id == "user123"
    assert "dept:finance" in auth.acl_tags
    assert "role:viewer" in auth.acl_tags


def test_IDP02_expired_token_rejected():
    """만료 토큰 거부."""
    token = _token({"sub": "u", "departments": ["finance"]}, exp_offset=-10)
    with pytest.raises(AuthError):
        _idp().verify(token)


def test_IDP03_bad_signature_rejected():
    """잘못된 서명(다른 키) 거부."""
    token = _token({"sub": "u", "departments": ["finance"]}, secret="wrong-secret")
    with pytest.raises(AuthError):
        _idp().verify(token)


def test_IDP04_malformed_token_rejected():
    """깨진 토큰 거부(크래시 아니라 AuthError)."""
    with pytest.raises(AuthError):
        _idp().verify("not.a.jwt")


def test_IDP05_no_permission_claims_fail_closed():
    """권한 클레임 없는 토큰 → 빈 acl_tags(게이트웨이가 fail-closed로 차단)."""
    token = _token({"sub": "u"})  # departments·roles 없음
    auth = _idp().verify(token)
    # acl_tags가 비어 있어야(게이트웨이 require_auth가 403 처리)
    assert not auth.acl_tags


def test_IDP06_none_algorithm_attack_rejected():
    """alg=none 공격 거부(서명 우회 시도)."""
    # alg=none 토큰 수동 생성 시도
    import base64, json
    header = base64.urlsafe_b64encode(json.dumps({"alg": "none", "typ": "JWT"}).encode()).rstrip(b"=")
    payload = base64.urlsafe_b64encode(json.dumps({"sub": "attacker", "departments": ["admin"]}).encode()).rstrip(b"=")
    forged = f"{header.decode()}.{payload.decode()}."
    with pytest.raises(AuthError):
        _idp().verify(forged)


def test_IDP07_custom_claim_mapping():
    """클레임→태그 매핑을 조직에 맞게 커스터마이즈 가능."""
    def mapper(claims):
        tags = set()
        for org in claims.get("orgs", []):
            tags.add(f"org:{org}")
        return claims.get("uid", ""), frozenset(tags)
    idp = JwtIdentityProvider(secret=SECRET, algorithms=["HS256"], claim_mapper=mapper)
    token = _token({"uid": "u9", "orgs": ["seoul-gov"]})
    auth = idp.verify(token)
    assert auth.user_id == "u9"
    assert "org:seoul-gov" in auth.acl_tags


# NOTE: IDP08(게이트웨이 통합)은 이 MVP에서 제외한다. 런타임 인증은 X-Owner-Id
# 소유자 격리([api/auth.py](src/harag/api/auth.py))를 쓰고, JwtIdentityProvider의
# 게이트웨이 배선(set_identity_provider)은 Phase 2다. IDP01~07이 JWT 검증 자체를 커버.


if __name__ == "__main__":
    import sys
    sys.exit(pytest.main([__file__, "-v"]))
