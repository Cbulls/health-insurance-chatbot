"""
API 게이트웨이 신뢰 경계 테스트.
인증·인가가 약화되면(예: 보호 라우트에서 require_auth 제거) 여기서 잡힌다.
"""
import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient

from harag.api.main import create_app
from harag.api.auth import set_identity_provider
from harag.contracts.boundaries import AuthContext

PASS, FAIL = [], []
def ok(n, cond): (PASS if cond else FAIL).append(n)


def _client():
    return TestClient(create_app())


# 1) 인증 없이 보호 라우트 → 401
c = _client()
ok("no auth -> 401", c.post("/v1/query", json={"query": "x"}).status_code == 401)

# 2) 잘못된 토큰 → 401
class BadIdP:
    def verify(self, token):
        raise HTTPException(status_code=401, detail="bad")
set_identity_provider(BadIdP())
ok("bad token -> 401",
   c.post("/v1/query", json={"query": "x"}, headers={"Authorization": "Bearer bad"}).status_code == 401)

# 3) 권한 태그 없는 신원 → 403 (fail-closed)
class NoScopeIdP:
    def verify(self, token):
        return AuthContext(user_id="u", acl_tags=frozenset())
set_identity_provider(NoScopeIdP())
ok("no scope -> 403 (fail-closed)",
   c.post("/v1/query", json={"query": "x"}, headers={"Authorization": "Bearer ok"}).status_code == 403)

# 4) 헬스체크는 인증 불필요
ok("health no-auth -> 200", c.get("/health").status_code == 200)

# 5) 모든 응답에 trace_id 헤더
r = c.get("/health")
ok("trace header present", "X-Trace-Id" in r.headers)

# 6) 잘못된 요청 스키마 → 422 (Pydantic 검증)
class OkIdP:
    def verify(self, token):
        return AuthContext(user_id="u", acl_tags=frozenset({"dept:finance"}))
set_identity_provider(OkIdP())
ok("empty query -> 422",
   c.post("/v1/query", json={"query": ""}, headers={"Authorization": "Bearer ok"}).status_code == 422)


print(f"\n{'='*56}")
print(f"PASS: {len(PASS)} / {len(PASS)+len(FAIL)}")
for n in PASS: print("  OK", n)
for n in FAIL: print("  X ", n)
print('='*56)
