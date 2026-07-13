"""
B-4 ACL 일관성 계약.

세 누수에 대한 입장:
  누수 A (두 인덱스 필터 불일치):
    - 1차 방어: 단일 필터 명세(AclFilterSpec)를 양쪽 인덱스에 같은 객체로 주입.
                필터를 두 번 작성하지 않는다 → 드리프트 불가.
    - 갈래2(분리 엔진)일 때를 위해, 두 엔진에 같은 spec이 갔는지 런타임 검증.
  누수 B (필터 시점):
    - ACL은 pre-filter(검색 시점)가 1차선. post-filter는 defense-in-depth 2차선.
    - 인덱스 쿼리는 'auth 없이는 만들 수 없게' 강제.
  누수 C (추론 누수):
    - 청크 단위 ACL로 원천 불가. 코드 계약 밖 = 제품 정책 영역. 여기서 막는 척하지 않는다.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Protocol, Any

from component_contracts import AuthContext, ContractViolation


# ── 단일 필터 명세(single source of truth) ──────────────
@dataclass(frozen=True)
class AclFilterSpec:
    """ACL 필터의 유일한 표현. dense·sparse 양쪽이 '이 같은 객체'를 받는다.
    필터 로직을 인덱스별로 복붙하지 않기 위한 핵심 장치."""
    required_any_tags: frozenset[str]   # 청크 acl_tags와 교집합이 있어야 통과

    @staticmethod
    def from_auth(auth: AuthContext) -> "AclFilterSpec":
        if not auth.acl_tags:
            # fail-closed: 권한 태그 없는 사용자는 빈 필터(아무것도 매칭 안 됨)
            return AclFilterSpec(required_any_tags=frozenset())
        return AclFilterSpec(required_any_tags=auth.acl_tags)

    def to_dense_filter(self) -> dict[str, Any]:
        """벡터DB 메타데이터 필터 절(pre-filter)."""
        return {"acl_tags": {"$overlap": sorted(self.required_any_tags)}}

    def to_sparse_filter(self) -> dict[str, Any]:
        """BM25 엔진 필터 절. 형식은 다르지만 의미는 동일해야 함."""
        return {"terms": {"acl_tags": sorted(self.required_any_tags)}}


# ── 인덱스 쿼리: auth 없이는 생성 불가(누수 B 차단) ────────
@dataclass(frozen=True)
class IndexQuery:
    query_text: str
    k: int
    acl_filter: AclFilterSpec   # 기본값 없음 — 필터 없는 쿼리는 만들 수 없다

    @staticmethod
    def build(query_text: str, k: int, auth: AuthContext) -> "IndexQuery":
        return IndexQuery(query_text=query_text, k=k,
                          acl_filter=AclFilterSpec.from_auth(auth))


# ── 갈래2(분리 엔진) 대비: 두 인덱스 일관성 런타임 검증 ────
def verify_filter_consistency(dense_query: IndexQuery, sparse_query: IndexQuery) -> None:
    """dense·sparse 두 쿼리가 동일 ACL spec을 쓰는지 검증.
    갈래1(단일 엔진)이면 애초에 한 쿼리라 불필요하지만, 갈래2면 이게 누수 A의 방어선."""
    if dense_query.acl_filter != sparse_query.acl_filter:
        raise ContractViolation(
            f"ACL filter drift between indices: "
            f"dense={dense_query.acl_filter} vs sparse={sparse_query.acl_filter} (누수 A)")


# ── 멀티턴: 권한은 캐시하지 않고 매 턴 재평가 ──────────────
class TurnAuthPolicy(Protocol):
    def current_auth(self, user_id: str) -> AuthContext: ...

def assert_fresh_auth(turn_auth: AuthContext, cached_auth: AuthContext | None) -> None:
    """재작성된 질의가 이전 턴 권한을 끌고 오지 않았는지.
    원칙: 매 턴 current_auth로 새로 평가. cached와 다르면 cached를 버린다(stale 금지)."""
    # 이 함수의 존재 자체가 계약: 호출자는 turn_auth를 매번 새로 받아야 한다.
    # cached가 turn과 다른데 cached를 썼다면 그건 버그 — 여기선 turn만 신뢰.
    if cached_auth is not None and cached_auth.acl_tags != turn_auth.acl_tags:
        # 권한이 턴 사이 변경됨 → 반드시 turn_auth 사용(여기선 위반 탐지용 신호)
        raise ContractViolation(
            "auth changed between turns; must re-evaluate with current turn auth, not cache")
