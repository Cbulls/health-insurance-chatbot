"""창구 ACL 헬퍼 — 공유 문서함·사내 지식 라이브러리·역할 판정."""
from __future__ import annotations

from harag.contracts.boundaries import AuthContext

_SHARE_ROLES = frozenset({"role:admin", "role:doc_admin"})
LIB_TAG = "lib:knowledge"


def with_library_access(auth: AuthContext) -> AuthContext:
    """모든 인증 사용자에게 사내 지식 라이브러리 검색 권한 부여."""
    return AuthContext(
        user_id=auth.user_id,
        acl_tags=frozenset(auth.acl_tags) | {LIB_TAG},
    )


def dept_from_auth(auth: AuthContext) -> str:
    for t in auth.acl_tags:
        if t.startswith("dept:"):
            return t.split(":", 1)[1]
    return ""


def can_manage_shared(auth: AuthContext) -> bool:
    return bool(auth.acl_tags & _SHARE_ROLES)


def can_manage_library(auth: AuthContext) -> bool:
    """라이브러리 컬렉션·문서 쓰기 — 공용 관리와 동일 역할."""
    return can_manage_shared(auth)


def personal_acl_tags(auth: AuthContext) -> list[str]:
    """개인 업로드 — owner 태그만 (부서 검색에 안 잡힘)."""
    return sorted(t for t in auth.acl_tags if t.startswith("owner:"))


def shared_acl_tags(auth: AuthContext) -> list[str]:
    """공용 업로드 — owner + dept."""
    tags = set()
    for t in auth.acl_tags:
        if t.startswith("owner:") or t.startswith("dept:"):
            tags.add(t)
    tags.add("scope:shared")
    return sorted(tags)


def library_acl_tags(auth: AuthContext, slug: str) -> list[str]:
    """라이브러리 업로드 — 전사 검색(lib:knowledge) + 컬렉션 태그."""
    tags = {LIB_TAG, "scope:library", f"collection:{slug}"}
    for t in auth.acl_tags:
        if t.startswith("owner:"):
            tags.add(t)
    return sorted(tags)
