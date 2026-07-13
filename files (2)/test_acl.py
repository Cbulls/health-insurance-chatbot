"""B-4 ACL 일관성 테스트 — 누수 A/B 차단 증명, 누수 C는 경계 밖임을 문서화."""
from component_contracts import AuthContext, ContractViolation
from acl_consistency import (
    AclFilterSpec, IndexQuery, verify_filter_consistency, assert_fresh_auth,
)

PASS, FAIL = [], []
def expect_ok(n, fn):
    try: fn(); PASS.append(n)
    except Exception as e: FAIL.append(f"{n} — raised {type(e).__name__}: {e}")
def expect_reject(n, fn):
    try: fn(); FAIL.append(f"{n} — should REJECT but accepted")
    except (ContractViolation, ValueError): PASS.append(n)

fin = AuthContext(user_id="u1", acl_tags=frozenset({"dept:fin"}))
hr  = AuthContext(user_id="u2", acl_tags=frozenset({"dept:hr"}))


# ── 단일 명세가 양쪽에 같은 의미를 전달 ──
def spec_single_source():
    spec = AclFilterSpec.from_auth(fin)
    d, s = spec.to_dense_filter(), spec.to_sparse_filter()
    # 형식은 달라도(벡터DB vs BM25) 같은 태그 집합을 담아야 함
    assert sorted(d["acl_tags"]["$overlap"]) == sorted(s["terms"]["acl_tags"])
expect_ok("B4/single spec -> consistent tags in both filter dialects", spec_single_source)


# ── 누수 B: auth 없이는 쿼리 생성 불가(타입 레벨) ──
def query_requires_auth():
    q = IndexQuery.build("출장비 한도", k=10, auth=fin)
    assert q.acl_filter.required_any_tags == frozenset({"dept:fin"})
expect_ok("B4/query carries pre-filter from auth", query_requires_auth)


# ── 누수 A: 두 인덱스가 같은 spec이면 통과, 다르면 거부 ──
def consistency_ok():
    q = IndexQuery.build("q", 10, fin)
    verify_filter_consistency(q, q)  # 동일 spec
expect_ok("B4/consistent filters across indices pass", consistency_ok)

def consistency_drift():
    # 누수 A 재현: dense엔 fin 필터, sparse엔 hr 필터(드리프트)
    dq = IndexQuery.build("q", 10, fin)
    sq = IndexQuery.build("q", 10, hr)
    verify_filter_consistency(dq, sq)
expect_reject("B4/누수A filter drift between dense/sparse rejected", consistency_drift)


# ── fail-closed: 권한 태그 없는 사용자는 빈 필터(아무것도 못 봄) ──
def empty_auth_fails_closed():
    noauth = AuthContext(user_id="x", acl_tags=frozenset())
    spec = AclFilterSpec.from_auth(noauth)
    assert spec.required_any_tags == frozenset()  # 빈 집합 -> overlap 매칭 0건
expect_ok("B4/no-tag user gets empty filter (fail-closed)", empty_auth_fails_closed)


# ── 멀티턴: 권한 변경 시 캐시 사용 금지 ──
def turn_auth_changed():
    # 1턴 fin이었다가 2턴 권한 회수됨 -> 캐시(fin) 쓰면 위반
    assert_fresh_auth(turn_auth=AuthContext("u1", frozenset()), cached_auth=fin)
expect_reject("B4/stale cross-turn auth rejected", turn_auth_changed)

def turn_auth_same():
    assert_fresh_auth(turn_auth=fin, cached_auth=fin)  # 동일하면 정상
expect_ok("B4/same auth across turns ok", turn_auth_same)


print(f"\n{'='*60}")
print(f"PASS: {len(PASS)} / {len(PASS)+len(FAIL)}")
for f in FAIL: print("  X", f)
if not FAIL: print("  OK 누수 A/B 차단 / 멀티턴 stale 차단 / fail-closed")
print('='*60)
print("\n[경계 밖] 누수 C(추론 누수): 권한 내 청크 조합으로 권한 외 사실 추론.")
print("  청크 단위 ACL로 원천 불가 — 코드로 막는 척하지 않음. 제품 정책 영역.")
